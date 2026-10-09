"""Controlled reconciliation/exclusion tests; example fixtures are never real approvals."""

import copy
from datetime import date, timedelta
import json
from pathlib import Path
import tempfile
import unittest

from tools.event_collector.extract import save_json
from tools.event_collector.extraction import ROOT
from tools.event_collector.reconcile import match_strength, merge_record, prepare, reconcile_records


def record(event_id, title="Example lattice workshop 2027", start="2027-05-12", url=None, source=None):
    result = json.loads((ROOT / "examples/04-manual-entry.json").read_text())
    result.update(id=event_id, origin="collected", revision=1, history=[], editor_overrides=[])
    result["facts"].update(title=title, start_date=start,
                           end_date=(date.fromisoformat(start) + timedelta(days=1)).isoformat() if start else None,
                           official_url=url, deadlines=[])
    result["decision"] = {"status": "pending", "reviewed_revision": None, "reason": None, "acknowledged_warnings": []}
    result["sources"] = [{"id": "source-" + event_id, "kind": "announcement",
                          "url": source or "https://example.org/announcements/" + event_id,
                          "posted_at": None, "retrieved_at": None, "note": None}]
    return result


class ReconciliationTests(unittest.TestCase):
    def test_reminder_preserves_allocated_id_across_runs(self):
        first = reconcile_records([record("candidate-a")], {}, {})
        reminder = reconcile_records([record("candidate-b")], {}, first["identities"])
        self.assertEqual(first["records"][0]["id"], reminder["records"][0]["id"])
        self.assertEqual(len(reminder["records"]), 1)
        self.assertEqual(reminder["routes"][0]["status"], "update")
        self.assertEqual(len(reminder["records"][0]["sources"]), 2)

    def test_same_batch_duplicate_sources_have_one_suggested_record(self):
        result = reconcile_records([record("candidate-a"), record("candidate-b")], {}, {})
        self.assertEqual(len(result["records"]), 1)
        self.assertEqual(result["routes"][0]["event_id"], result["routes"][1]["event_id"])

    def test_approved_update_is_pending_and_original_unchanged(self):
        old = record("approved", url="https://example.org/event/2027")
        old["decision"].update(status="approved", reviewed_revision=1)
        incoming = record("candidate", start="2027-05-14", url=old["facts"]["official_url"])
        original = copy.deepcopy(old)
        result = reconcile_records([incoming], {old["id"]: old}, {})
        updated = result["records"][0]
        self.assertEqual(updated["id"], old["id"])
        self.assertEqual(updated["revision"], 2)
        self.assertEqual(updated["decision"]["status"], "pending")
        self.assertIsNone(updated["decision"]["reviewed_revision"])
        self.assertEqual(updated["history"], old["history"])
        self.assertEqual(old, original)
        self.assertTrue(any(change["field"] == "/facts/start_date" for change in result["routes"][0]["changes"]))
        self.assertNotIn(old["id"], result["identities"])

    def test_unknown_values_never_erase_known_facts(self):
        old = record("old")
        old["facts"]["attendance"] = "online"
        incoming = record("candidate", start=None)
        incoming["facts"]["location"]["city"] = None
        merged, suggestions, changed = merge_record(old, incoming)
        self.assertEqual(merged["facts"]["start_date"], old["facts"]["start_date"])
        self.assertEqual(merged["facts"]["location"]["city"], old["facts"]["location"]["city"])
        self.assertEqual(merged["facts"]["attendance"], "online")

    def test_manual_fields_and_origin_survive_source_match(self):
        old = record("manual", url="https://example.org/event/2027")
        old["origin"] = "manual"
        incoming = record("candidate", title="Renamed workshop 2027", start="2027-06-01", url=old["facts"]["official_url"])
        incoming["facts"]["location"]["city"] = "Changed City"
        incoming["evidence"] = [{"field": "/facts/location/city", "source_id": incoming["sources"][0]["id"],
                                 "excerpt": "Changed City"}]
        result = reconcile_records([incoming], {old["id"]: old}, {})
        updated = result["records"][0]
        self.assertEqual(updated["facts"], old["facts"])
        self.assertEqual(updated["origin"], "manual")
        self.assertTrue(result["routes"][0]["suggestions"])
        self.assertFalse(any(item["excerpt"] == "Changed City" for item in updated["evidence"]))

    def test_editor_override_blocks_changed_city_only(self):
        old = record("existing", url="https://example.org/event/2027")
        old["editor_overrides"] = ["/facts/location/city"]
        incoming = record("candidate", start="2027-05-14", url=old["facts"]["official_url"])
        incoming["facts"]["location"]["city"] = "Changed City"
        result = reconcile_records([incoming], {old["id"]: old}, {})
        updated = result["records"][0]
        self.assertEqual(updated["facts"]["location"]["city"], old["facts"]["location"]["city"])
        self.assertEqual(updated["facts"]["start_date"], "2027-05-14")
        self.assertEqual(updated["editor_overrides"], old["editor_overrides"])

    def test_hidden_and_rejected_matches_cannot_restore(self):
        for status in ("hidden", "rejected"):
            with self.subTest(status=status):
                old = record("excluded", url="https://example.org/event/2027")
                old["decision"].update(status=status, reason="Editor exclusion")
                incoming = record("candidate", url=old["facts"]["official_url"])
                result = reconcile_records([incoming], {old["id"]: old}, {})
                self.assertEqual(result["routes"][0]["status"], "excluded")
                self.assertEqual(result["records"], [])
                self.assertEqual(old["decision"]["status"], status)
                with self.assertRaisesRegex(ValueError, "bypass"):
                    reconcile_records([incoming], {old["id"]: old}, {}, {incoming["id"]: "new"})

    def test_later_annual_edition_is_not_excluded_by_reused_url(self):
        old = record("old", title="Example lattice workshop 2027", url="https://example.org/annual")
        old["decision"].update(status="hidden", reason="Removed 2027")
        incoming = record("candidate", title="Example lattice workshop 2028", start="2028-05-12", url=old["facts"]["official_url"])
        result = reconcile_records([incoming], {old["id"]: old}, {})
        self.assertEqual(result["routes"][0]["status"], "new")
        self.assertNotEqual(result["records"][0]["id"], old["id"])

    def test_multiple_matches_remain_reviewable_without_auto_merge(self):
        first, second, incoming = record("first"), record("second"), record("candidate")
        result = reconcile_records([incoming], {first["id"]: first, second["id"]: second}, {})
        self.assertEqual(result["routes"][0]["status"], "identity_review")
        self.assertEqual(result["routes"][0]["possible_matches"], ["first", "second"])
        self.assertEqual(result["routes"][0]["incoming_record"]["facts"], incoming["facts"])
        self.assertEqual(result["records"], [])
        chosen = reconcile_records([incoming], {first["id"]: first, second["id"]: second}, {}, {incoming["id"]: "second"})
        self.assertEqual(chosen["records"][0]["id"], "second")

    def test_missing_date_reminder_requires_identity_review(self):
        old, incoming = record("old"), record("candidate", start=None)
        result = reconcile_records([incoming], {old["id"]: old}, {})
        self.assertEqual(result["routes"][0]["status"], "identity_review")

    def test_shared_message_does_not_merge_satellites(self):
        parent = record("parent", title="Lattice 2027", start="2027-06-20", source="https://example.org/circular")
        school = record("school", title="Lattice practices 2027", start="2027-06-14", source=parent["sources"][0]["url"])
        parent["related_events"] = [{"id": "school", "relation": "has_satellite"}]
        school["related_events"] = [{"id": "parent", "relation": "satellite_of"}]
        result = reconcile_records([parent, school], {}, {})
        self.assertEqual(len(result["records"]), 2)
        titles = {item["facts"]["title"]: item for item in result["records"]}
        self.assertEqual(titles["Lattice 2027"]["related_events"][0]["id"], titles["Lattice practices 2027"]["id"])

    def test_existing_url_alias_matches_changed_title(self):
        old = record("old")
        old["identity_aliases"] = ["https://example.org/event/2027"]
        incoming = record("candidate", title="Changed title 2027", url=old["identity_aliases"][0])
        self.assertEqual(match_strength(incoming, old), "strong")
        incoming["warnings"].append("url_unverified")
        self.assertEqual(match_strength(incoming, old), "possible")

    def test_related_workshops_sharing_url_remain_separate(self):
        first = record("first", title="Example methods workshop 2027", url="https://example.org/programme/2027")
        second = record("second", title="Example research workshop 2027", url=first["facts"]["official_url"])
        first["related_events"] = [{"id": second["id"], "relation": "related_to"}]
        result = reconcile_records([first, second], {}, {})
        self.assertEqual(len(result["records"]), 2)
        self.assertNotEqual(result["routes"][0]["event_id"], result["routes"][1]["event_id"])

    def test_yearless_reused_url_needs_review(self):
        first = record("first", title="Example workshop", start=None, url="https://example.org/events")
        second = record("second", title="Another workshop", start=None, url=first["facts"]["official_url"])
        self.assertEqual(match_strength(second, first), "possible")

    def test_namespaced_source_identity_handles_renamed_record(self):
        old = record("old", title="Event 2027")
        incoming = record("candidate", title="Renamed event 2028", start="2028-05-12")
        old["identity_aliases"] = incoming["identity_aliases"] = ["inspire:123456"]
        self.assertEqual(match_strength(incoming, old), "strong")

    def test_deadline_evidence_remaps_to_combined_array_index(self):
        old, incoming = record("old"), record("candidate")
        registration = {"kind": "registration", "label": "Register", "date": "2027-04-20", "time": None, "timezone": None}
        visa = {"kind": "visa", "label": "Visa request", "date": "2027-04-01", "time": None, "timezone": None}
        old["facts"]["deadlines"] = [registration]
        incoming["facts"]["deadlines"] = [visa]
        incoming["evidence"] = [{"field": "/facts/deadlines/0/date", "source_id": incoming["sources"][0]["id"],
                                 "excerpt": "Visa deadline April 1"}]
        merged, suggestions, changed = merge_record(old, incoming)
        self.assertEqual(merged["facts"]["deadlines"], [registration, visa])
        self.assertEqual(merged["evidence"][0]["field"], "/facts/deadlines/1/date")
        old["editor_overrides"] = ["/facts/deadlines/0/date"]
        protected, suggestions, changed = merge_record(old, incoming)
        self.assertEqual(protected["facts"]["deadlines"], [registration])
        self.assertTrue(suggestions)


class ReconciliationStorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        path = self.root / "schema/event.schema.json"
        path.parent.mkdir()
        path.write_bytes((ROOT / "schema/event.schema.json").read_bytes())
        self.bundle = self.root / "local/review/input"
        self.candidate = record("candidate")
        save_json(self.bundle / "candidate.json", self.candidate)
        save_json(self.bundle / "manifest.json", {"candidates": [{"id": "candidate"}]})

    def test_repeat_keeps_edits_and_never_writes_registry(self):
        directory, plan, reused = prepare(self.bundle, self.root)
        self.assertFalse(reused)
        candidate_path = directory / (plan["record_ids"][0] + ".json")
        candidate_path.write_text("Editor working copy")
        self.assertTrue(prepare(self.bundle, self.root)[2])
        self.assertEqual(candidate_path.read_text(), "Editor working copy")
        self.assertFalse((self.root / "records").exists())
        self.assertFalse(plan["eligible_for_export"])
        self.assertEqual(plan["api_requests"], 0)
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual((directory / "plan.json").stat().st_mode & 0o777, 0o600)

    def test_registry_exclusion_overrides_earlier_pending_allocation(self):
        directory, plan, reused = prepare(self.bundle, self.root)
        event_id = plan["record_ids"][0]
        authoritative = json.loads((directory / (event_id + ".json")).read_text())
        authoritative["decision"].update(status="hidden", reason="Editor removed event")
        path = self.root / "records" / (event_id + ".json")
        save_json(path, authoritative)
        before = path.read_bytes()
        directory, plan, reused = prepare(self.bundle, self.root)
        self.assertEqual(plan["routes"][0]["status"], "excluded")
        self.assertEqual(plan["record_ids"], [])
        self.assertEqual(path.read_bytes(), before)

    def test_stale_approval_is_rejected_before_planning(self):
        authoritative = record("canonical")
        authoritative.update(revision=2)
        authoritative["decision"].update(status="approved", reviewed_revision=1)
        save_json(self.root / "records/canonical.json", authoritative)
        with self.assertRaisesRegex(ValueError, "Approval"):
            prepare(self.bundle, self.root)
