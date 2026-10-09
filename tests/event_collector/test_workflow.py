"""Editorial/publication and source integration tests; no real approvals or API calls."""

import copy
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.event_collector.audit_site import audit
from tools.event_collector.backup import restore, snapshot
from tools.event_collector.collect import digest
from tools.event_collector.extract import save_json
from tools.event_collector.extraction import ROOT, MODEL
from tools.event_collector.ingest import inputs_for_month, main as ingest_main, process, production_input
from tools.event_collector.inspire import blank_facts, discover, map_record
from tools.event_collector.moderate import decide, edit, manual, merge_duplicate
from tools.event_collector.reconcile import prepare as reconcile, reconcile_records
from tools.event_collector.registry import check_base, export, public_data, public_url, validate_dataset
from tools.event_collector.review_changes import apply_package, prepare as changes
from tools.event_collector.replay_fixtures import fixture_result


def candidate(event_id="event-test"):
    facts = blank_facts()
    facts.update(title="Example lattice workshop 2027", type="workshop", start_date="2027-05-12",
                 end_date="2027-05-13", official_url="https://example.org/workshop/2027", lifecycle="scheduled")
    result = manual(facts, "fixture-editor")
    result.update(id=event_id, origin="collected", history=[])
    return result


def hit(event_id="123", **metadata):
    return {"id": event_id, "updated": "2026-10-09T12:00:00Z", "metadata": {
        "titles": [{"title": "Example lattice workshop 2027"}], "opening_date": "2027-05-12",
        "closing_date": "2027-05-13", "urls": [{"value": "https://example.org/workshop/2027"}], **metadata}}


class TemporaryRoot(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for filename in ("schema/event.schema.json", "extraction/response.schema.json", "extraction/prompt-v5.txt"):
            path = self.root / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((ROOT / filename).read_bytes())


class EditorialTests(TemporaryRoot):
    def test_approval_requires_warning_acknowledgement_and_seals_content(self):
        record = candidate()
        record["warnings"] = ["Check relevance"]
        with self.assertRaises(ValueError):
            decide(record, "approve", "fixture-editor")
        approved = decide(record, "approve", "fixture-editor", acknowledge=True)
        validate_dataset({approved["id"]: approved})
        approved["facts"]["title"] = "Changed without another review"
        with self.assertRaisesRegex(ValueError, "changed after review"):
            validate_dataset({approved["id"]: approved})

    def test_approval_requires_publishable_dates_type_and_timezone(self):
        for key, value in (("start_date", None), ("type", "unknown"), ("start_time", "09:00")):
            record = candidate()
            record["facts"][key] = value
            with self.subTest(key=key), self.assertRaises(Exception):
                decide(record, "approve", "fixture-editor")

    def test_hide_and_restore_preserve_id_and_require_explicit_restore(self):
        approved = decide(candidate(), "approve", "fixture-editor")
        hidden = decide(approved, "hide", "fixture-editor", "Not wanted in calendar")
        self.assertEqual(public_data({hidden["id"]: hidden})["events"], [])
        with self.assertRaisesRegex(ValueError, "restore"):
            decide(hidden, "approve", "fixture-editor")
        restored = decide(hidden, "restore", "fixture-editor")
        self.assertEqual(restored["id"], approved["id"])
        self.assertEqual(restored["history"][-1]["action"], "restore")
        validate_dataset({restored["id"]: restored})

    def test_rejection_requires_reason_and_is_not_exported(self):
        with self.assertRaises(ValueError):
            decide(candidate(), "reject", "fixture-editor")
        rejected = decide(candidate(), "reject", "fixture-editor", "Outside scope")
        validate_dataset({rejected["id"]: rejected}, require_decisions=True)
        self.assertFalse(public_data({rejected["id"]: rejected})["events"])

    def test_editor_correction_invalidates_approval_and_protects_selected_field(self):
        approved = decide(candidate(), "approve", "fixture-editor")
        corrected = edit(approved, {"location": {"city": "Example city"}}, "fixture-editor", ["/facts/location/city"])
        self.assertEqual(corrected["decision"]["status"], "pending")
        self.assertGreater(corrected["revision"], approved["revision"])
        incoming = candidate("incoming")
        incoming["facts"]["location"]["city"] = "Other city"
        result = reconcile_records([incoming], {corrected["id"]: corrected}, {})
        self.assertEqual(result["records"][0]["facts"]["location"]["city"], "Example city")

    def test_editing_an_exclusion_does_not_restore_it(self):
        hidden = decide(candidate(), "hide", "fixture-editor", "Editor exclusion")
        corrected = edit(hidden, {"title": "Corrected title"}, "fixture-editor")
        self.assertEqual(corrected["decision"]["status"], "hidden")
        with self.assertRaises(ValueError):
            decide(corrected, "approve", "fixture-editor")

    def test_manual_event_round_trip_to_review_and_export(self):
        record = manual(candidate()["facts"], "fixture-editor")
        record = decide(record, "approve", "fixture-editor")
        editor = self.root / "local/editor"
        save_json(editor / (record["id"] + ".json"), record)
        package, manifest = changes(editor, self.root)
        shared = json.loads((package / "changes/_event_collector/records" / (record["id"] + ".json")).read_text())
        output = self.root / "public.json"
        export({shared["id"]: shared}, output, self.root)
        data = json.loads(output.read_text())
        self.assertEqual(len(data["events"]), 1)
        self.assertNotIn("history", data["events"][0])
        self.assertNotIn("evidence", data["events"][0])
        self.assertEqual(manifest["remote_writes"], 0)

    def test_failed_export_preserves_previous_public_data(self):
        output = self.root / "public.json"
        output.write_text("Previous approved dataset")
        approved = decide(candidate(), "approve", "fixture-editor")
        approved["facts"]["start_date"] = "2027-05-01"
        with self.assertRaises(ValueError):
            export({approved["id"]: approved}, output, self.root)
        self.assertEqual(output.read_text(), "Previous approved dataset")

    def test_merge_duplicate_redirects_future_reminder_to_survivor(self):
        first, second = candidate("event-first"), candidate("event-second")
        merged, hidden, suggestions = merge_duplicate(first, second, "fixture-editor")
        validate_dataset({merged["id"]: merged, hidden["id"]: hidden})
        result = reconcile_records([candidate("incoming")], {merged["id"]: merged, hidden["id"]: hidden}, {})
        self.assertEqual(result["routes"][0]["event_id"], merged["id"])
        self.assertNotEqual(result["routes"][0]["status"], "identity_review")

    def test_dangling_and_cyclic_satellites_are_blocked(self):
        first, second = candidate("first"), candidate("second")
        first["related_events"] = [{"id": "second", "relation": "satellite_of"}]
        with self.assertRaises(ValueError):
            validate_dataset({"first": first})
        second["related_events"] = [{"id": "first", "relation": "satellite_of"}]
        with self.assertRaisesRegex(ValueError, "cycle"):
            validate_dataset({"first": first, "second": second})

    def test_private_urls_and_credentials_are_not_public_links(self):
        for value in ("javascript:alert(1)", "http://127.0.0.1/x", "http://localhost/x", "https://user:pass@example.org/x", "https://example.org/x\n"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                public_url(value)

    def test_pending_registry_cannot_pass_merge_check(self):
        with self.assertRaisesRegex(ValueError, "editorial decision"):
            validate_dataset({"event-test": candidate()}, require_decisions=True)

    def test_public_event_text_cannot_include_access_details(self):
        record = candidate()
        record["facts"]["summary"] = "Contact private@example.org; password: secret"
        with self.assertRaisesRegex(ValueError, "contact or access"):
            decide(record, "approve", "fixture-editor")

    def test_base_check_blocks_deletion_and_unchanged_revision(self):
        before = candidate()
        responses = [type("Result", (), {"stdout": "_event_collector/records/event-test.json\n"})(),
                     type("Result", (), {"stdout": json.dumps(before)})()]
        with patch("tools.event_collector.registry.subprocess.run", side_effect=responses):
            with self.assertRaisesRegex(ValueError, "instead of deleting"):
                check_base({}, "origin/main", self.root)
        after = copy.deepcopy(before)
        after["facts"]["summary"] = "Changed facts"
        with patch("tools.event_collector.registry.subprocess.run", side_effect=responses):
            with self.assertRaisesRegex(ValueError, "advance"):
                check_base({after["id"]: after}, "origin/main", self.root)

    def test_site_audit_rejects_accidental_private_assets(self):
        site = self.root / "site"
        site.mkdir()
        (site / "index.html").write_text("Public event page")
        audit(site)
        (site / "leak.json").write_text('{"input_sha256": "a-private-input"}')
        with self.assertRaises(ValueError):
            audit(site)

    def test_backup_restores_decisions_and_pending_ids_without_raw_inputs(self):
        approved = decide(candidate(), "approve", "fixture-editor")
        save_json(self.root / "records/event-test.json", approved)
        pending = candidate("event-pending")
        save_json(self.root / "local/reconciliation/identities.json", {"schema_version": 1,
                  "identities": {pending["id"]: {"record": pending}}})
        save_json(self.root / "local/credentials/private.json", {"secret": "must-not-be-backed-up"})
        value = snapshot(self.root)
        self.assertNotIn("must-not-be-backed-up", json.dumps(value))
        destination = self.root / "local/restored"
        self.assertEqual(restore(value, destination, self.root), 1)
        self.assertEqual(json.loads((destination / "records/event-test.json").read_text()), approved)
        with self.assertRaises(ValueError):
            restore(value, destination, self.root)
        value["records"]["event-test"]["facts"]["title"] = "Tampered backup"
        with self.assertRaisesRegex(ValueError, "integrity"):
            restore(value, self.root / "another", self.root)


class InspireTests(TemporaryRoot):
    def test_structured_mapping_keeps_missing_values_unknown(self):
        record = map_record(hit())
        self.assertEqual(record["identity_aliases"], ["inspire:123"])
        self.assertEqual(record["decision"]["status"], "pending")
        self.assertIsNone(record["facts"]["location"]["city"])
        self.assertEqual(record["facts"]["attendance"], "unknown")
        self.assertIsNone(record["facts"]["summary"])

    def test_addresses_are_not_guessed_from_multiple_locations(self):
        record = map_record(hit(addresses=[{"cities": ["Example city"], "country": "Example country"}]))
        self.assertEqual(record["facts"]["location"]["city"], "Example city")
        record = map_record(hit(addresses=[{"cities": ["A"]}, {"cities": ["B"]}]))
        self.assertIsNone(record["facts"]["location"]["city"])

    def test_invalid_date_needs_repair(self):
        with self.assertRaises(ValueError):
            map_record(hit(opening_date="2027-02-31"))

    def test_pagination_and_repeat_preserve_edits(self):
        class Fake:
            requests = 0
            def get(self, params):
                self.requests += 1
                return {"hits": {"total": 2, "hits": [hit(str(params["page"]))]}}
        client = Fake()
        bundle, report = discover(client, self.root, page_size=1)
        self.assertEqual(report["status"], "success")
        self.assertEqual(report["candidate_count"], 2)
        path = bundle / "proposed-inspire-1.json"
        path.write_text("Editor working copy")
        same, report = discover(Fake(), self.root, page_size=1)
        self.assertEqual(same, bundle)
        self.assertEqual(path.read_text(), "Editor working copy")

    def test_page_limit_and_outage_are_partial_not_deletion(self):
        class Fake:
            def get(self, params):
                if params["page"] == 2:
                    raise OSError("Source unavailable")
                return {"hits": {"total": 3, "hits": [hit()]}}
        bundle, report = discover(Fake(), self.root, page_size=1, max_pages=1)
        self.assertTrue(report["incomplete"])
        self.assertEqual(report["status"], "partial")
        other, report = discover(Fake(), self.root, page_size=1)
        self.assertEqual(report["candidate_count"], 1)
        self.assertTrue(report["failures"])
        self.assertFalse((self.root / "records").exists())

    def test_inspire_matches_mailing_list_and_preserves_exclusion(self):
        old = candidate()
        result = reconcile_records([map_record(hit())], {old["id"]: old}, {})
        self.assertEqual(result["records"][0]["id"], old["id"])
        hidden = decide(old, "hide", "fixture-editor", "Editor exclusion")
        result = reconcile_records([map_record(hit())], {hidden["id"]: hidden}, {})
        self.assertEqual(result["routes"][0]["status"], "excluded")


class ReviewChangesTests(TemporaryRoot):
    def plan(self):
        source = self.root / "local/input"
        record = candidate("proposed-test")
        save_json(source / "proposed-test.json", record)
        save_json(source / "manifest.json", {"candidates": [{"id": "proposed-test"}]})
        return reconcile(source, self.root)[0]

    def test_changed_canonical_state_requires_reconciliation_again(self):
        plan = self.plan()
        old = candidate("canonical")
        save_json(self.root / "records/canonical.json", old)
        with self.assertRaisesRegex(ValueError, "changed"):
            changes(plan, self.root)

    def test_excluded_no_change_and_ambiguous_routes_do_not_become_additions(self):
        plan = self.plan()
        data = json.loads((plan / "plan.json").read_text())
        for status in ("excluded", "no_change", "identity_review", "protected_conflict"):
            data["routes"][0]["status"] = status
            save_json(plan / "plan.json", data)
            package, manifest = changes(plan, self.root)
            self.assertFalse(manifest["record_ids"])

    def test_private_evidence_is_removed_with_review_warning(self):
        plan = self.plan()
        data = json.loads((plan / "plan.json").read_text())
        path = plan / (data["record_ids"][0] + ".json")
        record = json.loads(path.read_text())
        record["evidence"] = [{"field": "/facts/title", "source_id": "editor-reference",
                               "excerpt": "Contact private@example.org for details"}]
        save_json(path, record)
        package, manifest = changes(plan, self.root)
        shared = json.loads((package / "changes/_event_collector/records" / path.name).read_text())
        self.assertFalse(shared["evidence"])
        self.assertTrue(any("omitted" in warning for warning in shared["warnings"]))

    def test_package_apply_checks_integrity_and_canonical_state(self):
        plan = self.plan()
        package, manifest = changes(plan, self.root)
        record_path = package / "changes/_event_collector/records" / (manifest["record_ids"][0] + ".json")
        original = record_path.read_bytes()
        record = json.loads(original)
        record["facts"]["title"] = "Unreviewed package change"
        save_json(record_path, record)
        with self.assertRaisesRegex(ValueError, "content changed"):
            apply_package(package, self.root)
        record_path.write_bytes(original)
        self.assertEqual(apply_package(package, self.root), 1)
        with self.assertRaisesRegex(ValueError, "Canonical state changed"):
            apply_package(package, self.root)


class ProductionIngestionTests(TemporaryRoot):
    def test_explicit_month_reaches_collection_and_extraction_with_no_other_months(self):
        with patch("tools.event_collector.ingest.collect", return_value={"status": "success"}) as collect_month, \
                patch("tools.event_collector.ingest.inputs_for_month", return_value=[]) as inputs, \
                patch("tools.event_collector.ingest.process", return_value=(None, {"mode": "preflight", "api_requests": 0})) as extraction, \
                patch("tools.event_collector.ingest.recent_month") as recent, redirect_stdout(io.StringIO()) as output:
            status = ingest_main(["--root", str(self.root), "--month", "2026-08"])
        self.assertEqual(status, 0)
        self.assertEqual(collect_month.call_args.args[0], ["2026-08"])
        inputs.assert_called_once_with(self.root, "2026-08", 50)
        self.assertFalse(extraction.call_args.args[2])
        recent.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["month"], "2026-08")

    def test_invalid_or_multiple_months_fail_before_collection(self):
        for value in ("2026-00", "2026-13", "2026-9", "0000-09", "2026/09", "2026-09,2026-10"):
            with self.subTest(month=value), patch("tools.event_collector.ingest.collect") as collect_month, \
                    redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                ingest_main(["--root", str(self.root), "--month", value])
            self.assertEqual(error.exception.code, 2)
            collect_month.assert_not_called()

    def test_explicit_month_and_relative_window_are_mutually_exclusive(self):
        with patch("tools.event_collector.ingest.collect") as collect_month, redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit) as error:
            ingest_main(["--month", "2026-08", "--month-window", "previous"])
        self.assertEqual(error.exception.code, 2)
        collect_month.assert_not_called()

    def test_relative_month_defaults_are_preserved_and_cached_sources_can_be_selected(self):
        for options, window in (([], "current"), (["--month-window", "previous"], "previous")):
            with self.subTest(window=window), patch("tools.event_collector.ingest.recent_month", return_value="2026-09") as recent, \
                    patch("tools.event_collector.ingest.collect") as collect_month, \
                    patch("tools.event_collector.ingest.inputs_for_month", return_value=[]) as inputs, \
                    patch("tools.event_collector.ingest.process", return_value=(None, {"api_requests": 0})), redirect_stdout(io.StringIO()):
                self.assertEqual(ingest_main(["--root", str(self.root), "--skip-collection", *options]), 0)
            recent.assert_called_once_with(window)
            collect_month.assert_not_called()
            inputs.assert_called_once_with(self.root, "2026-09", 50)

    def fixture(self):
        labels = json.loads((ROOT / "evaluation/labels.json").read_text())
        case = next(item for item in labels["cases"] if item["case_id"] == "synthetic-06")
        record = json.loads((ROOT / "evaluation/synthetic_inputs/synthetic-06.json").read_text())
        record["source_url"] = "https://example.org/announcement/fixture"
        return record, fixture_result(case, record)

    def test_preflight_has_no_credentials_or_model_calls(self):
        record, result = self.fixture()
        destination, report = process([record], self.root)
        self.assertIsNone(destination)
        self.assertEqual(report["api_requests"], 0)
        self.assertFalse((self.root / "evaluation").exists())

    def test_production_extraction_cache_to_github_changes_without_labels(self):
        record, result = self.fixture()
        response = {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": json.dumps(result)}]}}],
                    "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 100}, "modelVersion": MODEL}
        calls = []
        def transport(request):
            calls.append(request)
            return response
        bundle, report = process([record], self.root, True, transport)
        self.assertEqual(report["candidate_count"], 1)
        self.assertEqual(report["api_requests"], 1)
        again, report = process([record], self.root, True, transport)
        self.assertEqual(report["api_requests"], 0)
        self.assertEqual(len(calls), 1)
        plan = reconcile(bundle, self.root)[0]
        package, manifest = changes(plan, self.root)
        self.assertEqual(len(manifest["record_ids"]), 1)
        self.assertFalse((self.root / "evaluation").exists())

    def test_transport_failure_is_visible_and_not_automatically_retried(self):
        record, result = self.fixture()
        calls = []
        def transport(request):
            calls.append(request)
            raise OSError("Simulated outage")
        bundle, report = process([record], self.root, True, transport)
        self.assertEqual(report["status"], "partial")
        self.assertFalse(report["candidate_count"])
        process([record], self.root, True, transport)
        self.assertEqual(len(calls), 1)

    def test_only_selected_archive_month_is_used(self):
        for month in ("2026-09", "2026-10"):
            source = {"url": "https://list.iu.edu/sympa/arc/latticenews-l/" + month + "/msg00001.html",
                      "subject": "Example", "body": "Contact private@example.org", "links": [], "posted_at": None}
            source["content_sha256"] = digest(json.dumps(source, sort_keys=True, ensure_ascii=False).encode())
            save_json(self.root / "local/announcements" / (month + ".json"), source)
        selected = inputs_for_month(self.root, "2026-10")
        self.assertEqual(len(selected), 1)
        self.assertIn("2026-10", selected[0]["source_url"])
        self.assertNotIn("private@example.org", selected[0]["body"])
        path = self.root / "local/announcements/2026-10.json"
        tampered = json.loads(path.read_text())
        tampered["body"] = "Modified after collection"
        save_json(path, tampered)
        with self.assertRaisesRegex(ValueError, "integrity"):
            inputs_for_month(self.root, "2026-10")


if __name__ == "__main__":
    unittest.main()
