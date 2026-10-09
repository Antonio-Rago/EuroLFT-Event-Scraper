"""Validate PR proposals and derive publication approval from an actual GitHub merge."""

import copy
import json
from pathlib import Path
import re
import subprocess

from .registry import review_hash, validate_public_record, validator
from .reconcile import validate_record


REPOSITORY = "EuroLFT/eurolft.github.io"


def publishable_candidate(record, root):
    """Check eventual publication without assigning a real editorial decision."""
    preview = copy.deepcopy(record)
    preview["decision"] = {"status": "approved", "reviewed_revision": preview["revision"],
                           "reason": None, "acknowledged_warnings": preview["warnings"][:],
                           "reviewed_content_sha256": review_hash(preview)}
    validate_record(preview, validator(root))
    validate_public_record(preview)


def git(site, *args):
    result = subprocess.run(["git", *args], cwd=site, capture_output=True, text=True)
    if result.returncode:
        raise ValueError("Cannot verify website Git history; fetch the complete history.")
    return result.stdout.strip()


def merged_decisions(records, site, root, client=None):
    """Return approved copies only when their exact Git revision belongs to a merged PR.

    Main's source files retain their proposal state. GitHub's immutable merged PR and
    website Git history are the authoritative review receipt; no bot commit is needed.
    """
    if client is None:
        from .submit import GitHub
        import os
        client = GitHub(os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"))
    site = Path(site).resolve()
    remote = git(site, "remote", "get-url", "origin")
    if remote not in ("https://github.com/" + REPOSITORY + ".git",
                      "https://github.com/" + REPOSITORY,
                      "git@github.com:" + REPOSITORY + ".git"):
        raise ValueError("Merge approvals are valid only for the EuroLFT website repository.")
    if git(site, "status", "--porcelain", "--", "_event_collector/records"):
        raise ValueError("Uncommitted event changes cannot inherit a GitHub merge approval.")
    result = copy.deepcopy(records)
    receipts = {}
    for event_id, record in result.items():
        if record["decision"]["status"] not in ("pending", "draft"):
            continue
        filename = "_event_collector/records/" + event_id + ".json"
        commit = git(site, "log", "-1", "--format=%H", "--", filename)
        if not re.fullmatch(r"[a-f0-9]{40}", commit):
            raise ValueError("No committed GitHub proposal for event " + event_id)
        if commit not in receipts:
            associated = client.request("/commits/" + commit + "/pulls")
            receipts[commit] = []
            for item in associated:
                if item.get("merged_at") and item.get("base", {}).get("ref") == "main":
                    pull = client.request("/pulls/" + str(item["number"]))
                    actor = pull.get("merged_by") or {}
                    base = pull.get("base", {})
                    if (not pull.get("merged") or not pull.get("merged_at")
                            or base.get("ref") != "main" or base.get("repo", {}).get("full_name") != REPOSITORY
                            or actor.get("type") != "User" or not actor.get("login")
                            or not re.fullmatch(r"[a-f0-9]{40}", pull.get("merge_commit_sha") or "")):
                        continue
                    merge = pull["merge_commit_sha"]
                    ancestry = subprocess.run(["git", "merge-base", "--is-ancestor", merge, "HEAD"], cwd=site)
                    if ancestry.returncode == 0:
                        receipts[commit].append(pull)
        matches = []
        for pull in receipts[commit]:
            # The final event blob must equal the record at that PR's merge revision.
            try:
                content = git(site, "show", pull["merge_commit_sha"] + ":" + filename)
                if json.loads(content) == record:
                    matches.append(pull)
            except ValueError:
                continue
        if not matches:
            raise ValueError("Event " + event_id + " has no matching human-merged website PR.")
        pull = max(matches, key=lambda value: value["merged_at"])
        record["decision"] = {"status": "approved", "reviewed_revision": record["revision"],
                              "reason": "Accepted by merging GitHub PR #" + str(pull["number"]),
                              "acknowledged_warnings": record["warnings"][:]}
        record["history"].append({"action": "approve", "actor": pull["merged_by"]["login"],
                                  "at": pull["merged_at"], "revision": record["revision"],
                                  "note": "GitHub merge: " + pull["html_url"]})
        record["decision"]["reviewed_content_sha256"] = review_hash(record)
        validate_record(record, validator(root))
        validate_public_record(record)
    return result
