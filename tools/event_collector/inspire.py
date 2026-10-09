"""Bounded INSPIRE conference discovery into pending, shared review bundles."""

import argparse
from datetime import date
import json
from pathlib import Path
import re
import time
from urllib.parse import urlencode
from urllib.request import Request, build_opener

from .check_corpus import input_hash
from .extract import NoRedirect, private_directory, save_json, timestamp
from .extraction import ROOT
from .proposals import md
from .reconcile import validate_record
from .registry import public_url, validator

ENDPOINT = "https://inspirehep.net/api/conferences"


class Client:
    def __init__(self, root=ROOT, offline=False):
        self.directory, self.offline = root / "local/inspire/cache", offline
        self.last_request = 0
        self.requests = 0
        self.opener = build_opener(NoRedirect())

    def get(self, params):
        url = ENDPOINT + "?" + urlencode(params)
        path = self.directory / (input_hash(url) + ".json")
        if self.offline:
            saved = json.loads(path.read_text())
            if saved["url"] != url or input_hash(saved["response"]) != saved["response_sha256"]:
                raise ValueError("INSPIRE cache integrity mismatch")
            return saved["response"]
        time.sleep(max(0, 1 - (time.monotonic() - self.last_request)))
        self.last_request = time.monotonic()
        self.requests += 1
        request = Request(url, headers={"Accept": "application/json", "User-Agent": "EuroLFT-event-collector/1.0"})
        with self.opener.open(request, timeout=30) as response:
            data = response.read(2 * 1024 * 1024 + 1)
            if len(data) > 2 * 1024 * 1024:
                raise ValueError("INSPIRE response size limit")
        result = json.loads(data)
        save_json(path, {"url": url, "retrieved_at": timestamp(), "response_sha256": input_hash(result), "response": result})
        return result


def blank_facts():
    return {"title": "", "type": "unknown", "start_date": None, "end_date": None,
            "start_time": None, "end_time": None, "timezone": None,
            "location": {"venue": None, "city": None, "country": None}, "attendance": "unknown",
            "official_url": None, "summary": None, "deadlines": [], "lifecycle": "unknown"}


def map_record(hit, retrieved_at=None):
    event_id = str(hit["id"])
    if not re.fullmatch(r"[0-9]+", event_id):
        raise ValueError("Invalid INSPIRE identity")
    metadata = hit["metadata"]
    facts, evidence = blank_facts(), []
    facts["title"] = metadata["titles"][0]["title"].strip()
    if not facts["title"]:
        raise ValueError("Missing INSPIRE title")
    warnings = ["Human review required for INSPIRE metadata and lattice relevance.",
                "Event type inferred from title/catalogue; confirm before approval."]
    title = facts["title"].casefold()
    facts["type"] = "school" if "school" in title else ("workshop" if "workshop" in title else "conference")
    facts["lifecycle"] = "scheduled"
    source_id = "inspire-" + event_id
    def fact(pointer, value):
        if value is not None:
            evidence.append({"field": pointer, "source_id": source_id, "excerpt": str(value)[:500]})
    fact("/facts/title", facts["title"])
    for target, key in (("start_date", "opening_date"), ("end_date", "closing_date")):
        value = metadata.get(key)
        if value:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ValueError("Incomplete INSPIRE calendar date needs repair")
            date.fromisoformat(value)
            facts[target] = value
            fact("/facts/" + target, value)
    addresses = metadata.get("addresses", [])
    if len(addresses) == 1:
        address = addresses[0]
        cities = address.get("cities", [])
        if len(cities) == 1:
            facts["location"]["city"] = cities[0]
        facts["location"]["country"] = address.get("country")
        for key, value in facts["location"].items():
            fact("/facts/location/" + key, value)
    elif addresses:
        warnings.append("Multiple catalogue locations: choose the event location manually.")
    urls = []
    for entry in metadata.get("urls", []):
        try:
            public_url(entry["value"])
            urls.append(entry["value"])
        except (ValueError, KeyError):
            warnings.append("Invalid organiser URL omitted; inspect the catalogue record.")
    if len(urls) == 1:
        facts["official_url"] = urls[0]
        fact("/facts/official_url", urls[0])
    elif urls:
        warnings.append("Several catalogue URLs: select an organiser link manually.")
    if "lattice" not in title:
        warnings.append("Broad Theory-HEP candidate: relevance requires human review.")
    record = {"schema_version": 1, "id": "proposed-inspire-" + event_id,
              "revision": 1, "origin": "collected", "facts": facts,
              "sources": [{"id": source_id, "kind": "announcement",
                           "url": "https://inspirehep.net/conferences/" + event_id,
                           "posted_at": None, "retrieved_at": retrieved_at,
                           "note": "Structured INSPIRE catalogue metadata; updated " + str(hit.get("updated", "unknown"))}],
              "evidence": evidence, "related_events": [], "identity_aliases": ["inspire:" + event_id],
              "warnings": list(dict.fromkeys(warnings)), "editor_overrides": [],
              "decision": {"status": "pending", "reviewed_revision": None, "reason": None, "acknowledged_warnings": []},
              "history": []}
    validate_record(record, validator())
    return record


def discover(client, root=ROOT, query=None, page_size=25, max_pages=6, max_records=100, subject="theory-hep"):
    if not 1 <= page_size <= 100 or not 1 <= max_pages <= 20 or not 1 <= max_records <= 500:
        raise ValueError("Invalid INSPIRE collection bounds")
    if subject not in ("theory-hep", "lattice"):
        raise ValueError("Unsupported INSPIRE subject")
    params = {"sort": "dateasc", "size": page_size, "start_date": "upcoming", "subject": subject}
    if query:
        params["q"] = query
    records, failures, seen = [], [], set()
    snapshots = []
    total, incomplete = None, False
    for page in range(1, max_pages + 1):
        try:
            response = client.get({**params, "page": page})
            value = response["hits"]["total"]
            current_total = value["value"] if isinstance(value, dict) else value
            if type(current_total) is not int or current_total < 0:
                raise ValueError("Invalid INSPIRE count")
            if total is not None and total != current_total:
                raise ValueError("INSPIRE count changed during collection")
            total = current_total
            hits = response["hits"]["hits"]
            if not isinstance(hits, list) or len(hits) > page_size:
                raise ValueError("Invalid INSPIRE page")
            snapshots.append(input_hash(response))
        except Exception as exc:
            failures.append({"page": page, "failure_type": type(exc).__name__})
            incomplete = True
            break
        for hit in hits:
            try:
                recid = str(hit["id"])
                if recid in seen:
                    raise ValueError("Repeated INSPIRE ID across pages; rescan")
                if len(seen) >= max_records:
                    incomplete = True
                    break
                seen.add(recid)
                records.append(map_record(hit))
            except Exception as exc:
                failures.append({"record_id": str(hit.get("id", "unknown")), "failure_type": type(exc).__name__,
                                 "status": "needs_repair", "source_url": "https://inspirehep.net/conferences/" + str(hit.get("id", ""))})
        if len(seen) >= total or len(hits) < page_size or incomplete:
            incomplete = incomplete or len(seen) < total
            break
    else:
        incomplete = total is None or len(seen) < total
    version = input_hash({"records": records, "query": params, "snapshots": snapshots, "failures": failures,
                          "incomplete": incomplete})
    destination = root / "local/review" / ("inspire-" + version[:20])
    report = {"schema_version": 1, "source": "inspire", "query": params, "source_total": total,
              "status": "partial" if incomplete or failures else "success", "incomplete": incomplete,
              "candidate_count": len(records), "failures": failures, "api_requests": 0,
              "network_requests": getattr(client, "requests", 0), "eligible_for_export": False,
              "candidates": [{"id": record["id"]} for record in records]}
    if destination.exists():
        saved = json.loads((destination / "manifest.json").read_text())
        return destination, {**saved, "network_requests": getattr(client, "requests", 0)}
    private_directory(destination)
    for record in records:
        save_json(destination / (record["id"] + ".json"), record)
    lines = ["# INSPIRE candidates", "", "All candidates require human relevance and fact review. Nothing is approved.", ""]
    for record in records:
        facts = record["facts"]
        lines.extend(["## " + md(facts["title"]), "", md(facts["start_date"]) + " to " + md(facts["end_date"]),
                      "", "Source: <" + record["sources"][0]["url"] + ">", ""])
        lines.extend("- " + md(warning) for warning in record["warnings"])
        lines.append("")
    lines.extend(["## Collection status", "", md(report["status"]) + "; discovered " + str(len(seen))
                  + " of " + str(total) + ". Repair items: " + str(len(failures)) + ".", ""])
    (destination / "review.md").write_text("\n".join(lines), encoding="utf-8")
    (destination / "review.md").chmod(0o600)
    save_json(destination / "manifest.json", report)
    return destination, report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--query", help="Optional query, e.g. lattice; default keeps the broad Theory-HEP selection")
    parser.add_argument("--subject", choices=("theory-hep", "lattice"), default="theory-hep")
    parser.add_argument("--page-size", type=int, default=25)
    parser.add_argument("--max-pages", type=int, default=6)
    parser.add_argument("--max-records", type=int, default=100)
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args(argv)
    try:
        destination, report = discover(Client(args.root, args.offline), args.root, args.query,
                                       args.page_size, args.max_pages, args.max_records, args.subject)
        print(json.dumps({key: report[key] for key in ("status", "candidate_count", "source_total", "network_requests")}
                         | {"review": str(destination / "review.md")}))
        return 0 if report["status"] == "success" else 1
    except Exception as exc:
        print(json.dumps({"status": "inspire_failed", "failure_type": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
