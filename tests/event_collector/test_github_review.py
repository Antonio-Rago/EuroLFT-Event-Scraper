"""GitHub decisions require a real matching merge, never a synthetic approval."""

import base64
import copy
import json
from pathlib import Path
from unittest.mock import patch

from test_weekly import WebsiteFixture, FakeGitHub
from test_workflow import TemporaryRoot, candidate
from tools.event_collector.extract import save_json
from tools.event_collector.github_exclusions import closed_rejections
from tools.event_collector.github_review import merged_decisions, publishable_candidate
from tools.event_collector.reconcile import prepare as reconcile
from tools.event_collector.registry import public_data, validate_dataset
from tools.event_collector.review_changes import prepare as changes
from tools.event_collector.submit import BRANCH, REMOTE, git, next_package, submit


class ReceiptClient:
    def __init__(self, pull):
        self.pull = pull
        self.calls = []

    def request(self, path):
        self.calls.append(path)
        if path.startswith("/commits/"):
            return [{"number": self.pull["number"], "merged_at": self.pull["merged_at"], "base": self.pull["base"]}]
        if path == "/pulls/23":
            return self.pull
        raise AssertionError(path)


class GitHubReceiptTests(TemporaryRoot):
    def setUp(self):
        super().setUp()
        self.site = self.root / "site"
        self.site.mkdir()
        self.record = candidate("event-proof")
        self.path = self.site / "_event_collector/records/event-proof.json"
        save_json(self.path, self.record)
        for args in (("init", "-q", "-b", "main"), ("config", "user.name", "Fixture"),
                     ("config", "user.email", "fixture@example.invalid"), ("add", "."),
                     ("commit", "-qm", "Proposal fixture"), ("remote", "add", "origin", REMOTE)):
            git(*args, cwd=self.site)
        self.commit = git("rev-parse", "HEAD", cwd=self.site)
        self.pull = {"number": 23, "merged": True, "merged_at": "2026-10-09T12:00:00Z",
                     "merged_by": {"type": "User", "login": "fixture-reviewer"},
                     "merge_commit_sha": self.commit, "html_url": "https://github.com/EuroLFT/eurolft.github.io/pull/23",
                     "base": {"ref": "main", "repo": {"full_name": "EuroLFT/eurolft.github.io"}}}

    def test_merge_receipt_approves_only_an_export_copy_and_uses_real_reviewer(self):
        before = self.path.read_bytes()
        records = {self.record["id"]: self.record}
        result = merged_decisions(records, self.site, self.root, ReceiptClient(self.pull))
        approved = result[self.record["id"]]
        validate_dataset(result, self.root, require_decisions=True)
        self.assertEqual(approved["history"][-1]["actor"], "fixture-reviewer")
        self.assertEqual(approved["history"][-1]["at"], self.pull["merged_at"])
        self.assertEqual(len(public_data(result)["events"]), 1)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.record["decision"]["status"], "pending")

    def test_actions_checkout_remote_without_dot_git_is_supported(self):
        git("remote", "set-url", "origin", REMOTE[:-4], cwd=self.site)
        result = merged_decisions({self.record["id"]: self.record}, self.site, self.root, ReceiptClient(self.pull))
        self.assertEqual(result[self.record["id"]]["decision"]["status"], "approved")

    def test_an_approved_but_unmerged_pr_cannot_publish(self):
        self.pull.update(merged=False, merged_at=None)
        with self.assertRaisesRegex(ValueError, "no matching human-merged"):
            merged_decisions({self.record["id"]: self.record}, self.site, self.root, ReceiptClient(self.pull))

    def test_bot_merge_wrong_repository_or_other_base_cannot_approve(self):
        for path, value in (("actor", {"type": "Bot", "login": "fixture-bot"}),
                            ("repository", "other/website"), ("base", "other-branch")):
            with self.subTest(path=path):
                pull = copy.deepcopy(self.pull)
                if path == "actor":
                    pull["merged_by"] = value
                elif path == "repository":
                    pull["base"]["repo"]["full_name"] = value
                else:
                    pull["base"]["ref"] = value
                with self.assertRaises(ValueError):
                    merged_decisions({self.record["id"]: self.record}, self.site, self.root, ReceiptClient(pull))

    def test_dirty_or_later_changed_content_cannot_inherit_an_older_approval(self):
        changed = copy.deepcopy(self.record)
        changed["facts"]["title"] = "Changed after approval"
        save_json(self.path, changed)
        with self.assertRaisesRegex(ValueError, "Uncommitted"):
            merged_decisions({changed["id"]: changed}, self.site, self.root, ReceiptClient(self.pull))
        git("add", ".", cwd=self.site)
        git("commit", "-qm", "Unreviewed change", cwd=self.site)
        with self.assertRaisesRegex(ValueError, "no matching human-merged"):
            merged_decisions({changed["id"]: changed}, self.site, self.root, ReceiptClient(self.pull))

    def test_merge_from_an_unrelated_git_history_cannot_approve(self):
        git("checkout", "--orphan", "unrelated", cwd=self.site)
        git("commit", "-qm", "Unrelated merge", cwd=self.site)
        self.pull["merge_commit_sha"] = git("rev-parse", "HEAD", cwd=self.site)
        git("checkout", "main", cwd=self.site)
        with self.assertRaises(ValueError):
            merged_decisions({self.record["id"]: self.record}, self.site, self.root, ReceiptClient(self.pull))

    def test_unknown_clock_timezone_still_requires_a_fact_correction(self):
        changed = copy.deepcopy(self.record)
        changed["facts"]["start_time"] = "09:00"
        with self.assertRaisesRegex(ValueError, "IANA timezone"):
            publishable_candidate(changed, self.root)
        self.assertEqual(changed["decision"]["status"], "pending")

    def test_a_related_event_can_wait_for_its_own_pr_without_a_broken_public_link(self):
        record = copy.deepcopy(self.record)
        record["related_events"] = [{"id": "event-other-proposal", "relation": "satellite_of"}]
        validate_dataset({record["id"]: record}, self.root, allow_pending_relations=True)
        with self.assertRaisesRegex(ValueError, "Dangling"):
            validate_dataset({record["id"]: record}, self.root)
        # A real merge receipt is recorded before exporting the event.
        save_json(self.path, record)
        git("add", ".", cwd=self.site)
        git("commit", "-qm", "Related proposal", cwd=self.site)
        self.pull["merge_commit_sha"] = git("rev-parse", "HEAD", cwd=self.site)
        result = merged_decisions({record["id"]: record}, self.site, self.root, ReceiptClient(self.pull))
        self.assertFalse(public_data(result)["events"][0]["related_events"])


class ClosedClient:
    def __init__(self, record):
        self.record = record
        self.merged = False
        self.human = True

    def request(self, path):
        if path.startswith("/pulls?state=closed"):
            return [{"number": 42, "merged_at": "2026-10-09T12:00:00Z" if self.merged else None,
                     "head": {"ref": BRANCH, "sha": "a" * 40, "repo": {"full_name": "EuroLFT/eurolft.github.io"}}}]
        if path.startswith("/issues/42/events"):
            return [{"event": "closed", "created_at": "2026-10-09T13:00:00Z",
                     "actor": {"type": "User" if self.human else "Bot", "login": "fixture-reviewer"}}]
        if path.startswith("/pulls/42/files"):
            return [{"filename": "_event_collector/records/" + self.record["id"] + ".json", "status": "added"}]
        if path.startswith("/contents/"):
            return {"encoding": "base64", "content": base64.b64encode(json.dumps(self.record).encode()).decode()}
        raise AssertionError(path)


class ClosedReviewTests(TemporaryRoot):
    def test_a_human_closed_pr_is_an_exclusion_with_its_actual_actor_and_time(self):
        record = candidate("event-closed")
        result = closed_rejections(ClosedClient(record), self.root)
        rejected = result[record["id"]]
        self.assertEqual(rejected["decision"]["status"], "rejected")
        self.assertEqual(rejected["history"][-1]["actor"], "fixture-reviewer")
        self.assertEqual(rejected["history"][-1]["at"], "2026-10-09T13:00:00Z")
        self.assertIn("42", rejected["decision"]["reason"])

    def test_merged_prs_or_automated_closure_are_not_rejections(self):
        client = ClosedClient(candidate("event-closed"))
        client.merged = True
        self.assertFalse(closed_rejections(client, self.root))
        client.merged = False
        client.human = False
        self.assertFalse(closed_rejections(client, self.root))

    def test_reconciliation_uses_closed_pr_exclusions_without_writing_them_into_main(self):
        record = candidate("event-closed")
        rejected = closed_rejections(ClosedClient(record), self.root)
        incoming = copy.deepcopy(record)
        incoming["id"] = "proposed-reminder"
        bundle = self.root / "local/reminder"
        save_json(bundle / (incoming["id"] + ".json"), incoming)
        save_json(bundle / "manifest.json", {"candidates": [{"id": incoming["id"]}]})
        records = self.root / "records"
        records.mkdir()
        plan, routing, _ = reconcile(bundle, self.root, records, github_exclusions=rejected)
        self.assertEqual(routing["routes"][0]["status"], "excluded")
        package, manifest = changes(plan, self.root, records)
        self.assertFalse(manifest["record_ids"])
        self.assertFalse(list(records.glob("*.json")))


class SingleEventQueueTests(WebsiteFixture):
    def two_events(self):
        first = candidate("proposed-first")
        second = candidate("proposed-second")
        second["facts"]["title"] = "Another lattice school 2028"
        second["facts"]["start_date"] = "2028-05-12"
        second["facts"]["end_date"] = "2028-05-13"
        second["facts"]["official_url"] = "https://example.org/other/2028"
        bundle = self.bundle("queue", first)
        save_json(bundle / (second["id"] + ".json"), second)
        save_json(bundle / "manifest.json", {"candidates": [{"id": first["id"]}, {"id": second["id"]}]})
        plan, _, _ = reconcile(bundle, self.root, self.records)
        return changes(plan, self.root, self.records)[0]

    def test_one_record_is_submitted_and_the_original_queue_is_untouched(self):
        package = self.two_events()
        before = (package / "manifest.json").read_bytes()
        remote = self.local_remote()
        with patch("tools.event_collector.submit.REMOTE", str(remote)):
            result = submit(package, self.root, FakeGitHub())
        self.assertEqual(result["record_count"], 1)
        self.assertEqual(result["remaining_queued"], 1)
        self.assertEqual((package / "manifest.json").read_bytes(), before)
        self.assertEqual(len(git("--git-dir=" + str(remote), "diff", "--name-only", "main", BRANCH).splitlines()), 1)

    def test_next_event_rebases_onto_main_after_the_previous_event_is_accepted(self):
        package = self.two_events()
        first, manifest = next_package(package, self.site, self.root)
        identity = manifest["record_ids"][0]
        save_json(self.records / (identity + ".json"), json.loads((first / "changes/_event_collector/records" / (identity + ".json")).read_text()))
        second, remaining = next_package(package, self.site, self.root)
        self.assertEqual(len(remaining["record_ids"]), 1)
        self.assertNotEqual(remaining["record_ids"][0], identity)

    def test_resolved_branch_is_reused_safely_for_the_next_event_after_merge(self):
        package = self.two_events()
        remote = self.local_remote()
        github = FakeGitHub()
        with patch("tools.event_collector.submit.REMOTE", str(remote)):
            first = submit(package, self.root, github)
            accepted = git("--git-dir=" + str(remote), "rev-parse", BRANCH)
            git("--git-dir=" + str(remote), "update-ref", "refs/heads/main", accepted)
            github.pull = None
            original_request = github.request
            def resolved(path, data=None):
                if data is None and path.startswith("/pulls?"):
                    return [{"number": 999, "merged_at": "2026-10-09T12:00:00Z", "head": {"sha": accepted}}]
                return original_request(path, data)
            github.request = resolved
            second = submit(package, self.root, github)
        self.assertEqual(first["remaining_queued"], 1)
        self.assertEqual(second["record_count"], 1)
        self.assertEqual(second["remaining_queued"], 0)
        self.assertEqual(len(git("--git-dir=" + str(remote), "diff", "--name-only", "main", BRANCH).splitlines()), 1)

    def test_closed_rejected_event_is_not_selected_again(self):
        package = self.two_events()
        first, manifest = next_package(package, self.site, self.root)
        identity = manifest["record_ids"][0]
        record = json.loads((first / "changes/_event_collector/records" / (identity + ".json")).read_text())
        exclusions = closed_rejections(ClosedClient(record), self.root)
        _, remaining = next_package(package, self.site, self.root, exclusions)
        self.assertNotIn(identity, remaining["record_ids"])

    def test_saved_queue_never_replaces_later_human_corrections(self):
        package = self.two_events()
        first, manifest = next_package(package, self.site, self.root)
        identity = manifest["record_ids"][0]
        record = json.loads((first / "changes/_event_collector/records" / (identity + ".json")).read_text())
        record["facts"]["summary"] = "Later human correction"
        record["revision"] += 1
        save_json(self.records / (identity + ".json"), record)
        _, remaining = next_package(package, self.site, self.root)
        self.assertNotIn(identity, remaining["record_ids"])
