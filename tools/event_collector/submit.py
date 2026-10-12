"""Submit queued events for GitHub review; each human merge accepts its event."""

import argparse
import base64
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, build_opener

from .check_corpus import input_hash
from .extract import NoRedirect, private_directory, save_json
from .extraction import ROOT
from .github_exclusions import closed_rejections, pages
from .reconcile import load_registry, match_strength
from .registry import validator
from .review_changes import apply_package
from .website import RECORDS_PATH, event_id_from_path, package_record, from_website, to_website, records_directory


REPOSITORY = "EuroLFT/eurolft.github.io"
REMOTE = "https://github.com/" + REPOSITORY + ".git"
BRANCH = "automation/community-events"
API = "https://api.github.com/repos/" + REPOSITORY


def git(*args, cwd=None, input_text=None):
    environment = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    token = environment.get("EVENT_REVIEW_TOKEN") or environment.get("GH_TOKEN")
    if token:
        if not re.fullmatch(r"[\x21-\x7e]+", token):
            raise ValueError("Invalid GitHub credential format.")
        index = int(environment.get("GIT_CONFIG_COUNT", "0"))
        environment["GIT_CONFIG_COUNT"] = str(index + 1)
        environment["GIT_CONFIG_KEY_" + str(index)] = "http." + REMOTE + ".extraHeader"
        authorization = base64.b64encode(("x-access-token:" + token).encode()).decode()
        environment["GIT_CONFIG_VALUE_" + str(index)] = "Authorization: Basic " + authorization
    result = subprocess.run(["git", *args], cwd=cwd, input=input_text, text=True,
                            capture_output=True, env=environment)
    if result.returncode:
        # Git/credential helper stderr can contain private URLs or credentials.
        raise ValueError("Git operation failed ({}); check your GitHub login and network.".format(args[0]))
    return result.stdout.strip()


def github_token():
    for name in ("EVENT_REVIEW_TOKEN", "GH_TOKEN"):
        if os.environ.get(name):
            token = os.environ[name]
            break
    else:
        result = git("credential", "fill", input_text="protocol=https\nhost=github.com\n\n")
        values = dict(line.split("=", 1) for line in result.splitlines() if "=" in line)
        token = values.get("password", "")
    if not token or not re.fullmatch(r"[\x21-\x7e]+", token):
        raise ValueError("Configure a GitHub HTTPS login or EVENT_REVIEW_TOKEN with website contents and PR access.")
    return token


class GitHub:
    def __init__(self, token=None):
        self.token = token

    def request(self, path, data=None, method=None):
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "EuroLFT-event-collector"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        payload = None if data is None else json.dumps(data).encode("utf-8")
        if payload is not None:
            headers["Content-Type"] = "application/json"
        request = Request(API + path, data=payload, headers=headers, method=method)
        try:
            with build_opener(NoRedirect()).open(request, timeout=30) as response:
                if response.status == 204:
                    return None
                raw = response.read(4 * 1024 * 1024 + 1)
                if len(raw) > 4 * 1024 * 1024:
                    raise ValueError("GitHub response exceeds the local limit.")
                return json.loads(raw)
        except HTTPError as exc:
            raise ValueError("GitHub returned HTTP {}; check access or retry later.".format(exc.code)) from None

    def open_pr(self):
        pulls = open_event_prs(self)
        return pulls[0] if pulls else None


def open_event_prs(client):
    """Include legacy proposals and independent event branches, never unrelated PRs."""
    return [pull for pull in pages(client, "/pulls?state=open&base=main&sort=created&direction=asc")
            if (pull.get("head", {}).get("ref") == BRANCH
                or pull.get("head", {}).get("ref", "").startswith(BRANCH + "/")
                or pull.get("head", {}).get("ref", "").startswith(BRANCH + "-event-"))
            and (pull.get("head", {}).get("repo") or {}).get("full_name") == REPOSITORY]


def open_record_prs(client):
    result = {}
    for pull in open_event_prs(client):
        for item in pages(client, "/pulls/" + str(pull["number"]) + "/files"):
            identity = event_id_from_path(item["filename"])
            if identity and item["status"] in ("added", "modified", "renamed"):
                result.setdefault(identity, pull)
    return result


def pending_review(client=None):
    pull = (client or GitHub()).open_pr()
    if pull:
        return {"status": "awaiting_review", "pr": pull["html_url"], "automatic_approval": False}
    return None


def next_package(package, site, root, exclusions=None, event_id=None, batch=False):
    """Keep the saved queue intact and rebase a selected new event onto current main."""
    original = json.loads((package / "manifest.json").read_text())
    records = []
    for identity in original["record_ids"]:
        if not re.fullmatch(r"event-[a-z0-9_-]+", identity):
            raise ValueError("Invalid queued event identity.")
        record = package_record(package, identity)
        if record["id"] != identity:
            raise ValueError("Queued event identity mismatch.")
        records.append(record)
    if input_hash(records) != original["records_sha256"]:
        raise ValueError("Review package content changed; prepare it again.")
    canonical = load_registry(records_directory(site), validator(root))
    exclusions = exclusions or {}
    eligible = []
    for record in records:
        old = canonical.get(record["id"])
        if old == record:
            continue
        if old and original["registry_sha256"] != input_hash(canonical):
            # A saved old queue never overwrites later human corrections.
            continue
        if any(match_strength(record, rejected) == "strong" for rejected in exclusions.values()):
            continue
        eligible.append(record)
    if event_id:
        eligible = [record for record in eligible if record["id"] == event_id]
        if not eligible:
            raise ValueError("Selected event is absent, already accepted or rejected; prepare a fresh update if needed.")
    queued_count = len(eligible)
    chosen = eligible if batch else eligible[:1]
    identity = input_hash({"canonical": canonical, "records": chosen, "review_mode": "jekyll-per-event-v1"})
    destination = root / "local/github-review" / ("next-" + identity[:20])
    private_directory(destination)
    from .review_changes import safe_record
    for record in chosen:
        validator(root).validate(record)
        if safe_record(record) != record:
            raise ValueError("Queued proposal requires privacy cleanup; prepare it again.")
        save_json(destination / "changes" / RECORDS_PATH / (record["id"] + ".json"), to_website(record))
    manifest = {"schema_version": 1, "registry_sha256": input_hash(canonical),
                "record_ids": [record["id"] for record in chosen], "records_sha256": input_hash(chosen),
                "automatic_approval": False, "remote_writes": 0, "queued_count": queued_count}
    save_json(destination / "manifest.json", manifest)
    from .proposals import md
    lines = ["# Event proposal", "", "Review the facts, source links and warnings below.", "",
             "**Accept:** review and merge this PR. The website records that merge as editorial approval.",
             "**Reject:** close this PR without merging. Future ingestion remembers the rejection.",
             "**Correct:** edit the event JSON or request changes before merging. No local approval commands are needed.", ""]
    for record in chosen:
        facts = record["facts"]
        lines.extend(["## " + md(facts["title"]), "", "- Dates: " + md(facts["start_date"]) + " to " + md(facts["end_date"]),
                      "- Location: " + md(", ".join(value for value in facts["location"].values() if value) or "Unknown"),
                      "- Type: " + md(facts["type"]), "- Record: `_data/community_events/" + record["id"] + ".json`", ""])
        lines.extend("- Source: <" + source["url"] + ">" for source in record["sources"] if source["url"])
        lines.extend("- Review: " + md(warning) for warning in record["warnings"])
        for deadline in facts["deadlines"]:
            lines.append("- Deadline: " + md(deadline["label"]) + ": " + md(deadline["date"]))
        lines.append("")
    (destination / "review.md").write_text("\n".join(lines), encoding="utf-8")
    (destination / "review.md").chmod(0o600)
    return destination, manifest


def inspect_package(package, site, root=ROOT):
    """Validate without writing to the operator's website checkout."""
    canonical = records_directory(site)
    private_directory(root / "local/submissions")
    with tempfile.TemporaryDirectory(dir=root / "local/submissions") as directory:
        registry = Path(directory) / "records"
        shutil.copytree(canonical, registry)
        count = apply_package(package, root, registry, allow_pending_relations=True)
    return count


def submission_site(base):
    canonical = base / "current-main"
    if not canonical.exists():
        git("clone", "--quiet", "--depth", "1", "--branch", "main", REMOTE, str(canonical))
    else:
        if git("remote", "get-url", "origin", cwd=canonical) != REMOTE or git("branch", "--show-current", cwd=canonical) != "main":
            raise ValueError("Submission base checkout points to a different repository or branch.")
        if git("status", "--porcelain", cwd=canonical):
            raise ValueError("Submission base checkout has local changes; preserve them before retrying.")
        git("pull", "--ff-only", "origin", "main", cwd=canonical)
    records_directory(canonical, require_ready=True)
    return canonical


def submit(package, root=ROOT, client=None, event_id=None, batch=False):
    package = Path(package).resolve()
    root = Path(root).resolve()
    base = root / "local/submissions"
    private_directory(base)
    descriptor = os.open(base / "submit.lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        client = client or GitHub(github_token())
        old = client.open_pr()
        if old:
            return {"status": "awaiting_review", "pr": old["html_url"], "automatic_approval": False}
        source_package = package
        canonical = submission_site(base)
        exclusions = closed_rejections(client, root)
        package, manifest = next_package(source_package, canonical, root, exclusions, event_id, batch)
        return submit_selected(package, manifest, canonical, root, client, BRANCH, source_package)


def matching_state(state, manifest, branch):
    if branch == BRANCH:
        return state.get("manifest_sha256") == input_hash(manifest)
    return state.get("branch") == branch and state.get("records_sha256") == manifest["records_sha256"]


def verify_saved_proposal(website, state, package, manifest):
    if (git("rev-parse", "HEAD", cwd=website) != state["commit"]
            or git("status", "--porcelain", cwd=website)):
        raise ValueError("Interrupted proposal has local changes; preserve them before retrying.")
    paths = {"_data/community_events/" + identity + ".json" for identity in manifest["record_ids"]}
    base_commit = state.get("base_commit") or git("rev-parse", "HEAD^", cwd=website)
    if set(git("diff", "--name-only", base_commit, state["commit"], cwd=website).splitlines()) != paths:
        raise ValueError("Saved proposal must contain exactly its selected event records.")
    for filename in paths:
        expected = package_record(package, Path(filename).stem)
        actual = from_website(json.loads(git("show", state["commit"] + ":" + filename, cwd=website)))
        if actual != expected:
            raise ValueError("Saved proposal content differs from the selected event.")


def submit_selected(package, manifest, canonical, root, client, branch, source_package):
    base = root / "local/submissions"
    # Recover a successful push followed by a failed PR request without a second push.
    identity = input_hash(manifest) if branch == BRANCH else input_hash({"branch": branch, "records": manifest["records_sha256"], "format": "jekyll-per-event-v1"})
    destination = base / ("batch-" + identity[:20])
    state_path = destination / "submission.json"
    website = destination / "website"
    ref = git("ls-remote", "--heads", REMOTE, "refs/heads/" + branch)
    if ref:
        previous_sha = ref.split()[0]
        query = urlencode({"state": "closed", "head": "EuroLFT:" + branch, "base": "main"})
        resolved = list(pages(client, "/pulls?" + query))
        if any(pull.get("head", {}).get("sha") == previous_sha for pull in resolved):
            # Remove only the exact resolved proposal revision. A concurrent update is preserved.
            git("push", "--force-with-lease=refs/heads/" + branch + ":" + previous_sha,
                "origin", ":refs/heads/" + branch, cwd=canonical)
            ref = ""
    if ref:
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        if ref.split()[0] != state.get("commit") or not matching_state(state, manifest, branch):
            return {"status": "proposal_branch_exists", "branch": branch, "automatic_approval": False}
        # Also refuse a modified retry package.
        inspect_package(package, canonical, root)
        verify_saved_proposal(website, state, package, manifest)
        count = state["record_count"]
    else:
        private_directory(destination)
        # Keep separate canonical and proposal checkouts for validation and recovery.
        count = inspect_package(package, canonical, root)
        if not count:
            return {"status": "no_changes", "record_count": 0, "automatic_approval": False}
        if website.exists():
            state = json.loads(state_path.read_text()) if state_path.exists() else {}
            if (not matching_state(state, manifest, branch)
                    or state.get("commit") != git("rev-parse", "HEAD", cwd=website)
                    or git("status", "--porcelain", cwd=website)):
                raise ValueError("An interrupted submission checkout has local work; inspect it before retrying.")
        else:
            git("clone", "--quiet", "--no-hardlinks", str(canonical), str(website))
            git("remote", "set-url", "origin", REMOTE, cwd=website)
            git("switch", "-c", branch, cwd=website)
            apply_package(package, root, records_directory(website), allow_pending_relations=True)
            paths = ["_data/community_events/" + event_id + ".json" for event_id in manifest["record_ids"]]
            git("add", "--", *paths, cwd=website)
            if set(git("diff", "--cached", "--name-only", cwd=website).splitlines()) != set(paths):
                raise ValueError("Submission must contain exactly the packaged event records.")
            git("diff", "--cached", "--check", cwd=website)
            git("-c", "user.name=EuroLFT event collector", "-c", "user.email=event-collector@users.noreply.github.com",
                "commit", "-m", "Prepare community events for editorial review", cwd=website)
            state = {"commit": git("rev-parse", "HEAD", cwd=website), "record_count": count,
                     "manifest_sha256": input_hash(manifest), "base_site": str(canonical),
                     "branch": branch, "records_sha256": manifest["records_sha256"],
                     "base_commit": git("rev-parse", "HEAD^", cwd=website)}
            save_json(state_path, state)
        verify_saved_proposal(website, state, package, manifest)
        # A concurrent proposal wins: ordinary push refuses to replace a divergent branch.
        git("push", "origin", "HEAD:refs/heads/" + branch, cwd=website)
    body = (package / "review.md").read_text(encoding="utf-8")
    intro = "Human review happens on GitHub. Merging accepts this proposal; closing without merging rejects it.\n\n"
    if len(intro + body) > 65000:
        body = ("Review the event JSON files in this PR for facts, sources and warnings. "
                "The complete readable report is retained in the operator's private review package.\n")
    title = "Review {} community event candidates".format(count)
    if count == 1:
        record = package_record(package, manifest["record_ids"][0])
        title = "Event: " + record["facts"]["title"][:200]
    pull = client.request("/pulls", {"title": title,
                                    "head": branch, "base": "main", "draft": False,
                                    "maintainer_can_modify": True, "body": intro + body})
    save_json(destination / "result.json", {"pr": pull["html_url"], "commit": state["commit"],
                                            "source_package": str(source_package), "record_ids": manifest["record_ids"]})
    return {"status": "proposal_pr_created", "record_count": count, "remaining_queued": manifest["queued_count"] - count, "pr": pull["html_url"],
            "automatic_approval": False}


def submit_all(package, root=ROOT, client=None, on_progress=None):
    """Open every eligible queued event independently; retry without duplicate PRs."""
    package, root = Path(package).resolve(), Path(root).resolve()
    base = root / "local/submissions"
    private_directory(base)
    descriptor = os.open(base / "submit.lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        client = client or GitHub(github_token())
        canonical = submission_site(base)
        exclusions = closed_rejections(client, root)
        selected, manifest = next_package(package, canonical, root, exclusions, batch=True)
        opened = open_record_prs(client)
        report = {"status": "submitting", "record_count": len(manifest["record_ids"]),
                  "created_count": 0, "existing_count": 0, "remaining_queued": 0,
                  "prs": [], "failures": [], "automatic_approval": False}
        pending = []
        # Validate every separate diff before performing any remote write.
        for identity in manifest["record_ids"]:
            if identity in opened:
                report["prs"].append({"event_id": identity, "pr": opened[identity]["html_url"], "status": "already_open"})
                report["existing_count"] += 1
            else:
                single, item_manifest = next_package(selected, canonical, root, event_id=identity)
                inspect_package(single, canonical, root)
                pending.append((identity, single, item_manifest))
        report["remaining_queued"] = len(pending)
        save_json(base / "all-latest.json", report)
        for index, (identity, single, item_manifest) in enumerate(pending):
            if index:
                time.sleep(2)
            branch = BRANCH + "-" + identity
            try:
                # Another operator may have opened this event since the initial inventory.
                query = urlencode({"state": "open", "head": "EuroLFT:" + branch, "base": "main"})
                existing = [pull for pull in client.request("/pulls?" + query)
                            if (pull.get("head", {}).get("repo") or {}).get("full_name") == REPOSITORY]
                if existing:
                    result = {"status": "already_open", "pr": existing[0]["html_url"]}
                    report["existing_count"] += 1
                else:
                    result = submit_selected(single, item_manifest, canonical, root, client, branch, package)
                    if result["status"] != "proposal_pr_created":
                        raise ValueError("Event branch contains unrecognized work; inspect it before retrying.")
                    report["created_count"] += 1
                item = {"event_id": identity, "pr": result["pr"], "status": result["status"]}
                report["prs"].append(item)
                report["remaining_queued"] = len(pending) - index - 1
                if on_progress:
                    on_progress(item)
            except Exception as exc:
                report["status"] = "partial_submission"
                report["remaining_queued"] = len(pending) - index
                report["failures"].append({"event_id": identity, "failure_type": type(exc).__name__,
                                           "reason": str(exc) if type(exc) is ValueError else "Submission interrupted; rerun to resume."})
                break
            finally:
                save_json(base / "all-latest.json", report)
        if report["status"] == "submitting":
            report["status"] = "proposal_prs_created"
        if not report["record_count"]:
            report["status"] = "no_changes"
        save_json(base / "all-latest.json", report)
        return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path, nargs="?")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--dry-run", action="store_true", help="Validate locally without GitHub access or Git changes")
    parser.add_argument("--site", type=Path, help="Canonical website checkout, required for --dry-run")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--all", dest="all_events", action="store_true", help="Open every remaining event as a separate PR")
    mode.add_argument("--event", dest="event_id", help="Choose one saved event ID instead of the next queued event")
    mode.add_argument("--batch", action="store_true", help="Put all queued events in one PR and review the batch together")
    parser.add_argument("--check", action="store_true", help="Report an existing open proposal without collecting or writing")
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args(argv)
    if args.dry_run and args.site is None:
        parser.error("--dry-run requires --site")
    if not args.check and args.package is None:
        parser.error("provide a package directory, or use --check")
    if args.check and (args.all_events or args.batch or args.event_id or args.dry_run):
        parser.error("--check cannot be combined with submission selection or --dry-run")
    try:
        if args.check:
            result = pending_review() or {"status": "no_open_proposal", "automatic_approval": False}
        elif args.dry_run and args.all_events:
            selected, manifest = next_package(args.package, args.site, args.root, batch=True)
            for identity in manifest["record_ids"]:
                single, _ = next_package(selected, args.site, args.root, event_id=identity)
                inspect_package(single, args.site, args.root)
            result = {"status": "validated_locally", "record_count": len(manifest["record_ids"]),
                      "pr_count": len(manifest["record_ids"]), "remote_writes": 0, "automatic_approval": False}
        elif args.dry_run:
            result = {"status": "validated_locally", "record_count": inspect_package(args.package, args.site, args.root),
                      "remote_writes": 0, "automatic_approval": False}
        elif args.all_events:
            result = submit_all(args.package, args.root, on_progress=lambda item: print(json.dumps(item), flush=True))
        else:
            result = submit(args.package, args.root, event_id=args.event_id, batch=args.batch)
        print(json.dumps(result))
        if args.github_output:
            with args.github_output.open("a") as output:
                output.write("blocked=" + ("true" if result["status"] == "awaiting_review" else "false") + "\n")
        return 0 if result["status"] in ("proposal_pr_created", "proposal_prs_created", "validated_locally", "no_changes", "no_open_proposal", "awaiting_review") else 1
    except Exception as exc:
        print(json.dumps({"status": "submission_failed", "failure_type": type(exc).__name__,
                          "reason": str(exc) if type(exc) is ValueError else "Check the local package and network."}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
