"""Structured extraction contract and local evidence validation (no network)."""

import json
import re
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

from .check_corpus import check_facts, input_hash, pointer_value


ROOT = Path(__file__).resolve().parents[2] / "_event_collector"
MODEL = "gemini-3.1-flash-lite"
SUPPORTED_MODELS = ("gemini-3.1-flash-lite", "gemini-3.8-flash")
MAX_OUTPUT_TOKENS = 8192
PROMPT_VERSION = "v5"
PROMPT_VERSIONS = ("v1", "v2", "v3", "v4", "v5")


class EvidenceCoverageError(ValueError):
    def __init__(self, local_id, missing_fields):
        super().__init__("Known fields lack quoted evidence")
        self.local_id = local_id
        self.missing_fields = sorted(missing_fields)


def object_schema(properties):
    return {"type": "object", "additionalProperties": False,
            "properties": properties, "required": list(properties)}


def build_schema(registry_schema, schema_version=3):
    """Use a deliberately small strict-output subset, not the registry conditions."""
    def simplify(node):
        if isinstance(node, list):
            return [simplify(value) for value in node]
        if not isinstance(node, dict):
            return node
        result = {key: simplify(value) for key, value in node.items()
                  if key in {"type", "enum", "anyOf", "properties", "required",
                             "additionalProperties", "items"}}
        if "properties" in node:
            result["properties"] = {name: simplify(value) for name, value in node["properties"].items()}
        if "anyOf" in result and all(set(branch) == {"type"} for branch in result["anyOf"]):
            result = {"type": [branch["type"] for branch in result["anyOf"]]}
        if "enum" in result and "type" not in result:
            result["type"] = "string"
        return result
    nullable_string = {"type": ["string", "null"]}
    string_array = {"type": "array", "items": {"type": "string"}}
    evidence = object_schema({
        "source_field": {"type": "string", "enum": ["subject", "body", "links"]},
        "link_index": {"type": ["integer", "null"]},
        "quote": {"type": "string"},
        "supports": string_array, "derivation": nullable_string,
    })
    relation_properties = {
        "local_id": {"type": "string"},
        "relation": {"type": "string", "enum": ["satellite_of", "has_satellite", "related_to"]},
    }
    quote_schema = object_schema({
        "source_field": {"type": "string", "enum": ["subject", "body", "links"]},
        "link_index": {"type": ["integer", "null"]}, "quote": {"type": "string"},
        "derivation": nullable_string,
    })
    if schema_version >= 2:
        relation_properties["evidence"] = quote_schema
    relation = object_schema(relation_properties)
    event = object_schema({
        "local_id": {"type": "string"},
        "facts": simplify(registry_schema["$defs"]["facts"]),
        "message_role": {"type": "string", "enum": ["announcement", "update", "reminder"]},
        "relevance": {"type": "string", "enum": ["include", "review"]},
        "review_flags": string_array,
        "related_events": {"type": "array", "items": relation},
        "evidence": {"type": "array", "items": evidence},
    })
    if schema_version >= 3:
        event["properties"]["classification_evidence"] = object_schema({
            "type": quote_schema, "lifecycle": quote_schema, "message_role": quote_schema,
            "relevance": quote_schema, "attendance": {"anyOf": [quote_schema, {"type": "null"}]},
        })
        event["required"].append("classification_evidence")
    return object_schema({
        "schema_version": {"type": "integer", "enum": [schema_version]},
        "disposition": {"type": "string", "enum": ["event_candidates", "no_event"]},
        "reason": {"type": "string"},
        "events": {"type": "array", "items": event}, "warnings": string_array,
    })


def load_contract(root=ROOT, prompt_version=PROMPT_VERSION):
    if prompt_version not in PROMPT_VERSIONS:
        raise ValueError("Unknown prompt version")
    registry = json.loads((root / "schema/event.schema.json").read_text(encoding="utf-8"))
    version = {"v1": 1, "v2": 1, "v3": 2, "v4": 3, "v5": 3}[prompt_version]
    filename = "response.schema.json" if version == 3 else "response-v{}.schema.json".format(version)
    schema = json.loads((root / "extraction" / filename).read_text(encoding="utf-8"))
    if schema != build_schema(registry, version):
        raise ValueError("Extraction schema is stale relative to the registry contract")
    return registry, schema, (root / ("extraction/prompt-" + prompt_version + ".txt")).read_text(encoding="utf-8")


def source_payload(record):
    # Never serialize expected facts, labels, split, tags, or gold identities.
    payload = {key: record[key] for key in ("source_url", "subject", "body", "links", "posted_at")}
    if not isinstance(payload["subject"], str) or not isinstance(payload["body"], str):
        raise ValueError("Input text is not a string")
    if not isinstance(payload["links"], list) or any(not isinstance(v, str) for v in payload["links"]):
        raise ValueError("Input links must be literal URL strings")
    return payload


def request_body(record, schema, prompt):
    return {
        "systemInstruction": {"parts": [{"text": prompt}]},
        "contents": [{"role": "user", "parts": [{"text": json.dumps(source_payload(record), ensure_ascii=False)}]}],
        "generationConfig": {
            "candidateCount": 1, "maxOutputTokens": MAX_OUTPUT_TOKENS if schema["properties"]["schema_version"]["enum"] == [3] else 4096,
            "thinkingConfig": {"thinkingLevel": "LOW" if schema["properties"]["schema_version"]["enum"] == [3] else "MINIMAL", "includeThoughts": False},
            "responseFormat": {"text": {"mimeType": "APPLICATION_JSON", "schema": schema}},
        },
    }


def request_identity(record, request, model=MODEL):
    return input_hash({"input_sha256": input_hash(record), "provider": "gemini",
                       "endpoint": "v1beta:generateContent", "model": model, "request": request})


def required_support(event):
    facts = event["facts"]
    pointers = {"/facts/title", "/message_role", "/relevance"}
    for key in ("type", "lifecycle", "attendance"):
        if facts[key] != "unknown":
            pointers.add("/facts/" + key)
    for key in ("start_date", "end_date", "start_time", "end_time", "timezone", "official_url"):
        if facts[key] is not None:
            pointers.add("/facts/" + key)
    for key, value in facts["location"].items():
        if value is not None:
            pointers.add("/facts/location/" + key)
    for index, deadline in enumerate(facts["deadlines"]):
        for key, value in deadline.items():
            if value is not None:
                pointers.add("/facts/deadlines/{}/{}".format(index, key))
    pointers.update("/related_events/{}".format(i) for i in range(len(event["related_events"])))
    return pointers


def allowed_support(event):
    # An explicit absence (e.g. "venue not yet announced") may support a null
    # field. Required coverage remains limited to known facts/classifications.
    pointers = {"/message_role", "/relevance"}
    pointers.update("/facts/" + key for key in ("title", "type", "lifecycle", "attendance",
        "start_date", "end_date", "start_time", "end_time", "timezone", "official_url"))
    pointers.update("/facts/location/" + key for key in ("venue", "city", "country"))
    pointers.update("/facts/deadlines/{}/{}".format(i, key) for i, deadline in enumerate(event["facts"]["deadlines"])
                    for key in deadline)
    pointers.update("/related_events/{}".format(i) for i in range(len(event["related_events"])))
    return pointers


def source_span(evidence, record):
    """Match literal text or whitespace-only differences, retaining source offsets.

    Link quotes remain exact. A whitespace match records the submitted quote
    separately; its verified quote is always the unchanged original source slice.
    """
    field, quote = evidence["source_field"], evidence["quote"]
    if not quote.strip():
        raise ValueError("Empty source evidence")
    if field == "links":
        index = evidence["link_index"]
        if type(index) is not int or not 0 <= index < len(record["links"]):
            raise ValueError("Link evidence index is invalid")
        text = record["links"][index]
        if text != quote:
            raise ValueError("Link evidence must quote the full URL")
    else:
        if evidence["link_index"] is not None:
            raise ValueError("Text evidence cannot have a link index")
        text = record[field]
    start = text.find(quote)
    if start >= 0:
        # Keep exact-match metadata compatible with previously verified caches.
        return {**evidence, "start": start, "end": start + len(quote)}
    if field != "links":
        # Escape every non-whitespace character. Only existing whitespace runs
        # may differ; words, punctuation, dates and character case cannot change.
        pattern = r"\s+".join(re.escape(part) for part in quote.split())
        match = re.search(pattern, text)
        if match:
            return {**evidence, "quote": text[match.start():match.end()],
                    "model_quote": quote, "match_method": "whitespace",
                    "start": match.start(), "end": match.end()}
    raise ValueError("Evidence quote does not occur in the source")


def validate_result(result, record, registry, schema):
    """Return verified quote offsets; this does not prove semantic entailment."""
    Draft202012Validator(schema).validate(result)
    if result["disposition"] != ("event_candidates" if result["events"] else "no_event"):
        raise ValueError("Disposition contradicts candidate count")
    if not result["reason"].strip():
        raise ValueError("Missing extraction rationale")
    if len(result["events"]) > 12:
        raise ValueError("Candidate count exceeds the pilot limit")
    ids = [event["local_id"] for event in result["events"]]
    if len(set(ids)) != len(ids) or any(not re.fullmatch(r"e[1-9][0-9]*", value) for value in ids):
        raise ValueError("Local candidate identities are invalid or duplicated")
    facts_schema = {"$ref": "#/$defs/facts", "$defs": registry["$defs"]}
    validator = Draft202012Validator(facts_schema, format_checker=FormatChecker())
    offsets = []
    for event_index, event in enumerate(result["events"]):
        facts = event["facts"]
        try:
            validator.validate(facts)
        except ValidationError as exc:
            # The facts validator runs against a subdocument. Restore the full
            # result path so a multi-event failure identifies the right candidate.
            exc.path.extendleft(reversed(["events", event_index, "facts"]))
            raise
        check_facts(facts)
        if facts["summary"] is not None:
            raise ValueError("Pilot summaries must remain null")
        if facts["start_date"] == facts["end_date"] and facts["start_date"]:
            if facts["start_time"] and facts["end_time"] and facts["end_time"] < facts["start_time"]:
                raise ValueError("End time precedes start time on the same date")
        flags = event["review_flags"]
        if event["relevance"] == "review" and "scope_review" not in flags:
            raise ValueError("Review-only candidate lacks scope_review")
        if (facts["start_date"] is None or facts["end_date"] is None) and "missing_dates" not in flags:
            raise ValueError("Incomplete dates lack missing_dates")
        if facts["lifecycle"] == "cancelled" and "cancellation_review" not in flags:
            raise ValueError("Cancellation lacks review flag")
        supported = set()
        spans = []
        for relation_index, related in enumerate(event["related_events"]):
            if related["local_id"] not in ids or related["local_id"] == event["local_id"]:
                raise ValueError("Relation target absent or self-referential")
            if result["schema_version"] >= 2:
                pointer = "/related_events/" + str(relation_index)
                span = source_span(related["evidence"], record)
                spans.append({**span, "supports": [pointer]})
                supported.add(pointer)
        if result["schema_version"] >= 3:
            for field, evidence in event["classification_evidence"].items():
                pointer = "/" + field if field in ("message_role", "relevance") else "/facts/" + field
                if evidence is None:
                    if field != "attendance" or facts["attendance"] != "unknown":
                        raise EvidenceCoverageError(event["local_id"], {pointer})
                    continue
                span = source_span(evidence, record)
                spans.append({**span, "supports": [pointer]})
                supported.add(pointer)
        for evidence in event["evidence"]:
            if not evidence["supports"]:
                raise ValueError("Empty source evidence")
            span = source_span(evidence, record)
            for pointer in evidence["supports"]:
                if pointer not in allowed_support(event):
                    raise ValueError("Evidence points to an unsupported field")
                pointer_value(event, pointer)
                supported.add(pointer)
            spans.append(span)
        if not required_support(event) <= supported:
            raise EvidenceCoverageError(event["local_id"], required_support(event) - supported)
        url = facts["official_url"]
        if url and not any(url in text for text in [record["subject"], record["body"], *record["links"]]):
            raise ValueError("Official URL was invented")
        offsets.append({"local_id": event["local_id"], "spans": spans})
    return offsets


def parse_response(response):
    if response.get("error") or response.get("promptFeedback", {}).get("blockReason"):
        raise ValueError("API failed or blocked the prompt")
    candidates = response.get("candidates", [])
    if len(candidates) != 1 or candidates[0].get("finishReason") != "STOP":
        raise ValueError("API response was refused, incomplete, or had an unexpected candidate count")
    parts = candidates[0].get("content", {}).get("parts", [])
    if not parts or any("text" not in part or part.get("thought") or
                        "functionCall" in part or "executableCode" in part for part in parts):
        raise ValueError("Expected only structured text; tools/thoughts are not extraction output")
    # No stripping fences, repairing JSON, or accepting truncated responses.
    return json.loads("".join(part["text"] for part in parts))


def main():
    """Explicitly regenerate the checked-in API schema after contract changes."""
    registry = json.loads((ROOT / "schema/event.schema.json").read_text(encoding="utf-8"))
    path = ROOT / "extraction/response.schema.json"
    path.write_text(json.dumps(build_schema(registry), indent=2) + "\n", encoding="utf-8")
    print(path)


if __name__ == "__main__":
    main()
