"""Score validated extraction runs against draft labels; never feed labels to a model."""

import argparse
from collections import Counter
import json
from pathlib import Path
import re
import unicodedata

from .check_corpus import input_hash, pointer_value
from .extract import load_input, save_json
from .extraction import ROOT, PROMPT_VERSIONS, load_contract, validate_result


def normalise(value):
    return " ".join(re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", value).casefold()))


def match_events(expected, predicted):
    """Only unique title-alias/URL matches; never choose the best-scoring pairing."""
    edges = {}
    for p, event in enumerate(predicted):
        title, url = event["facts"]["title"], event["facts"]["official_url"]
        edges[p] = []
        for e, gold in enumerate(expected):
            same_title = normalise(title) in {normalise(alias) for alias in gold["accepted_title_aliases"]}
            gold_url = gold["facts"]["official_url"]
            same_url = url and gold_url and url.rstrip("/") == gold_url.rstrip("/")
            if same_title or same_url:
                edges[p].append(e)
    degree = Counter(e for values in edges.values() for e in values)
    matches = [(values[0], p) for p, values in edges.items()
               if len(values) == 1 and degree[values[0]] == 1]
    ambiguous = [p for p, values in edges.items()
                 if values and (len(values) != 1 or degree[values[0]] != 1)]
    implicated = {e for p in ambiguous for e in edges[p]}
    matched_expected = {e for e, p in matches}
    return {
        "matches": matches, "ambiguous_predicted": ambiguous,
        "missing_expected": [e for e in range(len(expected)) if e not in matched_expected | implicated],
        "extra_predicted": [p for p, values in edges.items() if not values],
    }


def score_case(case, result):
    expected, predicted = case["expected"]["events"], result["events"]
    matching = match_events(expected, predicted)
    errors, flag_reviews = [], []
    checked_fields = 0
    deadline_checks = 0
    unknown_checks = 0
    relationship_checks = 0
    associations = []

    def compare(pointer, wanted, found, event_key):
        nonlocal checked_fields
        checked_fields += 1
        if wanted != found:
            kind = "missing" if found in (None, "unknown") else (
                "unexpected_non_null" if wanted in (None, "unknown") else "incorrect")
            errors.append({"event_key": event_key, "field": pointer, "kind": kind,
                           "expected": wanted, "actual": found})

    identity = {predicted[p]["local_id"]: expected[e]["event_key"] for e, p in matching["matches"]}
    for e, p in matching["matches"]:
        gold, event = expected[e], predicted[p]
        key = gold["event_key"]
        associations.append({"event_key": key, "local_id": event["local_id"], "message_role": event["message_role"]})
        checked_fields += 1
        if normalise(event["facts"]["title"]) not in {normalise(alias) for alias in gold["accepted_title_aliases"]}:
            errors.append({"event_key": key, "field": "/facts/title", "kind": "incorrect",
                           "expected": gold["accepted_title_aliases"], "actual": event["facts"]["title"]})
        for field in ("type", "start_date", "end_date", "start_time", "end_time", "timezone",
                      "attendance", "official_url", "lifecycle"):
            compare("/facts/" + field, gold["facts"][field], event["facts"][field], key)
        for field in ("venue", "city", "country"):
            compare("/facts/location/" + field, gold["facts"]["location"][field], event["facts"]["location"][field], key)
        for field in ("message_role", "relevance"):
            compare("/" + field, gold[field], event[field], key)
        expected_deadlines = gold["facts"]["deadlines"]
        actual_deadlines = event["facts"]["deadlines"]
        expected_keys = [(d["kind"], normalise(d["label"])) for d in expected_deadlines]
        actual_keys = [(d["kind"], normalise(d["label"])) for d in actual_deadlines]
        if len(set(expected_keys)) != len(expected_keys) or len(set(actual_keys)) != len(actual_keys):
            flag_reviews.append({"event_key": key, "reason": "ambiguous_deadline_matching"})
        else:
            actual_by_key = dict(zip(actual_keys, actual_deadlines))
            for index, (deadline_key, deadline) in enumerate(zip(expected_keys, expected_deadlines)):
                deadline_checks += 1
                if deadline_key not in actual_by_key:
                    errors.append({"event_key": key, "field": "/facts/deadlines/" + str(index),
                                   "kind": "missing_deadline", "expected": deadline, "actual": None})
                else:
                    for field in ("date", "time", "timezone"):
                        compare("/facts/deadlines/{}/{}".format(index, field), deadline[field],
                                actual_by_key[deadline_key][field], key)
            for deadline_key in set(actual_keys) - set(expected_keys):
                errors.append({"event_key": key, "field": "/facts/deadlines", "kind": "extra_deadline",
                               "expected": None, "actual": actual_by_key[deadline_key]})
        for pointer in gold["must_remain_unknown"]:
            unknown_checks += 1
            if pointer_value(event, pointer) not in (None, "unknown"):
                # Already counted by field comparisons: keep one error per field.
                if not any(error["event_key"] == key and error["field"] == pointer for error in errors):
                    errors.append({"event_key": key, "field": pointer, "kind": "unexpected_non_null",
                                   "expected": None, "actual": pointer_value(event, pointer)})
        for flag in gold["required_review_flags"]:
            if flag not in event["review_flags"]:
                flag_reviews.append({"event_key": key, "reason": "review_flag_requires_adjudication", "flag": flag})
        expected_relations = {(r["event_key"], r["relation"]) for r in gold["related_events"]}
        actual_relations = {(identity.get(r["local_id"], "unmatched:" + r["local_id"]), r["relation"])
                            for r in event["related_events"]}
        relationship_checks += len(expected_relations)
        if expected_relations != actual_relations:
            errors.append({"event_key": key, "field": "/related_events", "kind": "incorrect",
                           "expected": sorted(expected_relations), "actual": sorted(actual_relations)})
    return {
        "case_id": case["case_id"], "basis": case["basis"], "split": case["split"],
        "expected_events": len(expected), "predicted_events": len(predicted),
        "matched_events": len(matching["matches"]), "matching": matching,
        "disposition_correct": result["disposition"] == case["expected"]["message_disposition"],
        "multi_event_case": len(expected) > 1,
        "complete_multi_event_recovery": len(expected) > 1 and len(matching["matches"]) == len(expected)
                                         and len(predicted) == len(expected),
        "checked_fields": checked_fields, "deadline_checks": deadline_checks,
        "required_unknown_checks": unknown_checks, "relationship_checks": relationship_checks,
        "field_errors": errors, "review_adjudication": flag_reviews, "associations": associations,
        "expected_routing": dict(Counter(event["relevance"] for event in expected)),
        "predicted_routing": dict(Counter(event["relevance"] for event in predicted)),
    }


def aggregate(cases):
    expected = sum(c["expected_events"] for c in cases)
    predicted = sum(c["predicted_events"] for c in cases)
    matched = sum(c["matched_events"] for c in cases)
    errors = [error for case in cases for error in case["field_errors"]]
    unknowns = sum(error["kind"] == "unexpected_non_null" for error in errors)
    date_errors = sum(error["field"] in ("/facts/start_date", "/facts/end_date") for error in errors)
    ambiguous = sum(len(case["matching"]["ambiguous_predicted"]) for case in cases)
    return {
        "cases": len(cases), "expected_events": expected, "predicted_events": predicted,
        "matched_events": matched,
        "precision_confirmed_matches": matched / predicted if predicted else None,
        "recall_confirmed_matches": matched / expected if expected else None,
        "missing_events": sum(len(c["matching"]["missing_expected"]) for c in cases),
        "extra_events": sum(len(c["matching"]["extra_predicted"]) for c in cases),
        "ambiguous_matches": ambiguous,
        "disposition_correct": sum(c["disposition_correct"] for c in cases),
        "multi_event_cases": sum(c["multi_event_case"] for c in cases),
        "complete_multi_event_recovery": sum(c["complete_multi_event_recovery"] for c in cases),
        "checked_fields": sum(c["checked_fields"] for c in cases),
        "field_errors": len(errors), "errors_by_kind": dict(Counter(e["kind"] for e in errors)),
        "event_date_errors": date_errors, "unexpected_non_null_fields": unknowns,
        "deadline_checks": sum(c["deadline_checks"] for c in cases),
        "required_unknown_checks": sum(c["required_unknown_checks"] for c in cases),
        "review_adjudications": sum(len(c["review_adjudication"]) for c in cases),
        "expected_routing": dict(sum((Counter(c["expected_routing"]) for c in cases), Counter())),
        "predicted_routing": dict(sum((Counter(c["predicted_routing"]) for c in cases), Counter())),
    }


def evaluate_run(run, labels, loader, registry, schema, prompt):
    if run["mode"] not in ("live", "fixture_replay"):
        raise ValueError("Unknown run mode")
    if run["prompt_sha256"] != input_hash(prompt) or run["schema_sha256"] != input_hash(schema):
        raise ValueError("Run used a different prompt/schema; evaluate against the frozen matching contract")
    by_id = {case["case_id"]: case for case in labels["cases"]}
    scores, failures, seen = [], [], set()
    for entry in run["cases"]:
        case_id = entry["case_id"]
        if case_id in seen:
            raise ValueError("Run contains duplicate cases")
        seen.add(case_id)
        case = by_id[case_id]
        if case["split"] != run["split"]:
            raise ValueError("Run mixes split boundaries")
        if entry["status"] != "validated":
            failures.append(case_id)
            continue
        record = loader(case)
        artifact = entry["artifact"]
        if artifact["mode"] != run["mode"]:
            raise ValueError("Run mixes fixture and live predictions")
        if run["mode"] == "live" and artifact["model"] != run["model"]:
            raise ValueError("Run mixes requested models")
        if input_hash(record) != case["input_sha256"] or artifact["input_sha256"] != case["input_sha256"]:
            raise ValueError("Prediction does not match the labelled frozen input")
        if artifact["prompt_sha256"] != run["prompt_sha256"] or artifact["schema_sha256"] != run["schema_sha256"]:
            raise ValueError("Prediction contract differs from run contract")
        validate_result(artifact["result"], record, registry, schema)
        scores.append(score_case(case, artifact["result"]))
    grouped = {}
    coverage = {}
    for basis in ("real", "synthetic"):
        for split in ("development", "held_out"):
            name = basis + "/" + split
            subset = [case for case in scores if case["basis"] == basis and case["split"] == split]
            grouped[name] = aggregate(subset)
            available = {c["case_id"] for c in labels["cases"] if c["basis"] == basis and c["split"] == split}
            coverage[name] = {"available_cases": len(available), "scored_cases": len(subset),
                              "unscored_cases": sorted(available - {c["case_id"] for c in subset})}
    sequence_checks = []
    by_score = {case["case_id"]: case for case in scores}
    for sequence in labels["update_sequences"]:
        if all(case_id in by_score for case_id in sequence["cases"]):
            sequence_checks.append({"event_key": sequence["event_key"], "cases": sequence["cases"],
                "recognised_in_all_messages": all(any(a["event_key"] == sequence["event_key"]
                    for a in by_score[case_id]["associations"]) for case_id in sequence["cases"]),
                "identity_reconciliation_tested": False})
    complete_holdout = coverage["real/held_out"]["available_cases"] > 0 and not coverage["real/held_out"]["unscored_cases"]
    complete_holdout = complete_holdout and not coverage["synthetic/held_out"]["unscored_cases"]
    heldout_groups = [grouped["real/held_out"], grouped["synthetic/held_out"]]
    versions = dict(Counter(entry["artifact"].get("returned_model_version") or "unreported"
                            for entry in run["cases"] if entry["status"] == "validated"))
    parameters = {input_hash(entry["artifact"].get("generation_parameters")): entry["artifact"].get("generation_parameters")
                  for entry in run["cases"] if entry["status"] == "validated"}
    return {
        "schema_version": 1, "mode": run["mode"], "model": run["model"],
        "annotation_status": "draft_for_editor_review", "labels_sha256": input_hash(labels),
        "prompt_sha256": run["prompt_sha256"], "schema_sha256": run["schema_sha256"],
        "groups": grouped, "coverage": coverage, "failed_cases": failures,
        "returned_model_versions": versions,
        "generation_parameters": list(parameters.values()),
        "cases": scores, "update_sequence_checks": sequence_checks,
        "pilot_gate": "not_assessed" if not complete_holdout or run["mode"] != "live" or run.get("status") != "completed" else (
            "needs_review" if failures or len(versions) != 1 or "unreported" in versions
            or len(parameters) != 1 or None in parameters.values()
            or any(group[key] for group in heldout_groups for key in (
                "ambiguous_matches", "review_adjudications", "field_errors", "missing_events", "extra_events"))
            else "draft_labels_pass"),
        "limitations": ["Labels await independent editor review.",
                        "Unexpected non-null fields require source adjudication; this is not a semantic proof.",
                        "Sequence recognition does not test registry merging or supersession.",
                        "Fixture replay results are software tests, not model accuracy."],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    try:
        labels = json.loads((args.root / "evaluation/labels.json").read_text(encoding="utf-8"))
        run = json.loads(args.run.read_text(encoding="utf-8"))
        contracts = [load_contract(args.root, version) for version in PROMPT_VERSIONS]
        matches = [contract for contract in contracts if input_hash(contract[2]) == run["prompt_sha256"]]
        if len(matches) != 1:
            raise ValueError("Run prompt hash has no unique saved prompt version")
        registry, schema, prompt = matches[0]
        report = evaluate_run(run, labels, lambda case: load_input(args.root, case), registry, schema, prompt)
        output = args.run.parent / "evaluation.json"
        save_json(output, report)
        print(json.dumps({"mode": report["mode"], "pilot_gate": report["pilot_gate"],
                          "groups": report["groups"], "report": str(output)}, indent=2))
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(1, "Evaluation failed: " + str(exc) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
