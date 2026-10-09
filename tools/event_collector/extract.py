"""Run a Gemini free-tier pilot; preflight only unless --live is explicit."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid

from jsonschema.exceptions import ValidationError

from .check_corpus import input_hash
from .credentials import load_key
from .extraction import (ROOT, MODEL, SUPPORTED_MODELS, PROMPT_VERSION, PROMPT_VERSIONS, EvidenceCoverageError, load_contract, parse_response, request_body, request_identity,
                         validate_result)


def private_directory(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def save_json(path, value):
    private_directory(path.parent)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".pending-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def timestamp():
    return datetime.now(timezone.utc).isoformat()


class PilotFailure(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def failure_details(exc, stage):
    details = {"failure_type": type(exc).__name__, "stage": stage}
    if isinstance(exc, PilotFailure):
        details["failure_code"] = exc.code
    if isinstance(exc, EvidenceCoverageError):
        details.update(failure_code="missing_evidence", local_id=exc.local_id,
                       missing_evidence_fields=exc.missing_fields)
    if type(exc) is ValueError and stage == "source_validation" and str(exc) in (
            "Evidence quote does not occur in the source", "Evidence points to an unsupported or unknown field",
            "Official URL was invented", "Empty source evidence"):
        details["validation_reason"] = str(exc)
    if isinstance(exc, ValidationError):
        details["field_path"] = "/" + "/".join(str(part) for part in exc.absolute_path)
        details["schema_rule"] = exc.validator
        if exc.absolute_path and exc.absolute_path[-1] == "official_url":
            details["field_expectation"] = "An absolute HTTP(S) URL, or JSON null when no URL is given"
    if isinstance(exc, json.JSONDecodeError):
        details["json_line"] = exc.lineno
        details["json_column"] = exc.colno
    return details


class Quota:
    """Local attempt cap and pacing; provider/project limits remain authoritative."""

    def __init__(self, directory, limit=20, interval=60, now=time.time):
        self.directory = directory
        self.limit, self.interval, self.now = limit, interval, now
        if type(limit) is not int or not 1 <= limit <= 100 or interval < 60:
            raise ValueError("Pilot cap must be 1..100 requests/day; pacing at least 60 seconds")
        private_directory(directory)

    @contextmanager
    def ledger(self):
        lock_path = self.directory / "quota.lock"
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(descriptor, "a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            path = self.directory / "quota.json"
            ledger = json.loads(path.read_text()) if path.exists() else {"schema_version": 1, "requests": []}
            try:
                yield ledger
                save_json(path, ledger)
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def reserve(self, identity, retry_failed=False, model=MODEL):
        with self.ledger() as ledger:
            previous = [entry for entry in ledger["requests"] if entry["request_sha256"] == identity]
            if previous and (not retry_failed or previous[-1]["status"] != "failed"):
                raise PilotFailure("prior_attempt", "Prior attempt exists; use cache or explicitly retry a failed attempt")
            current = self.now()
            # Rolling 24-hour window is stricter than the API's calendar-day reset.
            recent = [entry for entry in ledger["requests"] if current - entry["started_epoch"] < 86400]
            if len(recent) >= self.limit:
                raise PilotFailure("daily_limit", "Local daily request cap reached")
            if recent and current - max(entry["started_epoch"] for entry in recent) < self.interval:
                raise PilotFailure("pacing", "Local request pacing requires waiting before another call")
            attempt_id = uuid.uuid4().hex
            ledger["requests"].append({"attempt_id": attempt_id, "request_sha256": identity,
                "status": "reserved", "started_epoch": current, "created_at": timestamp(), "model": model})
            return attempt_id

    def wait_seconds(self):
        with self.ledger() as ledger:
            if not ledger["requests"]:
                return 0
            return max(0, self.interval - (self.now() - max(e["started_epoch"] for e in ledger["requests"])))

    def finish(self, attempt_id, status, usage=None):
        with self.ledger() as ledger:
            entry = next(e for e in ledger["requests"] if e["attempt_id"] == attempt_id)
            entry.update(status=status, finished_at=timestamp())
            if usage is not None:
                entry["usage"] = usage


def usage_metadata(response):
    usage = response.get("usageMetadata", {})
    values = [usage.get("promptTokenCount"), usage.get("candidatesTokenCount", 0),
              usage.get("thoughtsTokenCount", 0)]
    if any(type(value) is not int or value < 0 for value in values):
        raise ValueError("API did not return valid token usage")
    return {"input_tokens": values[0], "output_tokens": values[1], "thinking_tokens": values[2]}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def call_gemini(request, key=None, model=MODEL):
    if model not in SUPPORTED_MODELS:
        raise ValueError("Unsupported Gemini pilot model")
    key = key or load_key()
    if not key:
        raise ValueError("GEMINI_API_KEY is not configured; never paste a key into chat")
    body = json.dumps(request, ensure_ascii=False).encode("utf-8")
    http_request = Request("https://generativelanguage.googleapis.com/v1beta/models/" + model + ":generateContent",
                           data=body, method="POST",
                           headers={"x-goog-api-key": key, "Content-Type": "application/json"})
    # Fixed HTTPS destination, no redirects, no tools, no automatic retries.
    try:
        with build_opener(NoRedirect()).open(http_request, timeout=60) as response:
            data = response.read(2 * 1024 * 1024 + 1)
            if len(data) > 2 * 1024 * 1024:
                raise ValueError("API response exceeded pilot size limit")
        return json.loads(data)
    except HTTPError as exc:
        # Error bodies may include submitted text; do not echo them or auth headers.
        raise PilotFailure("http_" + str(exc.code),
                           "API HTTP error {}; attempt counted; no automatic retry".format(exc.code)) from None
    except (URLError, TimeoutError, OSError) as exc:
        raise PilotFailure("transport", "API transport failure; attempt counted; no automatic retry") from None


def extract_one(record, directory, registry, schema, prompt, quota, transport=None, retry_failed=False, model=MODEL):
    request = request_body(record, schema, prompt)
    identity = request_identity(record, request, model=model)
    path = directory / "cache" / (identity + ".json")
    if path.exists():
        artifact = json.loads(path.read_text(encoding="utf-8"))
        integrity = artifact.pop("integrity_sha256")
        if input_hash(artifact) != integrity or artifact["request_sha256"] != identity:
            raise ValueError("Cached result integrity mismatch")
        if artifact["input_sha256"] != input_hash(record):
            raise ValueError("Cached input hash mismatch")
        if artifact["model"] != model:
            raise ValueError("Cached model mismatch")
        if artifact["mode"] != "live" or artifact["editorial_status"] != "pending":
            raise ValueError("Cached extraction cannot change editorial state or run mode")
        if artifact["prompt_sha256"] != input_hash(prompt) or artifact["schema_sha256"] != input_hash(schema):
            raise ValueError("Cached contract hash mismatch")
        offsets = validate_result(artifact["result"], record, registry, schema)
        if offsets != artifact["evidence_offsets"]:
            raise ValueError("Cached evidence offsets changed")
        return artifact, True
    attempt_id = quota.reserve(identity, retry_failed=retry_failed, model=model)
    stage = "transport"
    usage = None
    result = None
    response = None
    try:
        response = transport(request) if transport else call_gemini(request, model=model)
        stage = "usage"
        usage = usage_metadata(response)
        stage = "response_parsing"
        result = parse_response(response)
        stage = "source_validation"
        offsets = validate_result(result, record, registry, schema)
        stage = "save_result"
        artifact = {
            "schema_version": 1, "input_sha256": input_hash(record),
            "request_sha256": identity, "model": model,
            "prompt_sha256": input_hash(prompt), "schema_sha256": input_hash(schema),
            "mode": "live", "provider": "gemini", "returned_model_version": response.get("modelVersion"),
            "created_at": timestamp(), "editorial_status": "pending",
            "usage": usage, "result": result, "evidence_offsets": offsets,
            "generation_parameters": {key: value for key, value in request["generationConfig"].items() if key != "responseFormat"},
        }
        save_json(path, {**artifact, "integrity_sha256": input_hash(artifact)})
        quota.finish(attempt_id, "validated", usage)
        return artifact, False
    except Exception as exc:
        quota.finish(attempt_id, "failed", usage)
        details = failure_details(exc, stage)
        if result is not None:
            # Keep parsed proposals rejected by local validation for diagnosis.
            # They never enter the validated cache, evaluator or public export.
            rejected_path = directory / "rejected" / (attempt_id + ".json")
            proposal = {
                "schema_version": 1, "mode": "live", "status": "validation_failed",
                "editorial_status": "pending", "provider": "gemini", "model": model,
                "returned_model_version": response.get("modelVersion"),
                "request_sha256": identity, "input_sha256": input_hash(record),
                "prompt_sha256": input_hash(prompt), "schema_sha256": input_hash(schema),
                "attempt_id": attempt_id, "created_at": timestamp(), "usage": usage,
                "validation_error": details.copy(), "result": result,
                "generation_parameters": {key: value for key, value in request["generationConfig"].items() if key != "responseFormat"},
            }
            save_json(rejected_path, {**proposal, "integrity_sha256": input_hash(proposal)})
            details["rejected_proposal"] = str(rejected_path)
        exc.pilot_details = details
        # Error markers contain safe diagnostics, not raw API errors or credentials.
        save_json(directory / "failures" / (identity + ".json"), {
            "request_sha256": identity, "input_sha256": input_hash(record),
            "model": model, "failed_at": timestamp(), **details,
            "status": "failed", "editorial_status": "pending",
        })
        raise


def load_input(root, case):
    directory = root / ("local/evaluation_inputs" if case["basis"] == "real" else "evaluation/synthetic_inputs")
    record = json.loads((directory / (case["case_id"] + ".json")).read_text(encoding="utf-8"))
    if input_hash(record) != case["input_sha256"]:
        raise ValueError("Frozen source changed: " + case["case_id"])
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--split", choices=["development", "held_out"], default="development")
    parser.add_argument("--prompt-version", choices=PROMPT_VERSIONS, default=PROMPT_VERSION)
    parser.add_argument("--model", choices=SUPPORTED_MODELS, default=MODEL)
    parser.add_argument("--case", action="append", help="Explicit case ID; may be repeated")
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--daily-request-limit", type=int, default=20)
    parser.add_argument("--retry-failed", action="store_true", help="Explicitly retry a previous failed request")
    parser.add_argument("--keep-going", action="store_true", help="Retain failures for review and process the remaining cases")
    parser.add_argument("--live", action="store_true", help="Send selected frozen messages to Gemini; use a Free-tier project")
    args = parser.parse_args(argv)
    try:
        if not 1 <= args.limit <= 100:
            raise ValueError("Limit must be between 1 and 100")
        registry, schema, prompt = load_contract(args.root, args.prompt_version)
        labels = json.loads((args.root / "evaluation/labels.json").read_text(encoding="utf-8"))
        candidates = [case for case in labels["cases"] if case["split"] == args.split]
        if args.case:
            if len(set(args.case)) != len(args.case):
                raise ValueError("Duplicate requested case ID")
            candidates = [case for case in candidates if case["case_id"] in args.case]
            if {case["case_id"] for case in candidates} != set(args.case):
                raise ValueError("Unknown case ID or case outside requested split")
            if len(candidates) > args.limit:
                raise ValueError("Explicit selection exceeds limit; raise --limit deliberately")
        cases = candidates[:args.limit]
        if not cases:
            raise ValueError("No cases selected")
        inputs = [(case, load_input(args.root, case)) for case in cases]
        directory = args.root / "local/extraction"
        quota = Quota(directory, args.daily_request_limit)
        plan = [{"case_id": case["case_id"], "input_sha256": input_hash(record),
                 "source_bytes": len(json.dumps(record, ensure_ascii=False).encode("utf-8"))}
                for case, record in inputs]
        if not args.live:
            print(json.dumps({"mode": "preflight", "api_requests": 0, "model": args.model,
                              "provider": "gemini", "split": args.split,
                              "daily_request_limit": args.daily_request_limit,
                              "billing": "Use a Free-tier project; account tier is not verified by this tool",
                              "cases": plan}, indent=2))
            return 0
        key = load_key(args.root)
        if not key:
            raise ValueError("GEMINI_API_KEY is not configured; no calls or attempts recorded")
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
        path = directory / "runs" / run_id / "predictions.json"
        report = {"schema_version": 1, "mode": "live", "model": args.model,
                  "prompt_version": args.prompt_version,
                  "generation_parameters": {key: value for key, value in request_body(inputs[0][1], schema, prompt)["generationConfig"].items() if key != "responseFormat"},
                  "split": args.split, "prompt_sha256": input_hash(prompt),
                  "schema_sha256": input_hash(schema), "started_at": timestamp(),
                  "status": "running", "cases": []}
        save_json(path, report)
        for case, record in inputs:
            try:
                identity = request_identity(record, request_body(record, schema, prompt), model=args.model)
                if not (directory / "cache" / (identity + ".json")).exists():
                    delay = quota.wait_seconds()
                    if delay:
                        print("Waiting {:.1f}s for local request pacing".format(delay), flush=True)
                        time.sleep(delay)
                artifact, cached = extract_one(record, directory, registry, schema, prompt, quota,
                                               transport=lambda request: call_gemini(request, key=key, model=args.model),
                                               retry_failed=args.retry_failed, model=args.model)
                report["cases"].append({"case_id": case["case_id"], "status": "validated",
                                        "cached": cached, "artifact": artifact})
            except Exception as exc:
                details = getattr(exc, "pilot_details", failure_details(exc, "preflight_or_cache"))
                if isinstance(exc, PilotFailure) and exc.code == "prior_attempt":
                    previous_path = directory / "failures" / (identity + ".json")
                    if previous_path.exists():
                        previous = json.loads(previous_path.read_text(encoding="utf-8"))
                        details["previous_failure_stage"] = previous.get("stage")
                        if previous.get("rejected_proposal"):
                            details["rejected_proposal"] = previous["rejected_proposal"]
                report["cases"].append({"case_id": case["case_id"], "status": "failed",
                                        **details})
                report.update(status="failed", finished_at=timestamp())
                save_json(path, report)
                # Safe type only: JSON/schema exceptions can contain source contents.
                print(json.dumps({"status": "failed", "case_id": case["case_id"],
                                  **details, "report": str(path)}))
                if not args.keep_going:
                    return 1
                continue
            save_json(path, report)
            print(case["case_id"] + (": cached" if cached else ": extracted"), flush=True)
        failed = any(case["status"] == "failed" for case in report["cases"])
        report.update(status="completed_with_failures" if failed else "completed", finished_at=timestamp())
        save_json(path, report)
        print(json.dumps({"status": report["status"], "report": str(path)}))
        return 1 if failed else 0
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(1, "Extraction preflight failed: " + str(exc) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
