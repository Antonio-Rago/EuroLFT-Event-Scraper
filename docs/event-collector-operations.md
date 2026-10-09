# Operating the EuroLFT event scraper

## Repository responsibilities

This repository contains all collector/editorial Python code, tests, schemas, prompts,
source adapters and ingestion workflows. EuroLFT/eurolft.github.io contains the event
registry, review Issue form, calendar and review/publication workflows. The website
checks out a pinned collector commit to run validation and export; it never runs
source collection or Gemini in its build.

`_event_collector/records/*.json` in the website repository stores event facts and
stable identities. Human PR merges and closures supply the default editorial decisions;
legacy explicit approved, rejected and hidden JSON decisions remain supported. The same directory
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

## Weekly operating command and GitHub review

Run from the scraper checkout once a week:

```sh
_event_collector/local/venv/bin/python -m tools.event_collector.weekly --live --wait --open-pr
```

The command reads **one current UTC archive month**, collects bounded upcoming INSPIRE
Theory-HEP entries, reuses saved Gemini results and reconciles both sources against
website main. It keeps the full candidate queue privately and opens **one event PR**
at a time. It never merges a PR or decides which events the reviewer wants.

On GitHub, read the PR summary, sources and warnings, then:

- **Accept:** review and merge the PR. A teammate can use Approve first; when the PR
  was submitted with your own GitHub login, merge it after your review.
- **Reject:** close the PR without merging. The collector reads human closure receipts
  and remembers the event's identity and aliases, so it does not submit it again.
- **Correct:** edit the event JSON in GitHub or request changes before merging.
  CI checks the corrected facts. Existing-event updates must advance their revision.

No local moderation commands, approval seals, or per-event status editing are required.
Proposal CI accepts pending records and checks whether their facts can be published.
An undefined clock timezone or a missing required event date/type is a fact error;
correct it in the PR before merging. A date can remain known while an unknown-zone
clock value is set to null. Warning acknowledgement is part of reviewing and merging
those clearly displayed warnings.

The Pages workflow verifies the exact committed event against a **human-merged PR
into EuroLFT/eurolft.github.io main** before exporting it. The merge SHA must be an
ancestor of the build revision and the event content must match. A review on an
unmerged PR, a bot merge, or an unrelated/later edit does not grant publication.
Approval metadata is derived into the build's private export copy using the real
merger and merge time. The source JSON may retain its pending proposal marker; the
GitHub merge receipt is now authoritative. No extra bot commit changes the PR or
invalidates the human's review. Collection and Gemini never run during a website build.

Related events can be reviewed in separate PRs. Unmerged related targets remain in
the source record and acquire public links only after both events are accepted.

An existing open event PR stops the weekly command before source retrieval or Gemini.
After merging or closing it, run the command again to submit the next candidate.
For an already saved queue, use the submission command below to avoid recollection.
Resolved proposal branches are cleaned up only if their SHA still matches the closed
PR; another person's concurrent branch changes are preserved. Interrupted uploads
can be resumed with the unchanged package. Concurrent local runs are locked.

Use `--month-window previous` for the last completed UTC archive month or
`--month 2026-09` for a specific month. This is one calendar archive, not a rolling
30-day window. The archive month does not change INSPIRE's upcoming-event selection.

Omit `--live --wait --open-pr` for a preflight with no Gemini calls or submission.
Without `--open-pr`, a live run saves the full package locally. The default maintains
its own clean main checkout in ignored `_event_collector/local/weekly/website` and
preserves the operator's website checkout. `--site PATH` reads an explicit canonical
checkout; submission always checks current website main.

GitHub submission uses an existing HTTPS credential helper, or a privately configured
`EVENT_REVIEW_TOKEN`/`GH_TOKEN` with website contents and PR write access. Secrets are
not stored in commits, remote URLs, PR bodies or collector submission state.

Partial coverage, extraction failures and quota deferrals preserve candidates locally
and return a nonzero status without opening a PR. Rerun when capacity returns: the
20-attempt daily cap and 60-second pacing remain. Ambiguous identities need resolution
with `reconcile --matches` before submission. The latest live report is private at
`_event_collector/local/weekly/latest.json`. Keep private state between runs.

No recurring scheduler is enabled by these changes. The scraper's manual GitHub
workflow uses the same submission script for INSPIRE-only preparation.

## Submit the next event from an existing queue

```sh
_event_collector/local/venv/bin/python -m tools.event_collector.submit PACKAGE_DIRECTORY
```

Run this again after reviewing the preceding PR to advance the queue. Accepted and
rejected events are skipped, while the saved queue remains intact. It prints the new
PR URL and remaining count. Choose a particular event with `--event EVENT_ID`.
`--batch` is available when a reviewer explicitly wants to accept an entire batch.

For a local package integrity check with no GitHub access or website changes:

```sh
_event_collector/local/venv/bin/python -m tools.event_collector.submit PACKAGE_DIRECTORY \
  --dry-run --site ../eurolft.github.io
```

The script uses dedicated private checkouts to commit only packaged event records.
A stale saved queue cannot overwrite later human corrections. Reopen a rejected PR
to reconsider it; closing and deleting its branch does not erase the rejection receipt.

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
branch and submit its record changes as a review PR, or use `submit PACKAGE_DIRECTORY`
to handle the Git and PR steps. Reprepare if canonical decisions
or package contents change. The collector repository receives no canonical event records.

## Advanced local editorial tools (optional)

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
The commands above remain available for explicit local decisions, restoration and
duplicate merging. The default GitHub review flow does not require them; human closure
of a collector event PR is now a remembered rejection. Retain IDs instead
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
only a minimal INSPIRE review package. Optional event-PR submission requires an
`EVENT_REVIEW_TOKEN` secret explicitly permitted to write contents and PRs in the
website repository. The scraper's default GITHUB_TOKEN cannot grant cross-repository
write access. Without that secret, preparation still works and PR submission can use
the operator's existing Git login.

Existing open website event PRs are preserved. The submission job rechecks current
website state and refuses partial collection. It never accepts or merges an event.
Closed PRs supply rejection receipts; merging supplies publication approval. Private mailing-list/Gemini state stays on the local operator;
no public Actions cache/artifact contains raw sources or credentials.

## Website validation and private preview

Website workflows check out a specific 40-character collector commit and use its tools
to validate website proposals, verify GitHub merge receipts and export accepted facts and audit the built site. A collector
upgrade requires a website PR updating both workflow pins. Runtime cloning puts no
collector source in the website Git tree. The temporary `.event-tools` directory is
ignored and excluded from Jekyll.

A local approved export can be generated from here:

```sh
_event_collector/local/venv/bin/python -m tools.event_collector.registry --site ../eurolft.github.io \
  --github-merged --require-decisions --export ../eurolft.github.io/_data/community_events.json
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
