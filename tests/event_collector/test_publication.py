"""Website-ready records and migration preserve review content without build-time Python."""

import base64
import copy
import json
from unittest.mock import patch

from test_weekly import WebsiteFixture
from test_workflow import candidate
from tools.event_collector.extract import save_json
from tools.event_collector.migrate import migrate_site, migrate_proposals
from tools.event_collector.moderate import decide
from tools.event_collector.submit import BRANCH, REPOSITORY, git, next_package
from tools.event_collector.website import LEGACY_RECORDS_PATH, RECORDS_PATH, from_website, package_record, read_record, records_directory, to_website


class MigrationClient:
    def __init__(self, remote, branch):
        self.remote, self.branch, self.patches = remote, branch, []

    def pull(self):
        return {'number': 42, 'state': 'open', 'merged_at': None,
                'html_url': 'https://github.com/' + REPOSITORY + '/pull/42',
                'body': 'Reviewer note retained. File: _event_collector/records/event-migration.json',
                'head': {'ref': self.branch, 'sha': git('--git-dir=' + str(self.remote), 'rev-parse', self.branch),
                         'repo': {'full_name': REPOSITORY}}}

    def request(self, path, data=None, method=None):
        if method == 'PATCH':
            self.patches.append(data)
            return self.pull()
        if path.startswith('/pulls?'):
            return [self.pull()]
        if path.startswith('/pulls/42/files'):
            files = git('--git-dir=' + str(self.remote), 'diff', '--name-only', 'main...' + self.branch).splitlines()
            return [{'filename': name, 'status': 'added'} for name in files if name.endswith('.json')]
        if path == '/pulls/42':
            return self.pull()
        if path.startswith('/contents/'):
            filename, sha = path[len('/contents/'):].split('?ref=')
            data = git('--git-dir=' + str(self.remote), 'show', sha + ':' + filename)
            return {'encoding': 'base64', 'content': base64.b64encode(data.encode()).decode()}
        raise AssertionError(path)


class PublicationTests(WebsiteFixture):
    def test_flat_event_round_trip_preserves_all_fields_and_legacy_exclusions(self):
        for record in (candidate('event-roundtrip'), decide(candidate('event-hidden'), 'hide', 'editor', 'Withdrawn'),
                       decide(candidate('event-approved'), 'approve', 'editor')):
            encoded = to_website(record)
            self.assertNotIn('facts', encoded)
            self.assertEqual(encoded['title'], record['facts']['title'])
            self.assertEqual(from_website(encoded), record)

    def test_site_migration_is_lossless_repeatable_and_backed_up(self):
        legacy = self.site / LEGACY_RECORDS_PATH
        records = [candidate('event-migration'), decide(candidate('event-hidden'), 'hide', 'editor', 'Withdrawn')]
        for record in records:
            save_json(legacy / (record['id'] + '.json'), record)
        save_json(self.site / '_data/community_events.json', {'events': [], 'schema_version': 1})
        preview = migrate_site(self.site, self.root, True)
        self.assertEqual(preview['record_count'], 2)
        self.assertEqual(len(list(legacy.glob('*.json'))), 2)
        result = migrate_site(self.site, self.root)
        self.assertEqual(result['record_count'], 2)
        self.assertFalse(list(legacy.glob('*.json')))
        self.assertFalse((self.site / '_data/community_events.json').exists())
        for record in records:
            self.assertEqual(read_record(self.records / (record['id'] + '.json')), record)
        self.assertEqual(migrate_site(self.site, self.root)['record_count'], 2)

    def test_conflicting_old_and_new_records_are_preserved_for_manual_resolution(self):
        record = candidate('event-conflict')
        save_json(self.site / LEGACY_RECORDS_PATH / (record['id'] + '.json'), record)
        changed = copy.deepcopy(record)
        changed['facts']['title'] = 'Human correction'
        save_json(self.records / (record['id'] + '.json'), to_website(changed))
        with self.assertRaisesRegex(ValueError, 'Conflicting'):
            migrate_site(self.site, self.root)
        self.assertEqual(read_record(self.records / (record['id'] + '.json')), changed)
        self.assertEqual(read_record(self.site / LEGACY_RECORDS_PATH / (record['id'] + '.json')), record)

    def test_a_saved_legacy_package_becomes_one_flat_website_file_without_changing_the_queue(self):
        package = self.package()
        manifest = json.loads((package / 'manifest.json').read_text())
        identity = manifest['record_ids'][0]
        record = package_record(package, identity)
        current = package / 'changes' / RECORDS_PATH / (identity + '.json')
        save_json(package / 'changes' / LEGACY_RECORDS_PATH / (identity + '.json'), record)
        current.unlink()
        before = (package / 'manifest.json').read_bytes()
        migrated, selected = next_package(package, self.site, self.root)
        ready = json.loads((migrated / 'changes' / RECORDS_PATH / (identity + '.json')).read_text())
        self.assertNotIn('facts', ready)
        self.assertEqual(from_website(ready), record)
        self.assertEqual((package / 'manifest.json').read_bytes(), before)

    def test_a_legacy_proposal_merged_after_cutover_blocks_submission_until_it_is_converted(self):
        save_json(self.site / LEGACY_RECORDS_PATH / 'event-old.json', candidate('event-old'))
        with self.assertRaisesRegex(ValueError, 'Legacy event files'):
            records_directory(self.site, require_ready=True)

    def legacy_proposal(self):
        remote = self.local_remote()
        record = candidate('event-migration')
        record['facts']['title'] = 'Reviewer corrected this lattice workshop'
        branch = BRANCH + '-event-migration'
        editor = self.root / 'editor'
        git('clone', '-q', str(remote), str(editor))
        git('switch', '-c', branch, cwd=editor)
        save_json(editor / LEGACY_RECORDS_PATH / (record['id'] + '.json'), record)
        git('add', '.', cwd=editor)
        git('-c', 'user.name=Editor', '-c', 'user.email=editor@example.invalid', 'commit', '-qm', 'Reviewed candidate', cwd=editor)
        git('push', 'origin', branch, cwd=editor)
        return remote, branch, record, editor

    def test_open_pr_migration_preserves_human_content_and_existing_pr_number(self):
        remote, branch, record, _ = self.legacy_proposal()
        client = MigrationClient(remote, branch)
        with patch('tools.event_collector.submit.REMOTE', str(remote)), patch('tools.event_collector.migrate.REMOTE', str(remote)):
            preview = migrate_proposals(self.root, client, True)
            self.assertEqual(len(preview['prs']), 1)
            result = migrate_proposals(self.root, client)
            repeated = migrate_proposals(self.root, client)
        self.assertEqual(result['updated_count'], 1)
        self.assertEqual(repeated['already_ready_count'], 1)
        files = git('--git-dir=' + str(remote), 'diff', '--name-only', 'main', branch).splitlines()
        self.assertEqual(files, [str(RECORDS_PATH / (record['id'] + '.json'))])
        migrated = from_website(json.loads(git('--git-dir=' + str(remote), 'show', branch + ':' + files[0])))
        self.assertEqual(migrated, record)
        self.assertIn('Reviewer note retained', client.patches[0]['body'])
        self.assertIn('_data/community_events/', client.patches[0]['body'])

    def test_migration_refuses_to_replace_a_concurrent_reviewer_commit(self):
        remote, branch, record, editor = self.legacy_proposal()
        client = MigrationClient(remote, branch)
        record['facts']['summary'] = 'Another reviewer correction'
        save_json(editor / LEGACY_RECORDS_PATH / (record['id'] + '.json'), record)
        git('add', '.', cwd=editor)
        git('-c', 'user.name=Editor', '-c', 'user.email=editor@example.invalid', 'commit', '-qm', 'Concurrent correction', cwd=editor)
        advanced = git('rev-parse', 'HEAD', cwd=editor)
        def race(*args, **kwargs):
            if args[0] == 'push':
                git('push', 'origin', branch, cwd=editor)
            return git(*args, **kwargs)
        with patch('tools.event_collector.submit.REMOTE', str(remote)), patch('tools.event_collector.migrate.REMOTE', str(remote)), \
                patch('tools.event_collector.migrate.git', side_effect=race), self.assertRaises(ValueError):
            migrate_proposals(self.root, client)
        self.assertEqual(git('--git-dir=' + str(remote), 'rev-parse', branch), advanced)
        self.assertFalse(client.patches)
