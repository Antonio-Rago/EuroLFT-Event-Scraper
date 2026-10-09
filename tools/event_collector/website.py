"""Locate editorial state in a separate website checkout, never in collector code."""

from pathlib import Path


RECORDS_PATH = Path("_event_collector/records")


def records_directory(site):
    site = Path(site).resolve()
    directory = site / RECORDS_PATH
    if not (site / "_config.yml").is_file() or not directory.is_dir():
        raise ValueError("--site must name a website checkout with _config.yml and _event_collector/records")
    return directory
