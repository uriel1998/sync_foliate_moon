# Syncing Ebook

Reading-state sync across Moon+ Reader, Foliate, and EPW.

The readers involved here are:

- [Moon+ Reader](https://www.moondownload.com/), an Android ebook reader
- [Foliate](https://johnfactotum.github.io/foliate/), an ebook reader for Linux
- [epw](https://github.com/uriel1998/epw), an ebook reader for the terminal and a fork of epy, which is itself a fork of epr

This project synchronizes reading state between:

- Moon+ sidecar files such as `Title - Author.epub.po`
- Foliate JSON state files such as `9780765389206.json`
- EPW SQLite state rows in `states.db`

The script is designed as a practical compatibility tool. It does **not** claim lossless conversion between the two readers, because the two applications store fundamentally different notions of "position in a book."

## Why This Exists

Moon+, Foliate, and EPW all store useful reading progress information, but they do not store it in the same shape:

- Foliate stores structured JSON and usually an EPUB CFI in `lastLocation`
- Moon+ stores a proprietary compact locator or a looser key/value state dump
- EPW stores a content index plus rendered-row state in SQLite

That means "syncing progress" is straightforward, but "syncing exact position" is sometimes only approximate. This project tries to do the most reliable thing possible with the information available.

Note:  While I use Calibre for library management, I have Foliate configured as my reader.

## Features

- Creates and uses a local virtual environment
- Installs dependencies from `requirements.txt`
- Reads Moon+, Foliate, optional EPW locations, and optional `Calibre_DB` from its local config file
- Matches books by shared local paths or unambiguous title/contributor metadata
- Supports conflict resolution with `--position`, `--date`, `--moon`, `--foliate`, and `--epw`
- Supports `--loud` for noisy step-by-step progress output
- Updates Foliate progress and approximate reopen position
- Updates Moon+ key/value states directly
- Updates EPW SQLite reading state directly
- Attempts approximate Foliate -> Moon+ compact sync using the actual EPUB spine
- Bootstraps missing Foliate/EPW entries when one side already knows the book filepath
- Skips epw entries pointing at remote URLs; accepts local paths and local file URLs
- Populates Foliate's cached cover image when a Calibre cover or EPUB cover can be resolved during Foliate entry creation
- Counts only actual reading-position changes in the final `Updated N reading state file(s).` summary

## Quick Start

1. Create your local config from the example file:

```bash
cp env.example your-local-config
```

2. Rename that file to the expected local config filename and edit it so it points at your real state directories:

```text
Moon:/path/to/Moon+/
Foliate:/path/to/com.github.johnfactotum.Foliate/
EPW:/path/to/epw/or/states.db
Calibre_DB=/path/to/CalibreLibrary/metadata.db
```

All paths above are examples.
If you installed Foliate via Flatpak, one common example location is `$HOME/.var/app/com.github.johnfactotum.Foliate/data/com.github.johnfactotum.Foliate/`.

3. Run the sync:

```bash
./sync_reading_state.py
```

4. Optional conflict modes:

```bash
./sync_reading_state.py --position
./sync_reading_state.py --date
./sync_reading_state.py --moon
./sync_reading_state.py --foliate
./sync_reading_state.py --epw
./sync_reading_state.py --loud
./sync_reading_state.py --help
```

## How The Script Works

The script follows a fixed sequence.

### 1. Bootstrap Python

Before doing any sync work, the script:

- handles `-h`, `--help`, and invalid arguments first, so CLI help/errors exit cleanly without bootstrapping the virtual environment
- checks whether it is already running in a virtual environment
- creates its local virtual environment if needed
- re-executes itself with the venv Python interpreter
- installs everything from `requirements.txt`

This happens before parsing its local config or touching any reading state files.

### 2. Read Configuration

The script reads its local config file from the same real directory as `sync_reading_state.py`.

That means it still finds the correct config when you:

- run it from a different working directory
- invoke it through a symlink
- schedule it with cron

Application entries use:

```text
APPLICATION:directory
```

The optional Calibre database entry uses:

```text
Calibre_DB=/path/to/CalibreLibrary/metadata.db
```

Currently supported applications:

- `Moon`
- `Foliate`
- `EPW`

Unknown application names are ignored.

If `Calibre_DB` is configured, the script can use Calibre's `metadata.db` to
recover real ebook file paths from title/author matches. That especially helps
Moon+ one-sided books, since Moon+ itself does not store a stable library
filepath.

### 3. Load Moon+ State

Moon+ books are identified from filenames, not internal metadata.

The script expects filenames that effectively look like:

```text
Title - Author.epub.po
```

It strips known file suffixes and extensions, then extracts:

- title
- author

It supports two Moon+ on-disk formats:

- compact one-line state such as `1682031364441*37@0#1202:100%`
- key/value state such as `percent=41.3`

Both are normalized into one internal representation before syncing.

### 4. Load Foliate State

Foliate files are plain JSON. The script reads:

- `metadata.identifier`
- `metadata.title`
- `metadata.author`
- `metadata.modified`
- `progress`
- `lastLocation`

The `author` field is normalized from any of these shapes:

- string
- single object
- list of objects

Multi-author Foliate books are joined into a string like:

```text
Author A & Author B
```

so they can match Moon+ filenames using the same convention.

### 5. Match Books

The script groups books by shared local paths first, then by unambiguous
normalized title and contributor metadata.

Normalization intentionally ignores formatting details:

- case is folded
- punctuation is collapsed
- underscores become spaces
- repeated whitespace is collapsed

Title matching also recognizes English article-sorted names: `Butcher's
Masquerade, The` matches `The Butcher's Masquerade` (likewise for `A` and `An`).
Articles are retained. Apostrophes inside words are ignored (`Zoes Tale` matches
`Zoe's Tale`), and equivalent Unicode spellings are normalized.

Foliate and epw entries pointing to the same local book file are grouped even
when their title or contributor metadata differs. Local `file:` URLs are decoded
before comparing paths. A unique Calibre match can supply the same identity for
Moon+.

Without a shared path, titles must match. Contributor lists may be reordered;
shortened anthology lists must have at least three distinct names, matching first
and last contributors, and at least 80% of the shorter list in common. Ambiguous
matches involving multiple entries from one reader are kept separate, with a
warning under `--loud`. Calibre lookups also require a unique candidate.

This is a lossy comparison by design. The goal is resilience across metadata sources, not preservation of display formatting.

Moon+ filenames supply the title and contributors used for matching.
The final ` - ` separates the title from the author, so titles can contain
earlier separators. State-file suffixes and ebook extensions are recognized
regardless of case.

Foliate and EPW can also bootstrap one-sided entries when one side already
knows the real book filepath. That lets them sync even if the book has not yet
been opened in the other application.

If `Calibre_DB` is configured, Moon+ can also participate in that bootstrap
path indirectly: the script can match the Moon+ title/author to Calibre and use
the Calibre library path to create missing Foliate or EPW state.

When that Calibre-backed path is used to create a Foliate entry, the script
also tries to populate Foliate's cached cover image:

- first from Calibre's sibling `cover.jpg`
- otherwise from the EPUB's embedded cover image

### 6. Choose a Winner

For each matched book, the script picks which side wins.

#### `--position` (default)

The larger progress percentage wins.

#### `--date`

The newer in-file reading-state timestamp wins.

- Foliate prefers `metadata.modified`
- Moon+ prefers its own embedded timestamp fields
- filesystem mtime is used only as fallback

#### `--moon`

Moon+ always wins.

#### `--foliate`

Foliate always wins.

#### `--epw`

EPW always wins.

#### `--loud`

Print step-by-step progress output, including:

- configured paths and loaded state counts
- bootstrap creation steps
- per-book winner selection
- per-target update attempts
- inline non-fatal warnings as they occur

### 7. Write The Loser

Once a winner is chosen, the script updates the other side.

## How Writes Work

### Moon+ -> Foliate

When Moon+ wins, the script:

- converts Moon+ percent into Foliate's `[current, total]` progress scale
- updates `metadata.modified`
- tries to synthesize a new Foliate `lastLocation`

That `lastLocation` is not exact. It is an approximate EPUB CFI anchored to the beginning of the best matching EPUB spine section.

If the script cannot synthesize a better CFI, it preserves the existing Foliate `lastLocation` instead of deleting it.

### Foliate -> Moon+

There are two different cases.

#### Moon+ key/value files

These are updated directly:

- `percent` is rewritten
- existing activity timestamp fields are refreshed
- unknown keys are preserved

#### Moon+ compact files

These cannot be updated exactly from Foliate's CFI, because Moon+'s compact locator is proprietary.

Instead, the script attempts a section-level approximation:

1. resolve the backing EPUB path from `foliate/library/uri-store.json`
2. open the actual EPUB file
3. inspect the EPUB spine
4. parse Foliate's current CFI to identify the current spine item
5. rewrite Moon+ compact state to the start of that section

The resulting compact state is intentionally boundary-based. It resets page and offset to the start of the chosen section rather than inventing paragraph-accurate numbers.

### Foliate <-> EPW

When Foliate and EPW both know the same book filepath, the script can sync them
even if one side did not previously have a saved reading-state entry.

epw entries pointing at remote URLs are skipped because synchronization needs
direct filesystem access. Local `file:` URLs are supported, including escaped
spaces and punctuation; their host must be empty or `localhost`.

Foliate -> EPW:

- creates or updates an EPW `reading_states` row keyed by exact filepath
- updates the EPW `library` row for title, author, and progress
- approximates EPW `content_index` from Foliate's current CFI or overall progress
- uses a conservative boundary location with `row = 0` and `rel_pctg = 0.0`

EPW -> Foliate:

- creates or updates a Foliate JSON state file and `library/uri-store.json` entry
- uses EPUB metadata identifier when available
- otherwise uses Foliate's own fallback identifier shape: `foliate:` + MD5(first 10,000,000 bytes)
- approximates `lastLocation` from EPW's `content_index`

## Assumptions

The script makes several explicit assumptions.

### Matching Assumptions

- Moon+ filenames are the source of truth for title and author
- Moon+ filenames use the `Title - Author` convention
- Foliate metadata titles/authors refer to the same edition or a compatible edition
- joining multiple Foliate authors with ` & ` is acceptable for matching against Moon+ filenames
- EPW library title/author are good enough for cross-app matching

### Timestamp Assumptions

- Foliate `metadata.modified` is a better signal than filesystem mtime
- Moon+ embedded timestamps are better than filesystem mtime
- Moon+ timestamps may be stored in seconds or milliseconds

### Approximation Assumptions

- Moon+ chapter values are section-like enough to map to EPUB spine items
- a start-of-spine-item CFI is a safer approximation than fabricating a precise paragraph offset
- a start-of-spine-item Moon+ compact locator is safer than fabricating page/offset detail
- an EPW boundary position at `row = 0` is safer than inventing a rendered-line offset

### Environment Assumptions

- Foliate's `library/uri-store.json` exists when reverse approximation needs EPUB access
- the URI store points at an actually accessible local EPUB file
- the EPUB is well-formed enough to expose a valid OPF and spine
- EPW is closed while the script edits `states.db`, so a later in-process save does not overwrite external changes

## Caveats

These caveats are structural, not incidental.

### Exact Position Is Not Guaranteed

This script can often keep both apps in roughly the same reading region, but it cannot promise exact sentence-level round-trips between the two readers.

### Different Editions Can Still Drift

If Moon+ and Foliate refer to different editions of the same title, even a correct match may land in only the roughly corresponding section.

### Reverse Compact Sync Is Approximate

Foliate -> Moon+ compact sync is section-level only. It does not reconstruct Moon+'s internal locator semantics.

### Missing EPUB Access Limits Reverse Sync

If the actual EPUB file cannot be resolved through Foliate's URI store, the script cannot perform compact reverse approximation safely. In that case it prints a warning and leaves the Moon+ compact file unchanged.

### Update Count Means Position Changes

The final `Updated N reading state file(s).` summary counts only actual
reading-position changes.

It does not count:

- bootstrap creation of a missing Foliate or EPW entry
- Foliate cover-cache writes
- metadata-only writes such as refreshed timestamps or title/author normalization

### Some One-Sided Books Are Still Skipped

Moon+ entries without a reader counterpart or a unique Calibre match are skipped.

Foliate and epw can create missing counterpart entries when filepath information
is available. A unique Calibre match can provide a path for Moon+ as well.

### Unparseable Entries Are Skipped

The script skips:

- Moon+ files whose filenames do not parse into `Title - Author`
- Foliate JSON files without usable title metadata
- entries with insufficient progress data for the chosen conflict mode

## Troubleshooting Matches

Run with `--loud` to see book groups, selected readers, update attempts, and
warnings. This performs a normal sync; it is not a preview mode.

- `Skip one-sided entry` means only one reader has a state in that group after bootstrap.
- `Skip unresolved entry` means no winner could be selected, for example because progress is missing in the default mode.
- `Ambiguous book match` means multiple entries from one reader could belong to the same group, so the proposed groups were kept separate.

Check titles, contributor lists, and the backing book paths when an entry remains
unmatched. Similar titles alone are not enough to establish a match, and article
handling currently covers English `The`, `A`, and `An` only.

## Files

- [`sync_reading_state.py`](./sync_reading_state.py): main script
- [`requirements.txt`](./requirements.txt): Python dependencies
- [`env.example`](./env.example): example configuration
- [`CHANGELOG.md`](./CHANGELOG.md): release history

## License

This project is licensed under [`0BSD`](./LICENSE).


## AI Usage

![button_extensive-ai-use](https://i.imgur.com/aYq2HvX.png)

The code in this repository has been significantly written or altered by an AI tool with human supervision.  The instructions to the AI agents were detailed, step-by-step pseudocode with very specific limitations.  Whenever possible, the code is also explicitly and extensively commented so that it may be audited to determine that it does what it says it does.  Again, while the code in this repository works for me and I am using it, it should be considered a proof-of-concept for others to refine, fix, and build upon.  

You are encouraged to fork and refine or rebuild this program or create something better that has the same functionality.

This is what most people would call "vibe coded". 
