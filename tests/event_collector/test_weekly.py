"""Run the weekly handoff against private fixtures and a local Git remote."""

import copy
from contextlib import ExitStack, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
from unittest.mock import patch

from test_workflow import TemporaryRoot, candidate
from tools.event_collector.extract import save_json
from tools.event_collector.moderate import decide
from tools.event_collector.reconcile import prepare as reconcile, reconcile_records
from tools.event_collector.review_changes import prepare as changes
from tools.event_collector.submit import BRANCH, git, inspect_package, main as submit_main, submit
from tools.event_collector.weekly import combine, main as weekly_main, run


class FakeGitHub:
    def __init__(self):
        self.pull = None
        self.requests = []
        self.fail = False

    def open_pr(self):
        return self.pull

    def request(self, path, data=None):
        if data is None:
            return []
        self.requests.append((path, data))
        if self.fail:
            raise OSError("Simulated GitHub outage after push")
        self.pull = {"html_url": "https://github.com/EuroLFT/eurolft.github.io/pull/999"}
        return self.pull


class WebsiteFixture(TemporaryRoot):
    def setUp(self):
        super().setUp()
        self.site = self.root / "site"
        self.records = self.site / "_data/community_events"
        self.records.mkdir(parents=True)
        (self.site / "_config.yml").write_text("title: Fixture site\ncommunity_event_data: per_event\n")
        (self.records / "README.md").write_text("Editorial state\n")

    def bundle(self, name, record):
        directory = self.root / "local" / name
        save_json(directory / (record["id"] + ".json"), record)
        save_json(directory / "manifest.json", {"candidates": [{"id": record["id"]}]})
        return directory

    def package(self):
        plan, _, _ = reconcile(self.bundle("input", candidate("proposed-test")), self.root, self.records)
        return changes(plan, self.root, self.records)[0]

    def local_remote(self):
        for args in (("init", "-q", "-b", "main"), ("add", "."),
                     ("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                      "commit", "-qm", "Website fixture")):
            git(*args, cwd=self.site)
        remote = self.root / "remote.git"
        git("clone", "--bare", str(self.site), str(remote))
        return remote


class WeeklyTests(WebsiteFixture):
    def test_dry_run_validates_without_git_credentials_or_changes(self):
        package = self.package()
        before = list(self.records.glob("*.json"))
        with patch("tools.event_collector.submit.github_token") as credentials, \
                patch("tools.event_collector.submit.git") as operation, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(submit_main([str(package), "--root", str(self.root), "--site", str(self.site), "--dry-run"]), 0)
        self.assertEqual(json.loads(output.getvalue())["record_count"], 1)
        self.assertEqual(list(self.records.glob("*.json")), before)
        credentials.assert_not_called()
        operation.assert_not_called()

    def test_tampered_package_is_refused(self):
        package = self.package()
        path = next((package / "changes/_data/community_events").glob("*.json"))
        record = json.loads(path.read_text())
        record["title"] = "Tampered record"
        save_json(path, record)
        with self.assertRaisesRegex(ValueError, "content changed"):
            inspect_package(package, self.site, self.root)

    def test_real_git_submission_commits_only_records_and_opens_a_draft(self):
        package = self.package()
        remote = self.local_remote()
        github = FakeGitHub()
        with patch("tools.event_collector.submit.REMOTE", str(remote)):
            result = submit(package, self.root, github)
            repeated = submit(package, self.root, github)
        self.assertEqual(result["status"], "proposal_pr_created")
        self.assertEqual(repeated["status"], "awaiting_review")
        self.assertEqual(len(github.requests), 1)
        payload = github.requests[0][1]
        self.assertFalse(payload["draft"])
        self.assertEqual(payload["base"], "main")
        self.assertEqual(payload["head"], BRANCH)
        paths = git("--git-dir=" + str(remote), "diff", "--name-only", "main", BRANCH).splitlines()
        self.assertEqual(len(paths), 1)
        self.assertTrue(paths[0].startswith("_data/community_events/event-"))
        record = json.loads(git("--git-dir=" + str(remote), "show", BRANCH + ":" + paths[0]))
        self.assertEqual(record["decision"]["status"], "pending")
        self.assertFalse(list(self.records.glob("*.json")))

    def test_push_then_api_failure_can_resume_without_replacing_branch(self):
        package = self.package()
        remote = self.local_remote()
        github = FakeGitHub()
        github.fail = True
        with patch("tools.event_collector.submit.REMOTE", str(remote)):
            with self.assertRaises(OSError):
                submit(package, self.root, github)
            original = git("--git-dir=" + str(remote), "rev-parse", BRANCH)
            github.fail = False
            result = submit(package, self.root, github)
            self.assertEqual(original, git("--git-dir=" + str(remote), "rev-parse", BRANCH))
        self.assertEqual(result["status"], "proposal_pr_created")

    def test_push_network_failure_can_resume_the_saved_commit(self):
        package = self.package()
        remote = self.local_remote()
        github = FakeGitHub()
        def fail_push(*args, **kwargs):
            if args[0] == "push":
                raise ValueError("Simulated network failure")
            return git(*args, **kwargs)
        with patch("tools.event_collector.submit.REMOTE", str(remote)):
            with patch("tools.event_collector.submit.git", side_effect=fail_push), self.assertRaises(ValueError):
                submit(package, self.root, github)
            result = submit(package, self.root, github)
        self.assertEqual(result["status"], "proposal_pr_created")

    def test_unrelated_existing_proposal_branch_is_preserved(self):
        package = self.package()
        remote = self.local_remote()
        git("--git-dir=" + str(remote), "branch", BRANCH, "main")
        github = FakeGitHub()
        before = git("--git-dir=" + str(remote), "rev-parse", BRANCH)
        with patch("tools.event_collector.submit.REMOTE", str(remote)):
            result = submit(package, self.root, github)
        self.assertEqual(result["status"], "proposal_branch_exists")
        self.assertEqual(before, git("--git-dir=" + str(remote), "rev-parse", BRANCH))
        self.assertFalse(github.requests)

    def test_pending_ledger_is_not_mistaken_for_website_submission(self):
        record = candidate("proposed-test")
        first = reconcile_records([record], {}, {})
        again = reconcile_records([record], {}, first["identities"])
        self.assertEqual(first["records"][0]["id"], again["records"][0]["id"])
        self.assertEqual(again["routes"][0]["status"], "new")
        canonical = {again["records"][0]["id"]: again["records"][0]}
        third = reconcile_records([record], canonical, again["identities"])
        self.assertEqual(third["routes"][0]["status"], "no_change")

    def fixtures(self, extraction_status="success", collection_status="success", inspire_status="success", cases=None):
        mail = candidate("proposed-mail")
        inspire = copy.deepcopy(mail)
        inspire["id"] = "proposed-inspire-123"
        inspire["identity_aliases"].append("inspire:123")
        inspire["sources"][0]["url"] = "https://inspirehep.net/conferences/123"
        mail_bundle = self.bundle("mail", mail)
        inspire_bundle = self.bundle("inspire", inspire)
        stack = ExitStack()
        stack.enter_context(patch("tools.event_collector.weekly.collect", return_value={"status": collection_status}))
        stack.enter_context(patch("tools.event_collector.weekly.inputs_for_month", return_value=[]))
        stack.enter_context(patch("tools.event_collector.weekly.process", return_value=(mail_bundle,
                                  {"status": extraction_status, "api_requests": 0, "cases": cases or []})))
        stack.enter_context(patch("tools.event_collector.weekly.discover", return_value=(inspire_bundle,
                                  {"status": inspire_status, "candidate_count": 1})))
        return stack

    def test_two_sources_are_deduplicated_together_and_no_decision_is_approved(self):
        with self.fixtures():
            result = run(self.root, self.site, "2026-09", live=True)
        self.assertEqual(result["record_count"], 1)
        package = Path(result["package"])
        record = json.loads(next((package / "changes/_data/community_events").glob("*.json")).read_text())
        self.assertEqual(record["decision"]["status"], "pending")
        self.assertEqual(len(record["sources"]), 2)
        self.assertFalse(list(self.records.glob("*.json")))

    def test_partial_source_coverage_submits_valid_events_and_remains_visible(self):
        issue = {"case_id": "announcement-needs-repair", "status": "facts_need_repair",
                 "source_url": "https://example.org/announcement", "repairs": [{"failure_type": "ValueError"}]}
        for collection, extraction, inspire in (("partial", "success", "success"),
                ("success", "partial", "success"), ("success", "success", "partial")):
            with self.fixtures(extraction, collection, inspire, [issue] if extraction == "partial" else []), \
                    patch("tools.event_collector.weekly.pending_review", return_value=None), \
                    patch("tools.event_collector.weekly.github_token", return_value="fixture-token"), \
                    patch("tools.event_collector.github_exclusions.closed_rejections", return_value={}), \
                    patch("tools.event_collector.weekly.submit", return_value={"status": "proposal_pr_created", "pr": "fixture"}) as submission:
                result = run(self.root, self.site, "2026-09", live=True, open_pr=True)
            self.assertEqual(result["status"], "partial")
            submission.assert_called_once()
            self.assertEqual(result["submission_status"], "proposal_pr_created")
            self.assertEqual(result["remote_writes"], 1)
            self.assertFalse(result["coverage_complete"])
            if extraction == "partial":
                self.assertEqual(result["extraction_issues"], [issue])
            self.assertTrue(Path(result["package"]).exists())

    def test_partial_bulk_run_keeps_pr_links_and_counts(self):
        issue = {"case_id": "mixed-announcement", "status": "pending_review",
                 "repairs": [{"local_id": "e2", "failure_type": "ValueError"}]}
        with self.fixtures(extraction_status="partial", cases=[issue]), \
                patch("tools.event_collector.weekly.github_token", return_value="fixture-token"), \
                patch("tools.event_collector.github_exclusions.closed_rejections", return_value={}), \
                patch("tools.event_collector.weekly.submit_all", return_value={"status": "proposal_prs_created",
                    "created_count": 1, "prs": [{"pr": "fixture"}]}) as submission:
            result = run(self.root, self.site, "2026-09", live=True, open_pr=True, all_events=True)
        submission.assert_called_once()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["submission_status"], "proposal_prs_created")
        self.assertEqual(result["remote_writes"], 1)
        self.assertEqual(result["prs"], [{"pr": "fixture"}])
        self.assertEqual(result["extraction_issues"], [issue])

    def test_uncertain_identity_is_held_without_blocking_independent_records(self):
        uncertain = candidate("proposed-uncertain")
        uncertain["facts"].update(title="An ambiguous lattice workshop 2027", official_url=None)
        for identity in ("event-duplicate-a", "event-duplicate-b"):
            existing = copy.deepcopy(uncertain)
            existing["id"] = identity
            save_json(self.records / (identity + ".json"), existing)
        def submit_resolved(package, root):
            manifest = json.loads((package / "manifest.json").read_text())
            self.assertEqual(len(manifest["record_ids"]), 1)
            for path in (package / "changes/_data/community_events").glob("*.json"):
                self.assertNotEqual(json.loads(path.read_text())["title"], uncertain["facts"]["title"])
            return {"status": "proposal_prs_created", "created_count": 1, "prs": [{"pr": "fixture"}]}
        for coverage in ("success", "partial"):
            with self.subTest(coverage=coverage), self.fixtures(extraction_status=coverage), \
                    patch("tools.event_collector.weekly.github_token", return_value="fixture-token"), \
                    patch("tools.event_collector.github_exclusions.closed_rejections", return_value={}), \
                    patch("tools.event_collector.weekly.submit_all", side_effect=submit_resolved) as submission:
                directory = self.root / "local/mail"
                save_json(directory / (uncertain["id"] + ".json"), uncertain)
                manifest = json.loads((directory / "manifest.json").read_text())
                manifest["candidates"].append({"id": uncertain["id"]})
                save_json(directory / "manifest.json", manifest)
                result = run(self.root, self.site, "2026-09", live=True, open_pr=True, all_events=True)
            submission.assert_called_once()
            self.assertEqual(result["identity_review_count"], 1)
            self.assertEqual(result["status"], "identity_review_required")
            self.assertEqual(result["submission_status"], "proposal_prs_created")
            self.assertEqual(result["coverage_complete"], coverage == "success")
            self.assertEqual(result["remote_writes"], 1)
            self.assertEqual(result["identity_issues"][0]["title"], uncertain["facts"]["title"])
            self.assertTrue(Path(result["identity_review"]).is_file())

    def test_partial_preparation_without_open_pr_does_not_submit(self):
        with self.fixtures(extraction_status="partial"), \
                patch("tools.event_collector.weekly.submit") as submission, \
                patch("tools.event_collector.weekly.submit_all") as bulk:
            result = run(self.root, self.site, "2026-09", live=True)
        submission.assert_not_called()
        bulk.assert_not_called()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["submission_status"], "not_requested")
        self.assertEqual(result["remote_writes"], 0)

    def test_incomplete_cli_exit_does_not_hide_successful_submissions(self):
        report = {"status": "partial", "submission_status": "proposal_prs_created", "remote_writes": 1,
                  "prs": [{"pr": "fixture"}], "coverage_complete": False}
        with patch("tools.event_collector.weekly.run", return_value=report), redirect_stdout(io.StringIO()) as output:
            status = weekly_main([])
        self.assertEqual(status, 1)
        self.assertEqual(json.loads(output.getvalue()), report)

    def test_existing_review_stops_before_source_downloads_and_gemini(self):
        with patch("tools.event_collector.weekly.pending_review", return_value={"status": "awaiting_review", "pr": "fixture"}), \
                patch("tools.event_collector.weekly.collect") as collection, \
                patch("tools.event_collector.weekly.process") as extraction:
            result = run(self.root, self.site, "2026-09", live=True, open_pr=True)
        self.assertEqual(result["api_requests"], 0)
        collection.assert_not_called()
        extraction.assert_not_called()

    def test_bulk_weekly_submission_continues_despite_existing_review(self):
        with self.fixtures(), \
                patch("tools.event_collector.weekly.pending_review") as waiting, \
                patch("tools.event_collector.weekly.github_token", return_value="fixture-token"), \
                patch("tools.event_collector.github_exclusions.closed_rejections", return_value={}), \
                patch("tools.event_collector.weekly.submit_all", return_value={"status": "proposal_prs_created", "created_count": 1}) as submission:
            result = run(self.root, self.site, "2026-09", live=True, open_pr=True, all_events=True)
        waiting.assert_not_called()
        submission.assert_called_once()
        self.assertEqual(result["status"], "proposal_prs_created")
        self.assertEqual(result["remote_writes"], 1)

    def test_editor_exclusion_is_not_resubmitted(self):
        record = candidate("proposed-mail")
        first = reconcile_records([record], {}, {})
        excluded = decide(first["records"][0], "reject", "fixture-editor", "Outside scope")
        save_json(self.records / (excluded["id"] + ".json"), excluded)
        with self.fixtures():
            result = run(self.root, self.site, "2026-09", live=True)
        self.assertEqual(result["record_count"], 0)

    def test_partial_preflight_reports_incomplete_coverage(self):
        with self.fixtures(collection_status="partial"):
            result = run(self.root, self.site, "2026-09")
        self.assertEqual(result["status"], "partial")
        self.assertNotIn("package", result)

    def test_token_is_passed_only_in_environment_and_git_errors_are_redacted(self):
        secret = "fixture-secret-token"
        failure = subprocess.CompletedProcess([], 1, "", "private token: " + secret)
        with patch.dict(os.environ, {"EVENT_REVIEW_TOKEN": secret}), \
                patch("tools.event_collector.submit.subprocess.run", return_value=failure) as process:
            with self.assertRaises(ValueError) as error:
                git("push", "origin", "main")
        self.assertNotIn(secret, str(error.exception))
        self.assertNotIn(secret, str(process.call_args.args))
        self.assertTrue(any(key.startswith("GIT_CONFIG_VALUE_") for key in process.call_args.kwargs["env"]))
