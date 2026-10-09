"""Validate editorial state and export only sealed, approved public facts."""

import argparse
from datetime import datetime
import ipaddress
import json
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from jsonschema import Draft202012Validator, FormatChecker

from .check_corpus import input_hash
from .extraction import ROOT
from .reconcile import load_registry, validate_record
from .website import records_directory as website_records


def validator(root=ROOT):
    return Draft202012Validator(json.loads((root / "schema/event.schema.json").read_text()),
                               format_checker=FormatChecker())


def review_hash(record):
    return input_hash({key: record[key] for key in ("schema_version", "id", "revision", "origin", "facts",
                      "sources", "evidence", "related_events", "identity_aliases", "warnings", "editor_overrides")})


def public_url(value):
    if value is None:
        return
    parts = urlsplit(value)
    if (parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password
            or any(ord(c) < 33 or c in '<>"\\' for c in value)):
        raise ValueError("Unsafe public URL")
    hostname = parts.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith((".local", ".localhost", ".internal")):
        raise ValueError("Private public URL")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return
    if not address.is_global:
        raise ValueError("Private public URL")


def validate_public_record(record):
    if record["decision"]["status"] != "approved":
        return
    decision = record["decision"]
    if (decision["reviewed_revision"] != record["revision"]
            or decision.get("reviewed_content_sha256") != review_hash(record)):
        raise ValueError("Approved content changed after review; approve the current revision again")
    if set(record["warnings"]) - set(decision["acknowledged_warnings"]):
        raise ValueError("Approval must acknowledge all review warnings")
    if any(len(value) > 2000 for value in (record["facts"]["title"], record["facts"]["summary"] or "")):
        raise ValueError("Public title/summary exceeds the editorial length limit")
    for source in record["sources"]:
        public_url(source["url"])
    facts = record["facts"]
    public_url(facts["official_url"])
    text = json.dumps({"facts": facts, "urls": [source["url"] for source in record["sources"]]})
    if re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|zoom\.|(?:password|passcode|api[_ -]?key)\s*[:=]", text, re.I):
        raise ValueError("Public event data contains contact or access details; edit before approval")
    for item in [facts] + facts["deadlines"]:
        timezone = item["timezone"]
        if timezone:
            ZoneInfo(timezone)
        has_time = any(item.get(key) is not None for key in ("start_time", "end_time", "time"))
        if has_time and not timezone:
            raise ValueError("A published clock time requires an IANA timezone")
        if item.get("end_time") and not facts["end_date"]:
            raise ValueError("An end time requires an end date")
        if item.get("time") and not item.get("date"):
            raise ValueError("A deadline time requires a date")


def validate_dataset(records, root=ROOT, require_decisions=False, allow_pending_relations=False):
    check = validator(root)
    for event_id, record in records.items():
        if event_id != record["id"]:
            raise ValueError("Registry identity mismatch")
        validate_record(record, check)
        validate_public_record(record)
        if require_decisions and record["decision"]["status"] in ("pending", "draft"):
            raise ValueError("Canonical PR records require an editorial decision before merge")
        for relation in record["related_events"]:
            if relation["id"] == event_id or (relation["id"] not in records and not allow_pending_relations):
                raise ValueError("Dangling or self-referential related event")
        redirects = [alias[12:] for alias in record["identity_aliases"] if alias.startswith("merged-into:")]
        if redirects and (len(redirects) != 1 or redirects[0] not in records or redirects[0] == event_id
                          or record["decision"]["status"] != "hidden"):
            raise ValueError("Invalid duplicate redirect")
    # Follow satellite parent links; reciprocal has_satellite links are not cycles.
    def visit(event_id, path):
        if event_id in path:
            raise ValueError("Satellite relationship cycle")
        for relation in records[event_id]["related_events"]:
            if relation["relation"] == "satellite_of" and relation["id"] in records:
                visit(relation["id"], path | {event_id})
    for event_id in records:
        visit(event_id, set())
        seen, current = set(), event_id
        while True:
            redirects = [alias[12:] for alias in records[current]["identity_aliases"] if alias.startswith("merged-into:")]
            if not redirects:
                break
            if current in seen:
                raise ValueError("Duplicate redirect cycle")
            seen.add(current)
            current = redirects[0]
    return records


def public_data(records):
    approved = {key: value for key, value in records.items() if value["decision"]["status"] == "approved"}
    events = []
    def survivor(event_id):
        seen = set()
        while event_id not in seen:
            seen.add(event_id)
            redirects = [alias[12:] for alias in records[event_id]["identity_aliases"] if alias.startswith("merged-into:")]
            if not redirects:
                return event_id
            event_id = redirects[0]
        raise ValueError("Duplicate redirect cycle")
    for event_id, record in sorted(approved.items(), key=lambda item: (item[1]["facts"]["start_date"], item[0])):
        relations = []
        for item in record["related_events"]:
            if item["id"] not in records:
                continue
            target = survivor(item["id"])
            remapped = {**item, "id": target}
            if target in approved and target != event_id and remapped not in relations:
                relations.append(remapped)
        events.append({"id": event_id, **record["facts"],
                       "sources": list(dict.fromkeys(source["url"] for source in record["sources"] if source["url"])),
                       "related_events": relations})
    reviewed = [entry["at"] for record in approved.values() for entry in record["history"]
                if entry["action"] in ("approve", "restore") and entry["revision"] == record["revision"]]
    latest = max(reviewed, default=None, key=lambda value: datetime.fromisoformat(value.replace("Z", "+00:00")))
    return {"schema_version": 1, "last_editorial_update": latest, "events": events}


def export(records, destination, root=ROOT, allow_pending_relations=False):
    validate_dataset(records, root, allow_pending_relations=allow_pending_relations)
    content = json.dumps(public_data(records), indent=2, ensure_ascii=False) + "\n"
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".pending")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(destination)


def check_base(records, ref, root=ROOT, repository=None):
    repository = Path(repository).resolve() if repository is not None else root.parent
    listing = subprocess.run(["git", "ls-tree", "-r", "--name-only", ref, "--", "_event_collector/records/"],
                             cwd=repository, check=True, capture_output=True, text=True).stdout.splitlines()
    for filename in listing:
        if not filename.endswith(".json"):
            continue
        content = subprocess.run(["git", "show", ref + ":" + filename], cwd=repository,
                                 check=True, capture_output=True, text=True).stdout
        before = json.loads(content)
        after = records.get(before["id"])
        if after is None:
            raise ValueError("Keep the event ID as a hidden/rejected record instead of deleting it")
        if before != after and after["revision"] <= before["revision"]:
            raise ValueError("Changed canonical records must advance their revision")
        if before["decision"]["status"] in ("hidden", "rejected") and after["decision"]["status"] == "approved":
            if not any(item["action"] == "restore" and item["revision"] == after["revision"] for item in after["history"]):
                raise ValueError("An exclusion needs an explicit editor restore")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--site", type=Path, required=True, help="Separate EuroLFT website checkout")
    parser.add_argument("--require-decisions", action="store_true")
    parser.add_argument("--export", type=Path)
    parser.add_argument("--base", help="Git ref for PR deletion/revision checks")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--github-proposal", action="store_true", help="Check pending proposals for eventual publication; merging the PR accepts them")
    mode.add_argument("--github-merged", action="store_true", help="Verify human-merged PR receipts before exporting pending records")
    args = parser.parse_args(argv)
    try:
        records = load_registry(website_records(args.site), validator(args.root))
        if args.github_merged:
            from .github_review import merged_decisions
            records = merged_decisions(records, args.site, args.root)
        allow_relations = args.github_proposal or args.github_merged
        validate_dataset(records, args.root, args.require_decisions and not args.github_proposal, allow_relations)
        if args.github_proposal:
            from .github_review import publishable_candidate
            for event_id, record in records.items():
                if record["decision"]["status"] in ("pending", "draft"):
                    try:
                        publishable_candidate(record, args.root)
                    except ValueError as exc:
                        raise ValueError(event_id + ": " + str(exc)) from exc
        if args.base:
            check_base(records, args.base, args.root, args.site)
        if args.export:
            export(records, args.export, args.root, allow_relations)
        print(json.dumps({"status": "valid", "records": len(records),
                          "approved": sum(r["decision"]["status"] == "approved" for r in records.values())}))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "invalid_registry", "failure_type": type(exc).__name__, "reason": str(exc)
                          if type(exc) is ValueError else "Record validation failed"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
