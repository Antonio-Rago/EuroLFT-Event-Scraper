# Community events: Step 1 contract

> Collector code now lives in this separate repository. Earlier development commands
> describe the former combined checkout. Use docs/event-collector-operations.md
> for current commands and --site pointing to the website event registry.

Status: Steps 1–3 implemented locally, including draft annotations, and Step 4's
extraction/validation/evaluation infrastructure prepared. Live model testing awaits
development inspection and held-out evaluation. A Gemini key is configured.
GitHub settings, pull requests, scheduling and publication
are not enabled.
Schema version: 1.

## Initial scope

Working defaults for discussion:

- Worldwide conferences, workshops, schools, lattice courses and lattice-specific methods/software events.
- Broader QCD, quantum computing, tensor-network, ML or HPC meetings when lattice
  field theory, gauge theory or directly relevant QFT applications are a substantial part.
- Exclude jobs/fellowships, generic computing events and routine seminars initially.
- Lattice courses are included following the scope decision on 8 October 2026;
  broader courses and restricted collaboration meetings still need a scope decision.
- Every collected addition or material update receives human review before publication.

Other scope defaults remain provisional; the GitHub moderation route and inclusion
of lattice courses are agreed. Broad quantum/computing conferences with unclear lattice
content remain review-only candidates.

Moderation policy agreed on 9 October: favour capturing plausible community events
over narrowly filtering ingestion. Uncertain relevance, missing information, deadline
wording and evidence-quality issues should be visible review warnings, not reasons
to discard an otherwise readable event proposal. Human GitHub review decides inclusion
and corrections. Clearly non-event posts such as jobs, awards and surveys remain outside
the calendar. Durable human rejection/hidden decisions still take precedence.

## Files and ownership

The v1 schema is in `_event_collector/schema/event.schema.json`. The examples in
`_event_collector/examples/` are contract demonstrations, not a publication dataset.
They are excluded from the Jekyll build. Example filenames and reviewer names do not
record actual approvals. Synthetic examples are labelled in the examples manifest.

Proposed operational storage: one JSON event record per stable ID under
`_event_collector/records/`, plus separate extraction proposals. JSON keeps schema
validation simple and avoids YAML's implicit date conversions. A later export step
will generate `_data/community_events.yml` for the public page.

Only an editor may change `decision`, `revision`, `editor_overrides` and
`history`. The collector/LLM proposes `facts`, evidence and warnings; it cannot
approve, restore, remove, merge or publish an event. The canonical record schema
is not the LLM response schema; Step 4's restricted response contract is in
`_event_collector/extraction/response.schema.json`.

## Event format

| Field | Meaning |
| --- | --- |
| `schema_version` | Format version, initially 1 |
| `id` | Stable identifier; retained when title, date or location changes |
| `revision` | Canonical record version; advances when the record is changed |
| `origin` | `collected` or `manual`; a manual entry can later gain collected sources |
| `facts` | Title, type, event dates, optional times/timezone, location, attendance, official URL, brief summary, deadlines and lifecycle |
| `sources` | Archive/organiser URLs or editor-supplied references; local IDs identify them within the record |
| `evidence` | A facts-field JSON pointer, source ID and a short supporting excerpt |
| `related_events` | Links to satellites, parent events or other genuinely related events |
| `identity_aliases` | Additional source identities/canonical URLs used to recognise reminders and duplicates |
| `warnings` | Reasons for editorial attention; not numerical model confidence |
| `editor_overrides` | Fact fields deliberately corrected by an editor; future extraction must preserve these values |
| `decision` | Current moderation state, reviewed version and optional reason |
| `history` | Append-only editorial action summaries; commits/PRs provide additional history |

Unknown fields are null or `unknown`, never guesses. Times and timezones are optional;
an all-day/date-only event has no invented midnight timestamp. Event end dates are
inclusive calendar dates. Dates use YYYY-MM-DD; timestamps use timezone-aware ISO 8601.
Deadline dates are separate from event dates; there may be multiple typed deadlines.
`deadline.timezone` and event timezone need not be the same. Timezone names, when known,
use IANA names. Country is a human-readable label initially, avoiding inferred country codes.
Links are HTTP(S); validity of a URL does not establish that it is an official organiser page.

Event types: `conference`, `workshop`, `school`, `software_training`, `course`,
`seminar`, `collaboration_meeting`, `other`, `unknown`. The schema can retain out-of-scope
types as rejected examples; allowing a type does not approve it for publication.

Lifecycle: `scheduled`, `cancelled`, `unknown`. Upcoming/ongoing/past are computed
from dates at display/export time; they are not moderation states. Cancellation requires
explicit source evidence or an editor decision and may remain visible to readers.

## GitHub moderation

| State/action | Result |
| --- | --- |
| `pending` | Collected proposal awaiting review; invisible on the website |
| `draft` | Incomplete manual entry; invisible |
| Approve | Set `approved` and record the exact reviewed revision; becomes eligible for export after merge |
| Reject | Set `rejected` with reason; keep identity/exclusion information; invisible |
| Remove | Set `hidden` on an existing event; preserve identity and history; next published export omits it |
| Restore | An explicit editor action returns an excluded event to pending/draft, or approved after review |
| Edit | Change facts, record overrides and approve the revised record before publication |
| Merge duplicate | Preserve one ID, move sources/aliases and relationships; retain an alias for the retired ID |

Publication requires `decision.status == approved`,
`decision.reviewed_revision == revision`, and semantic validation. Required public facts:
nonempty title, a recognised in-scope type, start date, known lifecycle, and at least one
source reference. End date and location may remain unknown; the page must display that
honestly. Manual records need an editor reference but not LLM evidence. An official URL
is desirable, not mandatory. Evidence and unresolved warnings remain visible to reviewers;
warnings need to be resolved or explicitly acknowledged when approving.

PRs contain event summaries, sources and field changes. New collected information goes
into a separate proposal, leaving the current approved version available until review.
Both validated candidates and readable candidates with extraction-quality warnings
should be eligible for a review proposal. Display uncertain fields as unverified and
link the source; do not present a failed quote check as verified evidence. Preserve
the candidate even when one relation or deadline is problematic. A validation-rejected
model response is not an editor rejection and must not create a permanent exclusion.
Malformed responses or impossible values remain operational errors to fix/triage;
they must not become publication-ready records. Publication still requires a valid
record and human approval. The current extractor retains quality failures privately;
the local broader proposal route is now implemented as described below; remote
GitHub integration is still a later milestone.
The collector reads the latest main-branch decisions and existing open proposals before
creating another PR. Never continuously append changes to an already reviewed proposal;
changed PR content needs fresh approval under the agreed GitHub rules.

Closing an unmerged PR means deferred, not permanently rejected. To reject permanently,
merge a decision-only record with `rejected` state and no public export. A removal PR
similarly retains a `hidden` record. Deleting a record file would lose exclusion memory
and is not the removal operation. Reminders and cross-source matches consult exclusions
before proposing inclusion. Ambiguous potential matches remain unmerged proposals.
An exclusion for one annual edition does not apply to the next edition.

Manual addition uses the same schema, initially `draft`, with editor-supplied facts.
After review it is approved through a PR. If collected later, matching adds provenance
or proposes changes to that same ID; it does not replace manual corrections.

Reviewers, required checks and branch rules will be configured in a later step.
No moderator names or permissions are invented here. JSON examples show how the
data works; a friendlier GitHub submission form can be added later if desired.

## Validation contract

JSON Schema checks structure, enumerations and syntactic formats. The later validator
must additionally check real calendar dates/timezones, start/end ordering, unique IDs,
source/evidence references, field-pointer validity, quoted evidence support, relationship
integrity and cycles for parent/satellite relationships, revision approval, editor precedence,
scope and exclusion matching. Schema validity alone is never publication approval.

Editor-authored titles/summaries and overrides use editor references; automatically extracted
non-null factual fields need evidence. Duplicate IDs/aliases across records require explicit
reconciliation. Network evidence may be unavailable later; retain a bounded evidence excerpt
and retrieval reference, not personal signatures or complete emails in public data.

Raw records, exclusions and evidence are excluded from site output, but a public GitHub
repository still makes committed files publicly readable. Raw email archives, contact details,
credentials and complete model responses must not be committed. Public provenance and
minimal moderation metadata are acceptable working defaults, pending editor agreement.

## Examples and acceptance

The manifest separates real announcement-derived examples from synthetic workflow examples.
The real Lattice 2027 message demonstrates three related events from one announcement.
Synthetic examples demonstrate a reminder matched to the same event ID, rejected unrelated
content, manual entry, an incomplete candidate, and removal of an approved event.

Step 1 is complete when this format expresses those cases without guessing or losing
editorial decisions. Subsequent steps implement retrieval, extraction and operational
validation. These examples do not prove extraction quality or confirm every detail with
organiser websites. Next discussion: the initial archive lookback and a small collection sample.

Local verification on 8 October 2026: all eight records parse and pass checks of the
schema keywords used in this draft, date ordering, source IDs, field pointers and reviewed
revision consistency. The three real examples reference one announcement and have valid
relationship IDs. Synthetic snapshots preserve manual facts and exclusion aliases.
At that stage, full JSON Schema library checking and operational reconciliation/export
were still future work. Full schema checking and local reconciliation are now implemented;
publication export remains pending, as described below.
The Jekyll exclusions were checked in configuration; a complete site build has not been
run because Jekyll is not installed in the current environment.

## Step 2: local announcement collector

From the repository root:

```sh
python3 -m tools.event_collector
python3 -m tools.event_collector --month-window previous
python3 -m tools.event_collector --offline
python3 -m unittest discover -s tests/event_collector -v
```

Requires Python 3.9 or later and no third-party packages. It collects messages, not
event records, and does not call an LLM or follow organiser/attachment links.
Routine collection reads one archive month: the current UTC calendar month by
default, or the previous completed month with `--month-window previous`. On
9 October 2026 these are October and September respectively. It follows only that
month's index pagination and message links, plus the site's robots.txt; it never
crawls the whole archive or website. The explicit `--months YYYY-MM ...` override
is retained for manual research and reproducible development samples. The older
April–October corpus was a one-time evaluation sample, not the routine collection
window. Preserve prior records and editorial decisions when they leave the window;
leaving the window does not remove an event. Monthly source selection and the event's
own date are separate: a newly announced event may take place much later.
The default `_event_collector/local/` directory is ignored by Git and excluded
from Jekyll. Keep any alternative output path outside publishable content.

The collector checks robots.txt (404/410 means no supplied directives), uses an
identifying user agent, keeps anonymous session cookies in memory, and restricts
redirects to public archive paths on the same host. It will not follow a login flow.
When a refused redirect sets or changes an anonymous cookie, it retries the identical
public URL once in that anonymous session. Unchanged cookie state causes an immediate
failure; another cookie-changing redirect cannot cause an unbounded retry loop.
Requests are paced, time-limited and size-limited, with at most three attempts for
transient network/server failures. Archive indexes and linked pagination pages have
explicit limits; unexpected markup/counts are failures, not empty results.

Announcement records retain subject, sender `posted_at` when available, archive
`archived_at`, body text, link targets, source identity and a normalised content hash.
The two timestamps may differ. A missing sender timestamp remains null; it is never
silently substituted with the archive timestamp. Navigation and message header contacts
are excluded from body extraction. Body signatures are still present in local raw data;
later extraction must avoid republishing them. Link labels without href targets are
flagged; URLs are not invented. Attachments are not downloaded or interpreted.

Cached HTML supports conditional requests when the server provides ETag/Last-Modified,
and explicit offline reprocessing. Identical records are retained once per message URL.
Changed records retain the same ID and preserve previous versions. Every run writes
a manifest and reports `success`, `partial` or `failed`; incomplete runs exit nonzero.
Failed fetches never remove existing announcements. Cached results are never used to
pretend an unsuccessful live refresh succeeded. Do not run concurrent collectors against
the same output directory in this prototype; workflow concurrency is later work.

Observed on 8 October 2026: anonymous Python HTTP collection retrieved October's index
and one KEK-PH-LAT announcement. Its body, organiser URL and deadline wording agree
with the browser. September's landing page and its observed chronological-index link
redirected direct HTTP requests to Indiana University login; the browser still displayed
the month and message pages without a sign-in interaction. We have not established why
these access paths differ, or whether this behaviour will persist outside this environment.

The live two-month run therefore reports partial success with one saved announcement.
An offline rerun made zero network requests, reported one unchanged announcement and
the uncached September index as a failure. A separate manually transferred browser DOM
fragment confirmed parsing of the HTML Lattice 2027 message and its three event mentions;
it is labelled browser-derived and is not counted as unattended retrieval.

Follow-up on 8 October 2026 resolved the access gap in the tested sample. In a controlled
fresh anonymous session, the first September request was refused after a cookie appeared;
the identical second request returned the six-message index. This identifies missing
anonymous-session retry handling in the prototype; the server's internal redirect rationale
has not been established. The retry follows no authentication URL and uses no credentials.

After adding that bounded retry, live collection succeeded for August (3 messages),
September (6) and October (1), with all declared messages discovered, no failures and
the seven already collected records unchanged. The synthetic suite now has 18 passing
tests, including session retry and immediate failure without a cookie-state change.
Step 2's bounded local retrieval proof is complete. This does not establish access from
a GitHub runner or long-term availability; those remain automation/pilot checks. The next
milestone is a larger, manually labelled extraction evaluation sample, not scheduling.

## Step 3: draft extraction evaluation set

All 47 messages collected from April–October 2026 have been read and assigned draft
annotations. There are 31 development messages and 16 held-out messages, separated by
event/update family. Six fictional edge cases supplement the real data and must be
scored separately. The real set has 23 messages with candidate events, 24 non-event
messages, 28 event descriptions and 19 distinct candidate event identities.

These are manual source-read labels drafted by Codex, awaiting editor adjudication,
not independently human-validated ground truth. These labels were drafted before
live extraction testing; the later development pilot is described below.
Lattice courses are included following the user's decision; broad quantum/computing
events with unclear lattice content remain review-only. Missing dates and locations stay
unknown. No source enrichment is used for this closed-message evaluation.

Committed labels and scoring definitions are in `_event_collector/evaluation/`.
Real frozen inputs remain in ignored local storage; committed evidence uses character
offsets instead of whole emails. An input preparation command can recreate matching
frozen inputs from collected records; changed source versions/hashes fail visibly.

```sh
python3 -m tools.event_collector.prepare_corpus_inputs
python3 -m tools.event_collector.check_corpus
```

The checker validates provenance hashes, evidence bounds/field references, coverage of
known core facts, date order, unknown-field assertions, relationships and split isolation.
All 53 real/synthetic draft cases pass these checks. The collection/corpus regression
suite has 23 passing tests. Neither result establishes extraction accuracy or editorial
correctness. The real held-out set has no separate course family, so course performance
will require additional independent cases before making a real-held-out accuracy claim.

URLs explicitly printed in body text are available to extraction even when Sympa has
removed href attributes and the collector's structured `links` list is empty. Missing
href targets must not cause an extractor to overlook a printed URL or invent an absent one.

## Step 4: extraction and evaluation infrastructure

The [pilot guide](../_event_collector/extraction/README.md) describes the structured
response schema, versioned prompt, evidence validation, private cache, request cap,
credential setup and scoring commands. Following the user's request for a free API,
the prepared adapter uses Gemini 3.1 Flash-Lite with a Free-tier AI Studio project.
Live development calls now authenticate and return structured responses. Initial
model output exposed unsupported fields and missing evidence, which validation
rejected. Full accuracy remains unmeasured. Provider quotas and account tier must
be checked in AI Studio.

All candidates remain pending. The model cannot emit editorial decisions or registry
identities. Source hashes and quoted evidence are checked, but quote existence does
not establish semantic correctness. The evaluation reports real/synthetic and
development/held-out cases separately, including coverage, missed/extra events,
date/deadline errors, unexpected non-null fields and outstanding review.

Text evidence tolerates whitespace-only differences from wrapped email lines. Its
verified quote and character offsets always refer to the unchanged original source;
the submitted quote is retained separately for whitespace matches. Other characters
must match, and link evidence still requires the literal full URL. This proves quote
presence, not that a quoted passage establishes every claimed fact.

The local suite has 144 passing regression tests, including credential-format/error
handling regressions and full Draft 2020-12
validation of all eight canonical examples. Gold-derived fixture replay runs across
all 53 cases and is explicitly labelled a software check, with the accuracy gate
not assessed. Step 4 acceptance still requires satisfactory development results,
editorial inspection and fixed-prompt held-out evaluation. The draft labels await editor review.

## Local proposals for GitHub human review

Prepare a review bundle from a finished live extraction run without another API call:

```sh
_event_collector/local/venv/bin/python -m tools.event_collector.proposals \
  _event_collector/local/extraction/runs/RUN_ID/predictions.json
```

The ignored, Jekyll-excluded `local/review/` bundle contains a readable `review.md`,
one editable canonical-format JSON record per candidate, separate review metadata
and a manifest. All records start pending with no reviewed revision or editorial
history. This command never creates a branch/commit/PR or writes the event registry.
The bundle is a local preparation step for future GitHub proposals, not publication.

Readable, schema-valid model responses rejected for quote/coverage/relevance issues
are retained as candidates. Verified excerpts contain only matched source text;
unmatched quotations are not represented as verified evidence. The original model
facts remain visible, with unverified-field warnings. Human reviewers must inspect
all facts because quote existence is not semantic proof. Invalid calendar values
remain repair items, without discarding other usable candidates from that message.
Transport failures remain visible without fabricated event records. Synthetic
evaluation cases cannot enter these review drafts.

The builder verifies frozen input, saved prompt/schema/request hashes and response
integrity. Files/directories are owner-only. Rerunning an unchanged saved run preserves
an existing bundle and any manual edits rather than regenerating its files; the
summary/metadata are initial-generation snapshots, not live views of edited records.
Do not run concurrent builders for the same bundle. A partial write is an operational
failure to inspect, not a completed review bundle.

IDs in this first bundle are provisional request/candidate identities. Run the local
reconciliation step below before preparing registry changes. Do not copy provisional
draft IDs into an authoritative registry. Review metadata
and full model responses remain private; future public PRs should use minimal event
facts, source references and a readable change summary.

The current three-announcement batch produces five pending event records, including
all three Lattice candidates previously held back by relationship quote checks. All
144 regression tests pass, covering explicit single-month selection, quality-warning routing, source/provenance integrity,
synthetic exclusion, operational failures, invalid-date repairs, repeat preservation
and owner-only permissions. No new API calls or publication were made by this step.

INSPIRE is now connected as the first additional discovery adapter. Its bounded
upcoming Theory-HEP run fetched 65 records across three pages; they reconcile into
64 event proposals because two catalogue entries describe the same HEPMAD26 event.
Metadata maps into the same pending records, with broad relevance review and shared
identity/exclusion handling. See [the complete operating guide](event-collector-operations.md)
for production ingestion, editorial decisions, GitHub review, export and recovery.

## Reconcile identities and updates locally

```sh
_event_collector/local/venv/bin/python -m tools.event_collector.reconcile \
  _event_collector/local/review/PROPOSAL_ID
```

This creates a private `local/reconciliation/plan-…/` containing one suggested JSON
record per resolved event, a readable change summary and `plan.json` routing details.
It reads canonical editorial records from `_event_collector/records/` without changing
them. `--records DIRECTORY` selects another read-only registry. The current real pilot
has no canonical records: five candidates receive five pending event IDs, with the
conference and its satellites kept distinct. No existing website `_events/` Markdown
records are imported or matched by this command.

Strong matches use shared source-event identities, scoped message/title aliases,
compatible edition/type and organiser URLs, or an exact title and start date. A reused
URL with unknown edition or unverified provenance needs identity review. Different
known annual editions and explicitly related satellites stay separate. Similar titles,
multiple matches and other uncertain identities remain visible for a reviewer. Supply
`--matches choices.json` to map candidate IDs to an existing event ID or `"new"` after
inspection; a new identity cannot bypass a strongly matched rejected/hidden record.

Matched rejected/hidden records stay excluded. Manual records keep their facts,
including deliberately unknown values; field overrides on collected records are
preserved. Protected conflicts show the incoming suggestion alongside the kept value.
Unknown incoming values never erase known facts. Updates retain source provenance,
remap evidence and satellite IDs, advance the revision and return to pending review.
Deadline lists are combined by kind/label, with a review warning; a protected deadline
entry protects the entire list so array positions remain stable. The command does not
infer cancellation from disappearance or manufacture editorial history.

The private `identities.json` ledger keeps only pending identity allocations between
local batches. Canonical records always take precedence. Preserve this ledger until
identities are recorded in the shared registry; ephemeral runner storage is insufficient
for production. It is not an editorial decision store. Plans are snapshots: identical
candidate content, canonical registry and reviewer choices reuse the saved plan and
preserve edits, even if other pending ledger entries have subsequently changed. Edited
JSON does not refresh a summary or become authoritative. A partial plan needs inspection.

Plan actions distinguish new records, updates, protected conflicts, unchanged records,
identity-review items and exclusions. Unchanged approved copies are reference material,
not new approval or publication. Every plan has `eligible_for_export: false`. The
implemented review-package builder selects applicable actions and rechecks canonical
state. Publication additionally requires a sealed editorial approval and acknowledged
warnings. These tests establish software behaviour, not model accuracy or completed
remote deployment. Reconciliation makes no API calls or GitHub writes.

The initial multi-event message exposed missing/invalid evidence and unsupported
times/locations in model proposals. Extraction schema v3 and prompt v4 now require
attached evidence for classifications and relationships. Rejected proposals are
stored privately with complete candidate/field diagnostics. `--keep-going` allows
the rest of an ingestion batch to proceed while failed cases stay available for
review; failures remain visible and cannot pass the quality gate. A stronger free
Gemini model is selectable explicitly, but returned service errors in the initial
comparison. The first pilot therefore does not establish model reliability.

The three-case pilot now completes with failures retained: the non-event validates,
the synthetic injection case validates after correcting the null-evidence rule,
and the conference/satellite proposal still requires review. The synthetic case has
one warning to adjudicate. An offline revalidation command preserves unchanged
model output, verifies its provenance and reruns validation without API use.
Raw Zoom connection links are omitted from the evaluation input.
