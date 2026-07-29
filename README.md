# Syncing Ebook

Bidirectional reading-state sync between Moon+ Reader and Foliate.

This project synchronizes reading state between:

- Moon+ sidecar files such as `Title - Author.epub.po`
- Foliate JSON state files such as `9780765389206.json`

The script is designed as a practical compatibility tool. It does **not** claim lossless conversion between the two readers, because the two applications store fundamentally different notions of "position in a book."

## Why This Exists

Moon+ and Foliate both store useful reading progress information, but they do not store it in the same shape:

- Foliate stores structured JSON and usually an EPUB CFI in `lastLocation`
- Moon+ stores a proprietary compact locator or a looser key/value state dump

That means "syncing progress" is straightforward, but "syncing exact position" is sometimes only approximate. This project tries to do the most reliable thing possible with the information available.

Note:  While I use Calibre for library management, I have Foliate configured as my reader.

## Features

- Creates and uses a local `.venv`
- Installs dependencies from `requirements.txt`
- Reads Moon+ and Foliate directories from `.env`
- Matches books by normalized `title + author`
- Supports conflict resolution with `--position`, `--date`, `--moon`, and `--foliate`
- Updates Foliate progress and approximate reopen position
- Updates Moon+ key/value states directly
- Attempts approximate Foliate -> Moon+ compact sync using the actual EPUB spine
- Prints visible warnings when a reverse approximation cannot be performed safely

## Quick Start

1. Create your local config:

```bash
cp env.example .env
```

2. Edit `.env` so it points at your real state directories:

```text
Moon:/path/to/Moon+/
Foliate:/path/to/com.github.johnfactotum.Foliate/
```

I am using Moon+'s cloud sync with NextCloud, and then NextCloud's app to sync with my desktop.
If you installed Foliate via Flatpak, look in `$HOME/.var/app/com.github.johnfactotum.Foliate/data/com.github.johnfactotum.Foliate/` 

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
./sync_reading_state.py --help
```

## How The Script Works

The script follows a fixed sequence.

### 1. Bootstrap Python

Before doing any sync work, the script:

- checks whether it is already running in a virtual environment
- creates `.venv` if needed
- re-executes itself with the venv Python interpreter
- installs everything from `requirements.txt`

This happens before parsing `.env` or touching any reading state files.

### 2. Read Configuration

The script reads `.env` from the same real directory as `sync_reading_state.py`.

That means it still finds the correct config when you:

- run it from a different working directory
- invoke it through a symlink
- schedule it with cron

Each non-empty, non-comment line must be:

```text
APPLICATION:directory
```

Currently supported applications:

- `Moon`
- `Foliate`

Unknown application names are ignored.

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

The script matches books by normalized `(title, author)` pairs.

Normalization intentionally ignores formatting details:

- case is folded
- punctuation is collapsed
- underscores become spaces
- repeated whitespace is collapsed

This is a lossy comparison by design. The goal is resilience across metadata sources, not preservation of display formatting.

Only books present on **both** sides are considered for sync.

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

## Assumptions

The script makes several explicit assumptions.

### Matching Assumptions

- Moon+ filenames are the source of truth for title and author
- Moon+ filenames use the `Title - Author` convention
- Foliate metadata titles/authors refer to the same edition or a compatible edition
- joining multiple Foliate authors with ` & ` is acceptable for matching against Moon+ filenames

### Timestamp Assumptions

- Foliate `metadata.modified` is a better signal than filesystem mtime
- Moon+ embedded timestamps are better than filesystem mtime
- Moon+ timestamps may be stored in seconds or milliseconds

### Approximation Assumptions

- Moon+ chapter values are section-like enough to map to EPUB spine items
- a start-of-spine-item CFI is a safer approximation than fabricating a precise paragraph offset
- a start-of-spine-item Moon+ compact locator is safer than fabricating page/offset detail

### Environment Assumptions

- Foliate's `library/uri-store.json` exists when reverse approximation needs EPUB access
- the URI store points at an actually accessible local EPUB file
- the EPUB is well-formed enough to expose a valid OPF and spine

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

### One-Sided Books Are Skipped

Books present in only one application are ignored for now.

### Unparseable Entries Are Skipped

The script skips:

- Moon+ files whose filenames do not parse into `Title - Author`
- Foliate JSON files without usable title metadata
- entries with insufficient progress data for the chosen conflict mode

## Files

- [`sync_reading_state.py`](./sync_reading_state.py): main script
- [`requirements.txt`](./requirements.txt): Python dependencies
- [`env.example`](./env.example): example configuration
- [`1_reference/`](./1_reference/): reference notes and sample data used to derive the implementation

## License

This project is licensed under [`0BSD`](./LICENSE).


## AI Usage

![button_extensive-ai-use](https://i.imgur.com/aYq2HvX.png)

The code in this repository has been significantly written or altered by an AI tool with human supervision.  The instructions to the AI agents were detailed, step-by-step pseudocode with very specific limitations.  Whenever possible, the code is also explicitly and extensively commented so that it may be audited to determine that it does what it says it does.  Again, while the code in this repository works for me and I am using it, it should be considered a proof-of-concept for others to refine, fix, and build upon.  

You are encouraged to fork and refine or rebuild this program or create something better that has the same functionality.

This is what most people would call "vibe coded". 
