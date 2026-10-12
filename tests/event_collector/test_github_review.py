"""Event queues and actual rejection receipts preserve editorial identities."""

import base64
import copy
import json
from pathlib import Path
from unittest.mock import patch

from test_weekly import WebsiteFixture, FakeGitHub
from test_workflow import TemporaryRoot, candidate
from tools.event_collector.extract import save_json
from tools.event_collector.github_exclusions import closed_rejections
from tools.event_collector.reconcile import prepare as reconcile
from tools.event_collector.registry import public_data, validate_dataset
from tools.event_collector.review_changes import prepare as changes
from tools.event_collector.website import package_record
from tools.event_collector.submit import BRANCH, REMOTE, git, next_package, submit


class ClosedClient:
    def __init__(self, record):
        self.record = record
        self.merged = False
        self.human = True
        self.labels = []

    def request(self, path):
        if path.startswith("/pulls?state=closed"):
            return [{"number": 42, "labels": self.labels, "merged_at": "2026-10-09T12:00:00Z" if self.merged else None,
                     "head": {"ref": BRANCH, "sha": "a" * 40, "repo": {"full_name": "EuroLFT/eurolft.github.io"}}}]
        if path.startswith("/issues/42/events"):
            return [{"event": "closed", "created_at": "2026-10-09T13:00:00Z",
                     "actor": {"type": "User" if self.human else "Bot", "login": "fixture-reviewer"}}]
        if path.startswith("/pulls/42/files"):
            return [{"filename": "_data/community_events/" + self.record["id"] + ".json", "status": "added"}]
        if path.startswith("/contents/"):
            return {"encoding": "base64", "content": base64.b64encode(json.dumps(self.record).encode()).decode()}
        raise AssertionError(path)


class ClosedReviewTests(TemporaryRoot):
    def test_administrative_reset_does_not_reject_or_read_the_event(self):
        client = ClosedClient(candidate("event-reset"))
        client.labels = [{"name": "event-review-reset"}]
        original_request = client.request
        def request(path):
            self.assertTrue(path.startswith("/pulls?state=closed"))
            return original_request(path)
        client.request = request
        self.assertFalse(closed_rejections(client, self.root))

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
        save_json(self.records / (identity + ".json"), package_record(first, identity))
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
        record = package_record(first, identity)
        exclusions = closed_rejections(ClosedClient(record), self.root)
        _, remaining = next_package(package, self.site, self.root, exclusions)
        self.assertNotIn(identity, remaining["record_ids"])

    def test_saved_queue_never_replaces_later_human_corrections(self):
        package = self.two_events()
        first, manifest = next_package(package, self.site, self.root)
        identity = manifest["record_ids"][0]
        record = package_record(first, identity)
        record["facts"]["summary"] = "Later human correction"
        record["revision"] += 1
        save_json(self.records / (identity + ".json"), record)
        _, remaining = next_package(package, self.site, self.root)
        self.assertNotIn(identity, remaining["record_ids"])
