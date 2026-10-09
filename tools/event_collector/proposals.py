"""Prepare local, pending review drafts from saved extractions; no API or Git writes."""

import argparse
import html
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator, FormatChecker

from .check_corpus import check_facts, input_hash, pointer_value
from .extract import failure_details, load_input, private_directory, save_json
from .extraction import (ROOT, PROMPT_VERSIONS, SUPPORTED_MODELS, allowed_support,
                         load_contract, request_body, request_identity, required_support, source_span)


def checked_artifact(value):
    artifact = dict(value)
    integrity = artifact.pop("integrity_sha256")
    if input_hash(artifact) != integrity:
        raise ValueError("Saved extraction integrity mismatch")
    return artifact


def load_artifact(entry, run, record, root, schema, prompt):
    if entry["status"] == "validated":
        identity = entry["artifact"]["request_sha256"]
        if not re.fullmatch(r"[a-f0-9]{64}", identity):
            raise ValueError("Invalid request identity")
        path = root / "local/extraction/cache" / (identity + ".json")
        artifact = checked_artifact(json.loads(path.read_text()))
        if artifact != entry["artifact"]:
            raise ValueError("Run differs from saved extraction")
    else:
        path = Path(entry["rejected_proposal"])
        if not path.resolve().is_relative_to((root / "local/extraction/rejected").resolve()):
            raise ValueError("Rejected proposal is outside private extraction storage")
        artifact = checked_artifact(json.loads(path.read_text()))
        if artifact["status"] != "validation_failed":
            raise ValueError("Not a rejected extraction proposal")
    if (artifact["mode"] != "live" or artifact["editorial_status"] != "pending"
            or artifact["model"] not in SUPPORTED_MODELS or artifact["model"] != run["model"]
            or artifact["input_sha256"] != input_hash(record)
            or artifact["prompt_sha256"] != run["prompt_sha256"]
            or artifact["schema_sha256"] != run["schema_sha256"]):
        raise ValueError("Extraction provenance mismatch")
    request = request_body(record, schema, prompt)
    request["generationConfig"].update(artifact["generation_parameters"])
    if request_identity(record, request, model=artifact["model"]) != artifact["request_sha256"]:
        raise ValueError("Extraction request mismatch")
    return artifact


def build_drafts(artifact, record, registry, schema):
    result = artifact["result"]
    Draft202012Validator(schema).validate(result)
    if result["disposition"] != ("event_candidates" if result["events"] else "no_event"):
        raise ValueError("Contradictory extraction disposition")
    local_ids = [event["local_id"] for event in result["events"]]
    if (len(local_ids) > 12 or len(local_ids) != len(set(local_ids))
            or any(not re.fullmatch(r"e[1-9][0-9]*", value) for value in local_ids)):
        raise ValueError("Invalid or duplicate candidate identities")
    source_url = record["source_url"]
    url = urlsplit(source_url)
    if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password:
        raise ValueError("Invalid source URL")
    facts_validator = Draft202012Validator({"$ref": "#/$defs/facts", "$defs": registry["$defs"]},
                                          format_checker=FormatChecker())
    ids = {local_id: "proposed-" + artifact["request_sha256"][:20] + "-" + local_id for local_id in local_ids}
    drafts, repairs = [], []
    for event in result["events"]:
        try:
            facts_validator.validate(event["facts"])
            check_facts(event["facts"])
        except Exception as exc:
            repairs.append({"local_id": event["local_id"], **failure_details(exc, "facts_validation")})
    valid_ids = set(ids) - {repair["local_id"] for repair in repairs}
    source_id = "source-" + input_hash(source_url)[:20]
    for event in result["events"]:
        if event["local_id"] not in valid_ids:
            continue
        issues, spans, supported, evidence = [], [], set(), []

        def issue(code, field, message):
            item = {"code": code, "field": field, "message": message}
            if item not in issues:
                issues.append(item)

        def proof(value, pointers):
            try:
                span = source_span(value, record)
            except ValueError:
                for pointer in pointers:
                    issue("quote_unverified", pointer, "Quotation does not match the source; verify the claim manually.")
                return
            valid_pointers = []
            for pointer in pointers:
                if pointer not in allowed_support(event):
                    issue("invalid_evidence_pointer", pointer, "Evidence field reference needs correction.")
                    continue
                pointer_value(event, pointer)
                valid_pointers.append(pointer)
                supported.add(pointer)
                if pointer.startswith("/facts/"):
                    if len(span["quote"]) <= 500:
                        item = {"field": pointer, "source_id": source_id, "excerpt": span["quote"]}
                        if item not in evidence:
                            evidence.append(item)
                    else:
                        issue("long_excerpt", pointer, "Matched passage exceeds the record excerpt limit; select a shorter passage.")
            spans.append({**span, "supports": valid_pointers})

        for value in event["evidence"]:
            if not value["supports"]:
                issue("empty_evidence_pointer", "/evidence", "Quotation has no field references.")
            proof(value, value["supports"])
        if result["schema_version"] >= 3:
            for field, value in event["classification_evidence"].items():
                if value is not None:
                    pointer = "/" + field if field in ("message_role", "relevance") else "/facts/" + field
                    proof(value, [pointer])
        relations = []
        for index, relation in enumerate(event["related_events"]):
            pointer = "/related_events/" + str(index)
            if result["schema_version"] >= 2:
                proof(relation["evidence"], [pointer])
            target = relation["local_id"]
            if target in valid_ids and target != event["local_id"]:
                relations.append({"id": ids[target], "relation": relation["relation"]})
            else:
                issue("relationship_target", pointer, "Related candidate is missing, invalid or self-referential.")
        for pointer in sorted(required_support(event) - supported):
            issue("missing_evidence", pointer, "No matching quotation covers this field; verify it against the source.")
        official_url = event["facts"]["official_url"]
        if official_url and not any(official_url in text for text in
                                   (record["subject"], record["body"], *record["links"])):
            issue("url_unverified", "/facts/official_url", "Organiser URL is absent from the source; verify or remove it.")
        if event["relevance"] == "review":
            issue("scope_review", "/relevance", "Relevance is uncertain; an editor should decide inclusion.")
        if event["facts"]["type"] == "course":
            issue("course_schedule_review", "/facts", "Check the recurring timetable and course-period representation.")
        if event["facts"]["deadlines"]:
            issue("deadline_review", "/facts/deadlines", "Review deadline purposes, labels and any shared dates.")
        warnings = ["Human review required: matching quotations do not establish semantic correctness."]
        warnings.extend("Model flag: " + flag for flag in event["review_flags"])
        warnings.extend("Model warning: " + flag for flag in result["warnings"])
        warnings.extend(item["code"] + " at " + item["field"] + ": " + item["message"] for item in issues)
        candidate = {
            "schema_version": 1, "id": ids[event["local_id"]], "revision": 1, "origin": "collected",
            "facts": event["facts"],
            "sources": [{"id": source_id, "kind": "announcement", "url": source_url,
                         "posted_at": record["posted_at"], "retrieved_at": None,
                         "note": "Draft from a saved model extraction; source retrieval time is not supplied."}],
            "evidence": evidence, "related_events": relations, "identity_aliases": [],
            "warnings": list(dict.fromkeys(warnings)), "editor_overrides": [],
            "decision": {"status": "pending", "reviewed_revision": None, "reason": None,
                         "acknowledged_warnings": []}, "history": [],
        }
        Draft202012Validator(registry, format_checker=FormatChecker()).validate(candidate)
        drafts.append({"record": candidate, "issues": issues, "matched_source_spans": spans,
                       "local_id": event["local_id"], "request_sha256": artifact["request_sha256"],
                       "input_sha256": artifact["input_sha256"], "model": artifact["model"],
                       "original_validation": "passed" if "validation_error" not in artifact else "failed",
                       "identity_status": "provisional_until_reconciliation", "eligible_for_export": False})
    return drafts, repairs


def md(value):
    text = html.escape(str(value), quote=False).replace("\n", " ").replace("\r", " ")
    return re.sub(r"([\\`*_{}\[\]()|!#])", r"\\\1", text)


def render_review(drafts, cases, destination):
    titles = {draft["record"]["id"]: draft["record"]["facts"]["title"] for draft in drafts}
    lines = ["# Event proposals for human review", "",
             "Local draft only. Nothing is approved or published. IDs are provisional; identity reconciliation is pending.", "",
             "Matching evidence establishes quote presence only. Review every model fact, including fields without warnings.", ""]
    for draft in drafts:
        candidate, facts = draft["record"], draft["record"]["facts"]
        path = destination / (candidate["id"] + ".json")
        location = ", ".join(value for value in facts["location"].values() if value) or "Unknown"
        lines.extend(["## " + md(facts["title"]), "",
                      "- Type: " + md(facts["type"]),
                      "- Dates: " + md(facts["start_date"] or "Unknown") + " to " + md(facts["end_date"] or "Unknown"),
                      "- Location: " + md(location),
                      "- Attendance: " + md(facts["attendance"]),
                      "- Event times: " + md(facts["start_time"] or "Unknown") + " to " + md(facts["end_time"] or "Unknown"),
                      "- Timezone: " + md(facts["timezone"] or "Unknown"),
                      "- Source: <" + candidate["sources"][0]["url"] + ">",
                      "- Editable pending record: [JSON](<" + str(path) + ">)", ""])
        if facts["official_url"]:
            lines.extend(["Proposed organiser URL (review required): " + md(facts["official_url"]), ""])
        for deadline in facts["deadlines"]:
            lines.append("- Deadline: " + md(deadline["label"]) + " — " + md(deadline["date"] or "Unknown")
                         + " (" + md(deadline["kind"]) + "); time: " + md(deadline["time"] or "Unknown")
                         + "; timezone: " + md(deadline["timezone"] or "Unknown"))
        if facts["deadlines"]:
            lines.append("")
        for relation in candidate["related_events"]:
            lines.append("- Related event: " + md(relation["relation"]) + " — " + md(titles[relation["id"]]))
        if candidate["related_events"]:
            lines.append("")
        lines.extend(["Review notes:", ""])
        lines.extend("- " + md(warning) for warning in candidate["warnings"])
        lines.append("")
    lines.extend(["## Processing summary", ""])
    for case in cases:
        lines.append("- " + md(case["case_id"]) + ": " + md(case["status"]))
        if case.get("source_url"):
            lines.append("  Source: <" + case["source_url"] + ">")
        for repair in case.get("repairs", []):
            lines.append("  Candidate " + md(repair["local_id"]) + " needs facts repair ("
                         + md(repair["failure_type"]) + ").")
    return "\n".join(lines) + "\n"


def prepare(run_path, root=ROOT):
    root = root.resolve()
    run = json.loads(run_path.read_text())
    if run["mode"] != "live" or run["status"] not in ("completed", "completed_with_failures", "failed"):
        raise ValueError("Use a finished live extraction run")
    contracts = [load_contract(root, version) for version in PROMPT_VERSIONS]
    matches = [contract for contract in contracts if input_hash(contract[1]) == run["schema_sha256"]
               and input_hash(contract[2]) == run["prompt_sha256"]]
    if len(matches) != 1:
        raise ValueError("No unique saved extraction contract")
    registry, schema, prompt = matches[0]
    run_hash = input_hash(run)
    destination = root / "local/review" / ("proposal-" + run_hash[:20])
    if destination.exists():
        private_directory(destination.parent)
        manifest = json.loads((destination / "manifest.json").read_text())
        if manifest["run_sha256"] != run_hash:
            raise ValueError("Existing proposal provenance differs")
        return destination, manifest, True
    labels = json.loads((root / "evaluation/labels.json").read_text())
    by_id = {case["case_id"]: case for case in labels["cases"]}
    drafts, cases, seen = [], [], set()
    for entry in run["cases"]:
        cid = entry["case_id"]
        if cid in seen:
            raise ValueError("Duplicate case in run")
        seen.add(cid)
        case = by_id[cid]
        if case["split"] != run["split"]:
            raise ValueError("Run mixes development and held-out cases")
        if case["basis"] != "real":
            cases.append({"case_id": cid, "status": "synthetic_excluded"})
            continue
        record = load_input(root, case)
        if entry["status"] != "validated" and not entry.get("rejected_proposal"):
            cases.append({"case_id": cid, "status": "operational_failure", "source_url": record["source_url"]})
            continue
        artifact = load_artifact(entry, run, record, root, schema, prompt)
        built, repairs = build_drafts(artifact, record, registry, schema)
        drafts.extend(built)
        cases.append({"case_id": cid, "status": "pending_review" if built else (
            "facts_need_repair" if repairs else "no_event"), "candidate_count": len(built),
            "repairs": repairs, "source_url": record["source_url"]})
    manifest = {"schema_version": 1, "builder_version": "v1", "run_sha256": run_hash,
                "source_run": str(run_path.resolve()), "editorial_status": "pending",
                "eligible_for_export": False, "api_requests": 0, "candidate_count": len(drafts),
                "candidates": [{"id": draft["record"]["id"], "issues": draft["issues"],
                                "original_validation": draft["original_validation"]} for draft in drafts],
                "cases": cases}
    private_directory(destination.parent)
    private_directory(destination)
    for draft in drafts:
        candidate = draft["record"]
        save_json(destination / (candidate["id"] + ".json"), candidate)
        save_json(destination / (candidate["id"] + ".review.json"),
                  {key: value for key, value in draft.items() if key != "record"})
    descriptor = os.open(destination / "review.md", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(render_review(drafts, cases, destination))
    save_json(destination / "manifest.json", manifest)
    return destination, manifest, False


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    try:
        directory, manifest, reused = prepare(args.run, args.root)
        print(json.dumps({"status": "existing_preserved" if reused else "prepared",
                          "candidate_count": manifest["candidate_count"], "api_requests": 0,
                          "review": str(directory / "review.md")}))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "proposal_preparation_failed", "failure_type": type(exc).__name__, "api_requests": 0}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
