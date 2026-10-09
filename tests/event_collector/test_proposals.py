"""Review routing tests with synthetic source text; no API or Git changes."""

import copy
import json
from pathlib import Path
import tempfile
import unittest

from tools.event_collector.check_corpus import input_hash
from tools.event_collector.extract import Quota, extract_one, save_json
from tools.event_collector.extraction import ROOT, MODEL, PROMPT_VERSIONS, load_contract
from tools.event_collector.proposals import prepare, md
from tools.event_collector.replay_fixtures import fixture_result


class ProposalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        files = ["schema/event.schema.json", "extraction/response.schema.json",
                 "extraction/response-v1.schema.json", "extraction/response-v2.schema.json"]
        files += ["extraction/prompt-" + version + ".txt" for version in PROMPT_VERSIONS]
        for filename in files:
            path = self.root / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((ROOT / filename).read_bytes())
        labels = json.loads((ROOT / "evaluation/labels.json").read_text())
        self.case = copy.deepcopy(next(c for c in labels["cases"] if c["case_id"] == "synthetic-06"))
        self.record = json.loads((ROOT / "evaluation/synthetic_inputs/synthetic-06.json").read_text())
        self.case.update(case_id="real-test", basis="real", split="development")
        self.record.update(case_id="real-test", source_url="https://example.org/announcement")
        self.case["input_sha256"] = input_hash(self.record)
        save_json(self.root / "evaluation/labels.json", {"cases": [self.case]})
        save_json(self.root / "local/evaluation_inputs/real-test.json", self.record)
        self.registry, self.schema, self.prompt = load_contract(self.root)
        self.result = fixture_result(self.case, self.record)
        self.directory = self.root / "local/extraction"

    def run_file(self):
        response = {"candidates": [{"finishReason": "STOP", "content": {"parts": [
            {"text": json.dumps(self.result)}]}}], "modelVersion": MODEL,
            "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 100}}
        quota = Quota(self.directory)
        try:
            artifact, cached = extract_one(self.record, self.directory, self.registry, self.schema,
                                          self.prompt, quota, transport=lambda request: response)
            entry = {"case_id": self.case["case_id"], "status": "validated", "artifact": artifact}
        except Exception as exc:
            entry = {"case_id": self.case["case_id"], "status": "failed", **exc.pilot_details}
        run = {"mode": "live", "status": "completed_with_failures" if entry["status"] == "failed" else "completed",
               "model": MODEL, "split": "development", "prompt_sha256": input_hash(self.prompt),
               "schema_sha256": input_hash(self.schema), "cases": [entry]}
        path = self.root / "run.json"
        save_json(path, run)
        return path

    def records(self, directory):
        return [json.loads(path.read_text()) for path in directory.glob("proposed-*.json")
                if not path.name.endswith(".review.json")]

    def test_valid_output_remains_pending_and_provenance_is_unchanged(self):
        path = self.run_file()
        quota_before = (self.directory / "quota.json").read_bytes()
        run_before = path.read_bytes()
        directory, manifest, reused = prepare(path, self.root)
        record = self.records(directory)[0]
        self.assertFalse(reused)
        self.assertEqual(record["facts"], self.result["events"][0]["facts"])
        self.assertEqual(record["decision"]["status"], "pending")
        self.assertIsNone(record["decision"]["reviewed_revision"])
        self.assertFalse(manifest["eligible_for_export"])
        self.assertEqual(manifest["api_requests"], 0)
        self.assertEqual((self.directory / "quota.json").read_bytes(), quota_before)
        self.assertEqual(path.read_bytes(), run_before)
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual(directory.parent.stat().st_mode & 0o777, 0o700)
        for file in directory.iterdir():
            self.assertEqual(file.stat().st_mode & 0o777, 0o600)

    def test_bad_relation_quotes_keep_both_candidates_with_warnings(self):
        first = self.result["events"][0]
        second = copy.deepcopy(first)
        second.update(local_id="e2")
        first["related_events"] = [{"local_id": "e2", "relation": "has_satellite", "evidence": {
            "source_field": "body", "link_index": None, "quote": "Invented [...] relation", "derivation": None}}]
        self.result["events"].append(second)
        path = self.run_file()
        original = next((self.directory / "rejected").glob("*.json")).read_bytes()
        directory, manifest, reused = prepare(path, self.root)
        records = self.records(directory)
        self.assertEqual(len(records), 2)
        self.assertEqual(manifest["candidate_count"], 2)
        primary = next(r for r in records if r["id"].endswith("-e1"))
        self.assertEqual(primary["related_events"][0]["id"], next(r["id"] for r in records if r["id"].endswith("-e2")))
        self.assertTrue(any("quote_unverified" in warning for warning in primary["warnings"]))
        self.assertNotIn("Invented", json.dumps(primary["evidence"]))
        self.assertEqual(next((self.directory / "rejected").glob("*.json")).read_bytes(), original)

    def test_uncertain_relevance_is_retained(self):
        self.result["events"][0].update(relevance="review", review_flags=[])
        directory, manifest, reused = prepare(self.run_file(), self.root)
        self.assertEqual(manifest["candidate_count"], 1)
        self.assertTrue(any("scope_review" in warning for warning in self.records(directory)[0]["warnings"]))

    def test_invented_url_is_unverified_and_does_not_discard_event(self):
        self.result["events"][0]["facts"]["official_url"] = "https://unverified.example.org/"
        directory, manifest, reused = prepare(self.run_file(), self.root)
        record = self.records(directory)[0]
        self.assertEqual(record["facts"]["official_url"], "https://unverified.example.org/")
        self.assertTrue(any("url_unverified" in warning for warning in record["warnings"]))
        self.assertEqual(record["decision"]["status"], "pending")

    def test_invalid_date_needs_repair_without_losing_other_candidate(self):
        second = copy.deepcopy(self.result["events"][0])
        second["local_id"] = "e2"
        self.result["events"].append(second)
        self.result["events"][0]["facts"]["start_date"] = "2027-02-30"
        directory, manifest, reused = prepare(self.run_file(), self.root)
        self.assertEqual(manifest["candidate_count"], 1)
        self.assertEqual(manifest["cases"][0]["repairs"][0]["local_id"], "e1")
        self.assertEqual(self.records(directory)[0]["facts"], second["facts"])

    def test_repeat_preserves_manual_edits(self):
        path = self.run_file()
        directory, manifest, reused = prepare(path, self.root)
        candidate_path = next(path for path in directory.glob("proposed-*.json") if not path.name.endswith(".review.json"))
        candidate = json.loads(candidate_path.read_text())
        candidate["facts"]["title"] = "Editor correction"
        save_json(candidate_path, candidate)
        before = candidate_path.read_bytes()
        self.assertTrue(prepare(path, self.root)[2])
        self.assertEqual(candidate_path.read_bytes(), before)

    def test_synthetic_cases_cannot_enter_review_records(self):
        path = self.run_file()
        self.case["basis"] = "synthetic"
        save_json(self.root / "evaluation/labels.json", {"cases": [self.case]})
        directory, manifest, reused = prepare(path, self.root)
        self.assertEqual(manifest["candidate_count"], 0)
        self.assertEqual(manifest["cases"][0]["status"], "synthetic_excluded")

    def test_tampered_saved_response_is_rejected(self):
        self.result["events"][0]["evidence"][0]["quote"] = "Invented quote"
        path = self.run_file()
        rejected_path = next((self.directory / "rejected").glob("*.json"))
        rejected = json.loads(rejected_path.read_text())
        rejected["result"]["events"][0]["facts"]["title"] = "Tampered"
        save_json(rejected_path, rejected)
        with self.assertRaisesRegex(ValueError, "integrity"):
            prepare(path, self.root)
        self.assertFalse((self.root / "local/review").exists())

    def test_operational_failure_is_visible_without_fabricated_event(self):
        path = self.run_file()
        run = json.loads(path.read_text())
        run["status"] = "failed"
        run["cases"] = [{"case_id": "real-test", "status": "failed", "failure_code": "http_503"}]
        save_json(path, run)
        directory, manifest, reused = prepare(path, self.root)
        self.assertEqual(manifest["candidate_count"], 0)
        self.assertEqual(manifest["cases"][0]["status"], "operational_failure")

    def test_model_text_is_escaped_in_review_document(self):
        self.assertNotIn("<script>", md("<script>alert(1)</script>"))
        self.assertIn(r"\!\[", md("![image](https://example.org/)"))
