"""Exact source identity and fail-closed live adapter, using synthetic API fixtures."""
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
import collect_gh_evidence as adapter
from collect_gh import GhClient, GhError

SHA = 'a' * 40
NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
EXPECT = {'schema': 'ci-github-expectations/1', 'repository_id': 10, 'workflow_id': 20,
          'event': 'push', 'max_check_age_s': 3600, 'jobs': [{'name': 'test', 'app_id': 30}]}


class Api:
    def __init__(self):
        self.repo = {'id': 10, 'full_name': 'o/r'}
        self.run = {'id': 40, 'head_sha': SHA, 'workflow_id': 20, 'event': 'push',
                    'run_attempt': 1, 'check_suite_id': 50, 'repository': self.repo,
                    'created_at': '2026-11-19T11:55:00Z', 'updated_at': '2026-11-19T11:57:00Z',
                    'status': 'completed', 'conclusion': 'success'}
        self.runs = [self.run]
        self.jobs = [{'id': 60, 'run_id': 40, 'run_attempt': 1, 'head_sha': SHA, 'name': 'test',
                      'status': 'completed', 'conclusion': 'success',
                      'check_run_url': 'https://api.github.com/repos/o/r/check-runs/70'}]
        self.check = {'id': 70, 'name': 'test', 'head_sha': SHA, 'app': {'id': 30},
                      'check_suite': {'id': 50}, 'status': 'completed', 'conclusion': 'success',
                      'completed_at': '2026-11-19T11:57:00Z'}
        self.calls = []
        self.mutate = None

    def __call__(self, endpoint):
        self.calls.append(endpoint)
        if self.mutate:
            self.mutate(self, endpoint)
        path = urlsplit(endpoint).path
        if path == 'repos/o/r':
            result = self.repo
        elif path == 'repos/o/r/actions/workflows/20/runs':
            result = {'total_count': len(self.runs), 'workflow_runs': self.runs}
        elif path == 'repos/o/r/actions/runs/40':
            result = {**self.run, 'detail_only_field': True}
        elif path == 'repos/o/r/actions/runs/40/attempts/1/jobs':
            result = {'total_count': len(self.jobs), 'jobs': self.jobs}
        elif path == 'repos/o/r/check-runs/70':
            result = self.check
        else:
            raise GhError(kind='unexpected_endpoint')
        return copy.deepcopy(result)


class EvidenceTests(unittest.TestCase):
    def collect(self, api=None, expect=None, **kwargs):
        return adapter.collect('o/r', SHA, expect or copy.deepcopy(EXPECT), api or Api(),
                               clock=lambda: NOW, **kwargs)

    def test_pass_is_only_observed_check_evidence(self):
        result = self.collect()
        self.assertEqual(result['state'], 'pass')
        self.assertTrue(result['complete'])
        self.assertEqual(result['capabilities']['release_eligibility'], 'unknown')
        self.assertEqual(result['capabilities']['policy_binding'], 'unknown')
        self.assertNotIn('policy_digest', result)
        self.assertEqual(result['checks'][0]['attempt'], 1)
        self.assertTrue(result['sources'])
        self.assertEqual(result['schema'], 'ci-github-check-evidence/1')

    def test_wrong_source_identities_never_pass(self):
        mutations = [lambda a: a.repo.update(id=11), lambda a: a.run.update(head_sha='b'*40),
                     lambda a: a.run.update(event='pull_request'), lambda a: a.run.update(workflow_id=21),
                     lambda a: a.jobs[0].update(run_id=41), lambda a: a.jobs[0].update(run_attempt=2),
                     lambda a: a.jobs[0].update(head_sha='b'*40), lambda a: a.check['app'].update(id=31),
                     lambda a: a.check['check_suite'].update(id=51), lambda a: a.check.update(head_sha='b'*40),
                     lambda a: a.check.update(name='other')]
        for change in mutations:
            with self.subTest(change=change):
                api = Api()
                change(api)
                result = self.collect(api)
                self.assertEqual(result['state'], 'unknown')
                self.assertFalse(result['complete'])

    def test_incomplete_and_duplicate_jobs(self):
        for jobs in ([], [Api().jobs[0], Api().jobs[0]]):
            api = Api()
            api.jobs = jobs
            self.assertEqual(self.collect(api)['state'], 'unknown')

    def test_missing_page_and_cap_fail_closed(self):
        original = Api()
        for count in (101, 1001):
            def api(path):
                payload = original(path)
                if 'workflow_runs' in payload:
                    payload['total_count'] = count
                return payload
            self.assertEqual(self.collect(api)['state'], 'unknown')

    def test_complete_multi_page_enumeration(self):
        original = Api()
        old = [{**original.run, 'id': i + 100, 'created_at': '2026-11-19T11:50:00Z',
                'updated_at': '2026-11-19T11:51:00Z'} for i in range(100)]
        def api(path):
            if '/workflows/' in path:
                page = int(parse_qs(urlsplit(path).query)['page'][0])
                rows = [original.run] + old
                return {'total_count': 101, 'workflow_runs': rows[(page-1)*100:page*100]}
            return original(path)
        self.assertEqual(self.collect(api)['state'], 'pass')

    def test_wrong_host_url_is_never_followed(self):
        api = Api()
        api.jobs[0]['check_run_url'] = 'https://other.example/secret'
        self.assertEqual(self.collect(api)['state'], 'unknown')
        self.assertFalse(any('other.example' in p for p in api.calls))

    def test_stale_or_future_completion(self):
        for completion in ('2026-11-19T10:00:00Z', '2026-11-19T12:01:00Z', None):
            api = Api()
            api.check['completed_at'] = completion
            self.assertEqual(self.collect(api)['state'], 'unknown')

    def test_failed_cancelled_running_skipped_do_not_pass(self):
        for conclusion in ('failure', 'cancelled', 'skipped', 'neutral', None):
            api = Api()
            api.run['conclusion'] = conclusion
            api.jobs[0]['conclusion'] = conclusion
            api.check['conclusion'] = conclusion
            self.assertEqual(self.collect(api)['state'], 'blocked')

    def test_later_run_failure_wins(self):
        api = Api()
        api.runs.append({**api.run, 'id': 39, 'created_at': '2026-11-19T11:50:00Z',
                         'updated_at': '2026-11-19T11:51:00Z'})
        api.run['conclusion'] = api.jobs[0]['conclusion'] = api.check['conclusion'] = 'failure'
        self.assertEqual(self.collect(api)['state'], 'blocked')

    def test_late_older_run_rerun_is_ambiguous(self):
        api = Api()
        api.runs.append({**api.run, 'id': 39, 'created_at': '2026-11-19T11:50:00Z',
                         'updated_at': '2026-11-19T11:59:00Z', 'run_attempt': 2})
        self.assertEqual(self.collect(api)['state'], 'unknown')

    def test_concurrent_rerun_or_new_run(self):
        for kind in ('attempt', 'new_run', 'check'):
            api = Api()
            def mutate(a, endpoint):
                if kind == 'attempt' and a.calls.count('repos/o/r/actions/runs/40') == 2:
                    a.run['run_attempt'] = 2
                if kind == 'check' and a.calls.count('repos/o/r/check-runs/70') == 2:
                    a.check['conclusion'] = 'failure'
                if kind == 'new_run' and '/workflows/' in endpoint and len(a.calls) > 2:
                    a.runs.append({**a.run, 'id': 41, 'created_at': '2026-11-19T11:58:00Z'})
            api.mutate = mutate
            self.assertEqual(self.collect(api)['state'], 'unknown')

    def test_api_error_is_sanitized(self):
        def api(path):
            raise GhError(kind='private-secret-string')
        report = self.collect(api)
        self.assertEqual(report['state'], 'unknown')
        self.assertNotIn('private-secret-string', json.dumps(report))

    def test_bounded_collection(self):
        ticks = iter([0, 0, 121])
        report = self.collect(monotonic=lambda: next(ticks))
        self.assertEqual(report['state'], 'unknown')

    def test_no_asserted_verification_fields(self):
        expected = copy.deepcopy(EXPECT)
        expected['verified'] = True
        with self.assertRaises(ValueError):
            self.collect(expect=expected)

    def test_invalid_policy_and_revision(self):
        for field, value in [('max_check_age_s', 0), ('repository_id', True), ('jobs', [])]:
            expected = copy.deepcopy(EXPECT)
            expected[field] = value
            with self.assertRaises(ValueError):
                self.collect(expect=expected)
        with self.assertRaises(ValueError):
            adapter.collect('o/r', 'main', EXPECT, Api())

    def test_cli_state_exit_codes(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'expect.json'
            output = Path(directory) / 'out.json'
            source.write_text(json.dumps(EXPECT))
            args = ['--repo', 'o/r', '--sha', SHA, '--expect', str(source), '--out', str(output)]
            for state, code in [('pass', 0), ('unknown', 1), ('blocked', 1)]:
                with mock.patch.object(adapter, 'collect', return_value={'state': state}):
                    self.assertEqual(adapter.main(args), code)
                    self.assertEqual(json.loads(output.read_text())['state'], state)

    def test_real_transport_is_explicit_get_only(self):
        seen = []
        def runner(command, **kwargs):
            seen.append(command)
            return subprocess.CompletedProcess(command, 0, 'HTTP/2.0 200 OK\n\n{}', '')
        client = GhClient(runner=runner, retries=0)
        client('repos/o/r')
        self.assertEqual(seen[0][seen[0].index('--method')+1], 'GET')
        self.assertEqual(seen[0][seen[0].index('--hostname')+1], 'github.com')


if __name__ == '__main__':
    unittest.main()
