"""Prepare explicit editor decisions, corrections, manual additions and duplicate merges."""

import argparse
import copy
import json
from pathlib import Path
import uuid

from .check_corpus import pointer_value
from .extract import save_json, timestamp
from .extraction import ROOT
from .reconcile import aliases, merge_record, validate_record
from .registry import review_hash, validate_public_record, validator


def pending():
    return {"status": "pending", "reviewed_revision": None, "reason": None, "acknowledged_warnings": []}


def history(record, action, actor, note=None):
    if not actor or not actor.strip():
        raise ValueError("An editor identity is required")
    record["history"].append({"action": action, "actor": actor.strip(), "at": timestamp(),
                              "revision": record["revision"], "note": note})


def manual(facts, actor):
    result = {"schema_version": 1, "id": "event-manual-" + uuid.uuid4().hex, "revision": 1,
              "origin": "manual", "facts": copy.deepcopy(facts),
              "sources": [{"id": "editor-reference", "kind": "editor", "url": facts.get("official_url"),
                           "posted_at": None, "retrieved_at": None, "note": "Manual event entry"}],
              "evidence": [], "related_events": [], "identity_aliases": [], "warnings": [],
              "editor_overrides": [], "decision": pending(), "history": []}
    history(result, "edit", actor, "Manual addition")
    return result


def edit(record, patch, actor, protect=()):
    result = copy.deepcopy(record)
    def apply(base, incoming):
        for key, value in incoming.items():
            if key not in base:
                raise ValueError("Unknown event fact")
            if isinstance(value, dict) and isinstance(base[key], dict):
                apply(base[key], value)
            else:
                base[key] = copy.deepcopy(value)
    apply(result["facts"], patch)
    for pointer in protect:
        if not pointer.startswith("/facts/"):
            raise ValueError("Only event facts can be protected")
        pointer_value(result, pointer)
    result["editor_overrides"] = sorted(set(result["editor_overrides"]) | set(protect))
    # Field edits invalidate proof for changed values, including changed deadline indices.
    result["evidence"] = [item for item in record["evidence"]
                          if pointer_value(record, item["field"]) == _value(result, item["field"])]
    result["revision"] += 1
    result["decision"] = (copy.deepcopy(record["decision"])
                          if record["decision"]["status"] in ("hidden", "rejected") else pending())
    history(result, "edit", actor, "Editor correction")
    return result


def _value(record, pointer):
    try:
        return pointer_value(record, pointer)
    except (IndexError, KeyError):
        return object()


def decide(record, action, actor, reason=None, acknowledge=False):
    result = copy.deepcopy(record)
    status = record["decision"]["status"]
    if action == "approve" and status in ("rejected", "hidden"):
        raise ValueError("Use restore explicitly for an excluded event")
    if action == "restore" and status not in ("rejected", "hidden"):
        raise ValueError("Only excluded events can be restored")
    if action in ("reject", "hide") and not reason:
        raise ValueError("An exclusion reason is required")
    result["revision"] += 1
    approving = action in ("approve", "restore")
    if approving and result["warnings"] and not acknowledge:
        raise ValueError("Inspect and acknowledge all warnings before approval")
    result["decision"] = {"status": "approved" if approving else ("rejected" if action == "reject" else "hidden"),
                          "reviewed_revision": result["revision"] if approving else None,
                          "reason": reason, "acknowledged_warnings": result["warnings"][:] if approving else []}
    history(result, action, actor, reason)
    if approving:
        result["decision"]["reviewed_content_sha256"] = review_hash(result)
        validate_record(result, validator())
        validate_public_record(result)
    return result


def merge_duplicate(target, duplicate, actor):
    if target["id"] == duplicate["id"] or target["decision"]["status"] in ("hidden", "rejected"):
        raise ValueError("Choose a distinct, non-excluded surviving event")
    merged, suggestions, changed = merge_record(target, duplicate)
    merged["revision"] = target["revision"] + 1
    merged["decision"] = pending()
    merged["identity_aliases"] = sorted(aliases(target) | aliases(duplicate) | {"event:" + duplicate["id"]})
    merged["related_events"] = [item for item in merged["related_events"]
                               if item["id"] not in (target["id"], duplicate["id"])]
    merged["warnings"] = list(dict.fromkeys(merged["warnings"] + ["Editor must review combined duplicate facts and sources."]))
    history(merged, "merge", actor, "Merged duplicate " + duplicate["id"])
    excluded = decide(duplicate, "hide", actor, "Merged into " + target["id"])
    excluded["identity_aliases"] = sorted(set(excluded["identity_aliases"]) | {"merged-into:" + target["id"]})
    excluded["related_events"] = []
    return merged, excluded, suggestions


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("manual", "edit", "approve", "reject", "hide", "restore", "merge"))
    parser.add_argument("input", type=Path, help="Facts JSON for manual; record JSON for other actions")
    parser.add_argument("--actor", required=True)
    parser.add_argument("--reason")
    parser.add_argument("--acknowledge-all", action="store_true")
    parser.add_argument("--patch", type=Path)
    parser.add_argument("--protect", action="append", default=[])
    parser.add_argument("--duplicate", type=Path)
    parser.add_argument("--output", type=Path, required=True, help="Local output directory; canonical input is unchanged")
    args = parser.parse_args(argv)
    try:
        original = json.loads(args.input.read_text())
        if args.action == "manual":
            records = [manual(original, args.actor)]
        else:
            validate_record(original, validator())
            if args.action == "edit":
                if not args.patch:
                    raise ValueError("An edit requires --patch")
                records = [edit(original, json.loads(args.patch.read_text()), args.actor, args.protect)]
            elif args.action == "merge":
                if not args.duplicate:
                    raise ValueError("A merge requires --duplicate")
                duplicate = json.loads(args.duplicate.read_text())
                validate_record(duplicate, validator())
                records = list(merge_duplicate(original, duplicate, args.actor)[:2])
            else:
                records = [decide(original, args.action, args.actor, args.reason, args.acknowledge_all)]
        if any((args.output / (record["id"] + ".json")).resolve() == args.input.resolve() for record in records):
            raise ValueError("Choose an output distinct from the input")
        for record in records:
            validate_record(record, validator())
            validate_public_record(record)
        for record in records:
            save_json(args.output / (record["id"] + ".json"), record)
        print(json.dumps({"status": "prepared", "records": len(records), "output": str(args.output)}))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "editor_action_failed", "failure_type": type(exc).__name__,
                          "reason": str(exc) if type(exc) is ValueError else "Invalid editor input"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
