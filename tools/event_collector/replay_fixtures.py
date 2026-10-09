"""Replay gold-derived fixtures to check software only, never model accuracy."""

import json

from .check_corpus import input_hash
from .evaluate import evaluate_run
from .extract import load_input, save_json
from .extraction import ROOT, load_contract, required_support, validate_result


def fixture_result(case, record, schema_version=3):
    events = []
    local_ids = {gold["event_key"]: "e" + str(i + 1) for i, gold in enumerate(case["expected"]["events"])}
    for gold in case["expected"]["events"]:
        facts = json.loads(json.dumps(gold["facts"]))
        flags = list(gold["required_review_flags"])
        if facts["start_date"] is None or facts["end_date"] is None:
            flags.append("missing_dates")
        if facts["lifecycle"] == "cancelled":
            flags.append("cancellation_review")
        event = {
            "local_id": local_ids[gold["event_key"]], "facts": facts,
            "message_role": gold["message_role"], "relevance": gold["relevance"],
            "review_flags": sorted(set(flags)),
            "related_events": [{"local_id": local_ids[r["event_key"]], "relation": r["relation"]}
                               for r in gold["related_events"]], "evidence": [],
        }
        if schema_version >= 2:
            for relation in event["related_events"]:
                relation["evidence"] = {"source_field": "body", "link_index": None,
                    "quote": record["body"], "derivation": "Gold-derived software fixture only."}
        if schema_version >= 3:
            proof = {"source_field": "body", "link_index": None, "quote": record["body"],
                     "derivation": "Gold-derived software fixture only."}
            event["classification_evidence"] = {field: dict(proof) for field in
                ("type", "lifecycle", "message_role", "relevance")}
            event["classification_evidence"]["attendance"] = None if facts["attendance"] == "unknown" else dict(proof)
        required = required_support(event)
        covered = set()
        for span in gold["evidence"]:
            supports = sorted(set(span["supports"]) & required)
            if supports:
                event["evidence"].append({"source_field": span["source_field"], "link_index": None,
                    "quote": record[span["source_field"]][span["start"]:span["end"]],
                    "supports": supports, "derivation": span["derivation"]})
                covered.update(supports)
        if required - covered:
            # Deliberately broad fixture span for routing/role/relation fields: not
            # a semantic evaluation. The source-read gold annotations are the input.
            event["evidence"].append({"source_field": "body", "link_index": None, "quote": record["body"],
                "supports": sorted(required - covered), "derivation": "Gold-derived software fixture only."})
        events.append(event)
    return {"schema_version": schema_version, "disposition": case["expected"]["message_disposition"],
            "reason": case["expected"]["reason"] or "Gold-derived fixture candidate.", "events": events, "warnings": []}


def main():
    registry, schema, prompt = load_contract()
    labels = json.loads((ROOT / "evaluation/labels.json").read_text(encoding="utf-8"))
    for split in ("development", "held_out"):
        run = {"schema_version": 1, "mode": "fixture_replay", "model": None, "split": split,
               "prompt_sha256": input_hash(prompt), "schema_sha256": input_hash(schema),
               "status": "completed", "cases": []}
        for case in labels["cases"]:
            if case["split"] != split:
                continue
            record = load_input(ROOT, case)
            result = fixture_result(case, record)
            offsets = validate_result(result, record, registry, schema)
            run["cases"].append({"case_id": case["case_id"], "status": "validated", "artifact": {
                "mode": "fixture_replay", "input_sha256": input_hash(record), "editorial_status": "pending",
                "prompt_sha256": input_hash(prompt), "schema_sha256": input_hash(schema),
                "result": result, "evidence_offsets": offsets}})
        directory = ROOT / "local/extraction/fixture_replay" / split
        save_json(directory / "predictions.json", run)
        report = evaluate_run(run, labels, lambda case: load_input(ROOT, case), registry, schema, prompt)
        save_json(directory / "evaluation.json", report)
        print(json.dumps({"mode": "fixture_replay", "split": split, "cases": len(run["cases"]),
                          "pilot_gate": report["pilot_gate"], "report": str(directory / "evaluation.json")}))


if __name__ == "__main__":
    main()
