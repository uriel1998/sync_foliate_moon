#!/usr/bin/env python3
"""Sync reading position across Moon+, Foliate, and EPW.

This module is deliberately written as a "practical translator" between two
reader applications that store reading state in different formats:

- Moon+ stores state in sidecar files whose filenames encode `Title - Author`
  and whose contents are either a compact proprietary locator string or a loose
  key/value document.
- Foliate stores state in JSON files whose metadata includes title, author,
  progress, and usually an EPUB CFI in `lastLocation`.
- EPW stores state in a SQLite database whose rows record filepath, content
  index, rendered row, and library/display progress.

The core challenge is that these readers do *not* expose equivalent location
models. Moon+ uses a proprietary chapter/page/offset encoding, Foliate uses
standard EPUB CFI, and EPW stores content index plus rendered-line offsets.
Exact conversion between those models is not available from the reference
material in this repository.

The script therefore follows a conservative strategy:

1. Match books only when both title and author can be normalized to the same
   logical key.
2. Use percentage progress as the common comparison metric.
3. Preserve each application's native file format where possible.
4. Use section-level approximations only when exact conversion is impossible.
5. Warn rather than silently invent state when required source data is missing.

In short: this script aims to keep the two readers *practically aligned*, not
to provide lossless round-trip position translation.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import venv
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zipfile import ZipFile


APP_MOON = "Moon"
APP_FOLIATE = "Foliate"
APP_EPW = "EPW"
SUPPORTED_APPS = {APP_MOON, APP_FOLIATE, APP_EPW}
REQUIRED_APPS = {APP_MOON, APP_FOLIATE}
COMPACT_MOON_RE = re.compile(
    r"^(?P<timestamp>\d+)\*(?P<chapter>\d+)"
    r"(?:@(?P<page>\d+))?(?:#(?P<offset>\d+))?:"
    r"(?P<percent>\d+(?:\.\d+)?)%$"
)
TITLE_AUTHOR_RE = re.compile(r"^(?P<title>.+?) - (?P<author>.+)$")


def script_root() -> Path:
    """Return the canonical directory containing this script.

    `resolve()` follows symlinks, so this stays correct when the script is
    launched through a symlink or from a different working directory.
    """

    return Path(__file__).resolve().parent


def script_path() -> Path:
    """Return the canonical path to this script."""

    return Path(__file__).resolve()


@dataclass
class MoonState:
    """Normalized Moon+ state parsed from one `.po` file.

    Why normalize?
    Moon+ state files are not rigidly schema-driven. The reference material in
    `1_reference/` shows at least two shapes:

    - a compact single-line form:
      `timestamp*chapter@page#offset:percent%`
    - a version-tolerant key/value form:
      `key=value`

    The rest of the sync algorithm does not want to care which representation a
    particular file used on disk. This dataclass is the "common internal model"
    the rest of the script operates on.

    Notes on the fields:
    - `modified_time` is a normalized timestamp used for `--date` comparisons.
    - `format_type` tells the write path whether the original file was compact
      or key/value.
    - `chapter`, `page`, and `offset` may be unavailable for some inputs.
    - `data` preserves original key/value content so unknown Moon+ fields can
      survive a rewrite unchanged.
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
    """Normalized Foliate state parsed from one JSON sidecar file.

    Foliate already stores structured JSON, so normalization is simpler than it
    is for Moon+. The main purpose of this dataclass is to:

    - flatten the `author` field into a comparable string
    - preserve the raw JSON payload for writing updates back
    - precompute a percentage value from Foliate's `progress` array
    - keep the optional `identifier`, which lets the script resolve the backing
      EPUB path through Foliate's URI store when approximations are needed
    """

    path: Path
    title: str
    author: str
    identifier: str | None
    modified_time: float
    payload: dict[str, Any]
    progress_current: int | None
    progress_total: int | None
    percent: float | None


@dataclass
class EPWState:
    """Normalized EPW state parsed from one SQLite row pair."""

    db_path: Path
    filepath: str
    title: str
    author: str
    modified_time: float
    percent: float | None
    content_index: int
    textwidth: int
    row: int
    rel_pctg: float | None


@dataclass
class UpdateResult:
    """Result of attempting to update one app's stored state.

    Returning a structured result lets the caller distinguish:

    - "the file changed"
    - "the file stayed the same"
    - "the file stayed the same and the user should be told why"
    """

    changed: bool
    warning: str | None = None


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

    # `root` anchors all repo-local files, regardless of the directory from
    # which the user launched the script.
    root = script_root()
    script = script_path()
    venv_dir = root / ".venv"
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)

    if not in_venv:
        if not venv_dir.exists():
            # Build an isolated environment in the repository so repeated runs
            # are predictable and do not depend on the caller's global Python.
            builder = venv.EnvBuilder(with_pip=True)
            builder.create(venv_dir)

        # Instead of trying to "activate" a venv in the shell sense, the script
        # simply re-executes itself with the venv's interpreter. That is more
        # reliable and keeps all original command-line arguments intact.
        python_path = venv_dir / "bin" / "python"
        env = os.environ.copy()
        env["SYNC_EBOOK_BOOTSTRAPPED"] = "1"
        subprocess.check_call(
            [str(python_path), str(script), *sys.argv[1:]],
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

    # These options are mutually exclusive because a single run should use
    # exactly one rule for choosing the source of truth.
    parser = argparse.ArgumentParser(
        description="Sync reading position across Moon+ Reader, Foliate, and EPW."
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
    group.add_argument(
        "--epw",
        action="store_true",
        help="Always prefer the EPW state.",
    )
    return parser.parse_args()


def load_env_directories(env_path: Path) -> dict[str, Path]:
    """Read application directories from the repository-local `.env`.

    Expected format:
        Moon:/path/to/moon/files
        Foliate:/path/to/foliate/files

    Unknown app names are ignored so the file can hold extra local notes without
    breaking the sync process.
    """

    # The env file is intentionally very small and custom. Using a trivial
    # parser keeps the configuration format obvious to non-Python users.
    directories: dict[str, Path] = {}
    if not env_path.exists():
        raise FileNotFoundError(f"Missing .env file at {env_path}")

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        # `partition` splits only on the first `:`, which allows paths to
        # contain additional colons after the application name.
        app, sep, directory = line.partition(":")
        if not sep:
            raise ValueError(f"Invalid .env entry: {raw_line!r}")
        app = app.strip()
        if app not in SUPPORTED_APPS:
            continue
        directories[app] = Path(directory.strip()).expanduser()

    missing = REQUIRED_APPS - directories.keys()
    if missing:
        raise ValueError(f"Missing application directories in .env: {sorted(missing)}")
    return directories


def normalize_name(value: str) -> str:
    """Normalize names so cross-app matching tolerates punctuation differences.

    The matching key intentionally collapses case, punctuation, underscores, and
    repeated whitespace. That lets `Zoes Tale`, `Zoe's Tale`, and
    `Zoes_Tale` compare more reliably when metadata sources differ slightly.
    """

    # Matching by exact raw strings would fail too often across ebook sources.
    # This normalization is intentionally lossy: it sacrifices formatting
    # details in exchange for more stable title/author keys.
    lowered = value.casefold().replace("_", " ")
    lowered = re.sub(r"[^\w]+", " ", lowered)
    return " ".join(lowered.split())


def parse_title_author_from_filename(path: Path) -> tuple[str, str] | None:
    """Extract `Title - Author` from a Moon+ filename.

    Moon+ matching is filename-based in the plan, so this strips the state-file
    suffix (`.po`, `.an`) and common ebook/document extensions before applying
    the `Title - Author` split.
    """

    # Moon+ matching is filename-driven, so the filename itself is the source of
    # truth for title and author on that side.
    name = path.name
    for suffix in (".po", ".an"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    for ext in (".epub", ".pdf", ".md", ".txt", ".mobi", ".azw3", ".cbz"):
        if name.endswith(ext):
            name = name[: -len(ext)]
    # The split is intentionally greedy toward the author side so that titles
    # containing hyphens still work as long as the final separator is ` - `.
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

    # `strip()` makes the compact-format regex robust to trailing newlines.
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

    # Fallback: treat the file as a permissive key/value state dump. Unknown
    # lines are ignored rather than treated as fatal parse errors.
    data: dict[str, str] = {}
    for line in raw_text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip()

    # Many later decisions rely on percent, so parse it eagerly if available.
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

    # Foliate files are expected to be plain JSON documents. Any JSON parse
    # failure should surface clearly to the caller rather than being hidden.
    payload = json.loads(path.read_text(encoding="utf-8"))
    metadata = payload.get("metadata") or {}
    title = metadata.get("title")
    if not title:
        return None

    # Foliate's `author` field is annoyingly flexible: string, object, or list.
    # Normalize it once here so the rest of the code can treat it as a string.
    author = normalize_foliate_author(metadata.get("author", ""))

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

    # Prefer the application-level "modified" timestamp over filesystem mtime,
    # because filesystem metadata can be perturbed by copies, restores, or sync
    # tools that do not reflect actual reading activity.
    modified_time = parse_iso8601_timestamp(metadata.get("modified"))
    if modified_time is None:
        modified_time = path.stat().st_mtime

    return FoliateState(
        path=path,
        title=title,
        author=author,
        identifier=metadata.get("identifier") if isinstance(metadata.get("identifier"), str) else None,
        modified_time=modified_time,
        payload=payload,
        progress_current=progress_current,
        progress_total=progress_total,
        percent=percent,
    )


def maybe_int(value: str | None) -> int | None:
    """Return an int when possible, otherwise `None`.

    This helper keeps the parsing code compact and communicates an important
    rule: malformed optional integers are treated as absent rather than fatal.
    """

    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def first_int(*values: str | None) -> int | None:
    """Return the first string value that parses as an integer.

    Moon+ uses multiple timestamp field names across versions. This helper lets
    the parser say "take the first plausible timestamp we can find."
    """

    for value in values:
        parsed = maybe_int(value)
        if parsed is not None:
            return parsed
    return None


def epochish_to_unix_seconds(value: int | None, fallback: float) -> float:
    """Normalize Moon+ timestamps that may be stored in seconds or milliseconds.

    Some Moon+ timestamps appear to be Unix seconds, others milliseconds. This
    helper standardizes both into floating-point Unix seconds so date-based
    comparisons can be performed consistently.
    """

    if value is None:
        return fallback
    return value / 1000 if value >= 1_000_000_000_000 else float(value)


def parse_iso8601_timestamp(value: Any) -> float | None:
    """Parse Foliate's ISO-8601 `metadata.modified` timestamp.

    Foliate commonly uses a `Z` suffix. `datetime.fromisoformat()` prefers an
    explicit offset, so the function normalizes `Z` to `+00:00` first.
    """

    if not isinstance(value, str) or not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized).astimezone(timezone.utc).timestamp()
    except ValueError:
        return None


def parse_epw_timestamp(value: Any) -> float | None:
    """Parse EPW's SQLite timestamp format into Unix seconds."""

    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def payload_string(value: Any) -> str | None:
    """Return a non-empty string payload value when present.

    Foliate payloads are JSON, so fields may exist with the wrong type. This
    helper centralizes the "string or nothing" rule for optional payload values.
    """

    return value if isinstance(value, str) and value else None


def normalize_foliate_author(value: Any) -> str:
    """Normalize Foliate's `author` field into a comparable display string.

    Foliate may store authors as:

    - a plain string
    - one author object
    - a list of author objects

    Internally we collapse those forms into a single string, joining multiple
    authors with ` & ` because that matches the Moon+ filename convention used
    in the local sample corpus.
    """

    if isinstance(value, dict):
        name = value.get("name")
        return name if isinstance(name, str) else ""

    if isinstance(value, list):
        names: list[str] = []
        for item in value:
            if isinstance(item, dict):
                name = item.get("name")
                if isinstance(name, str) and name:
                    names.append(name)
            elif isinstance(item, str) and item:
                names.append(item)
        return " & ".join(names)

    if isinstance(value, str):
        return value

    return ""


def canonical_locator(value: str | Path) -> str:
    """Return a stable comparable locator for local paths or URIs."""

    text = str(value)
    if text.startswith("file://"):
        return Path(text.removeprefix("file://")).expanduser().resolve(strict=False).as_posix()
    if "://" in text:
        return text
    return Path(text).expanduser().resolve(strict=False).as_posix()


def epw_local_book_path(filepath: str) -> Path | None:
    """Return a local book path for an EPW filepath when one exists."""

    if "://" in filepath:
        return None
    return Path(filepath).expanduser()


def resolve_epw_db_path(path: Path) -> Path:
    """Resolve an EPW config entry to a concrete SQLite database path."""

    expanded = path.expanduser()
    return expanded / "states.db" if expanded.is_dir() else expanded


def init_epw_db(db_path: Path) -> None:
    """Create an EPW-compatible SQLite database when it is missing."""

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS reading_states (
                filepath TEXT PRIMARY KEY,
                content_index INTEGER,
                textwidth INTEGER,
                row INTEGER,
                rel_pctg REAL
            );

            CREATE TABLE IF NOT EXISTS library (
                last_read DATETIME DEFAULT (datetime('now','localtime')),
                filepath TEXT PRIMARY KEY,
                title TEXT,
                author TEXT,
                reading_progress REAL,
                FOREIGN KEY (filepath) REFERENCES reading_states(filepath)
                ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS bookmarks (
                id TEXT PRIMARY KEY,
                filepath TEXT,
                name TEXT,
                content_index INTEGER,
                textwidth INTEGER,
                row INTEGER,
                rel_pctg REAL,
                FOREIGN KEY (filepath) REFERENCES reading_states(filepath)
                ON DELETE CASCADE
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


def load_foliate_uri_store(foliate_dir: Path) -> dict[str, str]:
    """Load Foliate's identifier -> URI/path store."""

    uri_store = foliate_dir / "library" / "uri-store.json"
    if not uri_store.exists():
        return {}
    try:
        payload = json.loads(uri_store.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    uris = payload.get("uris")
    if not isinstance(uris, list):
        return {}

    result: dict[str, str] = {}
    for entry in uris:
        if (
            isinstance(entry, list)
            and len(entry) == 2
            and isinstance(entry[0], str)
            and isinstance(entry[1], str)
        ):
            result[entry[0]] = entry[1]
    return result


def save_foliate_uri_store(foliate_dir: Path, mapping: dict[str, str]) -> None:
    """Persist Foliate's identifier -> URI/path store."""

    uri_store = foliate_dir / "library" / "uri-store.json"
    uri_store.parent.mkdir(parents=True, exist_ok=True)
    payload = {"uris": [[key, value] for key, value in mapping.items()]}
    uri_store.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )


def foliate_store_value_for_path(book_path: Path) -> str:
    """Format a local path the way Foliate stores it in `uri-store.json`."""

    home_dir = str(Path.home())
    resolved = str(book_path.expanduser())
    return resolved.replace(home_dir, "~", 1) if resolved.startswith(home_dir) else f"file://{resolved}"


def get_foliate_book_locator(foliate: FoliateState) -> str | None:
    """Resolve the raw locator string from Foliate's URI store."""

    if not foliate.identifier:
        return None

    uri_store = load_foliate_uri_store(foliate.path.parent)
    locator = uri_store.get(foliate.identifier)
    if not locator:
        return None
    return locator.replace("~", str(Path.home()), 1) if locator.startswith("~") else locator


def resolve_foliate_book_path(foliate: FoliateState) -> Path | None:
    """Resolve the underlying book path from Foliate's local URI store.

    This step is essential for approximation work. Foliate state JSON by itself
    does not contain enough structure to map a CFI back to a section boundary;
    we need the actual EPUB package to inspect its spine.
    """

    locator = get_foliate_book_locator(foliate)
    if not locator or "://" in locator:
        return None
    candidate = Path(locator).expanduser()
    return candidate if candidate.exists() else None


def read_epub_spine_items(book_path: Path) -> list[str] | None:
    """Return the ordered spine hrefs for an EPUB.

    The spine is the ordered reading sequence of the EPUB. For this script it is
    the best common structural unit both readers can be approximated against.
    """

    try:
        with ZipFile(book_path) as archive:
            opf_path, spine = read_epub_package(archive)
            opf_dir = Path(opf_path).parent
            opf = ET.fromstring(archive.read(opf_path))
            opf_ns = {"opf": "http://www.idpf.org/2007/opf"}
            manifest = {
                item.attrib["id"]: (opf_dir / item.attrib["href"]).as_posix()
                for item in opf.findall(".//opf:item", opf_ns)
                if "id" in item.attrib and "href" in item.attrib
            }
    except (KeyError, OSError, ET.ParseError):
        return None

    # We return resolved href-like paths so the caller can reason in terms of
    # actual content items rather than manifest ids.
    items: list[str] = []
    for idref in spine:
        href = manifest.get(idref)
        if href:
            items.append(href)
    return items or None


def read_epub_package(archive: ZipFile) -> tuple[str, list[str]]:
    """Return the OPF path and ordered spine ids from an EPUB archive.

    EPUB files are zip archives. `META-INF/container.xml` points at the OPF
    package document, and the OPF spine gives the reading order.
    """

    container = ET.fromstring(archive.read("META-INF/container.xml"))
    container_ns = {"c": "urn:oasis:names:tc:opendocument:xmlns:container"}
    rootfile = container.find(".//c:rootfile", container_ns)
    if rootfile is None:
        raise KeyError("Missing rootfile in EPUB container")

    opf_path = rootfile.attrib["full-path"]
    opf = ET.fromstring(archive.read(opf_path))
    opf_ns = {"opf": "http://www.idpf.org/2007/opf"}
    spine = [item.attrib["idref"] for item in opf.findall(".//opf:spine/opf:itemref", opf_ns)]
    return opf_path, spine


def read_epub_spine_length(book_path: Path) -> int | None:
    """Return the number of spine items in an EPUB.

    This convenience helper exists because several approximations only need the
    count, not the actual list of spine item paths.
    """

    items = read_epub_spine_items(book_path)
    return len(items) if items else None


def read_epub_metadata(book_path: Path) -> dict[str, str]:
    """Read a small subset of EPUB metadata needed for external sync."""

    try:
        with ZipFile(book_path) as archive:
            opf_path, _ = read_epub_package(archive)
            opf = ET.fromstring(archive.read(opf_path))
    except (KeyError, OSError, ET.ParseError):
        return {}

    ns = {
        "opf": "http://www.idpf.org/2007/opf",
        "dc": "http://purl.org/dc/elements/1.1/",
    }

    def first_text(pattern: str) -> str | None:
        for element in opf.findall(pattern, ns):
            if element.text and element.text.strip():
                return element.text.strip()
        return None

    creators = [
        element.text.strip()
        for element in opf.findall(".//opf:metadata/dc:creator", ns)
        if element.text and element.text.strip()
    ]
    metadata: dict[str, str] = {}
    identifier = first_text(".//opf:metadata/dc:identifier")
    title = first_text(".//opf:metadata/dc:title")
    if identifier:
        metadata["identifier"] = identifier
    if title:
        metadata["title"] = title
    if creators:
        metadata["author"] = " & ".join(creators)
    return metadata


def make_foliate_fallback_identifier(book_path: Path) -> str:
    """Match Foliate's fallback identifier generation for local files."""

    with book_path.open("rb") as handle:
        digest = hashlib.md5(handle.read(10_000_000)).hexdigest()
    return f"foliate:{digest}"


def choose_spine_index(moon: MoonState, spine_length: int) -> int | None:
    """Choose a 1-based EPUB spine index from Moon+ state.

    Heuristic order:
    1. Prefer Moon+'s chapter value when present, because it is already a
       section-like notion.
    2. Otherwise, derive an approximate section from overall percentage.

    The result is clamped into the actual EPUB spine range so the synthesized
    CFI always points at a real spine item.
    """

    if moon.chapter is not None and moon.chapter > 0:
        return max(1, min(spine_length, moon.chapter))

    if moon.percent is None:
        return None

    derived = round((moon.percent / 100) * spine_length)
    return max(1, min(spine_length, derived))


def build_spine_item_start_cfi(book_path: Path, spine_index: int) -> str | None:
    """Build a minimal EPUB CFI pointing to the start of one spine item.

    This intentionally chooses a *boundary* location rather than trying to
    reconstruct an exact paragraph offset. The goal is reliable reopen behavior,
    not precision that the source data cannot support.
    """

    try:
        with ZipFile(book_path) as archive:
            _, spine = read_epub_package(archive)
    except (KeyError, OSError, ET.ParseError):
        return None

    if spine_index < 1 or spine_index > len(spine):
        return None

    # In EPUB CFI, even-numbered steps address elements. The `/6/<n>!` portion
    # selects a spine item through the package document, and the tail points at
    # a stable start-of-document text position within that item.
    return f"epubcfi(/6/{spine_index * 2}!/4/2/2/1:0)"


def parse_foliate_spine_index(last_location: str) -> int | None:
    """Extract the 1-based spine index from a Foliate CFI.

    This helper only understands the portion of the CFI needed by this script:
    the package-document step that identifies which spine item is active.
    """

    match = re.match(r"^epubcfi\(/6/(?P<step>\d+)!", last_location)
    if not match:
        return None
    step = int(match.group("step"))
    if step < 2 or step % 2:
        return None
    return step // 2


def moon_compact_from_spine_boundary(
    moon: MoonState, foliate: FoliateState, book_path: Path
) -> str | None:
    """Approximate a compact Moon+ locator from Foliate's current spine item.

    This is the reverse-direction counterpart to Foliate CFI synthesis. Instead
    of trying to fabricate Moon+'s inner page/offset values, it deliberately
    snaps to the start of the resolved section:

    - chapter -> chosen spine item
    - page -> 0
    - offset -> 0

    That makes the approximation obvious and stable, even though it is not
    paragraph-accurate.
    """

    if foliate.percent is None:
        return None

    last_location = payload_string(foliate.payload.get("lastLocation"))
    if not last_location:
        return None

    # We only need the count here, but loading the concrete items is a useful
    # sanity check that the EPUB could actually be parsed successfully.
    spine_items = read_epub_spine_items(book_path)
    if not spine_items:
        return None

    spine_index = parse_foliate_spine_index(last_location)
    if spine_index is None:
        return None

    spine_index = max(1, min(len(spine_items), spine_index))
    timestamp_ms = int(time.time() * 1000)
    # Anchor Moon+ to the start of the resolved spine item. This is only a
    # section-level approximation, so page/offset are reset to the boundary.
    return f"{timestamp_ms}*{spine_index}@0#0:{foliate.percent:.1f}%"


def moon_compact_from_epw(moon: MoonState, epw: EPWState) -> str | None:
    """Approximate a compact Moon+ locator from EPW's content index."""

    if epw.percent is None:
        return None
    timestamp_ms = int(time.time() * 1000)
    chapter = max(1, epw.content_index + 1)
    return f"{timestamp_ms}*{chapter}@0#0:{epw.percent:.1f}%"


def synthesize_foliate_location(foliate: FoliateState, moon: MoonState) -> str | None:
    """Build an approximate Foliate location from Moon+ state.

    The goal is not an exact conversion; it is to keep Foliate opening in the
    right section of the book rather than falling back to the beginning.
    """

    # If the backing EPUB cannot be found, preserving the previous CFI is safer
    # than replacing it with a guess based on percentage alone.
    book_path = resolve_foliate_book_path(foliate)
    if not book_path:
        return payload_string(foliate.payload.get("lastLocation"))

    spine_length = read_epub_spine_length(book_path)
    if spine_length is None or spine_length <= 0:
        return payload_string(foliate.payload.get("lastLocation"))

    spine_index = choose_spine_index(moon, spine_length)
    if spine_index is None:
        return payload_string(foliate.payload.get("lastLocation"))

    return build_spine_item_start_cfi(book_path, spine_index) or payload_string(
        foliate.payload.get("lastLocation")
    )


def synthesize_foliate_location_from_epw(foliate: FoliateState, epw: EPWState) -> str | None:
    """Build an approximate Foliate location from EPW state."""

    book_path = resolve_foliate_book_path(foliate)
    if not book_path:
        return payload_string(foliate.payload.get("lastLocation"))

    spine_length = read_epub_spine_length(book_path)
    if spine_length is None or spine_length <= 0:
        return payload_string(foliate.payload.get("lastLocation"))

    spine_index = max(1, min(spine_length, epw.content_index + 1))
    return build_spine_item_start_cfi(book_path, spine_index) or payload_string(
        foliate.payload.get("lastLocation")
    )


def choose_epw_content_index_from_moon(moon: MoonState, filepath: str) -> int | None:
    """Choose a zero-based EPW content index from Moon+ state."""

    if moon.chapter is not None and moon.chapter > 0:
        return moon.chapter - 1

    book_path = epw_local_book_path(filepath)
    if not book_path or not book_path.exists():
        return None

    spine_length = read_epub_spine_length(book_path)
    if spine_length is None or spine_length <= 0:
        return None

    spine_index = choose_spine_index(moon, spine_length)
    return None if spine_index is None else spine_index - 1


def choose_epw_content_index_from_foliate(foliate: FoliateState, filepath: str) -> int | None:
    """Choose a zero-based EPW content index from Foliate state."""

    last_location = payload_string(foliate.payload.get("lastLocation"))
    if last_location:
        spine_index = parse_foliate_spine_index(last_location)
        if spine_index is not None:
            return max(0, spine_index - 1)

    book_path = epw_local_book_path(filepath)
    if not book_path or not book_path.exists():
        return None

    spine_length = read_epub_spine_length(book_path)
    if spine_length is None or spine_length <= 0 or foliate.percent is None:
        return None

    derived = round((foliate.percent / 100) * spine_length)
    return max(0, min(spine_length - 1, derived - 1))


def load_moon_states(directory: Path) -> dict[tuple[str, str], MoonState]:
    """Load all Moon+ `.po` states and key them by normalized title/author.

    The returned dictionary represents Moon+ as a lookup table keyed by the same
    normalized pair Foliate uses. That is what makes cross-application matching
    a simple set intersection later.
    """

    states: dict[tuple[str, str], MoonState] = {}
    for path in sorted(directory.glob("*.po")):
        state = parse_moon_state(path)
        if not state:
            continue
        key = (normalize_name(state.title), normalize_name(state.author))
        states[key] = state
    return states


def load_foliate_states(directory: Path) -> dict[tuple[str, str], FoliateState]:
    """Load all Foliate `.json` states and key them by normalized title/author.

    Files that do not parse into a usable title/author pair are skipped. This
    keeps matching strict enough to avoid obvious false positives.
    """

    states: dict[tuple[str, str], FoliateState] = {}
    for path in sorted(directory.glob("*.json")):
        state = parse_foliate_state(path)
        if not state:
            continue
        key = (normalize_name(state.title), normalize_name(state.author))
        states[key] = state
    return states


def build_foliate_filepath_map(states: dict[tuple[str, str], FoliateState]) -> dict[str, FoliateState]:
    """Index Foliate states by the canonical locator of their backing book."""

    mapping: dict[str, FoliateState] = {}
    for state in states.values():
        locator = get_foliate_book_locator(state)
        if locator:
            mapping[canonical_locator(locator)] = state
    return mapping


def load_epw_states(
    db_path: Path,
) -> tuple[dict[tuple[str, str], EPWState], dict[str, EPWState]]:
    """Load EPW rows and index them by title/author and filepath."""

    db_path = resolve_epw_db_path(db_path)
    if not db_path.exists():
        return {}, {}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT
                rs.filepath,
                rs.content_index,
                rs.textwidth,
                rs.row,
                rs.rel_pctg,
                l.last_read,
                l.title,
                l.author,
                l.reading_progress
            FROM reading_states rs
            LEFT JOIN library l ON l.filepath = rs.filepath
            """
        ).fetchall()
    finally:
        conn.close()

    by_key: dict[tuple[str, str], EPWState] = {}
    by_filepath: dict[str, EPWState] = {}
    for row in rows:
        filepath = row["filepath"]
        if not isinstance(filepath, str):
            continue

        reading_progress = row["reading_progress"]
        percent = None
        if isinstance(reading_progress, (int, float)):
            percent = float(reading_progress) * 100

        modified_time = parse_epw_timestamp(row["last_read"])
        if modified_time is None:
            modified_time = db_path.stat().st_mtime

        content_index = int(row["content_index"] or 0)
        rel_pctg = float(row["rel_pctg"]) if isinstance(row["rel_pctg"], (int, float)) else None
        state = EPWState(
            db_path=db_path,
            filepath=filepath,
            title=row["title"] or "",
            author=row["author"] or "",
            modified_time=modified_time,
            percent=percent,
            content_index=content_index,
            textwidth=int(row["textwidth"] or 80),
            row=int(row["row"] or 0),
            rel_pctg=rel_pctg,
        )

        if state.percent is None:
            local_path = epw_local_book_path(filepath)
            spine_length = read_epub_spine_length(local_path) if local_path and local_path.exists() else None
            if spine_length and spine_length > 0:
                intra = rel_pctg if rel_pctg is not None else 0.0
                state.percent = ((content_index + intra) / spine_length) * 100

        by_filepath[canonical_locator(filepath)] = state
        if state.title and state.author:
            by_key[(normalize_name(state.title), normalize_name(state.author))] = state

    return by_key, by_filepath


def choose_winner(args: argparse.Namespace, states: dict[str, Any]) -> str | None:
    """Choose which application's state wins for a matched book."""

    if args.moon and APP_MOON in states:
        return APP_MOON
    if args.foliate and APP_FOLIATE in states:
        return APP_FOLIATE
    if args.epw and APP_EPW in states:
        return APP_EPW

    if args.date:
        winner, winner_state = max(
            states.items(),
            key=lambda item: (item[1].modified_time, item[1].percent or -1.0, item[0]),
        )
        return winner if winner_state else None

    if any(state.percent is None for state in states.values()):
        return None

    winner, _ = max(
        states.items(),
        key=lambda item: (item[1].percent or -1.0, item[1].modified_time, item[0]),
    )
    return winner


def update_foliate_from_moon(foliate: FoliateState, moon: MoonState) -> bool:
    """Write Moon+'s progress into a Foliate JSON file.

    The location transform here is intentionally shallow:
    - `progress` is updated from Moon+'s percentage
    - metadata `modified` is refreshed
    - `lastLocation` is updated to an approximate CFI anchored to the matching
      EPUB spine item, or preserved if that approximation cannot be built
    """

    if moon.percent is None:
        return False

    # If Foliate already knows its total progress units, preserve that scale.
    # Otherwise fall back to an arbitrary stable denominator.
    total = foliate.progress_total or 1000
    # Preserve Foliate's existing total unit count when available so the updated
    # progress stays on the same scale the file was already using.
    current = min(total, max(0, round((moon.percent / 100) * total)))
    payload = foliate.payload
    changed = payload.get("progress") != [current, total]
    payload["progress"] = [current, total]

    # Keep Foliate's own metadata block and only touch the fields needed for
    # sync. Unknown metadata survives unchanged.
    metadata = payload.setdefault("metadata", {})
    if isinstance(metadata, dict):
        modified = iso_utc_now()
        if metadata.get("modified") != modified:
            changed = True
        metadata["modified"] = modified

    # Recompute an approximate CFI so reopening in Foliate lands in the right
    # section instead of merely showing an updated percentage.
    synthesized_location = synthesize_foliate_location(foliate, moon)
    if synthesized_location and payload.get("lastLocation") != synthesized_location:
        payload["lastLocation"] = synthesized_location
        changed = True

    if changed:
        foliate.path.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
    return changed


def update_foliate_from_epw(foliate: FoliateState, epw: EPWState) -> bool:
    """Write EPW progress into a Foliate JSON file."""

    if epw.percent is None:
        return False

    total = foliate.progress_total or 1000
    current = min(total, max(0, round((epw.percent / 100) * total)))
    payload = foliate.payload
    changed = payload.get("progress") != [current, total]
    payload["progress"] = [current, total]

    metadata = payload.setdefault("metadata", {})
    if isinstance(metadata, dict):
        modified = iso_utc_now()
        if metadata.get("modified") != modified:
            changed = True
        metadata["modified"] = modified

    synthesized_location = synthesize_foliate_location_from_epw(foliate, epw)
    if synthesized_location and payload.get("lastLocation") != synthesized_location:
        payload["lastLocation"] = synthesized_location
        changed = True

    if changed:
        foliate.path.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
    return changed


def update_moon_from_foliate(moon: MoonState, foliate: FoliateState) -> UpdateResult:
    """Write Foliate's progress back into a Moon+ state file.

    Exact reverse mapping from EPUB CFI to Moon+'s location format is not
    available from the provided reference material. For compact Moon+ files,
    attempt a section-level approximation using the actual EPUB spine. If that
    is not possible, warn visibly instead of failing silently.
    """

    if foliate.percent is None:
        return UpdateResult(False, "Foliate state has no usable progress value.")

    # Compact Moon+ files need approximation because Foliate's CFI cannot be
    # translated into Moon+'s internal locator exactly.
    if moon.format_type == "compact":
        book_path = resolve_foliate_book_path(foliate)
        if not book_path:
            return UpdateResult(
                False,
                f"Could not approximate Foliate -> Moon+ for '{moon.title}' because the EPUB file is not accessible.",
            )
        new_text = moon_compact_from_spine_boundary(moon, foliate, book_path)
        if new_text is None:
            return UpdateResult(
                False,
                f"Could not approximate Foliate -> Moon+ for '{moon.title}' from the EPUB spine/CFI data.",
            )
    else:
        new_text = build_kv_moon_state(moon, foliate.percent)

    if new_text == moon.raw_text:
        return UpdateResult(False)

    moon.path.write_text(new_text, encoding="utf-8")
    return UpdateResult(True)


def update_moon_from_epw(moon: MoonState, epw: EPWState) -> UpdateResult:
    """Write EPW progress into a Moon+ state file."""

    if epw.percent is None:
        return UpdateResult(False, "EPW state has no usable progress value.")

    if moon.format_type == "compact":
        new_text = moon_compact_from_epw(moon, epw)
        if new_text is None:
            return UpdateResult(
                False,
                f"Could not approximate EPW -> Moon+ for '{moon.title}' from the stored content index.",
            )
    else:
        new_text = build_kv_moon_state(moon, epw.percent)

    if new_text == moon.raw_text:
        return UpdateResult(False)

    moon.path.write_text(new_text, encoding="utf-8")
    return UpdateResult(True)


def build_compact_moon_state(moon: MoonState, percent: float) -> str:
    """Build a compact Moon+ state string from an existing compact state.

    This helper is retained for completeness, but compact Moon+ files are not
    rewritten during Foliate-driven sync because we cannot construct a valid
    proprietary locator from Foliate data alone.
    """
    # This helper is intentionally simple: it preserves the existing structural
    # fields and only refreshes timestamp/progress. The more interesting compact
    # reverse-sync path lives in `moon_compact_from_spine_boundary`.
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

    # Copy first so we do not mutate the parsed representation in memory. That
    # makes reasoning about "before" and "after" states easier.
    data = dict(moon.data or {})
    data["percent"] = f"{percent:.1f}"
    timestamp_ms = str(int(time.time() * 1000))
    # Update every known Moon+ activity timestamp that already exists. We do not
    # invent missing timestamp fields, because that would be more intrusive than
    # necessary for a compatibility-oriented sync tool.
    for key in ("modifiedTime", "lastReadTime", "lastAccess"):
        if key in data:
            data[key] = timestamp_ms
    lines = [f"{key}={value}" for key, value in data.items()]
    return "\n".join(lines)


def upsert_epw_state(
    db_path: Path,
    filepath: str,
    title: str,
    author: str,
    percent: float | None,
    content_index: int,
    textwidth: int,
    row: int,
    rel_pctg: float | None,
) -> bool:
    """Insert or update an EPW reading state plus its library row."""

    init_epw_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        existing = conn.execute(
            """
            SELECT rs.content_index, rs.textwidth, rs.row, rs.rel_pctg,
                   l.title, l.author, l.reading_progress
            FROM reading_states rs
            LEFT JOIN library l ON l.filepath = rs.filepath
            WHERE rs.filepath = ?
            """,
            (filepath,),
        ).fetchone()

        reading_progress = None if percent is None else percent / 100
        changed = (
            existing is None
            or existing[0] != content_index
            or existing[1] != textwidth
            or existing[2] != row
            or existing[3] != rel_pctg
            or existing[4] != title
            or existing[5] != author
            or existing[6] != reading_progress
        )

        conn.execute(
            """
            INSERT OR REPLACE INTO reading_states
            (filepath, content_index, textwidth, row, rel_pctg)
            VALUES (?, ?, ?, ?, ?)
            """,
            (filepath, content_index, textwidth, row, rel_pctg),
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO library
            (filepath, title, author, reading_progress)
            VALUES (?, ?, ?, ?)
            """,
            (filepath, title, author, reading_progress),
        )
        conn.commit()
        return changed
    finally:
        conn.close()


def update_epw_from_foliate(epw: EPWState, foliate: FoliateState) -> UpdateResult:
    """Write Foliate progress into EPW's SQLite state."""

    if foliate.percent is None:
        return UpdateResult(False, "Foliate state has no usable progress value.")

    content_index = choose_epw_content_index_from_foliate(foliate, epw.filepath)
    if content_index is None:
        return UpdateResult(
            False,
            f"Could not approximate Foliate -> EPW for '{epw.title or foliate.title}' because no content index could be derived.",
        )

    changed = upsert_epw_state(
        epw.db_path,
        epw.filepath,
        epw.title or foliate.title,
        epw.author or foliate.author,
        foliate.percent,
        content_index,
        epw.textwidth or 80,
        0,
        0.0,
    )
    return UpdateResult(changed)


def update_epw_from_moon(epw: EPWState, moon: MoonState) -> UpdateResult:
    """Write Moon+ progress into EPW's SQLite state."""

    if moon.percent is None:
        return UpdateResult(False, "Moon+ state has no usable progress value.")

    content_index = choose_epw_content_index_from_moon(moon, epw.filepath)
    if content_index is None:
        return UpdateResult(
            False,
            f"Could not approximate Moon+ -> EPW for '{moon.title}' because no content index could be derived.",
        )

    changed = upsert_epw_state(
        epw.db_path,
        epw.filepath,
        epw.title or moon.title,
        epw.author or moon.author,
        moon.percent,
        content_index,
        epw.textwidth or 80,
        0,
        0.0,
    )
    return UpdateResult(changed)


def create_epw_state_from_foliate(db_path: Path, foliate: FoliateState) -> UpdateResult:
    """Create a missing EPW entry from an existing Foliate state."""

    locator = get_foliate_book_locator(foliate)
    book_path = resolve_foliate_book_path(foliate)
    if not locator or not book_path:
        return UpdateResult(
            False,
            f"Could not create EPW state for '{foliate.title}' because Foliate does not expose an accessible local book path.",
        )

    content_index = choose_epw_content_index_from_foliate(foliate, str(book_path))
    if content_index is None:
        return UpdateResult(
            False,
            f"Could not create EPW state for '{foliate.title}' because no content index could be derived.",
        )

    changed = upsert_epw_state(
        resolve_epw_db_path(db_path),
        str(book_path),
        foliate.title,
        foliate.author,
        foliate.percent,
        content_index,
        80,
        0,
        0.0,
    )
    return UpdateResult(changed)


def create_foliate_state_from_epw(foliate_dir: Path, epw: EPWState) -> UpdateResult:
    """Create a missing Foliate entry from an existing EPW state."""

    book_path = epw_local_book_path(epw.filepath)
    if not book_path or not book_path.exists():
        return UpdateResult(
            False,
            f"Could not create Foliate state for '{epw.title}' because EPW does not point at an accessible local file.",
        )

    try:
        epub_metadata = read_epub_metadata(book_path)
        identifier = epub_metadata.get("identifier") or make_foliate_fallback_identifier(book_path)
    except OSError as exc:
        return UpdateResult(False, f"Could not create Foliate state for '{epw.title}': {exc}")

    metadata = {
        "identifier": identifier,
        "title": epub_metadata.get("title") or epw.title,
        "author": {"name": epub_metadata.get("author") or epw.author},
        "modified": iso_utc_now(),
    }
    total = 1000
    current = round(((epw.percent or 0.0) / 100) * total)
    payload = {
        "metadata": metadata,
        "progress": [current, total],
    }

    cfi = build_spine_item_start_cfi(book_path, epw.content_index + 1)
    if cfi:
        payload["lastLocation"] = cfi

    state_path = foliate_dir / f"{identifier}.json"
    changed = True
    if state_path.exists():
        try:
            existing = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = {}
        changed = existing != payload

    state_path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    uri_store = load_foliate_uri_store(foliate_dir)
    desired_uri = foliate_store_value_for_path(book_path)
    if uri_store.get(identifier) != desired_uri:
        uri_store[identifier] = desired_uri
        save_foliate_uri_store(foliate_dir, uri_store)
        changed = True

    return UpdateResult(changed)


def iso_utc_now() -> str:
    """Return the current time in the ISO-8601 UTC form Foliate already uses."""

    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def bootstrap_missing_foliate_epw_entries(
    foliate_dir: Path,
    epw_db_path: Path,
    foliate_by_filepath: dict[str, FoliateState],
    epw_by_filepath: dict[str, EPWState],
) -> tuple[int, list[str]]:
    """Create missing Foliate/EPW entries when one side already knows the filepath."""

    updates = 0
    warnings: list[str] = []

    for filepath, foliate in foliate_by_filepath.items():
        if filepath in epw_by_filepath:
            continue
        result = create_epw_state_from_foliate(epw_db_path, foliate)
        if result.changed:
            updates += 1
        if result.warning:
            warnings.append(result.warning)

    for filepath, epw in epw_by_filepath.items():
        if filepath in foliate_by_filepath:
            continue
        result = create_foliate_state_from_epw(foliate_dir, epw)
        if result.changed:
            updates += 1
        if result.warning:
            warnings.append(result.warning)

    return updates, warnings


def update_target_from_source(target_app: str, target: Any, source_app: str, source: Any) -> UpdateResult:
    """Dispatch one cross-application update."""

    if target_app == APP_FOLIATE and source_app == APP_MOON:
        return UpdateResult(update_foliate_from_moon(target, source))
    if target_app == APP_FOLIATE and source_app == APP_EPW:
        return UpdateResult(update_foliate_from_epw(target, source))
    if target_app == APP_MOON and source_app == APP_FOLIATE:
        return update_moon_from_foliate(target, source)
    if target_app == APP_MOON and source_app == APP_EPW:
        return update_moon_from_epw(target, source)
    if target_app == APP_EPW and source_app == APP_FOLIATE:
        return update_epw_from_foliate(target, source)
    if target_app == APP_EPW and source_app == APP_MOON:
        return update_epw_from_moon(target, source)
    return UpdateResult(False, f"Unsupported sync direction: {source_app} -> {target_app}")


def sync_states(
    args: argparse.Namespace,
    moon_dir: Path,
    foliate_dir: Path,
    epw_path: Path | None = None,
) -> int:
    """Load, match, resolve, and sync all eligible titles.

    Books are matched by normalized title/author across the configured apps.
    Foliate and EPW also bootstrap missing entries from filepath when one side
    already knows the book.
    """

    moon_states = load_moon_states(moon_dir)
    foliate_states = load_foliate_states(foliate_dir)
    updates = 0
    warnings: list[str] = []

    epw_states: dict[tuple[str, str], EPWState] = {}
    if epw_path is not None:
        epw_states, epw_by_filepath = load_epw_states(epw_path)
        foliate_by_filepath = build_foliate_filepath_map(foliate_states)
        bootstrap_updates, bootstrap_warnings = bootstrap_missing_foliate_epw_entries(
            foliate_dir,
            epw_path,
            foliate_by_filepath,
            epw_by_filepath,
        )
        updates += bootstrap_updates
        warnings.extend(bootstrap_warnings)

        # Reload after bootstrapping so newly created entries can participate in
        # the normal winner-selection pass.
        foliate_states = load_foliate_states(foliate_dir)
        epw_states, _ = load_epw_states(epw_path)

    all_keys = sorted(set(moon_states) | set(foliate_states) | set(epw_states))
    for key in all_keys:
        states: dict[str, Any] = {}
        if key in moon_states:
            states[APP_MOON] = moon_states[key]
        if key in foliate_states:
            states[APP_FOLIATE] = foliate_states[key]
        if key in epw_states:
            states[APP_EPW] = epw_states[key]
        if len(states) < 2:
            continue

        winner = choose_winner(args, states)
        if not winner:
            continue

        for app_name, state in states.items():
            if app_name == winner:
                continue
            result = update_target_from_source(app_name, state, winner, states[winner])
            if result.changed:
                updates += 1
            if result.warning:
                warnings.append(result.warning)

    # Warnings are emitted after the sync loop so normal per-book processing
    # stays simple and the user still sees all issues found during the run.
    for warning in warnings:
        print(f"WARNING: {warning}", file=sys.stderr)

    return updates


def main() -> int:
    """Script entrypoint.

    Order matters here:
    1. bootstrap the environment
    2. parse CLI arguments
    3. resolve directories from the repository-local `.env`
    4. perform sync
    """

    # Keep startup steps in the same order described by the project plan.
    ensure_venv()
    args = parse_args()
    env_directories = load_env_directories(script_root() / ".env")
    updates = sync_states(
        args,
        env_directories[APP_MOON],
        env_directories[APP_FOLIATE],
        env_directories.get(APP_EPW),
    )
    print(f"Updated {updates} reading state file(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
