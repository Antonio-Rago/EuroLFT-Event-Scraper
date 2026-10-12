# EuroLFT Event Scraper

Collect lattice-community announcements and prepare one website-ready JSON file per
event for review in [EuroLFT/eurolft.github.io](https://github.com/EuroLFT/eurolft.github.io).
All scraping, Gemini extraction, reconciliation and Python code live in this separate
repository. The website runs Ruby/Jekyll only and reads the prepared files directly.

Run from this checkout after the one-time website migration:

```sh
_event_collector/local/venv/bin/python -m tools.event_collector.weekly --live --wait --open-pr --all
```

This reads one current archive month plus upcoming INSPIRE **Lattice** conferences.
It reuses cached Gemini extraction and opens every eligible event as its own PR.
Use `--month YYYY-MM` or `--month-window previous` to select another archive month.
Accepted, rejected and already-open events are skipped; reviewer edits are preserved.
Review and merge to publish, or close without merging to reject. No approval commands,
website Python checkout, aggregate-file update or merge-receipt API lookup is needed.

Submit a saved queue without recollecting or calling Gemini:

```sh
_event_collector/local/venv/bin/python -m tools.event_collector.submit PACKAGE_DIRECTORY --all
```

See [the operating guide](docs/event-collector-operations.md) for credentials, migration,
manual additions, corrections, backups and private previews. No recurring schedule is enabled.
