# Operating the EuroLFT event scraper

## Repository responsibilities

This repository contains all collector/editorial Python code, tests, schemas, prompts,
source adapters and ingestion workflows. EuroLFT/eurolft.github.io contains the event
registry, review Issue form, calendar and review/publication workflows. The website
checks out a pinned collector commit to run validation and export; it never runs
source collection or Gemini in its build.

`_event_collector/records/*.json` in the website repository is the authoritative
editorial registry, including approved, rejected and hidden events. The same directory
name in legacy development examples does not make the collector authoritative.
Operational reconciliation, review packaging and publication require `--site` pointing
to a separate website checkout. A missing registry path is an error, not an empty registry.

## Local setup

Clone both repositories next to one another:

```sh
git clone git@github.com:Antonio-Rago/EuroLFT-Event-Scraper.git
git clone https://github.com/EuroLFT/eurolft.github.io.git
cd EuroLFT-Event-Scraper
python3 -m venv _event_collector/local/venv
_event_collector/local/venv/bin/python -m pip install -r tools/event_collector/requirements.txt
_event_collector/local/venv/bin/python -m tools.event_collector.credentials
```

Python 3.9 or newer is required. Enter the Gemini key only at the hidden terminal
prompt. Use the configured Free-tier project with billing disabled; the tool cannot
verify billing settings. Private keys, raw messages, model responses and request/identity
ledgers remain in ignored collector-local storage. Preserve this state between runs.
Update the website checkout from its reviewed main branch before reconciliation;
resolve outstanding local editorial changes before updating it.

## Select and extract one archive month

Run from the scraper checkout. Preflight collects the selected archive and makes no
Gemini requests; the second command extracts from those saved messages:

```sh
_event_collector/local/venv/bin/python -m tools.event_collector.ingest --month 2026-09
_event_collector/local/venv/bin/python -m tools.event_collector.ingest --month 2026-09 \
  --skip-collection --live --wait
```

Change the YYYY-MM value for another month. Alternatively select `--month-window
current` (the default) or `--month-window previous` (last completed UTC month).
Selection options are mutually exclusive. Each invocation reads one archive month,
not the full mailing-list website. Announcement month and event dates are different.

The default source limit is 50 messages; use `--limit 100` consistently in both commands
for a larger month. This is a completeness bound, not a selection of the first few
messages. Inspect `collection_status` and all per-message cases. A partial run does
not imply complete coverage.

Gemini calls have a local 20-attempt rolling-24-hour cap and at least 60-second pacing.
`--wait` paces a batch; it does not wait for daily quota renewal. Rerun after capacity
returns to process unattempted messages; successful cached responses are reused.
Failed API requests are not automatically retried. Readable parsed responses with
validation issues may still become pending candidates carrying warnings.
`--offline` applies to archive downloads; it does not disable model calls when
`--live` is supplied. No case labels or evaluation split are used in routine ingestion.

The command prints the review.md path in a private `local/review/ingest-...` bundle.
No event decision or website update is made.

## Reconcile and prepare website review changes

Use the directory containing the printed review.md, then the resulting plan directory:

```sh
_event_collector/local/venv/bin/python -m tools.event_collector.reconcile BUNDLE_DIRECTORY \
  --site ../eurolft.github.io
_event_collector/local/venv/bin/python -m tools.event_collector.review_changes PLAN_DIRECTORY \
  --site ../eurolft.github.io
```

Canonical website records are read only during these steps. Exclusions, duplicate
redirects, manual facts and protected corrections are preserved. A reviewer can supply
`reconcile --matches choices.json` for ambiguous identities, mapping candidate IDs
to canonical IDs or "new". An existing strong exclusion cannot be bypassed with a new ID.

The private package contains `changes/_event_collector/records/*.json`, review.md and
a manifest checking current canonical state and package integrity. It omits unchanged,
excluded and unresolved identity items and strips private evidence details from pending
records with a warning. No raw messages, keys or full model responses are submitted.

Apply a prepared package only to an explicitly selected local website checkout:

```sh
_event_collector/local/venv/bin/python -m tools.event_collector.review_changes PACKAGE_DIRECTORY \
  --site ../eurolft.github.io --apply
```

This stages no Git commit and performs no remote operation. Use a website proposal
branch and submit its record changes as a draft PR. Reprepare if canonical decisions
or package contents change. The collector repository receives no canonical event records.

## Editorial actions

The website Issue form handles missing-event suggestions and corrections; submission
does not publish or call a model. Use the facts template at
`_event_collector/manual-event.template.json`. Run editorial commands here, with explicit
actor identity and a private output directory. Outputs preserve canonical inputs.

```sh
_event_collector/local/venv/bin/python -m tools.event_collector.moderate manual FACTS.json \
  --actor YOUR_GITHUB_HANDLE --output _event_collector/local/editor
_event_collector/local/venv/bin/python -m tools.event_collector.moderate edit RECORD.json \
  --patch CORRECTION.json --protect /facts/location/city \
  --actor YOUR_GITHUB_HANDLE --output _event_collector/local/editor
_event_collector/local/venv/bin/python -m tools.event_collector.moderate approve RECORD.json \
  --actor YOUR_GITHUB_HANDLE --acknowledge-all --output _event_collector/local/editor
```

Inspect every fact and source before approval. Approvals require supported type,
start date, valid chronology, safe public links, warning acknowledgement and IANA
timezones for published clock times. Verify or remove an unknown-zone clock time while
retaining its known date. Corrections invalidate approval. Content seals also cover
evidence, aliases, relationships, warnings and editor overrides.

Use `reject` or `hide` with `--reason`; reversing an exclusion requires explicit
`restore`. Edits to excluded records keep the exclusion. For duplicates, use
`moderate merge SURVIVOR.json --duplicate DUPLICATE.json` with actor/output arguments.
It creates a pending survivor and a hidden redirect; review and approve the survivor.

Prepare and apply editor output with `review_changes`, always supplying `--site`.
Merge-ready website records require explicit approved/rejected/hidden decisions.
Merge durable exclusion records; closing a PR alone means deferred. Retain IDs instead
of deleting records. The website PR checks enforce advancing revisions, restoration
history, current-content approval and safe publication.

## INSPIRE and collection automation

For a bounded upcoming Theory-HEP catalogue pull, no LLM is required:

```sh
_event_collector/local/venv/bin/python -m tools.event_collector.inspire
```

Reconcile its bundle with the same website. Or collect, reconcile and package in one step:

```sh
_event_collector/local/venv/bin/python -m tools.event_collector.automation --site ../eurolft.github.io
```

Defaults bound collection to six pages, 25 entries/page and 100 entries. Type and broad
conference relevance remain human-review questions. Missing facts stay unknown.
An outage or limit produces partial coverage; it never removes or cancels events.

The manual "Prepare community event review" workflow belongs to this scraper
repository. It checks out the website separately, reads its decisions, and uploads
only a minimal INSPIRE review package. Optional draft-PR creation requires an
`EVENT_REVIEW_TOKEN` secret explicitly permitted to write contents and PRs in the
website repository. The scraper's default GITHUB_TOKEN cannot grant cross-repository
write access. Without that secret, preparation still works and PR submission can use
the operator's existing Git login.

Existing website `automation/community-events` proposal branches are preserved.
Resolve their decisions and remove a resolved branch before another automated proposal.
The publish job rechecks latest website state and refuses partial collection. It never
approves or merges. Private mailing-list/Gemini state stays on the local operator;
no public Actions cache/artifact contains raw sources or credentials.

## Website validation and private preview

Website workflows check out a specific 40-character collector commit and use its tools
to validate website records, export approved facts and audit the built site. A collector
upgrade requires a website PR updating both workflow pins. Runtime cloning puts no
collector source in the website Git tree. The temporary `.event-tools` directory is
ignored and excluded from Jekyll.

A local approved export can be generated from here:

```sh
_event_collector/local/venv/bin/python -m tools.event_collector.registry --site ../eurolft.github.io \
  --require-decisions --export ../eurolft.github.io/_data/community_events.json
```

For a private pending-data preview:

```sh
_event_collector/local/venv/bin/python -m tools.event_collector.preview PLAN_DIRECTORY \
  --site ../eurolft.github.io
```

Then run Jekyll from the website checkout with its printed preview configuration.
The unapproved preview is private; canonical decisions and public dataset remain unchanged.

## Recovery and initial activation

Save authoritative website decisions and private pending IDs together, excluding keys/raw sources:

```sh
_event_collector/local/venv/bin/python -m tools.event_collector.backup save \
  _event_collector/local/backups/editorial.json --site ../eurolft.github.io
_event_collector/local/venv/bin/python -m tools.event_collector.backup restore \
  _event_collector/local/backups/editorial.json --destination _event_collector/local/recovered
```

Restoration verifies integrity and writes a new separate directory. Preserve the private
quota ledger separately; editorial recovery must not reset request allowances.

Review/merge the scraper code and website integration PRs, designate event reviewers,
and require human review plus the event validation check on website main. Check the
optional cross-repository draft-PR credential only if enabling that job. Verify one
reviewed event merge and Pages deployment before scheduling. Current workflows start
manually; no recurring collection schedule is enabled. Observe missed events, false
positives, corrections, duplicates, review time and usage over a 2–4 week pilot.
Software regression tests do not establish Gemini extraction accuracy.
