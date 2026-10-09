"""Check draft evaluation annotations against their frozen local inputs.

This verifies provenance, bounds, split isolation and structural consistency;
it does not certify editorial correctness or measure extraction quality.
"""

import argparse
import hashlib
import json
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo


def input_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def pointer_value(value, pointer):
    if not pointer.startswith("/"):
        raise ValueError("Evidence support must be a JSON pointer")
    for part in pointer[1:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


def check_span(span, input_record, target):
    field = span["source_field"]
    if field not in ("subject", "body"):
        raise ValueError("Evidence is not tied to subject/body")
    start, end = span["start"], span["end"]
    if not isinstance(start, int) or not isinstance(end, int) or not 0 <= start < end <= len(input_record[field]):
        raise ValueError("Evidence offset is outside the frozen input")
    if not span["supports"]:
        raise ValueError("Evidence has no supported fields")
    for pointer in span["supports"]:
        pointer_value(target, pointer)


def check_facts(facts):
    expected = {"title", "type", "start_date", "end_date", "start_time", "end_time", "timezone",
                "location", "attendance", "official_url", "summary", "deadlines", "lifecycle"}
    if set(facts) != expected or not isinstance(facts["title"], str) or not facts["title"].strip():
        raise ValueError("Facts do not match the v1 event contract")
    if facts["type"] not in ("conference", "workshop", "school", "software_training", "course", "seminar",
                              "collaboration_meeting", "other", "unknown"):
        raise ValueError("Unknown event type")
    if facts["attendance"] not in ("unknown", "in_person", "online", "hybrid"):
        raise ValueError("Unknown attendance mode")
    if facts["lifecycle"] not in ("scheduled", "cancelled", "unknown"):
        raise ValueError("Unknown lifecycle")
    if set(facts["location"]) != {"venue", "city", "country"}:
        raise ValueError("Unexpected location fields")
    for field in ("start_date", "end_date"):
        if facts[field] is not None:
            if date.fromisoformat(facts[field]).isoformat() != facts[field]:
                raise ValueError("Date is not canonical YYYY-MM-DD")
    if facts["start_date"] and facts["end_date"] and facts["end_date"] < facts["start_date"]:
        raise ValueError("End date precedes start date")
    for field in ("start_time", "end_time"):
        if facts[field] is not None:
            datetime.strptime(facts[field], "%H:%M")
    if facts["timezone"]:
        ZoneInfo(facts["timezone"])
    if facts["official_url"] and urlsplit(facts["official_url"]).scheme not in ("http", "https"):
        raise ValueError("Official URL is not HTTP(S)")
    for deadline in facts["deadlines"]:
        if set(deadline) != {"kind", "label", "date", "time", "timezone"}:
            raise ValueError("Deadline fields do not match the contract")
        if deadline["kind"] not in ("registration", "abstract", "payment", "funding", "visa", "other"):
            raise ValueError("Unknown deadline type")
        if deadline["date"] is not None:
            if date.fromisoformat(deadline["date"]).isoformat() != deadline["date"]:
                raise ValueError("Deadline date is not canonical YYYY-MM-DD")
        if deadline["time"]:
            datetime.strptime(deadline["time"], "%H:%M")
        if deadline["timezone"]:
            ZoneInfo(deadline["timezone"])


def check_corpus(labels, input_loader):
    ids, group_splits, event_splits = set(), {}, {}
    by_id = {}
    for case in labels["cases"]:
        case_id = case["case_id"]
        if case_id in ids:
            raise ValueError("Duplicate case ID: " + case_id)
        ids.add(case_id)
        by_id[case_id] = case
        if case["split"] not in ("development", "held_out"):
            raise ValueError("Invalid evaluation split")
        if case["basis"] not in ("real", "synthetic"):
            raise ValueError("Missing real/synthetic distinction")
        if case["annotation_status"] != "draft_for_editor_review":
            raise ValueError("Only draft annotations are supported in corpus v1")
        group_splits.setdefault(case["group"], set()).add(case["split"])
        input_record = input_loader(case)
        if input_hash(input_record) != case["input_sha256"]:
            raise ValueError("Frozen input hash changed: " + case_id)
        if input_record["case_id"] != case_id or input_record["source_url"] != case["source_url"]:
            raise ValueError("Input identity/provenance mismatch")
        expected = case["expected"]
        if expected["message_disposition"] != ("event_candidates" if expected["events"] else "no_event"):
            raise ValueError("Disposition contradicts expected candidate list")
        if not expected["events"] and not expected["reason"]:
            raise ValueError("Negative case has no annotation rationale")
        for span in expected["evidence"]:
            check_span(span, input_record, expected)
        keys = [event["event_key"] for event in expected["events"]]
        if len(keys) != len(set(keys)):
            raise ValueError("One case duplicates an event identity")
        for event in expected["events"]:
            event_splits.setdefault(event["event_key"], set()).add(case["split"])
            if event["relevance"] not in ("include", "review"):
                raise ValueError("Candidate has no relevance decision")
            if event["message_role"] not in ("announcement", "update", "reminder"):
                raise ValueError("Invalid message role")
            if event["relevance"] == "review" and "scope_review" not in event["required_review_flags"]:
                raise ValueError("Review-only candidate lacks scope flag")
            if event["facts"]["title"] not in event["accepted_title_aliases"]:
                raise ValueError("Canonical title missing from aliases")
            check_facts(event["facts"])
            for span in event["evidence"]:
                check_span(span, input_record, event)
            supported = {pointer for span in event["evidence"] for pointer in span["supports"]}
            required_support = {"/facts/title", "/facts/type", "/facts/lifecycle"}
            for field in ("start_date", "end_date", "start_time", "end_time", "timezone", "official_url"):
                if event["facts"][field] is not None:
                    required_support.add("/facts/" + field)
            for field, value in event["facts"]["location"].items():
                if value is not None:
                    required_support.add("/facts/location/" + field)
            if event["facts"]["attendance"] != "unknown":
                required_support.add("/facts/attendance")
            for index, deadline in enumerate(event["facts"]["deadlines"]):
                for field in ("date", "time", "timezone"):
                    if deadline[field] is not None:
                        required_support.add("/facts/deadlines/{}/{}".format(index, field))
            if not required_support <= supported:
                raise ValueError("Known event fields lack evidence references: " + event["event_key"])
            for pointer in event["must_remain_unknown"]:
                if pointer_value(event, pointer) not in (None, "unknown"):
                    raise ValueError("A required unknown field has been populated")
            for related in event["related_events"]:
                if related["event_key"] not in keys or related["event_key"] == event["event_key"]:
                    raise ValueError("Related event is absent or self-referential")
    if any(len(splits) != 1 for splits in group_splits.values()):
        raise ValueError("Same announcement group crosses the split boundary")
    if any(len(splits) != 1 for splits in event_splits.values()):
        raise ValueError("Same expected event crosses the split boundary")
    for sequence in labels["update_sequences"]:
        sequence_splits = set()
        for case_id in sequence["cases"]:
            case = by_id[case_id]
            sequence_splits.add(case["split"])
            if sequence["event_key"] not in {e["event_key"] for e in case["expected"]["events"]}:
                raise ValueError("Update sequence references a missing expected event")
        if len(sequence_splits) != 1:
            raise ValueError("Update sequence crosses the split boundary")
    return {
        "cases": len(ids),
        "real_splits": dict(Counter(c["split"] for c in labels["cases"] if c["basis"] == "real")),
        "synthetic_cases": sum(c["basis"] == "synthetic" for c in labels["cases"]),
        "real_dispositions": dict(Counter(c["expected"]["message_disposition"] for c in labels["cases"] if c["basis"] == "real")),
        "real_event_occurrences": sum(len(c["expected"]["events"]) for c in labels["cases"] if c["basis"] == "real"),
        "real_unique_events": len({e["event_key"] for c in labels["cases"] if c["basis"] == "real" for e in c["expected"]["events"]}),
        "update_sequences": len(labels["update_sequences"]),
        "annotation_status": "draft_for_editor_review",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("_event_collector"))
    args = parser.parse_args(argv)
    try:
        labels = json.loads((args.root / "evaluation/labels.json").read_text(encoding="utf-8"))
        def load(case):
            directory = args.root / ("local/evaluation_inputs" if case["basis"] == "real" else "evaluation/synthetic_inputs")
            return json.loads((directory / (case["case_id"] + ".json")).read_text(encoding="utf-8"))
        report = check_corpus(labels, load)
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        parser.exit(1, "Corpus check failed: " + str(exc) + "\n")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
