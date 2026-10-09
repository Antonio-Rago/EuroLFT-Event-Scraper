"""Protect evaluation provenance and holdout integrity using fictional cases."""

import copy
import unittest

from tools.event_collector.check_corpus import check_corpus, input_hash
from tools.event_collector.prepare_corpus_inputs import sanitize_text


def fixture():
    text = {"schema_version": 1, "case_id": "example", "source_url": None,
            "subject": "Example", "body": "An example announcement.", "links": [], "posted_at": None}
    case = {"case_id": "example", "basis": "synthetic", "group": "example-group", "split": "development",
            "source_url": None, "input_sha256": input_hash(text), "annotation_status": "draft_for_editor_review",
            "expected": {"message_disposition": "no_event", "reason": "Fictional non-event.", "events": [],
                         "evidence": [{"source_field": "body", "start": 0, "end": 2,
                                       "supports": ["/message_disposition"]}]}}
    return {"cases": [case], "update_sequences": []}, text


class CorpusTests(unittest.TestCase):
    def test_modified_input_cannot_silently_reuse_labels(self):
        labels, text = fixture()
        text["body"] += " Changed dates."
        with self.assertRaisesRegex(ValueError, "hash changed"):
            check_corpus(labels, lambda case: text)

    def test_invalid_evidence_offset_is_rejected(self):
        labels, text = fixture()
        labels["cases"][0]["expected"]["evidence"][0]["end"] = 5000
        with self.assertRaisesRegex(ValueError, "offset"):
            check_corpus(labels, lambda case: text)

    def test_related_messages_cannot_cross_holdout_boundary(self):
        labels, text = fixture()
        second = copy.deepcopy(labels["cases"][0])
        second["case_id"] = "example-2"
        second["split"] = "held_out"
        second_input = dict(text, case_id="example-2")
        second["input_sha256"] = input_hash(second_input)
        labels["cases"].append(second)
        with self.assertRaisesRegex(ValueError, "group crosses"):
            check_corpus(labels, lambda case: text if case["case_id"] == "example" else second_input)

    def test_non_event_cannot_have_no_reason(self):
        labels, text = fixture()
        labels["cases"][0]["expected"]["reason"] = None
        with self.assertRaisesRegex(ValueError, "rationale"):
            check_corpus(labels, lambda case: text)

    def test_contact_redaction_preserves_dates(self):
        text = "Workshop July 12-13, 2027.\nEmail: test@example.org\nTel:+1234567\nJoin https://test.zoom.us/j/123?pwd=secret"
        clean = sanitize_text(text)
        self.assertIn("July 12-13, 2027", clean)
        self.assertNotIn("test@example.org", clean)
        self.assertNotIn("+1234567", clean)
        self.assertNotIn("secret", clean)


if __name__ == "__main__":
    unittest.main()
