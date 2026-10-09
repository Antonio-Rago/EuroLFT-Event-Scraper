"""Plan identity and field updates locally; never change canonical editorial records."""

import argparse
import copy
import json
import os
from pathlib import Path
import re
import unicodedata
from urllib.parse import urlsplit, urlunsplit

from jsonschema import Draft202012Validator, FormatChecker

from .check_corpus import check_facts, input_hash, pointer_value
from .extract import private_directory, save_json
from .extraction import ROOT
from .website import records_directory as website_records
from .proposals import md


def text_key(value):
    return " ".join(re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", value).casefold()))


def url_key(value):
    if not value:
        return None
    parts = urlsplit(value)
    # Keep scheme/query/fragment: anchors and query IDs can identify different events.
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"),
                       parts.query, parts.fragment))


def edition(record):
    years = set(re.findall(r"\b(?:19|20)\d{2}\b", record["facts"]["title"]))
    if years:
        return years
    start = record["facts"]["start_date"]
    return {start[:4]} if start else set()


def aliases(record):
    result = set(record["identity_aliases"])
    title = text_key(record["facts"]["title"])
    for source in record["sources"]:
        if source["url"]:
            result.add("source-event:" + url_key(source["url"]) + "|title:" + title)
    return result


def match_strength(candidate, existing):
    shared = set(candidate["identity_aliases"]) & set(existing["identity_aliases"])
    if any(value.startswith(("inspire:", "event:")) for value in shared):
        return "strong"
    a, b = edition(candidate), edition(existing)
    if a and b and a.isdisjoint(b):
        return None
    if aliases(candidate) & aliases(existing):
        return "strong"
    left, right = candidate["facts"], existing["facts"]
    urls = {url_key(value[4:] if value.startswith("url:") else value) for value in existing["identity_aliases"]
            if value.startswith(("http://", "https://", "url:http://", "url:https://"))}
    if right["official_url"]:
        urls.add(url_key(right["official_url"]))
    if left["official_url"] and url_key(left["official_url"]) in urls:
        # Yearless/reusable URLs need identity review rather than an automatic match.
        unverified = any("url_unverified" in warning for warning in candidate["warnings"])
        same_kind = left["type"] == right["type"] and left["type"] != "unknown"
        return "strong" if a and b and same_kind and not unverified else "possible"
    titles = {text_key(right["title"])}
    titles.update(text_key(value[6:]) for value in existing["identity_aliases"] if value.startswith("title:"))
    if text_key(left["title"]) in titles:
        if left["start_date"] and left["start_date"] == right["start_date"]:
            return "strong"
        return "possible"
    # Acronyms/expanded titles can be proposed to a reviewer without fuzzy merging.
    first, second = text_key(left["title"]), text_key(right["title"])
    if a and b and (" " + first + " " in " " + second + " " or " " + second + " " in " " + first + " "):
        return "possible"
    return None


def validate_record(record, validator):
    validator.validate(record)
    check_facts(record["facts"])
    if record["decision"]["status"] == "approved" and record["decision"]["reviewed_revision"] != record["revision"]:
        raise ValueError("Approval is not for the current record revision")
    source_ids = [source["id"] for source in record["sources"]]
    if len(source_ids) != len(set(source_ids)):
        raise ValueError("Duplicate source IDs")
    for evidence in record["evidence"]:
        if evidence["source_id"] not in source_ids:
            raise ValueError("Unknown evidence source")
        pointer_value(record, evidence["field"])
    for pointer in record["editor_overrides"]:
        pointer_value(record, pointer)


def load_registry(directory, validator):
    result = {}
    for path in sorted(directory.glob("*.json")):
        record = json.loads(path.read_text())
        validate_record(record, validator)
        if record["id"] in result:
            raise ValueError("Duplicate canonical IDs")
        result[record["id"]] = record
    return result


def merge_sources(base, incoming):
    sources = copy.deepcopy(base["sources"])
    mapping = {}
    for source in incoming["sources"]:
        match = next((item for item in sources if source["url"] and url_key(item["url"]) == url_key(source["url"])), None)
        if match is None:
            match = copy.deepcopy(source)
            if any(item["id"] == match["id"] for item in sources):
                match["id"] = "source-" + input_hash(source)[:20]
            if any(item["id"] == match["id"] for item in sources):
                raise ValueError("Source identity collision")
            sources.append(match)
        mapping[source["id"]] = match["id"]
    return sources, mapping


def protected(record, pointer):
    if record["origin"] == "manual":
        return True
    return any(pointer == value or pointer.startswith(value + "/")
               or (pointer == "/facts/deadlines" and value.startswith(pointer + "/"))
               for value in record["editor_overrides"])


def merge_record(base, incoming):
    merged = copy.deepcopy(base)
    suggestions = []

    def field(pointer, old, new):
        if new in (None, "unknown") or new == old:
            return old
        if protected(base, pointer):
            suggestions.append({"field": pointer, "kept": old, "suggested": new,
                                "reason": "manual_record" if base["origin"] == "manual" else "editor_override"})
            return old
        return copy.deepcopy(new)

    for key, value in incoming["facts"].items():
        if key == "location":
            for part, new in value.items():
                pointer = "/facts/location/" + part
                merged["facts"][key][part] = field(pointer, base["facts"][key][part], new)
        elif key == "deadlines":
            if value and protected(base, "/facts/deadlines"):
                merged["facts"][key] = field("/facts/deadlines", base["facts"][key], value)
            elif value:
                combined = copy.deepcopy(base["facts"][key])
                for deadline in value:
                    index = next((i for i, old in enumerate(combined) if old["kind"] == deadline["kind"]
                                  and text_key(old["label"]) == text_key(deadline["label"])), None)
                    if index is None:
                        combined.append(copy.deepcopy(deadline))
                    else:
                        for part, new in deadline.items():
                            if new is not None:
                                combined[index][part] = copy.deepcopy(new)
                merged["facts"][key] = combined
        else:
            merged["facts"][key] = field("/facts/" + key, base["facts"][key], value)
    merged["sources"], source_ids = merge_sources(base, incoming)
    merged["related_events"] = list(base["related_events"])
    for relation in incoming["related_events"]:
        if relation not in merged["related_events"]:
            merged["related_events"].append(copy.deepcopy(relation))
    changed = any(merged[key] != base[key] for key in ("facts", "sources", "related_events"))
    if not changed:
        return copy.deepcopy(base), suggestions, False
    # Retain old evidence only for facts whose value did not change. Incoming
    # evidence cannot establish a protected value that differs from its suggestion.
    merged["evidence"] = []
    for owner, mapping in ((base, {source["id"]: source["id"] for source in base["sources"]}), (incoming, source_ids)):
        for evidence in owner["evidence"]:
            pointer = evidence["field"]
            deadline_pointer = re.fullmatch(r"/facts/deadlines/(\d+)/(\w+)", pointer)
            if deadline_pointer:
                original = owner["facts"]["deadlines"][int(deadline_pointer[1])]
                index = next((i for i, deadline in enumerate(merged["facts"]["deadlines"])
                              if deadline["kind"] == original["kind"]
                              and text_key(deadline["label"]) == text_key(original["label"])), None)
                if index is None:
                    continue
                pointer = "/facts/deadlines/{}/{}".format(index, deadline_pointer[2])
            try:
                same_value = pointer_value(owner, evidence["field"]) == pointer_value(merged, pointer)
            except (KeyError, IndexError):
                same_value = False
            if same_value:
                item = {**evidence, "field": pointer, "source_id": mapping[evidence["source_id"]]}
                if item not in merged["evidence"]:
                    merged["evidence"].append(item)
    merged["identity_aliases"] = sorted(aliases(base) | aliases(incoming))
    merged["warnings"] = list(dict.fromkeys(base["warnings"] + incoming["warnings"]))
    if base["facts"]["deadlines"] and incoming["facts"]["deadlines"]:
        merged["warnings"].append("Review deadline labels and purposes when combining sources.")
        merged["warnings"] = list(dict.fromkeys(merged["warnings"]))
    merged["revision"] = base["revision"] + 1
    merged["decision"] = {"status": "pending", "reviewed_revision": None, "reason": None, "acknowledged_warnings": []}
    # Origin, editor overrides and real editorial history stay unchanged.
    return merged, suggestions, True


def fact_changes(before, after, pointer="/facts"):
    if isinstance(before, dict) and isinstance(after, dict):
        return [change for key in before for change in fact_changes(before[key], after[key], pointer + "/" + key)]
    return [] if before == after else [{"field": pointer, "before": before, "after": after}]


def reconcile_records(candidates, registry, identities, choices=None):
    choices = choices or {}
    candidate_ids = [candidate["id"] for candidate in candidates]
    if len(candidate_ids) != len(set(candidate_ids)) or set(choices) - set(candidate_ids):
        raise ValueError("Duplicate candidates or unknown reviewer match choices")
    catalog = {key: copy.deepcopy(value["record"]) for key, value in identities.items()}
    catalog.update(copy.deepcopy(registry))
    next_identities = copy.deepcopy(identities)
    routing, remap = [], {}
    def survivor(event_id):
        visited = set()
        while True:
            if event_id in visited:
                raise ValueError("Duplicate redirect cycle")
            visited.add(event_id)
            targets = [value[12:] for value in catalog[event_id]["identity_aliases"] if value.startswith("merged-into:")]
            if not targets:
                return event_id
            if len(targets) != 1 or targets[0] not in catalog or catalog[event_id]["decision"]["status"] != "hidden":
                raise ValueError("Invalid duplicate redirect")
            event_id = targets[0]
    for candidate in sorted(candidates, key=lambda value: value["id"]):
        strong, possible = [], []
        related_ids = {remap.get(item["id"], item["id"]) for item in candidate["related_events"]}
        for event_id, existing in catalog.items():
            if event_id in related_ids or any(item["id"] == candidate["id"] for item in existing["related_events"]):
                # Explicitly related candidates should not become one event just
                # because they share an organiser page (e.g. conference satellites).
                continue
            strength = match_strength(candidate, existing)
            if strength:
                target_id = survivor(event_id)
                group = strong if strength == "strong" else possible
                if target_id not in group:
                    group.append(target_id)
        selected = choices.get(candidate["id"])
        if selected == "new" and any(catalog[key]["decision"]["status"] in ("rejected", "hidden") for key in strong):
            raise ValueError("A new identity cannot bypass an excluded strong match; restore the canonical record explicitly")
        if selected is not None and selected != "new" and selected not in catalog:
            raise ValueError("Reviewer match points to an unknown event ID")
        if selected and selected != "new":
            selected = survivor(selected)
        if selected and selected != "new":
            target, status = selected, "matched"
        elif selected != "new" and len(strong) == 1:
            target, status = strong[0], "matched"
        elif selected != "new" and (strong or possible):
            target, status = None, "identity_review"
        else:
            target, status = None, "new"
        if target and catalog[target]["decision"]["status"] in ("rejected", "hidden"):
            status = "excluded"
        if status == "identity_review":
            # Do not create a competing authoritative identity for a possible duplicate.
            target = candidate["id"]
        elif target is None:
            target = "event-" + input_hash({"first_candidate": candidate["id"]})[:24]
            if target in catalog:
                raise ValueError("New identity collision")
        remap[candidate["id"]] = target
        routing.append({"candidate_id": candidate["id"], "event_id": target, "status": status,
                        "possible_matches": sorted(set(strong + possible)), "suggestions": []})
        if status == "new":
            provisional = copy.deepcopy(candidate)
            provisional["id"] = target
            provisional["identity_aliases"] = sorted(aliases(candidate))
            catalog[target] = provisional
    working = {}
    for route in routing:
        incoming = copy.deepcopy(next(candidate for candidate in candidates if candidate["id"] == route["candidate_id"]))
        incoming["id"] = route["event_id"]
        relations = []
        for relation in incoming["related_events"]:
            target = remap.get(relation["id"], relation["id"])
            if target == incoming["id"]:
                incoming["warnings"].append("Related proposals resolved to the same event; review the relationship.")
            elif target in set(remap.values()) | set(catalog):
                item = {**relation, "id": target}
                if item not in relations:
                    relations.append(item)
            else:
                incoming["warnings"].append("Unresolved related-event target requires review: " + relation["id"])
        incoming["related_events"] = relations
        event_id, status = route["event_id"], route["status"]
        if status == "excluded":
            route["reason"] = catalog[event_id]["decision"]["reason"]
            route["incoming_facts"] = incoming["facts"]
            continue
        if status == "identity_review":
            route["incoming_record"] = incoming
            continue
        base = working.get(event_id)
        if base is None and status == "matched":
            base = catalog[event_id]
        if base is None:
            merged = incoming
            merged["identity_aliases"] = sorted(aliases(incoming))
            changed = True
        else:
            merged, suggestions, changed = merge_record(base, incoming)
            route["suggestions"] = suggestions
            route["changes"] = fact_changes(base["facts"], merged["facts"])
        route["status"] = "new" if status == "new" else ("update" if changed else (
            "protected_conflict" if route["suggestions"] else "no_change"))
        working[event_id] = merged
    # One suggested record per resolved ID, even for several announcements in a batch.
    for event_id, record in working.items():
        if event_id not in registry:
            next_identities[event_id] = {"record": copy.deepcopy(record)}
    return {"routes": routing, "records": list(working.values()), "identities": next_identities}


def render_plan(result, registry):
    lines = ["# Reconciled event proposals", "", "Local review plan. Canonical records are unchanged; nothing is published.", ""]
    by_id = {record["id"]: record for record in result["records"]}
    for route in result["routes"]:
        record = by_id.get(route["event_id"]) or registry.get(route["event_id"]) or route.get("incoming_record")
        title = record["facts"]["title"] if record else route["event_id"]
        lines.extend(["## " + md(title), "", "- Action: " + md(route["status"]), "- Event ID: " + md(route["event_id"]), ""])
        if route["status"] == "excluded":
            lines.extend(["Existing editorial exclusion is preserved. Only an editor can restore this event.", "Reason: " + md(route["reason"]), ""])
        if route["status"] == "identity_review":
            lines.extend(["Choose whether this is an existing event or a new one; candidate retained for review.",
                          "Possible matches: " + md(", ".join(route["possible_matches"])), ""])
        for suggestion in route["suggestions"]:
            lines.append("- Protected " + md(suggestion["field"]) + ": kept " + md(suggestion["kept"])
                         + "; incoming suggestion " + md(suggestion["suggested"]))
        for change in route.get("changes", []):
            lines.append("- Proposed change " + md(change["field"]) + ": " + md(change["before"])
                         + " → " + md(change["after"]))
        if record and route["status"] not in ("excluded", "identity_review"):
            facts = record["facts"]
            lines.extend(["- Dates: " + md(facts["start_date"] or "Unknown") + " to " + md(facts["end_date"] or "Unknown"),
                          "- Source count: " + str(len(record["sources"])), ""])
            lines.extend("- Review note: " + md(warning) for warning in record["warnings"])
            lines.append("")
    return "\n".join(lines) + "\n"


def prepare(bundle, root=ROOT, records_directory=None, choices=None):
    root = root.resolve()
    records_directory = records_directory or root / "records"
    registry_schema = json.loads((root / "schema/event.schema.json").read_text())
    validator = Draft202012Validator(registry_schema, format_checker=FormatChecker())
    registry = load_registry(records_directory, validator)
    manifest = json.loads((bundle / "manifest.json").read_text())
    candidates = []
    for entry in manifest["candidates"]:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", entry["id"]):
            raise ValueError("Invalid candidate filename")
        candidate = json.loads((bundle / (entry["id"] + ".json")).read_text())
        validate_record(candidate, validator)
        if candidate["id"] != entry["id"] or candidate["decision"]["status"] not in ("pending", "draft"):
            raise ValueError("Proposal identity/editorial state needs review")
        candidates.append(candidate)
    base = root / "local/reconciliation"
    private_directory(base)
    import fcntl
    descriptor = os.open(base / "state.lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        state_path = base / "identities.json"
        state = json.loads(state_path.read_text()) if state_path.exists() else {"schema_version": 1, "identities": {}}
        if state["schema_version"] != 1:
            raise ValueError("Unknown identity ledger version")
        for event_id, value in state["identities"].items():
            validate_record(value["record"], validator)
            if event_id != value["record"]["id"]:
                raise ValueError("Identity ledger mismatch")
            if value["record"]["decision"]["status"] not in ("pending", "draft"):
                raise ValueError("Pending identity ledger cannot hold authoritative editorial decisions")
        plan_hash = input_hash({"candidates": candidates, "registry": registry, "choices": choices or {}, "version": 1})
        destination = base / ("plan-" + plan_hash[:20])
        if destination.exists():
            saved = json.loads((destination / "plan.json").read_text())
            if saved["plan_sha256"] != plan_hash:
                raise ValueError("Existing reconciliation plan differs")
            return destination, saved, True
        result = reconcile_records(candidates, registry, state["identities"], choices)
        for record in result["records"]:
            validate_record(record, validator)
        saved = {"schema_version": 1, "plan_sha256": plan_hash, "source_bundle": str(bundle.resolve()),
                 "registry_directory": str(records_directory.resolve()), "registry_record_count": len(registry),
                 "registry_sha256": input_hash(registry), "eligible_for_export": False, "api_requests": 0,
                 "routes": result["routes"], "record_ids": [record["id"] for record in result["records"]]}
        private_directory(destination)
        for record in result["records"]:
            save_json(destination / (record["id"] + ".json"), record)
        descriptor = os.open(destination / "review.md", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(render_plan(result, registry))
        save_json(state_path, {"schema_version": 1, "identities": result["identities"]})
        save_json(destination / "plan.json", saved)
        return destination, saved, False


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--site", type=Path, required=True, help="Separate website checkout; canonical records are read only")
    parser.add_argument("--matches", type=Path, help="Reviewer choices: JSON mapping candidate IDs to event IDs or 'new'")
    args = parser.parse_args(argv)
    try:
        choices = json.loads(args.matches.read_text()) if args.matches else None
        directory, plan, reused = prepare(args.bundle, args.root, website_records(args.site), choices)
        from collections import Counter
        print(json.dumps({"status": "existing_preserved" if reused else "prepared", "api_requests": 0,
                          "actions": dict(Counter(route["status"] for route in plan["routes"])),
                          "canonical_records": plan["registry_record_count"], "review": str(directory / "review.md")}))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "reconciliation_failed", "failure_type": type(exc).__name__, "api_requests": 0}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
