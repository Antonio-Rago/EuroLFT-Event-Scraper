"""Collect one archive month plus INSPIRE, reconcile, and optionally submit a draft PR."""

import argparse
import fcntl
import json
import os
from pathlib import Path

from .check_corpus import input_hash
from .collect import Fetcher, collect, recent_month
from .extract import private_directory, save_json
from .extraction import ROOT
from .ingest import archive_month, inputs_for_month, process
from .inspire import Client, discover
from .reconcile import prepare as reconcile
from .review_changes import prepare as changes
from .submit import REMOTE, GitHub, git, github_token, pending_review, submit
from .website import records_directory


def combine(bundles, root=ROOT):
    records = {}
    for bundle in bundles:
        manifest = json.loads((bundle / "manifest.json").read_text())
        for entry in manifest["candidates"]:
            event_id = entry["id"]
            if not isinstance(event_id, str) or not event_id.startswith("proposed-") or any(
                    character not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for character in event_id):
                raise ValueError("Invalid source candidate identity.")
            record = json.loads((bundle / (event_id + ".json")).read_text())
            if record["id"] != event_id or record["decision"]["status"] not in ("pending", "draft"):
                raise ValueError("Weekly ingestion accepts only unapproved source candidates.")
            if event_id in records and records[event_id] != record:
                raise ValueError("Conflicting source candidates have the same identity.")
            records[event_id] = record
    destination = root / "local/review" / ("weekly-" + input_hash(records)[:20])
    private_directory(destination)
    for event_id, record in records.items():
        save_json(destination / (event_id + ".json"), record)
    save_json(destination / "manifest.json", {"schema_version": 1,
              "candidates": [{"id": event_id} for event_id in sorted(records)], "eligible_for_export": False})
    return destination


def canonical_site(root):
    site = root / "local/weekly/website"
    private_directory(site.parent)
    if not site.exists():
        git("clone", "--quiet", "--depth", "1", "--branch", "main", REMOTE, str(site))
    else:
        if git("remote", "get-url", "origin", cwd=site) != REMOTE:
            raise ValueError("Weekly checkout points to a different repository.")
        if git("branch", "--show-current", cwd=site) != "main" or git("status", "--porcelain", cwd=site):
            raise ValueError("Weekly checkout contains local work; preserve it before rerunning.")
        git("pull", "--ff-only", "origin", "main", cwd=site)
    records_directory(site)
    return site


def run(root=ROOT, site=None, month=None, month_window="current", live=False,
        wait=False, offline=False, limit=50, open_pr=False, batch=False):
    root = Path(root).resolve()
    private_directory(root / "local/weekly")
    descriptor = os.open(root / "local/weekly/run.lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, "a") as handle:
        # Concurrent scheduled invocations never spend model quota or upload twice.
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "already_running", "api_requests": 0, "automatic_approval": False}
        if open_pr and not live:
            raise ValueError("--open-pr requires --live; preflight never submits records.")
        if open_pr:
            waiting = pending_review()
            if waiting:
                return {**waiting, "api_requests": 0}
        exclusions = {}
        if open_pr:
            from .github_exclusions import closed_rejections
            exclusions = closed_rejections(GitHub(github_token()), root)
        if site is None:
            if offline:
                raise ValueError("--offline requires an explicit --site; no remote checkout update is allowed.")
            site = canonical_site(root)
        directory = records_directory(site)
        month = month or recent_month(month_window)
        collection = collect([month], root / "local", Fetcher(root / "local/cache", offline=offline), limit, 8)
        bundle, extraction = process(inputs_for_month(root, month, limit), root, live, wait=wait)
        inspire_bundle, inspire = discover(Client(root, offline), root)
        report = {"status": "preflight" if not live else "prepared", "month": month,
                  "collection_status": collection["status"], "extraction_status": extraction.get("status", "preflight"),
                  "inspire_status": inspire["status"], "api_requests": extraction["api_requests"],
                  "automatic_approval": False, "remote_writes": 0}
        if not live:
            report["message_count"] = len(extraction["cases"])
            report["inspire_candidates"] = inspire["candidate_count"]
            if collection["status"] != "success" or inspire["status"] != "success":
                report["status"] = "partial"
            return report
        combined = combine([bundle, inspire_bundle], root)
        plan, routing, _ = reconcile(combined, root, directory, github_exclusions=exclusions)
        package, manifest = changes(plan, root, directory)
        report.update(package=str(package), record_count=len(manifest["record_ids"]),
                      identity_review_count=sum(route["status"] == "identity_review" for route in routing["routes"]))
        complete = collection["status"] == extraction["status"] == inspire["status"] == "success"
        if not complete:
            # Preserve useful partial candidates locally; do not imply full source coverage in a PR.
            report["status"] = "partial"
        elif report["identity_review_count"]:
            report["status"] = "identity_review_required"
        elif open_pr:
            report.update(submit(package, root, batch=batch))
            report["remote_writes"] = int(report["status"] == "proposal_pr_created")
        save_json(root / "local/weekly/latest.json", report)
        return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--site", type=Path, help="Read this canonical checkout; default updates a dedicated private main checkout")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--month", type=archive_month)
    selection.add_argument("--month-window", choices=("current", "previous"), default="current")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--live", action="store_true", help="Permit bounded Gemini extraction; otherwise preflight only")
    parser.add_argument("--wait", action="store_true", help="Pace new Gemini requests, up to the existing daily limit")
    parser.add_argument("--offline", action="store_true", help="Use cached source responses; --live still permits model requests")
    parser.add_argument("--open-pr", action="store_true", help="Submit the next event for GitHub review; merging accepts it")
    parser.add_argument("--batch", action="store_true", help="Review all queued events together in one PR")
    args = parser.parse_args(argv)
    try:
        report = run(**vars(args))
        print(json.dumps(report, indent=2))
        return 0 if report["status"] in ("preflight", "prepared", "proposal_pr_created", "no_changes", "awaiting_review", "already_running") else 1
    except Exception as exc:
        print(json.dumps({"status": "weekly_run_failed", "failure_type": type(exc).__name__,
                          "reason": str(exc) if type(exc) is ValueError else "Inspect private source state or retry after a network failure."}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
