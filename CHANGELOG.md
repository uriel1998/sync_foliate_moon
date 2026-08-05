# Changelog

## v.0.9.11

- document that EPW entries pointing at URLs are skipped
- document that the final update count reflects actual reading-position changes only
- document that non-fatal warnings are shown under `--loud` rather than by default
- update `--loud` help text to mention non-fatal warnings
- align README behavior notes with current sync semantics
- remove non-example path references from the README
- make bootstrap choose the platform-appropriate Python interpreter path instead of hard-coding `bin/python`

## v.0.9.10

- add EPW as a first-class sync target with `--epw` support
- add `Calibre_DB` path resolution through `metadata.db` for Moon+ bootstrap flows
- allow Moon+ to create missing Foliate and EPW entries indirectly via Calibre matches
- create missing Foliate state from EPW and missing EPW state from Foliate when filepath information is available
- populate Foliate cached cover images from Calibre `cover.jpg` or EPUB embedded covers during Foliate entry creation
- add `--loud` for step-by-step progress output
- make `-h` and `--help` exit cleanly before bootstrap
- make invalid arguments exit cleanly with usage output
- update README and sample configuration for EPW, `Calibre_DB`, cover caching, and loud mode

## v0.9.9

- add EPW as a sync participant alongside Moon+ Reader and Foliate
- add `--epw` winner selection support
- add EPW SQLite state loading and writing
- add Foliate <-> EPW synchronization based on shared local filepaths
- bootstrap missing Foliate or EPW state entries when the other side already knows the book filepath
- update sample configuration and README usage/docs for EPW support

## v0.9.8

- read configuration from the real directory containing `sync_reading_state.py` instead of the caller's working directory
- make the script work correctly when run from a different directory
- make the script work correctly when invoked through a symlink
- make the script work correctly under cron
- update the documentation to describe the repository-local config lookup behavior

## v0.9.7

- expand the README into full operational documentation, including workflow, assumptions, caveats, and file format behavior
- add `env.example`
- add the `0BSD` license file
- improve Foliate <-> Moon+ approximation logic using EPUB spine inspection and Foliate's URI store
- preserve or synthesize Foliate reopen locations more carefully instead of dropping location state
- surface visible warnings when reverse approximation cannot be done safely

## v0.9.6

- add initial repository scaffolding
- add repository ignore rules
- add the initial project plan and format notes
