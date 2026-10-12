"""Website-ready per-event data, with lossless access to legacy editorial records."""

import copy
import json
from pathlib import Path
import re

RECORDS_PATH = Path('_data/community_events')
LEGACY_RECORDS_PATH = Path('_event_collector/records')
FACT_FIELDS = ('title', 'type', 'start_date', 'end_date', 'start_time', 'end_time',
               'timezone', 'location', 'attendance', 'official_url', 'summary',
               'deadlines', 'lifecycle')


def to_website(record):
    """Flatten facts for Jekyll; retain identities, evidence and exclusion metadata."""
    result = copy.deepcopy(record)
    facts = result.pop('facts')
    if set(facts) != set(FACT_FIELDS) or set(result) & set(FACT_FIELDS):
        raise ValueError('Unexpected event fields; cannot prepare website data')
    header = {key: result.pop(key) for key in ('schema_version', 'id', 'revision')}
    return {**header, **facts, **result}


def from_website(value):
    """Decode either a website file or an older nested editorial record."""
    result = copy.deepcopy(value)
    if 'facts' not in result:
        result['facts'] = {key: result.pop(key) for key in FACT_FIELDS}
    return result


def read_record(path):
    record = from_website(json.loads(Path(path).read_text()))
    if Path(path).stem != record['id']:
        raise ValueError('Event filename does not match its identity')
    return record


def event_id_from_path(filename):
    match = re.fullmatch(r'(?:_data/community_events|_event_collector/records)/(event-[a-z0-9_-]+)\.json', filename)
    return match[1] if match else None


def package_record(package, identity):
    for directory in (RECORDS_PATH, LEGACY_RECORDS_PATH):
        path = Path(package) / 'changes' / directory / (identity + '.json')
        if path.exists():
            return read_record(path)
    raise ValueError('Review package is missing event ' + identity)


def records_directory(site, require_ready=False):
    site = Path(site).resolve()
    directory = site / RECORDS_PATH
    if not (site / '_config.yml').is_file():
        raise ValueError('--site must name a website checkout with _config.yml')
    ready = re.search(r'^community_event_data:\s*per_event\s*$', (site / '_config.yml').read_text(), re.M)
    if directory.is_dir() and not (site / '_data/community_events.json').exists() and (ready or not require_ready):
        if require_ready and any((site / LEGACY_RECORDS_PATH).glob('*.json')):
            raise ValueError('Legacy event files remain; migrate them before submitting more proposals')
        return directory
    legacy = site / LEGACY_RECORDS_PATH
    if not require_ready and legacy.is_dir():
        return legacy
    raise ValueError('Website checkout needs the per-event data migration before submission')


def write_record(directory, record):
    from .extract import save_json
    directory = Path(directory)
    value = to_website(record) if directory.name == 'community_events' and directory.parent.name == '_data' else record
    save_json(directory / (record['id'] + '.json'), value)
