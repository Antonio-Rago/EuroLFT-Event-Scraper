# Collector operation and direct Jekyll publication

The collector repository owns all Python code, prompts, extraction schemas and private
caches. The website repository owns the reviewed event files and the calendar template.
Scraping prepares each complete website-ready JSON file before opening its PR.

## Website data and review

One event is stored in `_data/community_events/EVENT_ID.json`. Title, dates, location,
deadlines and links are ordinary top-level JSON fields. Stable ID, revision, sources,
evidence, identity aliases, warnings and editorial history are retained for review,
matching and corrections. The collector decodes this file into its internal nested
record when reconciling; no transformation runs in website Actions.

GitHub review is the publication boundary:

- Merge an event PR into website main to publish that file with Jekyll.
- Close an unmerged collector PR to reject it. Rejection identities stay remembered.
- Edit the event JSON before merging to correct it. Advance `revision` on updates.
- To hide a published event, submit a new PR setting `decision.status` to `hidden`
  with a reason and an advanced revision. Keep its ID and file to prevent reingestion.

`decision` is retained for exclusions and historical compatibility. Its legacy
`pending`/`draft` marker does not require an approval command: merging the file into
main accepts it. Jekyll displays main's files except `hidden` and `rejected` records.
History from earlier explicit decisions is preserved, with no invented approval actor
or timestamp. The site does not look up GitHub merge receipts during its build.

Unmerged branch data is a proposal. The production Pages workflow builds main only.
Review unknown dates, timezones, relevance and warnings before merging. Unknown values
remain null and the calendar labels them as unconfirmed. Relations acquire a public
link after their target event is also on main; hidden targets are omitted or redirected
to their surviving duplicate. Each event PR changes its own file, so bulk review does
not create conflicts over a shared calendar JSON file.

The website deployment installs Ruby, builds Jekyll and deploys Pages. It downloads
no collector code, installs no Python packages, and performs no scraping or Gemini calls.
The previous per-PR Python check workflow is removed; no repeated event check alerts
are introduced. The pre-existing institution utility is unrelated to this pipeline.

## One-time transition

The website migration PR converts existing records losslessly into `_data/community_events/`,
removes the old aggregate JSON and switches the page and Actions to direct Jekyll.
Review and merge the collector implementation and website migration PRs.

**After the website migration is merged, convert the existing managed event PRs before
merging those old proposals:**

```sh
_event_collector/local/venv/bin/python -m tools.event_collector.migrate --open-prs --dry-run
_event_collector/local/venv/bin/python -m tools.event_collector.migrate --open-prs
```

The command keeps the existing PR numbers and branches, uses their final current
record contents, retains reviewer notes, and updates the file paths. It saves a private
backup and checkpoint. Its conditional branch update refuses concurrent edits; rerun
to pick up the newer review. It never accepts, closes or merges a proposal. Already
converted proposals are skipped. Inspect `local/submissions/migration-latest.json`.

To convert a website checkout locally without committing or pushing:

```sh
_event_collector/local/venv/bin/python -m tools.event_collector.migrate --site WEBSITE_CHECKOUT --dry-run
_event_collector/local/venv/bin/python -m tools.event_collector.migrate --site WEBSITE_CHECKOUT
```

This also preserves hidden/rejected IDs and all historical metadata. Conflicting
old/new copies stop migration without overwriting either. Backups are collector-local.
Old saved review packages are still accepted and converted when preparing new PRs.
Submission refuses an unmigrated website and stops if an old-format event is merged
onto main after the cutover; convert that record before continuing.

## Setup and credentials

Use Python 3.9 or newer and run from the scraper repository:

```sh
python3 -m venv _event_collector/local/venv
_event_collector/local/venv/bin/python -m pip install -r tools/event_collector/requirements.txt
_event_collector/local/venv/bin/python -m tools.event_collector.credentials
```

Enter the Gemini key only at the hidden terminal prompt. Keep the configured Free-tier
project with billing disabled. Keys, raw messages, full model replies and cache/identity
ledgers remain ignored and private. Existing HTTPS Git login or `EVENT_REVIEW_TOKEN`
supplies GitHub website contents/PR access; never paste a key or token into chat.

## Entire acquisition and submission

```sh
_event_collector/local/venv/bin/python -m tools.event_collector.weekly --live --wait --open-pr --all
```

The run reads one current UTC archive month, discovers bounded upcoming INSPIRE Lattice
events, reuses Gemini cache entries, reconciles against website main, and opens all
eligible candidates as separate ready PRs. Use `--month YYYY-MM` for another archive
month or `--month-window previous` for the last completed month. This is a calendar
archive, not a rolling thirty-day query. INSPIRE uses `subject=lattice`, 25 results
per page, at most six pages and 100 records; relevance remains a human review question.

Partial source coverage, model failures and quota deferrals preserve the useful data
locally. With `--open-pr`, independently validated events are still submitted; one
unresolved announcement does not block other event PRs. The summary retains
`status: partial`, `coverage_complete: false` and a nonzero exit code until coverage
is complete. Check `submission_status`, `remote_writes` and the PR links to see what
was submitted. `extraction_issues` lists announcements needing repair, and
`extraction_review` points to the private detailed report. Identity conflicts still
require resolution before submission. Existing 20-attempt daily and 60-second
request-pacing limits remain. Rerun to reuse completed extraction.

Saved model failures with parsed event facts are reconsidered locally without another
Gemini request. In particular, a draft timezone `JST` is represented as `Asia/Tokyo`
only when a verified quotation supporting that exact timezone field explicitly says
`JST (UTC+9)`. The draft includes a review warning; the original saved model reply and
its integrity hash remain unchanged. Unsupported or ambiguous zones remain repair
items. This also lets an already listed event acquire a newly announced deadline
through an ordinary correction PR.
No scheduler is enabled. The scraper's optional Actions workflow prepares INSPIRE data
and uses the same separate-PR submission logic with an explicit cross-repository token.

## Submit saved events

```sh
_event_collector/local/venv/bin/python -m tools.event_collector.submit PACKAGE_DIRECTORY --all
```

Each remaining event gets a flat sibling branch `automation/community-events-EVENT_ID`.
Merged, rejected and already-open events are skipped, including old shared-branch PRs.
The queue stays intact. Progress prints PR links and is saved privately in
`local/submissions/all-latest.json`. Uploads are paced; rerun the same command after
an interruption to resume without duplicates or overwriting reviewer edits.
Omit `--all` for the legacy one-at-a-time mode; `--event EVENT_ID` selects one event.
`--batch` combines all events into one PR and cannot be combined with those options.

Local validation without GitHub access or writes to the website:

```sh
_event_collector/local/venv/bin/python -m tools.event_collector.submit PACKAGE_DIRECTORY --all --dry-run --site WEBSITE_CHECKOUT
_event_collector/local/venv/bin/python -m tools.event_collector.registry --site WEBSITE_CHECKOUT --publication
```

## Manual additions, edits and private previews

Use the website Issue form for missing events and corrections, or prepare a facts JSON
file and run the collector's existing manual editor. This works without Gemini:

```sh
_event_collector/local/venv/bin/python -m tools.event_collector.moderate manual FACTS_JSON --actor YOUR_GITHUB_LOGIN --output _event_collector/local/manual
_event_collector/local/venv/bin/python -m tools.event_collector.review_changes _event_collector/local/manual --site WEBSITE_CHECKOUT
```

Submit the package printed by the second command with `submit PACKAGE --all`, then
review and merge the PR. No separate approve command is required. The same editor
accepts the new website JSON files for corrections, hides and duplicate merges.
A maintainer can label a proposal `event-review-reset` before an administrative closure
so it remains eligible; ordinary human closures are editorial rejections.

`preview PLAN --site WEBSITE_CHECKOUT` creates an isolated, clearly labelled Jekyll
preview data directory and config; it never changes the reviewed data files. Build
that private preview with the config printed by the command. `backup save --site ...`
preserves website records and private identity state without credentials or raw inputs;
restore into a separate directory and inspect it before replacing any state.

The earlier sealed-approval aggregate export API remains for old offline tools only.
It is unused by ingestion, event PR creation and website publication. The previous
`--github-merged` publication mode and merge-receipt implementation are removed.
