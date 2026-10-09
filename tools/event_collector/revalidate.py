"""Recheck an immutable rejected API proposal offline after a validator fix."""

import argparse
import json
from pathlib import Path

from .check_corpus import input_hash
from .extract import failure_details, load_input, save_json, timestamp
from .extraction import ROOT, SUPPORTED_MODELS, PROMPT_VERSIONS, load_contract, request_body, request_identity, validate_result


def revalidate(proposal, record, root=ROOT):
    artifact = dict(proposal)
    integrity = artifact.pop("integrity_sha256")
    if input_hash(artifact) != integrity:
        raise ValueError("Rejected proposal changed; immutable API provenance is required")
    if artifact["mode"] != "live" or artifact["status"] != "validation_failed" or artifact["editorial_status"] != "pending":
        raise ValueError("Only pending, rejected live API proposals can be revalidated")
    if artifact["model"] not in SUPPORTED_MODELS or artifact["input_sha256"] != input_hash(record):
        raise ValueError("Model/input provenance mismatch")
    matches = []
    for version in PROMPT_VERSIONS:
        contract = load_contract(root, version)
        if input_hash(contract[1]) == artifact["schema_sha256"] and input_hash(contract[2]) == artifact["prompt_sha256"]:
            matches.append(contract)
    if len(matches) != 1:
        raise ValueError("No unique saved prompt/schema contract matches this response")
    registry, schema, prompt = matches[0]
    request = request_body(record, schema, prompt)
    request["generationConfig"].update(artifact.get("generation_parameters", {}))
    if request_identity(record, request, model=artifact["model"]) != artifact["request_sha256"]:
        raise ValueError("Request parameters do not match the saved response")
    try:
        offsets = validate_result(artifact["result"], record, registry, schema)
    except Exception as exc:
        exc.pilot_details = failure_details(exc, "source_validation")
        raise
    result = {key: value for key, value in artifact.items() if key not in ("status", "validation_error")}
    result.update(evidence_offsets=offsets, revalidated_at=timestamp(), revalidated_from_sha256=integrity)
    path = root / "local/extraction/cache" / (artifact["request_sha256"] + ".json")
    if path.exists():
        previous = json.loads(path.read_text(encoding="utf-8"))
        if previous["result"] != result["result"]:
            raise ValueError("Existing cached result differs; refusing to overwrite it")
    save_json(path, {**result, "integrity_sha256": input_hash(result)})
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("proposal", type=Path)
    parser.add_argument("--case", required=True)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    try:
        labels = json.loads((args.root / "evaluation/labels.json").read_text(encoding="utf-8"))
        case = next(case for case in labels["cases"] if case["case_id"] == args.case)
        record = load_input(args.root, case)
        proposal = json.loads(args.proposal.read_text(encoding="utf-8"))
        path = revalidate(proposal, record, args.root)
        print(json.dumps({"status": "revalidated", "api_requests": 0, "case_id": args.case, "cache": str(path)}))
        return 0
    except Exception as exc:
        # Validation exceptions can embed source excerpts; use safe field/type
        # diagnostics without printing their messages or rejected source content.
        details = getattr(exc, "pilot_details", failure_details(exc, "offline_revalidation"))
        print(json.dumps({"status": "revalidation_failed", **details, "api_requests": 0}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
