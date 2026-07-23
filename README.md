# Syncing Ebook

Bidirectional reading-state sync between Moon+ Reader and Foliate.

The current implementation matches books by normalized `Title - Author` data and uses reading percentage as the shared sync value. That is enough to keep the two applications broadly aligned, but it is not an exact location converter because Moon+ and Foliate store fundamentally different locator formats.

## What the Script Does

`sync_reading_state.py`:

- creates and uses a local `.venv` if one is not already active
- installs everything listed in `requirements.txt`
- reads application data directories from `.env` in the directory you run it from
- loads Moon+ `.po` files and Foliate `.json` files
- matches books present on both sides
- chooses a winning side based on the command-line switch you provide
- writes the winning progress back to the losing side

## How Matching Works

Moon+ matching comes from the filename:

```text
Title - Author.epub.po
```

Foliate matching comes from JSON metadata:

- `metadata.title`
- `metadata.author.name`

Both values are normalized before comparison:

- case is ignored
- underscores are treated like spaces
- punctuation is collapsed
- repeated whitespace is collapsed

That makes matching more tolerant of small formatting differences between the two apps.

## Conflict Resolution

By default the script uses the furthest reading position:

```bash
./sync_reading_state.py
```

or explicitly:

```bash
./sync_reading_state.py --position
```

Other options:

```bash
./sync_reading_state.py --date
./sync_reading_state.py --moon
./sync_reading_state.py --foliate
./sync_reading_state.py --help
```

Behavior:

- `--position`: higher percentage wins
- `--date`: newer in-file reading-state modified timestamp wins, falling back to filesystem mtime if needed
- `--moon`: Moon+ always wins
- `--foliate`: Foliate always wins

## `.env` Format

Create `.env` in the directory where you run the script:

```text
Moon:/path/to/Moon+/
Foliate:/path/to/com.github.johnfactotum.Foliate/
```

Each line is one `APPLICATION:directory` pair.

## Data Formats

### Moon+

Moon+ is matched from filenames and read from `.po` files.

The current script supports:

- compact one-line `.po` state like `1682031364441*37@0#1202:100%`
- key/value `.po` state as described in `1_reference/Moon_Format.md`

### Foliate

Foliate state is read from JSON files in the data directory root, for example:

```text
9780765389206.json
```

The script uses:

- `metadata.title`
- `metadata.author.name`
- `progress`
- `metadata.modified`

## Important Limitation

Moon+ and Foliate do not store the same kind of location:

- Foliate uses EPUB CFI
- Moon+ uses a proprietary compact locator

Because of that:

- Moon+ to Foliate updates adjust `progress`, refresh `metadata.modified`, and remove stale `lastLocation`
- Foliate to Moon+ updates are limited to Moon+ key/value states that can be changed without inventing a proprietary locator
- compact Moon+ `.po` states are not rewritten from Foliate data because an exact reverse mapping is unavailable
- exact in-book position equivalence is not guaranteed

In other words, this is a practical state sync, not a lossless location translator.

## Files

- [`sync_reading_state.py`](./sync_reading_state.py): main sync script
- [`requirements.txt`](./requirements.txt): Python dependencies
- [`1_reference/`](./1_reference/): sample files and format notes used to derive the implementation

## Development Notes

The script currently skips:

- books that exist on only one side
- Moon+ files that do not follow `Title - Author`
- Foliate JSON files missing title metadata
- entries without enough progress data to resolve safely

## Running

From the repository root:

```bash
./sync_reading_state.py
```

If you want help text:

```bash
./sync_reading_state.py --help
```
