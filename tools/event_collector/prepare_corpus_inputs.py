"""Recreate frozen private evaluation inputs from already collected announcements.

No network access: content hashes must match the draft annotation version.
"""

import argparse
import json
import re
from pathlib import Path

from .check_corpus import input_hash
from .collect import save_json


def sanitize_text(text):
    text = re.sub(r"https?://[^\s<>]*zoom\.[^\s<>]*", "[connection URL omitted]", text, flags=re.I)
    text = re.sub(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", "[email omitted]", text)
    return re.sub(r"(?im)^.*(?:\bTel:|\bPhone:|\bFax:|\bMob:|\bphone:|\bFAX:).*$", "[contact line omitted]", text)


def prepare(labels, root):
    prepared = 0
    for case in labels["cases"]:
        if case["basis"] != "real":
            continue
        source = json.loads((root / "local/announcements" / (case["source_id"] + ".json")).read_text(encoding="utf-8"))
        if source["url"] != case["source_url"] or source["content_sha256"] != case["source_content_sha256"]:
            raise ValueError("Collected source version differs from labelled version: " + case["case_id"])
        frozen = {"schema_version": 1, "case_id": case["case_id"], "source_url": source["url"],
                  "subject": source["subject"], "body": sanitize_text(source["body"]),
                  "links": [url for url in source["links"] if "zoom." not in url], "posted_at": source["posted_at"]}
        if input_hash(frozen) != case["input_sha256"]:
            raise ValueError("Input no longer matches the frozen annotation hash: " + case["case_id"])
        save_json(root / "local/evaluation_inputs" / (case["case_id"] + ".json"), frozen)
        prepared += 1
    return prepared


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("_event_collector"))
    args = parser.parse_args(argv)
    try:
        labels = json.loads((args.root / "evaluation/labels.json").read_text(encoding="utf-8"))
        prepared = prepare(labels, args.root)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(1, "Input preparation failed: " + str(exc) + "\n")
    print("Prepared {} matching private inputs; no network requests made.".format(prepared))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
