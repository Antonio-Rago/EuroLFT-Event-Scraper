"""Submit the next event as a website PR; human merge is its editorial acceptance."""

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
from .website import records_directory


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

    def request(self, path, data=None):
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "EuroLFT-event-collector"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        payload = None if data is None else json.dumps(data).encode("utf-8")
        if payload is not None:
            headers["Content-Type"] = "application/json"
        request = Request(API + path, data=payload, headers=headers)
        try:
            with build_opener(NoRedirect()).open(request, timeout=30) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
                if len(raw) > 4 * 1024 * 1024:
                    raise ValueError("GitHub response exceeds the local limit.")
                return json.loads(raw)
        except HTTPError as exc:
            raise ValueError("GitHub returned HTTP {}; check access or retry later.".format(exc.code)) from None

    def open_pr(self):
        params = urlencode({"state": "open", "head": "EuroLFT:" + BRANCH, "base": "main"})
        pulls = self.request("/pulls?" + params)
        return pulls[0] if pulls else None


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
        record = json.loads((package / "changes/_event_collector/records" / (identity + ".json")).read_text())
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
    identity = input_hash({"canonical": canonical, "records": chosen, "review_mode": "github-merge"})
    destination = root / "local/github-review" / ("next-" + identity[:20])
    private_directory(destination)
    from .review_changes import safe_record
    for record in chosen:
        validator(root).validate(record)
        if safe_record(record) != record:
            raise ValueError("Queued proposal requires privacy cleanup; prepare it again.")
        save_json(destination / "changes/_event_collector/records" / (record["id"] + ".json"), record)
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
                      "- Type: " + md(facts["type"]), "- Record: `_event_collector/records/" + record["id"] + ".json`", ""])
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
        canonical = base / "current-main"
        if not canonical.exists():
            git("clone", "--quiet", "--depth", "1", "--branch", "main", REMOTE, str(canonical))
        else:
            if git("status", "--porcelain", cwd=canonical):
                raise ValueError("Submission base checkout has local changes; preserve them before retrying.")
            git("pull", "--ff-only", "origin", "main", cwd=canonical)
        exclusions = closed_rejections(client, root)
        package, manifest = next_package(source_package, canonical, root, exclusions, event_id, batch)
        # Recover a successful push followed by a failed PR request without a second push.
        destination = base / ("batch-" + input_hash(manifest)[:20])
        state_path = destination / "submission.json"
        website = destination / "website"
        ref = git("ls-remote", "--heads", REMOTE, "refs/heads/" + BRANCH)
        if ref:
            previous_sha = ref.split()[0]
            query = urlencode({"state": "closed", "head": "EuroLFT:" + BRANCH, "base": "main"})
            resolved = list(pages(client, "/pulls?" + query))
            if any(pull.get("head", {}).get("sha") == previous_sha for pull in resolved):
                # Remove only the exact resolved proposal revision. A concurrent update is preserved.
                git("push", "--force-with-lease=refs/heads/" + BRANCH + ":" + previous_sha,
                    "origin", ":refs/heads/" + BRANCH, cwd=canonical)
                ref = ""
        if ref:
            state = json.loads(state_path.read_text()) if state_path.exists() else {}
            if ref.split()[0] != state.get("commit") or state.get("manifest_sha256") != input_hash(manifest):
                return {"status": "proposal_branch_exists", "branch": BRANCH, "automatic_approval": False}
            # Also refuse a modified retry package.
            inspect_package(package, state["base_site"], root)
            count = state["record_count"]
        else:
            private_directory(destination)
            # Keep separate canonical and proposal checkouts for validation and recovery.
            count = inspect_package(package, canonical, root)
            if not count:
                return {"status": "no_changes", "record_count": 0, "automatic_approval": False}
            if website.exists():
                state = json.loads(state_path.read_text()) if state_path.exists() else {}
                if (state.get("manifest_sha256") != input_hash(manifest)
                        or state.get("commit") != git("rev-parse", "HEAD", cwd=website)
                        or git("status", "--porcelain", cwd=website)):
                    raise ValueError("An interrupted submission checkout has local work; inspect it before retrying.")
            else:
                git("clone", "--quiet", "--no-hardlinks", str(canonical), str(website))
                git("remote", "set-url", "origin", REMOTE, cwd=website)
                git("switch", "-c", BRANCH, cwd=website)
                apply_package(package, root, records_directory(website), allow_pending_relations=True)
                paths = ["_event_collector/records/" + event_id + ".json" for event_id in manifest["record_ids"]]
                git("add", "--", *paths, cwd=website)
                if set(git("diff", "--cached", "--name-only", cwd=website).splitlines()) != set(paths):
                    raise ValueError("Submission must contain exactly the packaged event records.")
                git("diff", "--cached", "--check", cwd=website)
                git("-c", "user.name=EuroLFT event collector", "-c", "user.email=event-collector@users.noreply.github.com",
                    "commit", "-m", "Prepare community events for editorial review", cwd=website)
                state = {"commit": git("rev-parse", "HEAD", cwd=website), "record_count": count,
                         "manifest_sha256": input_hash(manifest), "base_site": str(canonical)}
                save_json(state_path, state)
            # A concurrent proposal wins: ordinary push refuses to replace a divergent branch.
            git("push", "origin", "HEAD:refs/heads/" + BRANCH, cwd=website)
        body = (package / "review.md").read_text(encoding="utf-8")
        intro = "Human review happens on GitHub. Merging accepts this proposal; closing without merging rejects it.\n\n"
        if len(intro + body) > 65000:
            body = ("Review the event JSON files in this PR for facts, sources and warnings. "
                    "The complete readable report is retained in the operator's private review package.\n")
        title = "Review {} community event candidates".format(count)
        if count == 1:
            record = json.loads((package / "changes/_event_collector/records" / (manifest["record_ids"][0] + ".json")).read_text())
            title = "Event: " + record["facts"]["title"][:200]
        pull = client.request("/pulls", {"title": title,
                                        "head": BRANCH, "base": "main", "draft": False,
                                        "maintainer_can_modify": True, "body": intro + body})
        save_json(destination / "result.json", {"pr": pull["html_url"], "commit": state["commit"],
                                                "source_package": str(source_package), "record_ids": manifest["record_ids"]})
        return {"status": "proposal_pr_created", "record_count": count, "remaining_queued": manifest["queued_count"] - count, "pr": pull["html_url"],
                "automatic_approval": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path, nargs="?")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--dry-run", action="store_true", help="Validate locally without GitHub access or Git changes")
    parser.add_argument("--site", type=Path, help="Canonical website checkout, required for --dry-run")
    parser.add_argument("--event", dest="event_id", help="Choose one saved event ID instead of the next queued event")
    parser.add_argument("--batch", action="store_true", help="Put all queued events in one PR and review the batch together")
    parser.add_argument("--check", action="store_true", help="Report an existing open proposal without collecting or writing")
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args(argv)
    if args.dry_run and args.site is None:
        parser.error("--dry-run requires --site")
    if not args.check and args.package is None:
        parser.error("provide a package directory, or use --check")
    try:
        if args.check:
            result = pending_review() or {"status": "no_open_proposal", "automatic_approval": False}
        elif args.dry_run:
            result = {"status": "validated_locally", "record_count": inspect_package(args.package, args.site, args.root),
                      "remote_writes": 0, "automatic_approval": False}
        else:
            result = submit(args.package, args.root, event_id=args.event_id, batch=args.batch)
        print(json.dumps(result))
        if args.github_output:
            with args.github_output.open("a") as output:
                output.write("blocked=" + ("true" if result["status"] == "awaiting_review" else "false") + "\n")
        return 0 if result["status"] in ("proposal_pr_created", "validated_locally", "no_changes", "no_open_proposal", "awaiting_review") else 1
    except Exception as exc:
        print(json.dumps({"status": "submission_failed", "failure_type": type(exc).__name__,
                          "reason": str(exc) if type(exc) is ValueError else "Check the local package and network."}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
