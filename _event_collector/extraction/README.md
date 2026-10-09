# Step 4: structured extraction pilot

> Collector code now lives in this separate repository. Earlier development commands
> describe the former combined checkout. Use docs/event-collector-operations.md
> for current commands and --site pointing to the website event registry.

The local extractor, evidence validator, cache, request ledger and evaluation tools
are implemented. A key has been configured and live development calls have begun.
Initial responses exposed invalid/missing evidence and invented details; the validator
held them back. Step 4 acceptance remains open until development results and a
fixed-prompt held-out evaluation are reviewed. No events have been published.

## Free provider

The user requested a free API after declining the proposed paid OpenAI pilot.
The adapter supports Google's `gemini-3.1-flash-lite` (default) and `gemini-3.8-flash`
via the Gemini Developer API. Google documents free input/output on the Free tier
and structured outputs for these models. The lighter model has shown compliance
and unsupported-fact failures in the development pilot; it is not yet accepted for
production. A two-attempt comparison with 3.8 Flash returned HTTP 503 and supplied
no extraction result, so its quality remains untested here.
Sources checked on 8 October 2026:

- [Pricing](https://ai.google.dev/gemini-api/docs/pricing#gemini-3.1-flash-lite)
- [Model and structured extraction example](https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-lite)
- [Gemini 3.8 Flash model](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash)
- [REST request/response reference](https://ai.google.dev/api/generate-content)
- [Project quotas](https://ai.google.dev/gemini-api/docs/rate-limits)

Use a Google AI Studio project whose tier is **Free**, with no active billing account.
An API key does not tell this tool the project's billing tier. The adapter cannot
force a paid project to consume free quota; zero cost depends on the account setup.
There is no automatic provider/model switch or paid fallback. Google's account quota
is authoritative; the local cap is an additional limit, not a promise of capacity.

[Google AI Studio](https://aistudio.google.com/api-keys) is where the project/key is
created. A Google account and an API key are still needed even though the tier is free.
For comparison, [Groq also documents a Free plan](https://console.groq.com/docs/rate-limits)
and [strict structured outputs](https://console.groq.com/docs/structured-outputs);
that adapter has not been implemented or tested here.

The [Gemini terms](https://ai.google.dev/gemini-api/terms) include regional data-handling
rules and restrict making API clients available to users in the EEA, UK and Switzerland
to paid services. This is a local development pilot; do not assume its free-tier
arrangement covers a future publicly available API client. The planned website serves
reviewed static event data rather than calling the model in the visitor's browser.

## Prepare the local environment

Run these commands from the website repository. The existing environment is already
prepared in this workspace. Python 3.9 or later is required; the HTTP collector itself
still has no third-party dependency. Full JSON Schema validation uses `jsonschema`.

```sh
python3 -m venv _event_collector/local/venv
_event_collector/local/venv/bin/python -m pip install -r tools/event_collector/requirements.txt
```

Inputs, credentials, caches, request ledgers and reports live under the Git-ignored,
Jekyll-excluded `_event_collector/local/`. They are not part of the public website.
New local data directories/files use owner-only permissions. This implementation
uses Unix file locking and is intended for macOS/Linux, including later Linux CI.

## Enter the key securely

After creating a key for the Free-tier project, run this in an interactive terminal:

```sh
_event_collector/local/venv/bin/python -m tools.event_collector.credentials
```

The prompt hides the key. It saves an owner-only file under
`_event_collector/local/credentials/`, excluded from Git and Jekyll. Do not paste the
key into chat or put it in a command argument, source file or pull request.
An existing `GEMINI_API_KEY` environment variable takes precedence over this file.
The local key has been used for the development pilot; its value is never printed
or included in model inputs, evaluation labels or reports.

The helper accepts keys containing dots and longer authorization-key values; it
does not assume a fixed key prefix or alphabet. Paste only the key value, without
quotes. Empty/invalid entries, unavailable hidden input and file-writing failures
now have distinct diagnostics that never display the secret. These are local
input/storage checks; only a real API request can confirm that Google accepts a key.

## Small development pilot

Inspect the selected cases first. Without `--live`, this command makes zero API calls:

```sh
_event_collector/local/venv/bin/python -m tools.event_collector.extract \
  --case real-07 --case real-43 --case synthetic-05 --limit 3
```

This development selection covers a non-event, a conference with two satellites,
and a source containing instructions the model must ignore. Add `--live` to make
the selected requests after the Free-tier key is configured.

Use `--model gemini-3.8-flash` for an explicit comparison when that model is available.
There is no automatic model fallback. Model identifiers participate in cache identities,
request ledgers and result provenance, so results cannot silently mix models.

The default selection is the first three development cases. `--case` may be repeated;
explicit selections must belong to `--split` and fit `--limit`. Held-out messages
require `--split held_out`. Source hash changes fail before sending. The payload
contains only source URL, subject, sanitised body, literal links and posting date.
Labels, gold event identities, expected facts and split metadata never enter it.

The pilot defaults to at most 20 attempted requests per rolling 24 hours and one new
request per 60 seconds. This conservative cap is a local choice, not Google's quota.
The CLI waits for pacing between calls. By default, HTTP 429 or another failure stops
the run. `--keep-going` retains that failure and processes remaining cases; the final
status is `completed_with_failures` with a nonzero exit code. Rejected proposals are
available for editorial inspection and never enter the validated cache or publication
data. Already-failed requests are not resent unless `--retry-failed` is explicit.
There are no automatic retries. Failed attempts still count. After investigating,
use `--retry-failed` explicitly to retry a failed request, subject to the same cap.
An interrupted process with a reserved attempt needs inspection before retrying.
Raise `--daily-request-limit` only after checking the project's actual quota.

The current response cap is 8192 output tokens with LOW reasoning. Earlier pilots
used MINIMAL reasoning and a 4096-token cap; prompt/schema versions preserve those
settings, with request hashes distinguishing later reasoning-setting changes.
Multiple events with many evidence quotes
can reach it; truncation is a failure, never a silently accepted partial extraction.
The HTTP timeout is 60 seconds and response-size cap is 2 MiB. The API destination
is fixed HTTPS, the credential is in a header, and redirects are refused. No tools,
organiser-page fetches, attachment downloads or model-triggered actions are enabled.

## Validation, caching and editorial state

`response.schema.json` is the model's restricted JSON contract (extraction version 3;
the canonical event registry remains version 1). It has candidate-local
IDs and no approval, revision, history or global event IDs. It is generated from the
canonical facts contract, with unsupported schema features removed. The local
validator then applies the full canonical facts schema and calendar/time checks.

`prompt-v5.txt` is current and handles scope, courses, broad-event review, multi-event announcements,
deadlines, missing dates, signatures, partial broadcasts and untrusted source text.
Pilot summaries remain null. All returned candidates remain editorially pending.
Version 5 adds explicit event-specific location/attendance rules, forbids inheriting
parent locations into satellites, requires contiguous evidence passages, and gives
a deadline-pointer checklist covering kind/label as well as dates. Shared deadlines
for different purposes may produce separate typed entries. The model, reasoning,
token cap and response schema remain unchanged, so the pilot tests the prompt change.

Satellite relationships and event classifications have required attached evidence
objects. Flat evidence-pointer lists alone repeatedly omitted those claims during
the live development pilot. The change binds each quote directly to its claim; it
does not supply missing facts or relax validation. Earlier prompts v1–v4 and response
schemas v1–v2 remain saved. `--prompt-version` permits comparisons, and the evaluator
selects the saved contract by the run's prompt hash. Old results are still evaluable.

Every known fact and routing/relationship claim needs a source quotation tied to a
valid field pointer. Text quotations may differ only in whitespace: spaces, tabs,
line breaks and nonbreaking spaces may form equivalent runs. Every other character
must match, including words, punctuation, case and dates. Removing a word or adding
an ellipsis is not accepted. Link evidence still requires the exact full URL.

The validator first tries an exact match, preserving existing cache metadata. For
a whitespace match, the verified span's `quote` is the unchanged source slice and
`start`/`end` are Python character offsets in the original source. The submitted
quotation is retained as `model_quote` with `match_method: whitespace`. The immutable
model response and frozen input are not edited. Leading/trailing quote whitespace
is insignificant when exact matching fails; whitespace between words cannot disappear.
The validator calculates quote offsets
and rejects missing/invented quotations, absent URLs, impossible dates/times, bad
relationship targets and model-authored editorial fields. It checks quote existence
and coverage, **not whether the quotation logically establishes the claimed fact**.
Wrong interpretations still need evaluation and human review.

Parsed responses rejected by local validation are retained privately under
`local/extraction/rejected/`, with validation diagnostics and provenance hashes.
They never enter the validated cache or publication data. Errors identify the full
candidate/field path or list missing evidence fields without printing API errors.

Evidence may refer to a null fact when the source explicitly establishes its absence,
such as “venue not yet announced”. Known fields still require supporting quotes;
unrecognised field paths remain invalid. This corrected an overstrict validator rule
found by the live synthetic test.

After a validator bug fix, recheck an unchanged rejected response without another call:

```sh
_event_collector/local/venv/bin/python -m tools.event_collector.revalidate \
  PATH_TO_REJECTED_PROPOSAL.json --case synthetic-05
```

The command verifies response integrity, frozen input, model, saved contract and
request parameters, then runs full validation again. It does not alter model facts.
A successful result enters only the private pending cache; failed results remain
excluded. The original rejected response and request history are preserved.

The cache identity includes the frozen input, provider/model, request parameters,
full schema and prompt. Repeated unchanged requests reuse a locally revalidated
cache entry. Integrity mismatches stop rather than silently call the model again.
The requested stable Gemini model ID is not a date-pinned snapshot. Each live result
records the returned `modelVersion` when supplied; the evaluator flags missing/mixed
versions for review. Stored results are reproducible; future calls can change.

Runs record successful/failed case IDs, usage including thinking tokens, source and
contract hashes, pending state, cached status and diagnostics without raw error
bodies or credential values. Validation failures stop the pilot and are retained
privately with their stage and, where available, a schema field/rule or HTTP code.
Nothing here writes to the event registry, opens a PR, approves or publishes an event.

## Evaluate real model results

Each live run prints the path of its private `predictions.json`. Evaluate that file:

```sh
_event_collector/local/venv/bin/python -m tools.event_collector.evaluate \
  _event_collector/local/extraction/runs/RUN_ID/predictions.json
```

The adjacent `evaluation.json` separates real/synthetic and development/held-out
cases, states coverage/denominators, and lists misses, extra events, field errors,
event-date errors, deadline errors, unexpected non-null values and review flags
needing adjudication. Matching uses accepted title aliases or an explicit official
URL; ambiguous assignments are not silently optimised. Equivalent wording in deadline
labels and review warnings may need editor adjudication. Update sequences test
recognition across messages, not the future registry merge/supersession implementation.

The labels remain Codex-drafted annotations awaiting independent editor review.
Unexpected non-null values are discrepancies against those labels; determining
whether a fact is unsupported still requires reading its source. The evaluator's
`draft_labels_pass` gate is available only for a completed live run covering all real
and synthetic held-out cases, with no detection/field errors or outstanding review.
It does not approve publication.
Partial runs, failures and unresolved model-version changes cannot pass the gate.

Process development cases in small batches and inspect failures before changing the
prompt. Freeze the prompt/schema before evaluating the 16 real held-out messages.
A complete held-out run, including its three synthetic cases, uses
`--split held_out --limit 19 --live`. If a quota stops it partway through, rerun the
same selection after the quota is available; unchanged successful cases use the
cache, so the completed run includes those results without another API request.
A full 53-case run may span several days with the default local cap. Do not tune on
held-out errors and then describe the same set as a fresh independent test.

## Offline verification

```sh
_event_collector/local/venv/bin/python -m unittest discover -s tests/event_collector -v
_event_collector/local/venv/bin/python -m tools.event_collector.check_corpus
_event_collector/local/venv/bin/python -m tools.event_collector.replay_fixtures
```

All 75 regression tests pass locally, including one-month default selection,
January rollover, whitespace matching, exact original offsets, content-change
rejection, URL strictness, cache reuse and immutable offline revalidation.
The full Draft 2020-12 schema validates all
eight canonical examples. The corpus check passes for 53 cases. Fixture replay runs
the extraction/evaluation plumbing over 34 development and 19 held-out cases using
gold-derived responses; reports say `fixture_replay` and gate `not_assessed`.
This is a software test, **not measured Gemini accuracy**. Portable unit tests use
committed synthetic inputs/labels; full replay also needs the private real inputs
prepared during Step 3.

Remaining acceptance work: inspect the initial live development results,
address observed failures, then evaluate the fixed prompt on the held-out set and
review the draft annotations. Local identity reconciliation is now implemented;
website publication and automated GitHub PR creation remain later milestones.

## Latest three-case pilot outcome

The original selection was processed with `--keep-going`, using prompt v4, extraction
schema v3 and LOW reasoning. `real-07` is a validated non-event; `synthetic-05` is a
validated fictional candidate whose facts match the draft labels, with one required
review warning still needing adjudication. The latter was revalidated offline after
the null-evidence bug fix. `real-43` remains rejected: retries generated missing/
invented evidence, unsupported times or copied satellite locations. Its latest
proposal remains available privately for review. Two selected cases validate, one
requires review, and the run returns `completed_with_failures`; the pilot quality
gate is not assessed. No held-out model requests or publication have occurred.

On 9 October, a further Flash request returned HTTP 503, while Flash-Lite connected
on both the three-event source and October's KEK announcement. The former still
invented/altered evidence and inferred satellite locations. The KEK response supplied
correct event dates and its literal organiser URL, but joined wrapped source lines
in evidence quotes; the exact-substring validator rejected it. These are retained
pending proposals, not successful validated extractions. API access works; extraction
quality and evidence-format handling remain the next acceptance work.

Offline recheck after the whitespace matcher change: all 11 KEK evidence quotations
now match (eight exact, three whitespace-only). The response still lacks evidence
pointers for the kind/label of each of its three deadlines, so it remains rejected.
The Lattice response still contains two invented/shortened relationship quotations
and fails. No API requests, model-fact corrections, prompt/schema/label changes or
publication were made during this recheck. Safe revalidation diagnostics now identify
missing evidence fields. See the [review](../../../research/evidence-matching-review.md).

Prompt-v5 pilot: real-41 (course) and real-47 (KEK) pass structural/evidence validation.
KEK deadline proof is complete. The real-43 satellites now have null locations, but
two relationship quotations still fail. The run reports completed_with_failures.
Course-title matching, recurrence warnings, combined KEK deadline purposes and
equivalent labels need review. The unchanged scorer/gate do not establish accuracy
acceptance. All 75 tests pass; v1–v4 reports remain evaluable. See the
[current output](../../../research/selected-event-output.md) and
[pilot review](../../../research/development-pilot-v5-review.md).

## Prepare local human-review proposals

```sh
_event_collector/local/venv/bin/python -m tools.event_collector.proposals \
  _event_collector/local/extraction/runs/RUN_ID/predictions.json
```

The builder retains schema-valid, readable candidates with evidence-quality warnings,
including saved validation failures. It does not retry Gemini or alter original
responses. Source/contract/request integrity is checked; synthetic cases are excluded.
Each draft has an editable pending record, review metadata and source links. Unmatched
quotes are not verified excerpts. Every fact still needs human review. Invalid facts
become repair items while other candidates survive. Repeat runs preserve the initial
bundle and manual edits; summaries do not automatically reflect later edits.

Five real candidates from the v5 batch now have local drafts under private
`local/review/`; the Lattice candidates no longer disappear because of quote issues.
IDs in this first bundle are provisional. Reconcile them locally before preparing
registry changes:

```sh
_event_collector/local/venv/bin/python -m tools.event_collector.reconcile \
  _event_collector/local/review/PROPOSAL_ID
```

This reads canonical records without modifying them, assigns persistent pending IDs,
matches reminders, preserves manual fields/overrides and retains rejected/hidden
decisions. Ambiguous identities need reviewer choices. Plans and the pending identity
ledger remain private; unchanged plans preserve edits. See the community-events contract
for matching rules and snapshot limitations. The five real candidates now have a local
reconciled plan; no authoritative records exist yet. These preparation commands perform
no remote PR creation or approval. Publication and manual GitHub workflows are implemented
separately, with sealed human decisions required for export. All 144 software tests pass.
See the [operating guide](../../docs/event-collector-operations.md) for the complete flow.
