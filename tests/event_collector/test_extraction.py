"""Offline extractor/evaluator regression tests; no model calls or quality claims."""

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

from tools.event_collector.check_corpus import input_hash
from tools.event_collector.credentials import key_path, load_key, store_key
from tools.event_collector.evaluate import aggregate, evaluate_run, match_events, score_case, main as evaluate_main
from tools.event_collector.extract import (Quota, NoRedirect, call_gemini, extract_one,
                                         usage_metadata, main as extract_main, PilotFailure)
from tools.event_collector.extraction import (ROOT, MODEL, build_schema, load_contract,
                                            parse_response, request_body, request_identity,
                                            source_span, validate_result)
from tools.event_collector.replay_fixtures import fixture_result
from tools.event_collector.revalidate import revalidate


class SourceSpanTests(unittest.TestCase):
    def evidence(self, quote, field="body", link_index=None):
        return {"source_field": field, "link_index": link_index, "quote": quote,
                "supports": ["/facts/title"], "derivation": None}

    def test_whitespace_variants_preserve_original_unicode_offsets(self):
        for field in ("body", "subject"):
            for whitespace in ("\n ", "\r\n\t", "   ", "\u00a0", "\n\n"):
                with self.subTest(field=field, whitespace=repr(whitespace)):
                    original = "Lattice" + whitespace + "2027: 20–26 June."
                    record = {field: "🧪 Préface: " + original + " Trailing text."}
                    evidence = self.evidence("Lattice 2027: 20–26 June.", field)
                    unchanged = copy.deepcopy(evidence)
                    span = source_span(evidence, record)
                    self.assertEqual(span["quote"], original)
                    self.assertEqual(record[field][span["start"]:span["end"]], original)
                    self.assertEqual(span["model_quote"], evidence["quote"])
                    self.assertEqual(span["match_method"], "whitespace")
                    self.assertEqual(evidence, unchanged)

    def test_whitespace_in_model_quote_can_differ_from_source(self):
        span = source_span(self.evidence("  Lattice\n\t2027  "), {"body": "Lattice 2027"})
        self.assertEqual((span["start"], span["end"], span["quote"]), (0, 12, "Lattice 2027"))

    def test_exact_matches_preserve_existing_cache_metadata(self):
        evidence = self.evidence("Lattice 2027")
        self.assertEqual(source_span(evidence, {"body": "A Lattice 2027 event"}),
                         {**evidence, "start": 2, "end": 14})

    def test_whitespace_matching_does_not_repair_content(self):
        original = "Lattice\n2027 in Nicosia, 20–26 June 2027."
        altered = ("Lattice 2027 in Atlantis, 20–26 June 2027.",
                   "Lattice 2027 in Nicosia, 21–26 June 2027.",
                   "Lattice 2027...20–26 June 2027.",
                   "Lattice 2027 in Nicosia 20–26 June 2027.",
                   "Lattice 2027 in Nicosia, 20-26 June 2027.",
                   "lattice 2027 in Nicosia, 20–26 June 2027.",
                   "Lattice2027 in Nicosia, 20–26 June 2027.",
                   "Lat tice 2027 in Nicosia, 20–26 June 2027.",
                   r"Lattice 2027 in Nicosia, 20\u201326 June 2027.")
        for quote in altered:
            with self.subTest(quote=quote), self.assertRaisesRegex(ValueError, "does not occur"):
                source_span(self.evidence(quote), {"body": original})

    def test_whitespace_only_evidence_remains_invalid(self):
        with self.assertRaisesRegex(ValueError, "Empty source"):
            source_span(self.evidence("\n\t \u00a0"), {"body": "Example"})

    def test_link_quotes_require_exact_full_url(self):
        record = {"links": ["https://example.org/event"]}
        for quote in (" https://example.org/event ", "https://example.org/\nevent", "example.org/event"):
            with self.subTest(quote=quote), self.assertRaisesRegex(ValueError, "full URL"):
                source_span(self.evidence(quote, "links", 0), record)
        span = source_span(self.evidence(record["links"][0], "links", 0), record)
        self.assertEqual(record["links"][0][span["start"]:span["end"]], span["quote"])


class ExtractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry, cls.schema, cls.prompt = load_contract()
        cls.labels = json.loads((ROOT / "evaluation/labels.json").read_text())

    def setUp(self):
        self.case = next(c for c in self.labels["cases"] if c["case_id"] == "synthetic-06")
        self.record = json.loads((ROOT / "evaluation/synthetic_inputs/synthetic-06.json").read_text())
        self.result = fixture_result(self.case, self.record)
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.now = 1000000.0
        self.quota = Quota(self.directory, now=lambda: self.now)
        self.addCleanup(self.temporary.cleanup)

    def response(self, result=None):
        return {"candidates": [{"finishReason": "STOP", "content": {"parts": [
            {"text": json.dumps(result if result is not None else self.result)}]}}],
            "usageMetadata": {"promptTokenCount": 1000, "candidatesTokenCount": 500, "thoughtsTokenCount": 3},
            "modelVersion": MODEL}

    def validate(self):
        return validate_result(self.result, self.record, self.registry, self.schema)

    def run_extract(self, transport, retry=False):
        return extract_one(self.record, self.directory, self.registry, self.schema, self.prompt,
                           self.quota, transport=transport, retry_failed=retry)

    def test_full_registry_schema_examples(self):
        Draft202012Validator.check_schema(self.registry)
        validator = Draft202012Validator(self.registry, format_checker=FormatChecker())
        manifest = json.loads((ROOT / "examples/manifest.json").read_text())
        for entry in manifest["cases"]:
            with self.subTest(entry=entry["file"]):
                validator.validate(json.loads((ROOT / "examples" / entry["file"]).read_text()))
        hidden = json.loads((ROOT / "examples/04-manual-entry.json").read_text())
        hidden["facts"]["start_date"] = None
        with self.assertRaises(ValidationError):
            validator.validate(hidden)

    def test_strict_schema_is_complete_and_synchronised(self):
        self.assertEqual(build_schema(self.registry), self.schema)
        Draft202012Validator.check_schema(self.schema)
        self.assertEqual(set(self.registry["$defs"]["facts"]["properties"]),
                         set(self.schema["properties"]["events"]["items"]["properties"]["facts"]["properties"]))
        def check(node):
            if isinstance(node, dict):
                self.assertFalse(set(node) & {"allOf", "if", "then", "not", "$ref"})
                if node.get("type") == "object":
                    self.assertFalse(node["additionalProperties"])
                    self.assertEqual(set(node["required"]), set(node["properties"]))
                for value in node.values():
                    check(value)
            elif isinstance(node, list):
                for value in node:
                    check(value)
        check(self.schema)

    def test_no_gold_metadata_in_request(self):
        record = {**self.record, "expected": "SECRET_GOLD", "split": "SECRET_SPLIT", "event_key": "SECRET_ID"}
        request = request_body(record, self.schema, self.prompt)
        text = json.dumps(request)
        self.assertNotIn("SECRET_", text)
        self.assertNotIn("case_id", text)
        self.assertNotIn("tools", request)
        self.assertEqual(request["generationConfig"]["maxOutputTokens"], 8192)

    def test_quote_offsets_verified_on_overnight_event(self):
        offsets = self.validate()
        self.assertEqual(offsets[0]["local_id"], "e1")
        for span in offsets[0]["spans"]:
            self.assertEqual(self.record[span["source_field"]][span["start"]:span["end"]], span["quote"])

    def test_hallucinated_quote_rejected(self):
        self.result["events"][0]["evidence"][0]["quote"] = "Imaginary quotation"
        with self.assertRaisesRegex(ValueError, "does not occur"):
            self.validate()

    def test_full_validation_and_cache_preserve_whitespace_evidence_and_result(self):
        original = copy.deepcopy(self.result)
        self.record["body"] = self.record["body"].replace(" ", "\n\t ")
        artifact, cached = self.run_extract(lambda request: self.response())
        self.assertFalse(cached)
        self.assertEqual(artifact["result"], original)
        self.assertEqual(artifact["editorial_status"], "pending")
        spans = artifact["evidence_offsets"][0]["spans"]
        self.assertTrue(any(span.get("match_method") == "whitespace" for span in spans))
        for span in spans:
            self.assertEqual(self.record[span["source_field"]][span["start"]:span["end"]], span["quote"])
        cached_artifact, cached = self.run_extract(lambda request: self.fail("Cache must avoid an API call"))
        self.assertTrue(cached)
        self.assertEqual(cached_artifact, artifact)

    def test_invented_url_rejected_even_with_a_real_quote(self):
        event = self.result["events"][0]
        event["facts"]["official_url"] = "https://invented.example/"
        event["evidence"][0]["supports"].append("/facts/official_url")
        with self.assertRaisesRegex(ValueError, "URL was invented"):
            self.validate()

    def test_invalid_url_rejection_preserves_private_proposal_and_candidate_path(self):
        event = self.result["events"][0]
        event["facts"]["official_url"] = "Conference website"
        with self.assertRaises(ValidationError) as caught:
            self.run_extract(lambda request: self.response())
        details = caught.exception.pilot_details
        self.assertEqual(details["field_path"], "/events/0/facts/official_url")
        self.assertIn("JSON null", details["field_expectation"])
        rejected_path = Path(details["rejected_proposal"])
        saved = json.loads(rejected_path.read_text())
        integrity = saved.pop("integrity_sha256")
        self.assertEqual(input_hash(saved), integrity)
        self.assertEqual(saved["result"], self.result)
        self.assertEqual(saved["status"], "validation_failed")
        self.assertEqual(saved["editorial_status"], "pending")
        self.assertEqual(rejected_path.stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.directory / "cache").exists())

    def test_classification_evidence_cannot_be_omitted_from_output(self):
        del self.result["events"][0]["classification_evidence"]["message_role"]
        with self.assertRaises(ValidationError):
            self.validate()

    def test_attached_classification_quote_must_occur_in_source(self):
        self.result["events"][0]["classification_evidence"]["message_role"]["quote"] = "Invented classification quote"
        with self.assertRaisesRegex(ValueError, "does not occur"):
            self.validate()

    def test_explicit_unknown_field_may_have_source_evidence(self):
        self.result["events"][0]["evidence"].append({"source_field": "body", "link_index": None,
            "quote": "It is online only.", "supports": ["/facts/location/venue"],
            "derivation": "No physical venue is stated for this online event."})
        self.validate()
        self.assertIsNone(self.result["events"][0]["facts"]["location"]["venue"])
        self.result["events"][0]["evidence"][-1]["supports"] = ["/facts/nonexistent"]
        with self.assertRaisesRegex(ValueError, "unsupported field"):
            self.validate()

    def test_revalidation_reuses_unchanged_api_result_without_another_call(self):
        self.result["events"][0]["evidence"].append({"source_field": "body", "link_index": None,
            "quote": "It is online only.", "supports": ["/facts/location/venue"], "derivation": None})
        with patch("tools.event_collector.extract.validate_result", side_effect=ValueError("Simulated former validator bug")):
            with self.assertRaises(ValueError):
                self.run_extract(lambda request: self.response())
        proposal = json.loads(next((self.directory / "rejected").glob("*.json")).read_text())
        def contract(root, version):
            return load_contract(ROOT, version)
        with patch("tools.event_collector.revalidate.load_contract", side_effect=contract):
            path = revalidate(proposal, self.record, root=self.directory)
        saved = json.loads(path.read_text())
        self.assertEqual(saved["result"], self.result)
        self.assertEqual(saved["editorial_status"], "pending")
        self.assertEqual(saved["revalidated_from_sha256"], proposal["integrity_sha256"])
        proposal["result"]["events"][0]["facts"]["title"] = "Altered response"
        with self.assertRaisesRegex(ValueError, "proposal changed"):
            revalidate(proposal, self.record, root=self.directory)

    def test_revalidation_of_wrapped_evidence_retains_immutable_response(self):
        self.record["body"] = self.record["body"].replace(" ", "\r\n\t")
        with patch("tools.event_collector.extract.validate_result", side_effect=ValueError("Former exact-only matcher")):
            with self.assertRaises(ValueError):
                self.run_extract(lambda request: self.response())
        proposal_path = next((self.directory / "rejected").glob("*.json"))
        original = proposal_path.read_bytes()
        proposal = json.loads(original)
        with patch("tools.event_collector.revalidate.load_contract", side_effect=lambda root, version: load_contract(ROOT, version)):
            path = revalidate(proposal, self.record, root=self.directory)
        artifact = json.loads(path.read_text())
        self.assertEqual(artifact["result"], self.result)
        self.assertEqual(proposal_path.read_bytes(), original)
        self.assertEqual(artifact["editorial_status"], "pending")
        self.assertTrue(any(span.get("match_method") == "whitespace"
                            for span in artifact["evidence_offsets"][0]["spans"]))

    def test_revalidation_reports_missing_coverage_after_whitespace_matches(self):
        self.record["body"] = self.record["body"].replace(" ", "\n\t")
        for evidence in self.result["events"][0]["evidence"]:
            evidence["supports"] = [p for p in evidence["supports"] if p != "/facts/start_date"]
        with self.assertRaises(ValueError):
            self.run_extract(lambda request: self.response())
        proposal = json.loads(next((self.directory / "rejected").glob("*.json")).read_text())
        with patch("tools.event_collector.revalidate.load_contract", side_effect=lambda root, version: load_contract(ROOT, version)):
            with self.assertRaises(ValueError) as caught:
                revalidate(proposal, self.record, root=self.directory)
        self.assertEqual(caught.exception.pilot_details["missing_evidence_fields"], ["/facts/start_date"])
        self.assertEqual(caught.exception.pilot_details["stage"], "source_validation")
        self.assertFalse((self.directory / "local/extraction/cache").exists())

    def test_keep_going_preserves_failure_and_processes_remaining_cases(self):
        import io
        from contextlib import redirect_stdout
        root = self.directory / "collector"
        (root / "evaluation").mkdir(parents=True)
        cases = [{**self.case, "case_id": value, "split": "development"} for value in ("synthetic-a", "synthetic-b")]
        (root / "evaluation/labels.json").write_text(json.dumps({"cases": cases}))
        artifact = {"mode": "live", "model": MODEL, "editorial_status": "pending", "result": self.result}
        with patch("tools.event_collector.extract.load_contract", return_value=(self.registry, self.schema, self.prompt)), \
                patch("tools.event_collector.extract.load_input", return_value=self.record), \
                patch("tools.event_collector.extract.load_key", return_value="fake-test-key-never-send"), \
                patch("tools.event_collector.extract.extract_one", side_effect=[PilotFailure("fixture_failure", "Fixture failed"), (artifact, False)]) as extract, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(extract_main(["--root", str(root), "--live", "--limit", "2", "--keep-going"]), 1)
        self.assertEqual(extract.call_count, 2)
        report = json.loads(next((root / "local/extraction/runs").glob("*/predictions.json")).read_text())
        self.assertEqual(report["status"], "completed_with_failures")
        self.assertEqual([c["status"] for c in report["cases"]], ["failed", "validated"])

    def test_missing_field_evidence_rejected(self):
        for evidence in self.result["events"][0]["evidence"]:
            evidence["supports"] = [p for p in evidence["supports"] if p != "/facts/start_date"]
        self.result["events"][0]["evidence"] = [e for e in self.result["events"][0]["evidence"] if e["supports"]]
        with self.assertRaisesRegex(ValueError, "lack quoted"):
            self.validate()

    def test_editorial_fields_cannot_be_supplied_by_model(self):
        self.result["events"][0]["decision"] = {"status": "approved"}
        with self.assertRaises(ValidationError):
            self.validate()

    def test_impossible_date_and_short_time_rejected(self):
        for field, value in (("start_date", "2027-02-30"), ("start_time", "1:30")):
            with self.subTest(field=field):
                self.result = fixture_result(self.case, self.record)
                self.result["events"][0]["facts"][field] = value
                with self.assertRaises((ValidationError, ValueError)):
                    self.validate()

    def test_missing_dates_and_scope_require_review_flags(self):
        event = self.result["events"][0]
        event["relevance"] = "review"
        with self.assertRaisesRegex(ValueError, "scope_review"):
            self.validate()
        event["review_flags"] = ["scope_review"]
        event["facts"]["start_date"] = None
        with self.assertRaisesRegex(ValueError, "missing_dates"):
            self.validate()

    def test_missing_relation_target_rejected(self):
        self.result["events"][0]["related_events"] = [{"local_id": "e2", "relation": "satellite_of",
            "evidence": {"source_field": "body", "link_index": None, "quote": "fixture", "derivation": None}}]
        with self.assertRaisesRegex(ValueError, "target absent"):
            self.validate()

    def test_response_failures_are_not_repaired(self):
        samples = []
        response = self.response()
        response["candidates"][0]["finishReason"] = "MAX_TOKENS"
        samples.append(response)
        response = self.response()
        response["promptFeedback"] = {"blockReason": "SAFETY"}
        samples.append(response)
        response = self.response()
        response["candidates"][0]["content"]["parts"][0]["text"] = "```json\n{}\n```"
        samples.append(response)
        response = self.response()
        response["candidates"][0]["content"]["parts"] = [{"functionCall": {"name": "publish"}}]
        samples.append(response)
        for response in samples:
            with self.subTest(response=response):
                with self.assertRaises(ValueError):
                    parse_response(response)

    def test_cache_avoids_another_call_and_tamper_is_rejected(self):
        calls = []
        def transport(request):
            calls.append(request)
            return self.response()
        artifact, cached = self.run_extract(transport)
        self.assertFalse(cached)
        self.assertEqual(artifact["editorial_status"], "pending")
        self.assertTrue(self.run_extract(transport)[1])
        self.assertEqual(len(calls), 1)
        path = next((self.directory / "cache").glob("*.json"))
        data = json.loads(path.read_text())
        data["result"]["events"][0]["facts"]["title"] = "Changed cache"
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "integrity"):
            self.run_extract(transport)
        self.assertEqual(len(calls), 1)

    def test_changed_prompt_input_or_model_invalidates_identity(self):
        request = request_body(self.record, self.schema, self.prompt)
        identity = request_identity(self.record, request)
        self.assertNotEqual(identity, request_identity(self.record, request_body(self.record, self.schema, self.prompt + "v2")))
        changed = {**self.record, "body": self.record["body"] + "changed"}
        self.assertNotEqual(identity, request_identity(changed, request_body(changed, self.schema, self.prompt)))
        self.assertNotEqual(identity, request_identity(self.record, request, model="other-model"))

    def test_failed_call_counts_and_requires_explicit_retry(self):
        def fail(request):
            raise ValueError("Simulated HTTP 429")
        with self.assertRaises(ValueError):
            self.run_extract(fail)
        self.now += 61
        with self.assertRaisesRegex(ValueError, "Prior attempt"):
            self.run_extract(lambda request: self.response())
        self.assertFalse(self.run_extract(lambda request: self.response(), retry=True)[1])
        ledger = json.loads((self.directory / "quota.json").read_text())
        self.assertEqual([e["status"] for e in ledger["requests"]], ["failed", "validated"])

    def test_daily_cap_pacing_and_rolling_window(self):
        quota = Quota(self.directory, limit=1, now=lambda: self.now)
        quota.reserve("first")
        self.assertEqual(quota.wait_seconds(), 60)
        self.now += 61
        with self.assertRaisesRegex(ValueError, "daily request cap"):
            quota.reserve("second")
        self.now += 86400
        quota.reserve("second")

    def test_missing_key_no_network_and_auth_redirect_disabled(self):
        with patch("tools.event_collector.extract.load_key", return_value=None), patch("tools.event_collector.extract.build_opener") as opener:
            with self.assertRaisesRegex(ValueError, "not configured"):
                call_gemini({})
            opener.assert_not_called()
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.example/"))

    def test_token_usage_includes_thinking_and_rejects_invalid_counts(self):
        self.assertEqual(usage_metadata(self.response()), {"input_tokens": 1000, "output_tokens": 500, "thinking_tokens": 3})
        response = self.response()
        response["usageMetadata"]["promptTokenCount"] = -1
        with self.assertRaises(ValueError):
            usage_metadata(response)

    def test_wrong_date_and_invented_city_score_as_errors(self):
        event = self.result["events"][0]
        event["facts"]["start_date"] = "2027-06-06"
        event["facts"]["location"]["city"] = "Copenhagen"
        score = score_case(self.case, self.result)
        report = aggregate([score])
        self.assertEqual(report["event_date_errors"], 1)
        self.assertEqual(report["unexpected_non_null_fields"], 1)

    def test_duplicate_predictions_require_adjudication(self):
        predicted = [self.result["events"][0], copy.deepcopy(self.result["events"][0])]
        matching = match_events(self.case["expected"]["events"], predicted)
        self.assertEqual(matching["matches"], [])
        self.assertEqual(matching["ambiguous_predicted"], [0, 1])

    def test_missed_event_is_counted_separately_from_field_errors(self):
        score = score_case(self.case, {"disposition": "no_event", "events": []})
        report = aggregate([score])
        self.assertEqual(report["missing_events"], 1)
        self.assertEqual(report["checked_fields"], 0)
        self.assertEqual(report["recall_confirmed_matches"], 0)

    def test_http_429_has_safe_diagnostic_without_key_or_response_body(self):
        with patch("tools.event_collector.extract.build_opener") as opener:
            opener.return_value.open.side_effect = HTTPError("https://example.invalid/", 429, "rate limit", {}, None)
            with self.assertRaisesRegex(ValueError, "HTTP error 429") as caught:
                call_gemini({}, key="fake-test-key-never-send")
            self.assertEqual(caught.exception.code, "http_429")
            self.assertNotIn("fake-test-key", str(caught.exception))

    def test_private_credentials_roundtrip_and_permissions(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(load_key(self.directory))
            store_key("fake-test-key-never-send", self.directory)
            self.assertEqual(load_key(self.directory), "fake-test-key-never-send")
            self.assertEqual(key_path(self.directory).stat().st_mode & 0o777, 0o600)
            key_path(self.directory).chmod(0o644)
            with self.assertRaisesRegex(ValueError, "owner-only"):
                load_key(self.directory)

    def test_fixture_cannot_be_reported_as_live_accuracy(self):
        artifact = {"mode": "fixture_replay", "input_sha256": input_hash(self.record),
                    "prompt_sha256": input_hash(self.prompt), "schema_sha256": input_hash(self.schema),
                    "result": self.result}
        run = {"mode": "live", "model": MODEL, "split": "held_out",
               "prompt_sha256": input_hash(self.prompt), "schema_sha256": input_hash(self.schema),
               "cases": [{"case_id": self.case["case_id"], "status": "validated", "artifact": artifact}]}
        with self.assertRaisesRegex(ValueError, "mixes fixture"):
            evaluate_run(run, {"cases": [self.case], "update_sequences": []}, lambda case: self.record,
                         self.registry, self.schema, self.prompt)

    def test_deadline_date_is_scored_separately_from_event_date(self):
        case = copy.deepcopy(self.case)
        deadline = {"kind": "registration", "label": "Registration", "date": "2027-05-01", "time": None, "timezone": None}
        case["expected"]["events"][0]["facts"]["deadlines"] = [deadline]
        self.result["events"][0]["facts"]["deadlines"] = [{**deadline, "date": "2027-06-07"}]
        self.result["events"][0]["facts"]["start_date"] = "2027-05-01"
        report = aggregate([score_case(case, self.result)])
        self.assertEqual(report["event_date_errors"], 1)
        self.assertEqual(report["field_errors"], 2)
        self.assertEqual(report["deadline_checks"], 1)

    def test_multi_event_message_recovers_satellites_and_counts_missing_one(self):
        case = next(c for c in self.labels["cases"] if c["case_id"] == "real-43")
        # Scorer-only fixture: no source/network dependency in the portable test.
        result = fixture_result(case, {"subject": "fixture", "body": "fixture"})
        score = score_case(case, result)
        self.assertTrue(score["complete_multi_event_recovery"])
        self.assertEqual(score["matched_events"], 3)
        result["events"].pop()
        score = score_case(case, result)
        self.assertFalse(score["complete_multi_event_recovery"])
        self.assertEqual(len(score["matching"]["missing_expected"]), 1)

    def test_running_or_failed_run_cannot_pass_quality_gate(self):
        # Simulated live metadata is a gate unit test, not a model evaluation.
        case = {**self.case, "basis": "real"}
        artifact = {"mode": "live", "model": MODEL, "returned_model_version": "simulated-test-version",
                    "input_sha256": input_hash(self.record), "prompt_sha256": input_hash(self.prompt),
                    "schema_sha256": input_hash(self.schema), "result": self.result}
        run = {"mode": "live", "model": MODEL, "split": "held_out", "status": "running",
               "prompt_sha256": input_hash(self.prompt), "schema_sha256": input_hash(self.schema),
               "cases": [{"case_id": case["case_id"], "status": "validated", "artifact": artifact}]}
        for status in ("running", "failed"):
            run["status"] = status
            report = evaluate_run(run, {"cases": [case], "update_sequences": []}, lambda case: self.record,
                                  self.registry, self.schema, self.prompt)
            self.assertEqual(report["pilot_gate"], "not_assessed")

    def test_old_prompt_run_remains_evaluable_after_prompt_upgrade(self):
        import io
        from contextlib import redirect_stdout
        root = self.directory / "collector"
        for filename in ("schema/event.schema.json", "extraction/response.schema.json", "extraction/response-v1.schema.json",
                         "extraction/response-v2.schema.json", "extraction/prompt-v1.txt", "extraction/prompt-v2.txt",
                         "extraction/prompt-v3.txt", "extraction/prompt-v4.txt", "extraction/prompt-v5.txt"):
            path = root / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((ROOT / filename).read_bytes())
        (root / "evaluation/synthetic_inputs").mkdir(parents=True)
        (root / "evaluation/synthetic_inputs/synthetic-06.json").write_text(json.dumps(self.record))
        (root / "evaluation/labels.json").write_text(json.dumps({"cases": [self.case], "update_sequences": []}))
        old_registry, old_schema, old_prompt = load_contract(ROOT, "v1")
        artifact = {"mode": "fixture_replay", "input_sha256": input_hash(self.record),
                    "prompt_sha256": input_hash(old_prompt), "schema_sha256": input_hash(old_schema),
                    "result": fixture_result(self.case, self.record, schema_version=1)}
        run = {"mode": "fixture_replay", "model": None, "split": "held_out", "status": "completed",
               "prompt_sha256": input_hash(old_prompt), "schema_sha256": input_hash(old_schema),
               "cases": [{"case_id": self.case["case_id"], "status": "validated", "artifact": artifact}]}
        path = self.directory / "predictions.json"
        path.write_text(json.dumps(run))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(evaluate_main([str(path), "--root", str(root)]), 0)
        report = json.loads((path.parent / "evaluation.json").read_text())
        self.assertEqual(report["prompt_sha256"], input_hash(old_prompt))
        self.assertEqual(report["pilot_gate"], "not_assessed")


if __name__ == "__main__":
    unittest.main()
