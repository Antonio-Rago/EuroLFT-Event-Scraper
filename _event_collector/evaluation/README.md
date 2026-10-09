# Extraction evaluation corpus v1

This is a source-read, manually drafted annotation set by Codex, awaiting editor review.
The original annotations were drafted before extraction API calls. Live development
testing has now begun; see the Step 4 pilot guide for results and limitations. These
draft labels must not be described as independently human-validated ground truth.
One development-only literal title alias was added after model output exposed an
incomplete alias list; [the correction is documented](development-corrections.md).
Held-out annotations and frozen inputs remain unchanged.

## What is labelled

Each case references one archive message, a frozen local input hash, a split and an
event-family group. `expected.events` contains candidate event descriptions justified
by that message, including incomplete updates and review-only candidates. An empty
list means the message is not an event announcement/update for this calendar.

An event can be present but require relevance review. `include` means recommended
for editorial consideration under the current scope, never automatic publication.
`review` means relevance or scope is ambiguous. Lattice courses are included following
the user's scope decision; broad quantum/computing events remain review-only. Requests for host proposals,
prize nominations, survey replies, jobs, literature updates and plenary-slide input
are negative cases even when they mention real conferences and dates.

Participant information and reminders can update a parent conference; plenary talks,
mentoring schemes and broadcasts are not automatically separate calendar events.
One announcement can contain multiple candidate events. Dates absent from that message
remain null, even if another case or an external website supplies them. Organiser-page
enrichment is deliberately disabled for this closed-message evaluation.

`event_key` is an editorial gold-set identity for duplicate/update tests, not a value
the future extractor must guess. Titles may match an explicitly listed alias. Duplicate
messages stay in the same split. Expected event identities never cross the split boundary.
The held-out set is for evaluating a fixed prompt, not feeding labels or answers into it.
The developer has read these messages to draft labels, so this is a prompt-development
holdout rather than a claim of an unseen independent external benchmark.

## Evidence and privacy

Raw messages and frozen inputs remain under the ignored `_event_collector/local/`.
Committed labels contain event facts, archive references and evidence offsets rather than
full emails, contact details or access-bearing webinar links. Inputs are lightly sanitised
to remove email addresses, phone lines and Zoom connection URLs. Signatures/affiliations
can still appear as realistic distractors; they must not become an event location.

Every evidence span identifies `subject` or `body`, a start/end character offset, the
supported facts-field paths and a derivation note when normalisation is involved.
Offsets apply to the frozen sanitised input, not HTML bytes or a later refreshed message.
The validator requires the frozen-input hash to match and the referenced spans to exist.
This checks annotation consistency, not whether the editorial interpretation is correct.

Dates and clock formats can be normalised. A missing deadline year may be resolved
from an explicitly identified event year and message context, with that derivation recorded.
Ambiguous event dates, IANA timezone mappings and partial online access remain uncertain.
For example, broadcasting plenaries does not establish full hybrid conference participation.

## Scoring definitions for Step 4

Report development and held-out results separately, and real/synthetic cases separately.
Keep the held-out set fixed before prompt development. Measure:

1. Candidate detection precision/recall: true candidates found versus missed or invented
   events. Score before relevance filtering so review-only candidates do not become false negatives.
2. Relevance routing: include/review/no-event decisions, with per-class counts. Draft
   ambiguous labels are scored for review routing, not an assumed publication decision.
3. Multi-event recovery: all candidates in multi-event messages and no extra plenary or
   previous-edition events. Use event-level matching, not an announcement-level pass alone.
4. Field accuracy: titles (accepted aliases), event dates, locations, attendance and official
   URLs; score missing expected facts separately from incorrect facts. Nulls matter.
5. Deadline accuracy: match by deadline kind and label, then date/time/timezone. Compare
   deadline dates separately from event dates. Do not score newsletter/job/award deadlines
   as event dates or registration deadlines.
6. Unsupported non-null fields: values with no source support, including guessed countries,
   invented links and copied organiser signature addresses. Count errors per field and case.
7. Update association: use expected event keys to assess whether reminders share identity
   and deadline extensions supersede the earlier value. Identity reconciliation is a later stage.
8. Required-review recall: explicit ambiguity/incompleteness flags should reach review.

Match candidate events by normalised accepted title aliases and, where applicable, official
URL identity. Ambiguous assignment requires adjudication; do not silently pick the match
giving the model its best score. Supporting evidence is checked against the exact model input.
Self-reported confidence is not a substitute for these measurements.

The proposed initial extraction gate is zero unsupported facts and zero wrong event dates
in the held-out sample, with ambiguous cases routed to review. Results must include denominators
and errors; a small successful sample does not establish an error-free production system.

## Verify the draft

```sh
python3 -m tools.event_collector.prepare_corpus_inputs
python3 -m tools.event_collector.check_corpus
```

The check needs the private frozen inputs created for this local sample. A fresh clone
must rehydrate inputs from source and resolve any content-hash changes before using these
labels. The preparation command reads locally collected source versions and refuses changed
content instead of silently updating labelled inputs. No credentials, API calls or external
writes are needed for preparation or the local check.

`facts.summary` is not scored as an exact string; it is reserved for later factual summary
checks. `must_remain_unknown` identifies fields for which a guessed value is an error.
Known deadline timezone abbreviations are noted for review; they are not silently converted
to an IANA timezone. Meaning-equivalent review warnings require editorial adjudication rather
than literal warning-string matching.
