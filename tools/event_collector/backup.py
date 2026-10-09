"""Back up or restore editorial records and pending identities, without credentials/raw inputs."""

import argparse
import json
from pathlib import Path

from .check_corpus import input_hash
from .extract import save_json, timestamp
from .extraction import ROOT
from .reconcile import load_registry, validate_record
from .registry import validate_dataset, validator
from .website import records_directory as website_records


def snapshot(root=ROOT, records_directory=None):
    records = load_registry(records_directory or root / "records", validator(root))
    validate_dataset(records, root)
    path = root / "local/reconciliation/identities.json"
    identities = json.loads(path.read_text()) if path.exists() else {"schema_version": 1, "identities": {}}
    for event_id, entry in identities["identities"].items():
        validate_record(entry["record"], validator(root))
        if entry["record"]["id"] != event_id or entry["record"]["decision"]["status"] not in ("pending", "draft"):
            raise ValueError("Invalid pending identity ledger")
    content = {"schema_version": 1, "created_at": timestamp(), "records": records, "pending_identity_ledger": identities}
    return {**content, "integrity_sha256": input_hash(content)}


def restore(value, destination, root=ROOT):
    content = dict(value)
    integrity = content.pop("integrity_sha256")
    if content["schema_version"] != 1 or input_hash(content) != integrity:
        raise ValueError("Backup integrity mismatch")
    if destination.exists():
        raise ValueError("Restore into a new directory; inspect before replacing editorial state")
    validate_dataset(content["records"], root)
    ledger = content["pending_identity_ledger"]
    if ledger["schema_version"] != 1:
        raise ValueError("Unknown pending ledger version")
    for event_id, entry in ledger["identities"].items():
        validate_record(entry["record"], validator(root))
        if entry["record"]["id"] != event_id or entry["record"]["decision"]["status"] not in ("pending", "draft"):
            raise ValueError("Invalid pending identity ledger")
    for event_id, record in content["records"].items():
        save_json(destination / "records" / (event_id + ".json"), record)
    save_json(destination / "local/reconciliation/identities.json", ledger)
    return len(content["records"])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("save", "restore"))
    parser.add_argument("file", type=Path)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--site", type=Path, help="Separate website checkout; required when saving")
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.action == "save":
            if not args.site:
                raise ValueError("Saving editorial state requires --site")
            save_json(args.file, snapshot(args.root, website_records(args.site)))
            print("Private editorial backup saved; credentials and raw inputs are excluded")
        else:
            if not args.destination:
                raise ValueError("Restore requires a new --destination")
            count = restore(json.loads(args.file.read_text()), args.destination, args.root)
            print("Restored {} editorial records into a separate directory".format(count))
        return 0
    except Exception as exc:
        print("Editorial backup operation failed: " + type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
