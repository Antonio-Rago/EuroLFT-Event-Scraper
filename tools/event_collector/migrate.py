"""Move reviewed events and existing proposals to data Jekyll reads directly."""

import argparse
import base64
import fcntl
import os
import json
from pathlib import Path
import re
import time

from .check_corpus import input_hash
from .extract import private_directory, save_json
from .extraction import ROOT
from .reconcile import load_registry, validate_record
from .registry import validate_dataset, validator
from .submit import GitHub, REMOTE, git, github_token, open_event_prs, submission_site
from .github_exclusions import pages
from .website import RECORDS_PATH, LEGACY_RECORDS_PATH, event_id_from_path, from_website, read_record, records_directory, to_website


def migrate_site(site, root=ROOT, dry_run=False):
    site, root = Path(site).resolve(), Path(root).resolve()
    configuration = site / '_config.yml'
    if not configuration.is_file():
        raise ValueError('Migration requires a website checkout')
    legacy = site / LEGACY_RECORDS_PATH
    destination = site / RECORDS_PATH
    old = load_registry(legacy, validator(root)) if legacy.exists() else {}
    current = load_registry(destination, validator(root)) if destination.exists() else {}
    for identity in old.keys() & current.keys():
        if old[identity] != current[identity]:
            raise ValueError('Conflicting old and new copies of event ' + identity + '; preserve both and resolve manually')
    records = {**old, **current}
    validate_dataset(records, root, allow_pending_relations=True)
    # Conversion is structural and lossless, including reviewer edits and exclusions.
    for record in records.values():
        if from_website(to_website(record)) != record:
            raise ValueError('Event conversion is not lossless')
    if dry_run:
        return {'status': 'migration_preview', 'record_count': len(records), 'legacy_count': len(old), 'remote_writes': 0}
    backup = root / 'local/migration' / ('site-' + input_hash(records)[:20])
    private_directory(backup)
    save_json(backup / 'records.json', records)
    if not (backup / 'config.txt').exists():
        (backup / 'config.txt').write_text(configuration.read_text())
    aggregate = site / '_data/community_events.json'
    if aggregate.exists():
        (backup / 'legacy-aggregate.json').write_bytes(aggregate.read_bytes())
    destination.mkdir(parents=True, exist_ok=True)
    (destination / '.gitkeep').touch(exist_ok=True)
    for identity, record in records.items():
        path = destination / (identity + '.json')
        save_json(path, to_website(record))
        if read_record(path) != record:
            raise ValueError('Converted file verification failed; legacy files are preserved')
    for path in legacy.glob('*.json'):
        path.unlink()
    if aggregate.exists():
        aggregate.unlink()
    text = configuration.read_text()
    if re.search(r'^community_event_data:', text, re.M):
        text = re.sub(r'^community_event_data:.*$', 'community_event_data: per_event', text, flags=re.M)
    else:
        text += '\n# Community data is prepared in the collector and rendered directly by Jekyll.\ncommunity_event_data: per_event\n'
    configuration.write_text(text)
    records_directory(site, require_ready=True)
    return {'status': 'migrated_locally', 'record_count': len(records), 'backup': str(backup), 'remote_writes': 0}


def proposal_records(client, pull, root):
    records, legacy_found = {}, False
    for item in pages(client, '/pulls/' + str(pull['number']) + '/files'):
        filename = item['filename']
        identity = event_id_from_path(filename)
        if not identity or item['status'] not in ('added', 'modified', 'renamed'):
            continue
        data = client.request('/contents/' + filename + '?ref=' + pull['head']['sha'])
        if data.get('encoding') != 'base64':
            raise ValueError('Proposal data cannot be decoded')
        record = from_website(json.loads(base64.b64decode(data['content']).decode()))
        validate_record(record, validator(root))
        if record['id'] != identity or (identity in records and records[identity] != record):
            raise ValueError('Proposal has conflicting event identities')
        records[identity] = record
        legacy_found = legacy_found or filename.startswith(str(LEGACY_RECORDS_PATH) + '/')
    return records, legacy_found


def migrate_proposals(root=ROOT, client=None, dry_run=False, on_progress=None):
    root = Path(root).resolve()
    private_directory(root / 'local/submissions')
    descriptor = os.open(root / 'local/submissions/submit.lock', os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, 'a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        return _migrate_proposals(root, client, dry_run, on_progress)


def _migrate_proposals(root, client, dry_run, on_progress):
    client = client or GitHub(github_token())
    base = root / 'local/submissions'
    private_directory(base)
    canonical = submission_site(base)
    current = load_registry(records_directory(canonical), validator(root))
    report = {'status': 'migration_preview' if dry_run else 'proposals_migrated', 'updated_count': 0,
              'already_ready_count': 0, 'prs': [], 'automatic_approval': False}
    for index, pull in enumerate(open_event_prs(client)):
        if index and not dry_run:
            time.sleep(2)
        records, legacy_found = proposal_records(client, pull, root)
        if not records or not legacy_found:
            # A branch push may have succeeded before the previous body update failed.
            body = (pull.get('body') or '').replace(str(LEGACY_RECORDS_PATH) + '/', str(RECORDS_PATH) + '/')
            if records and not dry_run and body != (pull.get('body') or ''):
                client.request('/pulls/' + str(pull['number']), {'body': body}, method='PATCH')
            report['already_ready_count'] += 1
            continue
        for identity, record in records.items():
            if identity in current and record != current[identity] and record['revision'] <= current[identity]['revision']:
                raise ValueError('Proposal would replace newer website data for ' + identity)
        validate_dataset({**current, **records}, root, allow_pending_relations=True)
        item = {'pr': pull['html_url'], 'record_ids': list(records), 'branch': pull['head']['ref']}
        if dry_run:
            report['prs'].append(item)
            continue
        old_sha = pull['head']['sha']
        if not re.fullmatch(r'[a-f0-9]{40}', old_sha):
            raise ValueError('Invalid proposal revision')
        directory = root / 'local/migration' / ('pr-' + str(pull['number']) + '-' + old_sha[:12])
        private_directory(directory)
        save_json(directory / 'before.json', {'pull': pull, 'records': records})
        website = directory / 'website'
        if not website.exists():
            git('clone', '--quiet', '--no-hardlinks', str(canonical), str(website))
            git('remote', 'set-url', 'origin', REMOTE, cwd=website)
            git('switch', '-c', pull['head']['ref'], cwd=website)
            for identity, record in records.items():
                save_json(website / RECORDS_PATH / (identity + '.json'), to_website(record))
            paths = [str(RECORDS_PATH / (identity + '.json')) for identity in records]
            git('add', '--', *paths, cwd=website)
            actual = set(git('diff', '--cached', '--name-only', cwd=website).splitlines())
            if not actual <= set(paths) or not actual:
                raise ValueError('Migration has unexpected or empty changes; preserve the original PR')
            git('diff', '--cached', '--check', cwd=website)
            git('-c', 'user.name=EuroLFT event collector', '-c', 'user.email=event-collector@users.noreply.github.com',
                'commit', '-m', 'Prepare event data for direct Jekyll publication', cwd=website)
            save_json(directory / 'checkpoint.json', {'commit': git('rev-parse', 'HEAD', cwd=website)})
        checkpoint = json.loads((directory / 'checkpoint.json').read_text())
        if git('status', '--porcelain', cwd=website) or git('rev-parse', 'HEAD', cwd=website) != checkpoint['commit']:
            raise ValueError('Migration checkpoint has local edits; preserve them before retrying')
        for identity, record in records.items():
            if from_website(json.loads(git('show', 'HEAD:' + str(RECORDS_PATH / (identity + '.json')), cwd=website))) != record:
                raise ValueError('Migration checkpoint does not match the current proposal')
        latest = client.request('/pulls/' + str(pull['number']))
        if latest['state'] != 'open' or latest.get('merged_at') or latest['head']['sha'] != old_sha:
            raise ValueError('Proposal changed during migration; rerun to preserve the new review')
        # Only the exact source revision is replaced; concurrent edits reject this push.
        git('push', '--force-with-lease=refs/heads/' + pull['head']['ref'] + ':' + old_sha,
            'origin', 'HEAD:refs/heads/' + pull['head']['ref'], cwd=website)
        body = (latest.get('body') or '').replace(str(LEGACY_RECORDS_PATH) + '/', str(RECORDS_PATH) + '/')
        client.request('/pulls/' + str(pull['number']), {'body': body}, method='PATCH')
        report['updated_count'] += 1
        report['prs'].append(item)
        save_json(base / 'migration-latest.json', report)
        if on_progress:
            on_progress(item)
    save_json(base / 'migration-latest.json', report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument('--site', type=Path, help='Convert website data locally; no commit or push')
    choice.add_argument('--open-prs', action='store_true', help='Convert managed open event PRs, preserving their final content')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    try:
        result = migrate_site(args.site, args.root, args.dry_run) if args.site else migrate_proposals(
            args.root, dry_run=args.dry_run, on_progress=lambda item: print(json.dumps(item), flush=True))
        print(json.dumps(result))
        return 0
    except Exception as exc:
        print(json.dumps({'status': 'migration_failed', 'failure_type': type(exc).__name__,
                          'reason': str(exc) if type(exc) is ValueError else 'Inspect the saved checkpoint and retry'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
