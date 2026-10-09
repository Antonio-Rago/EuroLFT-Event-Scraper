# EuroLFT Event Scraper

Collector and editorial tooling for the lattice-community event calendar.

This repository owns the Python source, tests, prompts, schemas, source adapters and
collection workflows. [EuroLFT/eurolft.github.io](https://github.com/EuroLFT/eurolft.github.io)
owns event records, GitHub review and calendar publication. Human PR merges and
closures in the website repository supply acceptance and rejection receipts.

Private keys, raw announcements, model responses, cached requests and pending
identity/quota state stay in ignored `_event_collector/local/` storage here.

See [the operating guide](docs/event-collector-operations.md) for setup, monthly
ingestion and the `--site` connection to a separate website checkout. The website
checks out a pinned version of these tools for validation and publication; it stores
no collector source and never runs collection or Gemini during its build.

After setup, run the full weekly collection-to-event-PR workflow from this checkout:

```sh
_event_collector/local/venv/bin/python -m tools.event_collector.weekly --live --wait --open-pr
```

It combines one mailing-list archive month with INSPIRE and submits one event per PR.
Review and merge on GitHub to accept; close without merging to reject. No local
per-event approval commands are required.
Omit these flags for preflight only. Use `tools.event_collector.submit PACKAGE_DIRECTORY`
to upload an existing validated package without repeating collection or model calls.
