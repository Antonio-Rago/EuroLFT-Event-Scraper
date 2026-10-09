"""Exercise independent collector assets and website-owned editorial state."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import unittest

from test_workflow import TemporaryRoot, candidate
from tools.event_collector.extract import save_json
from tools.event_collector.moderate import decide
from tools.event_collector.preview import prepare as preview
from tools.event_collector.reconcile import prepare as reconcile
from tools.event_collector.registry import check_base, main as registry_main
from tools.event_collector.review_changes import apply_package, prepare as changes
from tools.event_collector.website import records_directory


class SeparateWebsiteTests(TemporaryRoot):
    def setUp(self):
        super().setUp()
        self.site = self.root / "website"
        self.records = self.site / "_event_collector/records"
        self.records.mkdir(parents=True)
        (self.site / "_config.yml").write_text("title: Fixture website\n")
        save_json(self.site / "_data/community_events.json", {"schema_version": 1, "events": []})

    def bundle(self, record):
        directory = self.root / "local/review/fixture"
        save_json(directory / (record["id"] + ".json"), record)
        save_json(directory / "manifest.json", {"candidates": [{"id": record["id"]}]})
        return directory

    def commit_website(self):
        for args in (("init", "-q"), ("config", "user.name", "Fixture Editor"),
                     ("config", "user.email", "fixture@example.invalid"),
                     ("add", "."), ("commit", "-qm", "Fixture editorial state")):
            subprocess.run(["git", *args], cwd=self.site, check=True, capture_output=True)

    def test_wrong_website_path_fails_instead_of_assuming_an_empty_registry(self):
        with self.assertRaisesRegex(ValueError, "website checkout"):
            records_directory(self.root)
        self.assertEqual(records_directory(self.site), self.records.resolve())

    def test_reconciliation_reads_exclusions_from_the_website(self):
        excluded = decide(candidate("event-excluded"), "reject", "fixture-editor", "Outside scope")
        save_json(self.records / "event-excluded.json", excluded)
        directory, plan, _ = reconcile(self.bundle(candidate("incoming")), self.root, records_directory(self.site))
        self.assertEqual(plan["routes"][0]["status"], "excluded")
        self.assertEqual(plan["routes"][0]["event_id"], "event-excluded")
        self.assertFalse((self.root / "records").exists())

    def test_package_application_and_publication_write_only_to_the_website(self):
        approved = decide(candidate(), "approve", "fixture-editor")
        editor = self.root / "local/editor"
        save_json(editor / (approved["id"] + ".json"), approved)
        package, _ = changes(editor, self.root, self.records)
        self.assertEqual(apply_package(package, self.root, self.records), 1)
        output = self.site / "_data/community_events.json"
        with redirect_stdout(io.StringIO()):
            self.assertEqual(registry_main(["--root", str(self.root), "--site", str(self.site),
                                           "--require-decisions", "--export", str(output)]), 0)
        self.assertEqual(len(json.loads(output.read_text())["events"]), 1)
        self.assertFalse((self.root / "records").exists())
        self.assertFalse((self.root / "_data").exists())

    def test_base_checks_use_the_website_git_history(self):
        approved = decide(candidate(), "approve", "fixture-editor")
        save_json(self.records / (approved["id"] + ".json"), approved)
        self.commit_website()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(registry_main(["--root", str(self.root), "--site", str(self.site),
                                           "--base", "HEAD", "--require-decisions"]), 0)
        with self.assertRaisesRegex(ValueError, "instead of deleting"):
            check_base({}, "HEAD", self.root, self.site)
        changed = json.loads(json.dumps(approved))
        changed["facts"]["summary"] = "Changed without advancing revision"
        with self.assertRaisesRegex(ValueError, "advance"):
            check_base({changed["id"]: changed}, "HEAD", self.root, self.site)

    def test_preview_uses_website_data_and_keeps_public_data_unchanged(self):
        plan = self.root / "local/fixture-plan"
        record = candidate()
        save_json(plan / (record["id"] + ".json"), record)
        save_json(plan / "plan.json", {"record_ids": [record["id"]]})
        public = (self.site / "_data/community_events.json").read_bytes()
        configuration = preview(plan, self.root, self.site)
        self.assertEqual(configuration.parent, (self.site / "_event_collector/local").resolve())
        data = json.loads(configuration.read_text())
        self.assertTrue(data["community_preview"])
        self.assertEqual((self.site / "_data/community_events.json").read_bytes(), public)
        self.assertFalse((self.root / "local/preview-data").exists())

    def test_cli_requires_an_explicit_website_checkout(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            registry_main(["--root", str(self.root)])
        self.assertEqual(error.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
