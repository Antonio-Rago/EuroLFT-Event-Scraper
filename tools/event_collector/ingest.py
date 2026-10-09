"""Collect one archive month and prepare production review candidates, without labels."""

import argparse
from datetime import date
import json
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

from .check_corpus import input_hash
from .collect import ARCHIVE, Fetcher, MESSAGE_PATH, archive_url, collect, digest, recent_month
from .credentials import load_key
from .extract import (Quota, call_gemini, extract_one, failure_details, private_directory, save_json, timestamp)
from .extraction import ROOT, MODEL, load_contract, request_body, request_identity
from .prepare_corpus_inputs import sanitize_text
from .proposals import build_drafts, checked_artifact, render_review


def production_input(source):
    # No case labels, held-out splits, or evaluation annotations in routine ingestion.
    return {"source_url": source["url"], "subject": sanitize_text(source["subject"]),
            "body": sanitize_text(source["body"]),
            "links": [url for url in source["links"] if "zoom." not in url.casefold()],
            "posted_at": source["posted_at"]}


def inputs_for_month(root, month, limit=50):
    if not 1 <= limit <= 100:
        raise ValueError("Message limit must be 1..100")
    records = []
    for path in sorted((root / "local/announcements").glob("*.json")):
        source = json.loads(path.read_text())
        if not source["url"].startswith(ARCHIVE + month + "/"):
            continue
        archive_url(source["url"], month)
        if not MESSAGE_PATH.fullmatch(urlsplit(source["url"]).path):
            raise ValueError("Not an archive message")
        content = {key: value for key, value in source.items() if key not in ("content_sha256", "retrieved_at")}
        if digest(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()) != source["content_sha256"]:
            raise ValueError("Collected announcement integrity mismatch")
        if len(records) >= limit:
            raise ValueError("Collected month exceeds extraction limit; raise the limit explicitly")
        records.append(production_input(source))
    return records


def process(inputs, root=ROOT, live=False, transport=None, wait=False):
    registry, schema, prompt = load_contract(root)
    directory = root / "local/extraction"
    quota = Quota(directory, 20)
    key = load_key(root) if live and transport is None else None
    if live and transport is None and not key:
        raise ValueError("Gemini key missing; no API calls made")
    drafts, cases, api_requests = [], [], 0
    for record in inputs:
        identity = request_identity(record, request_body(record, schema, prompt))
        cid = "announcement-" + input_hash(record)[:24]
        if not live:
            cases.append({"case_id": cid, "status": "preflight", "source_url": record["source_url"],
                          "request_sha256": identity, "cached": (directory / "cache" / (identity + ".json")).exists()})
            continue
        # Reuse parsed failures without repeatedly calling the model. Transport failures
        # remain operational items; explicit pilot retry is available separately.
        artifact, issue = None, None
        failure_path = directory / "failures" / (identity + ".json")
        cached = (directory / "cache" / (identity + ".json")).exists()
        if failure_path.exists() and not cached:
            issue = json.loads(failure_path.read_text())
            rejected = issue.get("rejected_proposal")
            if rejected:
                path = Path(rejected)
                if not path.resolve().is_relative_to((directory / "rejected").resolve()):
                    raise ValueError("Rejected extraction outside private storage")
                artifact = checked_artifact(json.loads(path.read_text()))
                if (artifact["input_sha256"] != input_hash(record) or artifact["request_sha256"] != identity
                        or artifact["prompt_sha256"] != input_hash(prompt) or artifact["schema_sha256"] != input_hash(schema)):
                    raise ValueError("Saved extraction contract/input mismatch")
        elif not cached and quota.wait_seconds() > 0 and not wait:
            cases.append({"case_id": cid, "status": "deferred_pacing", "source_url": record["source_url"]})
            continue
        else:
            try:
                if not cached and wait:
                    delay = quota.wait_seconds()
                    if delay:
                        print("Waiting for the local Gemini request interval", flush=True)
                        time.sleep(min(60, delay))
                artifact, reused = extract_one(record, directory, registry, schema, prompt, quota,
                                              transport=transport or (lambda request: call_gemini(request, key=key)))
                api_requests += int(not reused)
            except Exception as exc:
                issue = getattr(exc, "pilot_details", failure_details(exc, "preflight_or_cache"))
                api_requests += int(hasattr(exc, "pilot_details"))
                if issue.get("rejected_proposal"):
                    artifact = checked_artifact(json.loads(Path(issue["rejected_proposal"]).read_text()))
        if artifact is None:
            cases.append({"case_id": cid, "status": "operational_failure", "source_url": record["source_url"],
                          "details": issue})
            continue
        try:
            built, repairs = build_drafts(artifact, record, registry, schema)
            drafts.extend(built)
            cases.append({"case_id": cid, "status": "pending_review" if built else (
                "facts_need_repair" if repairs else "no_event"), "source_url": record["source_url"], "repairs": repairs})
        except Exception as exc:
            cases.append({"case_id": cid, "status": "unreadable_response", "source_url": record["source_url"],
                          "details": failure_details(exc, "review_preparation")})
    report = {"schema_version": 1, "mode": "live" if live else "preflight", "api_requests": api_requests,
              "candidate_count": len(drafts), "eligible_for_export": False, "cases": cases,
              "candidates": [{"id": draft["record"]["id"]} for draft in drafts]}
    if not live:
        return None, report
    # Stable content identity, independent of the number of API calls or today's timestamp.
    version = input_hash({"inputs": inputs, "cases": cases, "records": [d["record"] for d in drafts]})
    destination = root / "local/review" / ("ingest-" + version[:20])
    if destination.exists():
        saved = json.loads((destination / "manifest.json").read_text())
        return destination, {**saved, "api_requests": api_requests}
    private_directory(destination)
    for draft in drafts:
        save_json(destination / (draft["record"]["id"] + ".json"), draft["record"])
        save_json(destination / (draft["record"]["id"] + ".review.json"),
                  {key: value for key, value in draft.items() if key != "record"})
    (destination / "review.md").write_text(render_review(drafts, cases, destination), encoding="utf-8")
    (destination / "review.md").chmod(0o600)
    report["status"] = "partial" if any(case["status"] not in ("pending_review", "no_event") for case in cases) else "success"
    save_json(destination / "manifest.json", report)
    return destination, report


def archive_month(value):
    try:
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}", value):
            raise ValueError
        date.fromisoformat(value + "-01")
    except ValueError:
        raise argparse.ArgumentTypeError("Use one valid archive month in YYYY-MM format")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--month-window", choices=("current", "previous"), default="current")
    selection.add_argument("--month", type=archive_month, help="One explicit archive month, YYYY-MM")
    parser.add_argument("--offline", action="store_true", help="Collect using saved HTTP responses")
    parser.add_argument("--skip-collection", action="store_true", help="Use already collected messages for the selected month")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--live", action="store_true", help="Permit bounded Gemini calls; otherwise preflight only")
    parser.add_argument("--wait", action="store_true", help="Pace a multi-message batch instead of deferring later messages")
    args = parser.parse_args(argv)
    try:
        month = args.month if args.month is not None else recent_month(args.month_window)
        collection = None
        if not args.skip_collection:
            collection = collect([month], args.root / "local", Fetcher(args.root / "local/cache", offline=args.offline),
                                 args.limit, 8)
        destination, report = process(inputs_for_month(args.root, month, args.limit), args.root, args.live, wait=args.wait)
        report["month"] = month
        if collection:
            report["collection_status"] = collection["status"]
        report["review"] = str(destination / "review.md") if destination else None
        print(json.dumps(report, indent=2))
        return 1 if report.get("status") == "partial" or (collection and collection["status"] != "success") else 0
    except Exception as exc:
        print(json.dumps({"status": "ingestion_failed", "failure_type": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
