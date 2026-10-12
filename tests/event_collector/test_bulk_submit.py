"""Bulk submission keeps every event on its own independently reviewable branch."""

import copy
from contextlib import redirect_stdout
import io
import json
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

import test_github_review as review_fixtures
from test_weekly import WebsiteFixture
from tools.event_collector.extract import save_json
from tools.event_collector.website import package_record
from tools.event_collector.submit import BRANCH, REPOSITORY, GitHub, git, main, submit_all


class MultiGitHub:
    def __init__(self, remote):
        self.remote = remote
        self.pulls = []
        self.posts = []
        self.fail_at = None

    def request(self, path, data=None):
        if data is not None:
            self.posts.append(data)
            if len(self.posts) == self.fail_at:
                raise OSError('Simulated API failure after push')
            pull = {'number': 100 + len(self.pulls), 'head': {'ref': data['head'], 'repo': {'full_name': REPOSITORY}},
                    'html_url': 'https://github.com/' + REPOSITORY + '/pull/' + str(100 + len(self.pulls)),
                    'state': 'open', 'payload': copy.deepcopy(data)}
            self.pulls.append(pull)
            return pull
        query = parse_qs(urlsplit(path).query)
        if path.startswith('/pulls?'):
            if query.get('state') == ['closed']:
                return []
            pulls = [p for p in self.pulls if p['state'] == 'open']
            if 'head' in query:
                pulls = [p for p in pulls if 'EuroLFT:' + p['head']['ref'] == query['head'][0]]
            return pulls
        if path.startswith('/pulls/') and '/files?' in path:
            number = int(path.split('/')[2])
            pull = next(p for p in self.pulls if p['number'] == number)
            if 'files' in pull:
                return pull['files']
            filenames = git('--git-dir=' + str(self.remote), 'diff', '--name-only', 'main', pull['head']['ref']).splitlines()
            return [{'filename': name, 'status': 'added'} for name in filenames]
        raise AssertionError(path)


class BulkSubmissionTests(WebsiteFixture):
    two_events = review_fixtures.SingleEventQueueTests.two_events
    def package_ids(self, package):
        return json.loads((package / 'manifest.json').read_text())['record_ids']

    def context(self, remote):
        return patch('tools.event_collector.submit.REMOTE', str(remote))

    def test_all_events_are_independent_one_record_prs_and_repeat_preserves_human_changes(self):
        package = self.two_events()
        identities = self.package_ids(package)
        original = (package / 'manifest.json').read_bytes()
        remote = self.local_remote()
        client = MultiGitHub(remote)
        baseline = git('--git-dir=' + str(remote), 'rev-parse', 'main')
        with self.context(remote), patch('tools.event_collector.submit.time.sleep'):
            report = submit_all(package, self.root, client)
            self.assertEqual(report['created_count'], 2)
            self.assertEqual(report['remaining_queued'], 0)
            for identity, pull in zip(identities, client.pulls):
                branch = BRANCH + '-' + identity
                self.assertEqual(pull['head']['ref'], branch)
                self.assertFalse(pull['payload']['draft'])
                self.assertEqual(git('--git-dir=' + str(remote), 'rev-parse', branch + '^'), baseline)
                files = git('--git-dir=' + str(remote), 'diff', '--name-only', 'main', branch).splitlines()
                self.assertEqual(files, ['_data/community_events/' + identity + '.json'])
            # Human edits the first PR's source branch before the operator reruns.
            editor = self.root / 'editor'
            branch = client.pulls[0]['head']['ref']
            git('clone', '-q', '--branch', branch, str(remote), str(editor))
            path = editor / '_data/community_events' / (identities[0] + '.json')
            record = json.loads(path.read_text())
            record['summary'] = 'Human correction in the open PR'
            save_json(path, record)
            git('add', '.', cwd=editor)
            git('-c', 'user.name=Reviewer', '-c', 'user.email=reviewer@example.invalid', 'commit', '-qm', 'Correct event', cwd=editor)
            git('push', 'origin', branch, cwd=editor)
            corrected = git('--git-dir=' + str(remote), 'rev-parse', branch)
            repeated = submit_all(package, self.root, client)
        self.assertEqual(repeated['created_count'], 0)
        self.assertEqual(repeated['existing_count'], 2)
        self.assertEqual(len(client.posts), 2)
        self.assertEqual(git('--git-dir=' + str(remote), 'rev-parse', branch), corrected)
        self.assertEqual((package / 'manifest.json').read_bytes(), original)
        self.assertFalse(list(self.records.glob('*.json')))

    def test_legacy_open_event_is_skipped_and_other_events_are_still_submitted(self):
        package = self.two_events()
        identities = self.package_ids(package)
        remote = self.local_remote()
        git('--git-dir=' + str(remote), 'branch', BRANCH, 'main')
        legacy_head = git('--git-dir=' + str(remote), 'rev-parse', BRANCH)
        client = MultiGitHub(remote)
        client.pulls.append({'number': 42, 'state': 'open', 'head': {'ref': BRANCH, 'repo': {'full_name': REPOSITORY}},
                             'html_url': 'https://github.com/' + REPOSITORY + '/pull/42',
                             'files': [{'filename': '_data/community_events/' + identities[0] + '.json', 'status': 'added'}]})
        with self.context(remote):
            report = submit_all(package, self.root, client)
        self.assertEqual(report['existing_count'], 1)
        self.assertEqual(report['created_count'], 1)
        self.assertEqual(client.posts[0]['head'], BRANCH + '-' + identities[1])
        self.assertEqual(git('--git-dir=' + str(remote), 'rev-parse', BRANCH), legacy_head)

    def test_failure_is_resumable_even_after_another_event_is_merged(self):
        package = self.two_events()
        identities = self.package_ids(package)
        remote = self.local_remote()
        client = MultiGitHub(remote)
        client.fail_at = 2
        branch = BRANCH + '-' + identities[1]
        with self.context(remote), patch('tools.event_collector.submit.time.sleep'):
            first = submit_all(package, self.root, client)
            self.assertEqual(first['status'], 'partial_submission')
            self.assertEqual(first['created_count'], 1)
            self.assertEqual(first['remaining_queued'], 1)
            pushed = git('--git-dir=' + str(remote), 'rev-parse', branch)
            accepted = git('--git-dir=' + str(remote), 'rev-parse', client.pulls[0]['head']['ref'])
            git('--git-dir=' + str(remote), 'update-ref', 'refs/heads/main', accepted)
            client.pulls[0]['state'] = 'closed'
            client.fail_at = None
            resumed = submit_all(package, self.root, client)
        self.assertEqual(resumed['status'], 'proposal_prs_created')
        self.assertEqual(resumed['created_count'], 1)
        self.assertEqual(resumed['record_count'], 1)
        self.assertEqual(git('--git-dir=' + str(remote), 'rev-parse', branch), pushed)
        self.assertEqual(client.posts[-1]['head'], branch)
        self.assertEqual(json.loads((self.root / 'local/submissions/all-latest.json').read_text())['remaining_queued'], 0)

    def test_unrecognized_existing_branch_is_never_overwritten(self):
        package = self.two_events()
        identity = self.package_ids(package)[0]
        branch = BRANCH + '-' + identity
        remote = self.local_remote()
        git('--git-dir=' + str(remote), 'branch', branch, 'main')
        before = git('--git-dir=' + str(remote), 'rev-parse', branch)
        client = MultiGitHub(remote)
        with self.context(remote):
            report = submit_all(package, self.root, client)
        self.assertEqual(report['status'], 'partial_submission')
        self.assertEqual(report['created_count'], 0)
        self.assertEqual(git('--git-dir=' + str(remote), 'rev-parse', branch), before)
        self.assertFalse(client.posts)

    def test_rejections_are_skipped_and_foreign_prs_do_not_block_bulk_submission(self):
        package = self.two_events()
        identity = self.package_ids(package)[0]
        excluded = package_record(package, identity)
        remote = self.local_remote()
        client = MultiGitHub(remote)
        client.pulls.append({'number': 16, 'state': 'open', 'head': {'ref': 'calendar', 'repo': {'full_name': REPOSITORY}},
                             'html_url': 'https://github.com/' + REPOSITORY + '/pull/16',
                             'files': [{'filename': '_data/community_events/' + identity + '.json', 'status': 'added'}]})
        with self.context(remote), patch('tools.event_collector.submit.closed_rejections', return_value={identity: excluded}):
            report = submit_all(package, self.root, client)
        self.assertEqual(report['created_count'], 1)
        self.assertEqual(report['existing_count'], 0)
        self.assertEqual(client.pulls[0]['state'], 'open')

    def test_bulk_dry_run_uses_no_git_or_credentials_and_proposes_one_pr_per_event(self):
        package = self.two_events()
        with patch('tools.event_collector.submit.git') as operation, \
                patch('tools.event_collector.submit.github_token') as token, redirect_stdout(io.StringIO()) as output:
            code = main([str(package), '--all', '--dry-run', '--root', str(self.root), '--site', str(self.site)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())['pr_count'], 2)
        operation.assert_not_called()
        token.assert_not_called()

    def test_single_review_check_recognizes_independent_event_prs(self):
        client = GitHub()
        pull = {'number': 1, 'head': {'ref': BRANCH + '-event-existing', 'repo': {'full_name': REPOSITORY}}}
        with patch.object(client, 'request', return_value=[pull]):
            self.assertEqual(client.open_pr(), pull)

    def test_closed_independent_event_pr_still_supplies_a_rejection(self):
        record = review_fixtures.candidate('event-closed-bulk')
        client = review_fixtures.ClosedClient(record)
        original = client.request
        def request(path):
            result = original(path)
            if path.startswith('/pulls?state=closed'):
                result[0]['head']['ref'] = BRANCH + '-' + record['id']
            return result
        client.request = request
        rejected = review_fixtures.closed_rejections(client, self.root)
        self.assertEqual(rejected[record['id']]['decision']['status'], 'rejected')

    def test_a_foreign_fork_using_the_same_branch_name_does_not_block_an_event(self):
        package = self.two_events()
        identity = self.package_ids(package)[0]
        remote = self.local_remote()
        client = MultiGitHub(remote)
        client.pulls.append({'number': 50, 'state': 'open',
                             'head': {'ref': BRANCH + '-' + identity, 'repo': {'full_name': 'EuroLFT/other-fork'}},
                             'html_url': 'https://github.com/' + REPOSITORY + '/pull/50'})
        with self.context(remote), patch('tools.event_collector.submit.time.sleep'):
            report = submit_all(package, self.root, client)
        self.assertEqual(report['created_count'], 2)
        self.assertEqual(report['existing_count'], 0)

    def test_old_shared_branch_can_exist_while_every_other_event_gets_a_pr(self):
        package = self.two_events()
        remote = self.local_remote()
        git('--git-dir=' + str(remote), 'branch', BRANCH, 'main')
        legacy_head = git('--git-dir=' + str(remote), 'rev-parse', BRANCH)
        client = MultiGitHub(remote)
        with self.context(remote), patch('tools.event_collector.submit.time.sleep'):
            report = submit_all(package, self.root, client)
        self.assertEqual(report['created_count'], 2)
        self.assertEqual(report['status'], 'proposal_prs_created')
        self.assertEqual(git('--git-dir=' + str(remote), 'rev-parse', BRANCH), legacy_head)
        for identity in self.package_ids(package):
            branch = BRANCH + '-' + identity
            self.assertEqual(git('--git-dir=' + str(remote), 'diff', '--name-only', 'main', branch),
                             '_data/community_events/' + identity + '.json')
