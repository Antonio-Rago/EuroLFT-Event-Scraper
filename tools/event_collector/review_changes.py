"""Prepare minimal GitHub file changes from reconciliation or explicit editor output."""

import argparse
import copy
import json
from pathlib import Path
import re

from .check_corpus import input_hash
from .extract import private_directory, save_json
from .extraction import ROOT
from .proposals import md
from .reconcile import load_registry, validate_record
from .registry import public_url, validate_dataset, validator
from .website import records_directory as website_records


def safe_record(record):
    result = copy.deepcopy(record)
    for source in result["sources"]:
        public_url(source["url"])
    public_url(result["facts"]["official_url"])
    # Public PRs contain bounded event data, never full messages/model responses.
    # Preserve a readable candidate if a proof excerpt contains contact/access data.
    pattern = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|zoom\.|(?:password|passcode|api[_ -]?key)\s*[:=]", re.I)
    removed = [item for item in result["evidence"] if pattern.search(item["excerpt"])]
    if removed:
        if result["decision"]["status"] == "approved":
            raise ValueError("Approved proof contains private details; edit and reapprove before public review")
        result["evidence"] = [item for item in result["evidence"] if item not in removed]
        result["warnings"].append("Contact/access details omitted from evidence; inspect the source manually.")
    text = json.dumps({key: result[key] for key in ("facts", "sources", "history")})
    if pattern.search(text):
        raise ValueError("Review event text/source notes for contact or access details before public sharing")
    return result


def prepare(source, root=ROOT, records_directory=None):
    directory = records_directory or root / "records"
    canonical = load_registry(directory, validator(root))
    check = validator(root)
    ignored, records = [], []
    if (source / "plan.json").exists():
        plan = json.loads((source / "plan.json").read_text())
        if plan["registry_sha256"] != input_hash(canonical):
            raise ValueError("Canonical decisions changed; reconcile again before preparing GitHub changes")
        selected = {item["event_id"] for item in plan["routes"] if item["status"] in ("new", "update")}
        ignored = [item for item in plan["routes"] if item["status"] not in ("new", "update")]
        for event_id in sorted(selected):
            record = json.loads((source / (event_id + ".json")).read_text())
            if record["id"] != event_id or record["decision"]["status"] not in ("pending", "draft"):
                raise ValueError("Use explicit editor output for approval/exclusion decisions")
            records.append(record)
    else:
        for path in sorted(source.glob("event-*.json")):
            record = json.loads(path.read_text())
            if path.stem != record["id"]:
                raise ValueError("Editor record filename mismatch")
            if not record["history"]:
                raise ValueError("Explicit editor output requires editorial history")
            records.append(record)
    prepared = []
    for record in records:
        validate_record(record, check)
        old = canonical.get(record["id"])
        if old:
            if record == old:
                continue
            if record["revision"] <= old["revision"]:
                raise ValueError("A registry update must advance its revision")
            if old["decision"]["status"] in ("hidden", "rejected") and record["decision"]["status"] == "approved":
                if not any(item["action"] == "restore" and item["revision"] == record["revision"] for item in record["history"]):
                    raise ValueError("Restoring an exclusion requires an explicit editor restore")
        prepared.append(safe_record(record))
    proposed = {**canonical, **{record["id"]: record for record in prepared}}
    validate_dataset(proposed, root)
    identity = input_hash({"canonical": canonical, "records": prepared, "builder_version": 3})
    destination = root / "local/github-review" / ("changes-" + identity[:20])
    manifest = {"schema_version": 1, "registry_sha256": input_hash(canonical),
                "record_ids": [record["id"] for record in prepared], "ignored_count": len(ignored),
                "records_sha256": input_hash(prepared),
                "automatic_approval": False, "remote_writes": 0}
    if destination.exists():
        return destination, json.loads((destination / "manifest.json").read_text())
    private_directory(destination)
    lines = ["# Community event review", "", "Inspect every event and its source. Submit the next event with the collector's submit command; review and merge on GitHub to accept it.", ""]
    for record in prepared:
        save_json(destination / "changes/_event_collector/records" / (record["id"] + ".json"), record)
        facts = record["facts"]
        lines.extend(["## " + md(facts["title"]), "", "- Decision: " + md(record["decision"]["status"]),
                      "- Dates: " + md(facts["start_date"]) + " to " + md(facts["end_date"]),
                      "- Location: " + md(", ".join(value for value in facts["location"].values() if value) or "Unknown"),
                      "- Record: `_event_collector/records/" + record["id"] + ".json`", ""])
        for source_item in record["sources"]:
            if source_item["url"]:
                lines.append("- Source: <" + source_item["url"] + ">")
        for warning in record["warnings"]:
            lines.append("- Review: " + md(warning))
        if record["id"] in canonical:
            from .reconcile import fact_changes
            for change in fact_changes(canonical[record["id"]]["facts"], facts):
                lines.append("- Change " + md(change["field"]) + ": " + md(change["before"]) + " → " + md(change["after"]))
        lines.append("")
    lines.extend(["Validation: schema and current canonical-state checks passed. LLM extraction is not rerun during review or publication.",
                  "", "Close a collector event PR without merging to reject its event. Existing explicit rejected/hidden records remain excluded.", ""])
    (destination / "review.md").write_text("\n".join(lines), encoding="utf-8")
    (destination / "review.md").chmod(0o600)
    save_json(destination / "manifest.json", manifest)
    return destination, manifest


def apply_package(package, root=ROOT, records_directory=None, allow_pending_relations=False):
    manifest = json.loads((package / "manifest.json").read_text())
    directory = records_directory or root / "records"
    canonical = load_registry(directory, validator(root))
    if input_hash(canonical) != manifest["registry_sha256"]:
        raise ValueError("Canonical state changed; prepare the review package again")
    records = []
    for event_id in manifest["record_ids"]:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", event_id):
            raise ValueError("Invalid package identity")
        record = json.loads((package / "changes/_event_collector/records" / (event_id + ".json")).read_text())
        if record["id"] != event_id:
            raise ValueError("Package identity mismatch")
        records.append(record)
    if input_hash(records) != manifest["records_sha256"]:
        raise ValueError("Review package content changed; prepare it again")
    validate_dataset({**canonical, **{record["id"]: record for record in records}}, root,
                     allow_pending_relations=allow_pending_relations)
    for record in records:
        safe_record(record)
    for record in records:
        save_json(directory / (record["id"] + ".json"), record)
    return len(records)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Reconciliation plan or directory of explicit editor records")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--site", type=Path, required=True, help="Separate website checkout")
    parser.add_argument("--apply", action="store_true", help="Copy an integrity-checked package into the local registry; no Git/remote writes")
    args = parser.parse_args(argv)
    try:
        if args.apply:
            count = apply_package(args.source, args.root, website_records(args.site))
            print(json.dumps({"status": "applied_locally", "record_count": count, "remote_writes": 0}))
            return 0
        destination, manifest = prepare(args.source, args.root, website_records(args.site))
        print(json.dumps({"status": "prepared", "record_count": len(manifest["record_ids"]),
                          "remote_writes": 0, "review": str(destination / "review.md")}))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "changes_failed", "failure_type": type(exc).__name__,
                          "reason": str(exc) if type(exc) is ValueError else "Invalid review input"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
