#!/usr/bin/env python3
"""Bidirectionally sync reading position between Moon+ and Foliate.

The script is intentionally conservative:
- it only syncs titles that can be matched on both sides
- it treats percentage progress as the shared interchange format
- it preserves each application's native file structure where possible

Moon+ and Foliate do not use compatible location formats. Foliate stores an
EPUB CFI, while the reference Moon+ samples store a proprietary compact state
string. Because of that mismatch, Moon+ writes are best-effort updates driven
by progress percentage rather than exact in-book locator conversion.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
import re
import subprocess
import sys
import time
import venv
from dataclasses import dataclass
from pathlib import Path
from typing import Any


APP_MOON = "Moon"
APP_FOLIATE = "Foliate"
SUPPORTED_APPS = {APP_MOON, APP_FOLIATE}
COMPACT_MOON_RE = re.compile(
    r"^(?P<timestamp>\d+)\*(?P<chapter>\d+)"
    r"(?:@(?P<page>\d+))?(?:#(?P<offset>\d+))?:"
    r"(?P<percent>\d+(?:\.\d+)?)%$"
)
TITLE_AUTHOR_RE = re.compile(r"^(?P<title>.+?) - (?P<author>.+)$")


@dataclass
class MoonState:
    """Normalized Moon+ state parsed from one `.po` file.

    The sample data shows two possible representations:
    - a compact one-line format like `timestamp*chapter@page#offset:percent%`
    - a looser key/value format described in the reference notes

    Both are normalized into this dataclass so the sync logic can work with a
    consistent shape regardless of how the source file is encoded.
    """

    path: Path
    title: str
    author: str
    modified_time: float
    raw_text: str
    format_type: str
    percent: float | None
    timestamp_ms: int | None
    chapter: int | None
    page: int | None
    offset: int | None
    data: dict[str, str] | None


@dataclass
class FoliateState:
    """Normalized Foliate state parsed from one JSON sidecar file."""

    path: Path
    title: str
    author: str
    modified_time: float
    payload: dict[str, Any]
    progress_current: int | None
    progress_total: int | None
    percent: float | None


def ensure_venv() -> None:
    """Run the script inside a local virtual environment.

    How it works:
    - if the current interpreter is already inside a venv, install
      `requirements.txt` and continue
    - otherwise, create `.venv` if needed, then re-exec this script with the
      venv's Python interpreter while preserving the original CLI arguments

    This follows the repo plan literally: environment setup happens before any
    application-specific work such as parsing `.env` or touching state files.
    """

    root = Path(__file__).resolve().parent
    venv_dir = root / ".venv"
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)

    if not in_venv:
        if not venv_dir.exists():
            # Build an isolated environment in the repository so repeated runs
            # are predictable and do not depend on the caller's global Python.
            builder = venv.EnvBuilder(with_pip=True)
            builder.create(venv_dir)

        python_path = venv_dir / "bin" / "python"
        env = os.environ.copy()
        env["SYNC_EBOOK_BOOTSTRAPPED"] = "1"
        subprocess.check_call(
            [str(python_path), str(Path(__file__).resolve()), *sys.argv[1:]],
            env=env,
            cwd=str(Path.cwd()),
        )
        raise SystemExit(0)

    requirements_path = root / "requirements.txt"
    # Always run the requirements install step after activation so the script
    # stays aligned with the dependency declaration even if it is currently
    # standard-library only.
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "-r", str(requirements_path)],
        cwd=str(root),
    )


def parse_args() -> argparse.Namespace:
    """Parse conflict-resolution switches.

    The flags are mutually exclusive because each run should have exactly one
    winner-selection policy.
    """

    parser = argparse.ArgumentParser(
        description="Sync reading position between Moon+ Reader and Foliate."
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--position",
        action="store_true",
        help="Resolve conflicts by furthest position read (default).",
    )
    group.add_argument(
        "--date",
        action="store_true",
        help="Resolve conflicts by newest file modification time.",
    )
    group.add_argument(
        "--moon",
        action="store_true",
        help="Always prefer the Moon+ state.",
    )
    group.add_argument(
        "--foliate",
        action="store_true",
        help="Always prefer the Foliate state.",
    )
    return parser.parse_args()


def load_env_directories(env_path: Path) -> dict[str, Path]:
    """Read application directories from `.env` in the current working dir.

    Expected format:
        Moon:/path/to/moon/files
        Foliate:/path/to/foliate/files

    Unknown app names are ignored so the file can hold extra local notes without
    breaking the sync process.
    """

    directories: dict[str, Path] = {}
    if not env_path.exists():
        raise FileNotFoundError(f"Missing .env file at {env_path}")

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        app, sep, directory = line.partition(":")
        if not sep:
            raise ValueError(f"Invalid .env entry: {raw_line!r}")
        app = app.strip()
        if app not in SUPPORTED_APPS:
            continue
        directories[app] = Path(directory.strip()).expanduser()

    missing = SUPPORTED_APPS - directories.keys()
    if missing:
        raise ValueError(f"Missing application directories in .env: {sorted(missing)}")
    return directories


def normalize_name(value: str) -> str:
    """Normalize names so cross-app matching tolerates punctuation differences.

    The matching key intentionally collapses case, punctuation, underscores, and
    repeated whitespace. That lets `Zoes Tale`, `Zoe's Tale`, and
    `Zoes_Tale` compare more reliably when metadata sources differ slightly.
    """

    lowered = value.casefold().replace("_", " ")
    lowered = re.sub(r"[^\w]+", " ", lowered)
    return " ".join(lowered.split())


def parse_title_author_from_filename(path: Path) -> tuple[str, str] | None:
    """Extract `Title - Author` from a Moon+ filename.

    Moon+ matching is filename-based in the plan, so this strips the state-file
    suffix (`.po`, `.an`) and common ebook/document extensions before applying
    the `Title - Author` split.
    """

    name = path.name
    for suffix in (".po", ".an"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    for ext in (".epub", ".pdf", ".md", ".txt", ".mobi", ".azw3", ".cbz"):
        if name.endswith(ext):
            name = name[: -len(ext)]
    match = TITLE_AUTHOR_RE.match(name)
    if not match:
        return None
    return match.group("title"), match.group("author")


def parse_moon_state(path: Path) -> MoonState | None:
    """Parse one Moon+ state file into a normalized shape.

    The parser supports:
    - the compact one-line format found in the sample `.po` files
    - a fallback key/value format based on the reference documentation

    If the filename cannot be mapped to `Title - Author`, the entry is skipped
    because it cannot participate in cross-app matching safely.
    """

    title_author = parse_title_author_from_filename(path)
    if not title_author:
        return None

    raw_text = path.read_text(encoding="utf-8").strip()
    stat = path.stat()
    title, author = title_author

    compact_match = COMPACT_MOON_RE.match(raw_text)
    if compact_match:
        timestamp_ms = int(compact_match.group("timestamp"))
        # The compact format is not self-describing, so we unpack the few known
        # fields directly from the regex capture groups.
        return MoonState(
            path=path,
            title=title,
            author=author,
            modified_time=epochish_to_unix_seconds(timestamp_ms, fallback=stat.st_mtime),
            raw_text=raw_text,
            format_type="compact",
            percent=float(compact_match.group("percent")),
            timestamp_ms=timestamp_ms,
            chapter=int(compact_match.group("chapter")),
            page=int(compact_match.group("page")) if compact_match.group("page") else None,
            offset=int(compact_match.group("offset")) if compact_match.group("offset") else None,
            data=None,
        )

    data: dict[str, str] = {}
    for line in raw_text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip()

    percent = None
    if "percent" in data:
        try:
            percent = float(data["percent"])
        except ValueError:
            percent = None
    # Moon+ timestamp keys vary by version, so we take the first one that
    # parses cleanly instead of assuming a single canonical field name.
    chapter = maybe_int(data.get("chapterIndex"))
    page = maybe_int(data.get("page"))
    offset = maybe_int(data.get("offset"))
    timestamp_ms = first_int(
        data.get("modifiedTime"),
        data.get("lastReadTime"),
        data.get("lastAccess"),
    )
    return MoonState(
        path=path,
        title=title,
        author=author,
        modified_time=epochish_to_unix_seconds(timestamp_ms, fallback=stat.st_mtime),
        raw_text=raw_text,
        format_type="kv",
        percent=percent,
        timestamp_ms=timestamp_ms,
        chapter=chapter,
        page=page,
        offset=offset,
        data=data,
    )


def parse_foliate_state(path: Path) -> FoliateState | None:
    """Parse one Foliate JSON file into a normalized shape.

    Foliate is structurally easier to parse than Moon+ because it already stores
    JSON. The main normalization work here is handling the `author` field, which
    may be a nested object, and computing a percentage from the `progress`
    tuple-like array.
    """

    payload = json.loads(path.read_text(encoding="utf-8"))
    metadata = payload.get("metadata") or {}
    title = metadata.get("title")
    if not title:
        return None

    author = metadata.get("author", "")
    if isinstance(author, dict):
        author = author.get("name", "")
    elif not isinstance(author, str):
        author = ""

    progress = payload.get("progress")
    progress_current = None
    progress_total = None
    percent = None
    if (
        isinstance(progress, list)
        and len(progress) == 2
        and isinstance(progress[0], int)
        and isinstance(progress[1], int)
        and progress[1] > 0
    ):
        # Foliate's progress is `current, total`; converting that to percentage
        # gives us the common currency used for conflict resolution.
        progress_current, progress_total = progress
        percent = (progress_current / progress_total) * 100

    modified_time = parse_iso8601_timestamp(metadata.get("modified"))
    if modified_time is None:
        modified_time = path.stat().st_mtime

    return FoliateState(
        path=path,
        title=title,
        author=author,
        modified_time=modified_time,
        payload=payload,
        progress_current=progress_current,
        progress_total=progress_total,
        percent=percent,
    )


def maybe_int(value: str | None) -> int | None:
    """Return an int when possible, otherwise `None`."""

    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def first_int(*values: str | None) -> int | None:
    """Return the first string value that parses as an integer."""

    for value in values:
        parsed = maybe_int(value)
        if parsed is not None:
            return parsed
    return None


def epochish_to_unix_seconds(value: int | None, fallback: float) -> float:
    """Normalize Moon+ timestamps that may be stored in seconds or milliseconds."""

    if value is None:
        return fallback
    return value / 1000 if value >= 1_000_000_000_000 else float(value)


def parse_iso8601_timestamp(value: Any) -> float | None:
    """Parse Foliate's ISO-8601 `metadata.modified` timestamp."""

    if not isinstance(value, str) or not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized).astimezone(timezone.utc).timestamp()
    except ValueError:
        return None


def load_moon_states(directory: Path) -> dict[tuple[str, str], MoonState]:
    """Load all Moon+ `.po` states and key them by normalized title/author."""

    states: dict[tuple[str, str], MoonState] = {}
    for path in sorted(directory.glob("*.po")):
        state = parse_moon_state(path)
        if not state:
            continue
        key = (normalize_name(state.title), normalize_name(state.author))
        states[key] = state
    return states


def load_foliate_states(directory: Path) -> dict[tuple[str, str], FoliateState]:
    """Load all Foliate `.json` states and key them by normalized title/author."""

    states: dict[tuple[str, str], FoliateState] = {}
    for path in sorted(directory.glob("*.json")):
        state = parse_foliate_state(path)
        if not state:
            continue
        key = (normalize_name(state.title), normalize_name(state.author))
        states[key] = state
    return states


def choose_winner(
    args: argparse.Namespace, moon: MoonState, foliate: FoliateState
) -> str | None:
    """Choose which application's state wins for a matched book.

    Rules:
    - `--moon` and `--foliate` are absolute overrides
    - `--date` prefers the newer file modification timestamp
    - default `--position` prefers the larger percentage progress

    Returning `None` means the script does not have enough data to resolve the
    conflict safely, so that title is skipped.
    """

    if args.moon:
        return APP_MOON
    if args.foliate:
        return APP_FOLIATE
    if args.date:
        if moon.modified_time == foliate.modified_time:
            return APP_MOON if (moon.percent or -1) >= (foliate.percent or -1) else APP_FOLIATE
        return APP_MOON if moon.modified_time > foliate.modified_time else APP_FOLIATE

    if moon.percent is None or foliate.percent is None:
        return None
    if moon.percent == foliate.percent:
        return APP_MOON if moon.modified_time >= foliate.modified_time else APP_FOLIATE
    return APP_MOON if moon.percent > foliate.percent else APP_FOLIATE


def update_foliate_from_moon(foliate: FoliateState, moon: MoonState) -> bool:
    """Write Moon+'s progress into a Foliate JSON file.

    The location transform here is intentionally shallow:
    - `progress` is updated from Moon+'s percentage
    - metadata `modified` is refreshed
    - `lastLocation` is removed because we do not have enough information to
      synthesize a valid EPUB CFI from Moon+'s proprietary locator, and leaving
      the old one behind would create contradictory state
    """

    if moon.percent is None:
        return False

    total = foliate.progress_total or 1000
    # Preserve Foliate's existing total unit count when available so the updated
    # progress stays on the same scale the file was already using.
    current = min(total, max(0, round((moon.percent / 100) * total)))
    payload = foliate.payload
    changed = payload.get("progress") != [current, total]
    payload["progress"] = [current, total]

    metadata = payload.setdefault("metadata", {})
    if isinstance(metadata, dict):
        modified = iso_utc_now()
        if metadata.get("modified") != modified:
            changed = True
        metadata["modified"] = modified

    # Remove a stale exact-location field rather than leaving progress and CFI
    # disagreeing after a Moon+-driven sync.
    if "lastLocation" in payload:
        del payload["lastLocation"]
        changed = True

    if changed:
        foliate.path.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
    return changed


def update_moon_from_foliate(moon: MoonState, foliate: FoliateState) -> bool:
    """Write Foliate's progress back into a Moon+ state file.

    Exact reverse mapping from EPUB CFI to Moon+'s location format is not
    available from the provided reference material. Rather than emit a bogus
    locator, the write path only updates Moon+ files whose progress can be
    changed without inventing a new position.
    """

    if foliate.percent is None:
        return False

    if moon.format_type == "compact":
        return False
    else:
        new_text = build_kv_moon_state(moon, foliate.percent)

    if new_text == moon.raw_text:
        return False

    moon.path.write_text(new_text, encoding="utf-8")
    return True


def build_compact_moon_state(moon: MoonState, percent: float) -> str:
    """Build a compact Moon+ state string from an existing compact state.

    This helper is retained for completeness, but compact Moon+ files are not
    rewritten during Foliate-driven sync because we cannot construct a valid
    proprietary locator from Foliate data alone.
    """
    timestamp_ms = int(time.time() * 1000)
    chapter = moon.chapter or 0
    page = moon.page if moon.page is not None else 0
    offset = moon.offset if moon.offset is not None else 0
    return f"{timestamp_ms}*{chapter}@{page}#{offset}:{percent:.1f}%"


def build_kv_moon_state(moon: MoonState, percent: float) -> str:
    """Update a key/value Moon+ state while preserving unknown keys.

    Moon+ is treated as an append-only, version-tolerant state dump. This path
    only updates key/value files, where progress can be adjusted without having
    to fabricate a brand-new proprietary compact locator.
    """

    data = dict(moon.data or {})
    data["percent"] = f"{percent:.1f}"
    timestamp_ms = str(int(time.time() * 1000))
    for key in ("modifiedTime", "lastReadTime", "lastAccess"):
        if key in data:
            data[key] = timestamp_ms
    lines = [f"{key}={value}" for key, value in data.items()]
    return "\n".join(lines)


def iso_utc_now() -> str:
    """Return the current time in the ISO-8601 UTC form Foliate already uses."""

    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def sync_states(args: argparse.Namespace, moon_dir: Path, foliate_dir: Path) -> int:
    """Load, match, resolve, and sync all eligible titles.

    Only books present on both sides are considered. This matches the current
    plan requirement to skip one-sided entries for now.
    """

    moon_states = load_moon_states(moon_dir)
    foliate_states = load_foliate_states(foliate_dir)
    matched_keys = sorted(set(moon_states).intersection(foliate_states))

    updates = 0
    for key in matched_keys:
        moon = moon_states[key]
        foliate = foliate_states[key]
        winner = choose_winner(args, moon, foliate)
        if not winner:
            continue

        if winner == APP_MOON:
            if update_foliate_from_moon(foliate, moon):
                updates += 1
        else:
            if update_moon_from_foliate(moon, foliate):
                updates += 1

    return updates


def main() -> int:
    """Script entrypoint.

    Order matters here:
    1. bootstrap the environment
    2. parse CLI arguments
    3. resolve directories from the caller's working directory
    4. perform sync
    """

    ensure_venv()
    args = parse_args()
    env_directories = load_env_directories(Path.cwd() / ".env")
    updates = sync_states(
        args,
        env_directories[APP_MOON],
        env_directories[APP_FOLIATE],
    )
    print(f"Updated {updates} reading state file(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
