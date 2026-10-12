"""Prepare clearly labelled private Jekyll previews without approving any events."""

import argparse
import json
from pathlib import Path
import shutil

from .extract import save_json
from .extraction import ROOT
from .reconcile import validate_record
from .registry import validator
from .website import to_website, records_directory as website_records


def prepare(plan, root=ROOT, site=None):
    website = Path(site).resolve() if site is not None else root.parent
    if site is not None:
        website_records(website)
    preview_root = website / "_event_collector"
    manifest = json.loads((plan / "plan.json").read_text())
    records = [json.loads((plan / (event_id + ".json")).read_text()) for event_id in manifest["record_ids"]]
    for record in records:
        validate_record(record, validator(root))
    data = preview_root / "local/preview-data"
    shutil.copytree(website / "_data", data, dirs_exist_ok=True)
    # Preview only the selected proposals in an isolated data directory.
    aggregate = data / "community_events.json"
    if aggregate.exists():
        aggregate.unlink()
    if (data / "community_events").exists():
        shutil.rmtree(data / "community_events")
    (data / "community_events").mkdir()
    for record in records:
        save_json(data / "community_events" / (record["id"] + ".json"), to_website(record))
    configuration = preview_root / "local/preview.yml"
    save_json(configuration, {"community_preview": True,
                             "data_dir": str(data.relative_to(website)),
                             "destination": str((preview_root / "local/site-preview").relative_to(website))})
    return configuration


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--site", type=Path, required=True, help="Separate website checkout")
    args = parser.parse_args(argv)
    try:
        print("Prepared private preview config: " + str(prepare(args.plan, args.root, args.site)))
        print("From the website checkout, build with: bundle exec jekyll build --config _config.yml,_event_collector/local/preview.yml")
        return 0
    except Exception as exc:
        print("Preview preparation failed: " + type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
