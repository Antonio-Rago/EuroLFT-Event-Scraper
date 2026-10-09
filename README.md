# EuroLFT Event Scraper

Collector and editorial tooling for the lattice-community event calendar.

This repository owns the Python source, tests, prompts, schemas, source adapters and
collection workflows. [EuroLFT/eurolft.github.io](https://github.com/EuroLFT/eurolft.github.io)
owns event records, human review and calendar publication. Event approvals and
exclusions are authoritative only in the website repository.

Private keys, raw announcements, model responses, cached requests and pending
identity/quota state stay in ignored `_event_collector/local/` storage here.

See [the operating guide](docs/event-collector-operations.md) for setup, monthly
ingestion and the `--site` connection to a separate website checkout. The website
checks out a pinned version of these tools for validation and publication; it stores
no collector source and never runs collection or Gemini during its build.
