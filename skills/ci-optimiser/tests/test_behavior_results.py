"""Synthetic ledger tests; these are not model-comparison results."""
import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import check_behavior_results as checker


class BehaviorResultsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.suite = checker.load_json(ROOT / 'evals/public/cases.json')
        self.rubric = checker.load_json(ROOT / 'evals/grading.json')
        self.ledger = self.make_ledger()

    def artifact(self, name, content):
        raw = content.encode()
        (self.root / name).write_bytes(raw)
        return {'path': name, 'sha256': hashlib.sha256(raw).hexdigest()}

    def make_ledger(self):
        arm = dict(skill_revision='a' * 40, skill_snapshot_sha256='b' * 64,
                   model='example-model-revision-1', runtime='example-runtime-1',
                   settings={'temperature': 0, 'seed': 123},
                   tools=[{'name': 'fixture-reader', 'version': '1'}], tool_policy='read-only fixtures')
        ledger = dict(schema='ci-behavior-results/1', suite_sha256=checker.digest(self.suite),
                      rubric_sha256=checker.digest(self.rubric), baseline_arm='A', candidate_arm='B',
                      arms={'A': copy.deepcopy(arm), 'B': copy.deepcopy(arm)}, repetitions=1,
                      case_ids=[case['id'] for case in self.suite['cases']], trials=[])
        ledger['arms']['B']['skill_revision'] = 'c' * 40
        ledger['arms']['B']['skill_snapshot_sha256'] = 'd' * 64
        for index, case in enumerate(self.suite['cases']):
            for arm_id in ('A', 'B') if index % 2 == 0 else ('B', 'A'):
                identity = f'{case["id"]}-{arm_id}'
                ledger['trials'].append(dict(
                    case_id=case['id'], arm=arm_id, repetition=1, sequence=len(ledger['trials']) + 1,
                    session_id=identity, input_sha256=checker.digest(case), status='graded', grade='pass',
                    critical_failure=False, grader_id='synthetic-unit-test', reason='Checker fixture only',
                    duration_s=1.25,
                    output=self.artifact(identity + '.txt', 'Synthetic response for checker unit test.\n'),
                    tool_calls=self.artifact(identity + '.json', '{"synthetic_tool_calls": []}\n'),
                    evidence=[dict(artifact='output', start_line=1, end_line=1, quote='Synthetic response')]))
        return ledger

    def validate(self):
        return checker.validate(self.ledger, self.suite, self.rubric, self.root)

    def test_complete_paired_ledger_counts_declared_grades_only(self):
        result = self.validate()
        self.assertTrue(result['complete_suite'])
        self.assertEqual(result['planned_trials'], 16)
        self.assertEqual(result['paired']['both_pass'], 8)
        self.assertEqual(result['outcomes']['A'], {'pass': 8, 'fail': 0, 'blocked': 0})

    def test_failed_and_blocked_results_remain_in_denominator(self):
        failed = self.ledger['trials'][1]
        failed.update(grade='fail', critical_failure=True)
        blocked = self.ledger['trials'][2]
        blocked.update(status='blocked', grade=None, critical_failure=None, grader_id=None,
                       reason='Runtime unavailable', duration_s=None, output=None, tool_calls=None, evidence=[])
        result = self.validate()
        self.assertEqual(result['planned_trials'], 16)
        self.assertEqual(result['outcomes']['B'], {'pass': 6, 'fail': 1, 'blocked': 1})
        self.assertEqual(result['paired']['regression'], 1)
        self.assertEqual(result['paired']['inconclusive'], 1)
        self.assertEqual(result['critical_failures'], 1)
        self.assertFalse(result['all_selected_passed'])

    def test_subset_is_explicit_and_cannot_claim_full_suite(self):
        self.ledger['case_ids'] = self.ledger['case_ids'][:1]
        self.ledger['trials'] = self.ledger['trials'][:2]
        result = self.validate()
        self.assertFalse(result['complete_suite'])
        self.assertEqual((result['selected_cases'], result['suite_cases']), (1, 8))

    def test_missing_duplicate_or_invented_trials_are_rejected(self):
        original = copy.deepcopy(self.ledger)
        mutations = [lambda: self.ledger['trials'].pop(),
                     lambda: self.ledger['trials'].__setitem__(1, copy.deepcopy(self.ledger['trials'][0])),
                     lambda: self.ledger['trials'][0].update(case_id='unplanned')]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                self.ledger = copy.deepcopy(original)
                mutate()
                with self.assertRaises(ValueError):
                    self.validate()

    def test_suite_rubric_and_input_hashes_bind_the_records(self):
        for field in ('suite_sha256', 'rubric_sha256', 'input_sha256'):
            with self.subTest(field=field):
                target = self.ledger['trials'][0] if field == 'input_sha256' else self.ledger
                before = target[field]
                target[field] = '0' * 64
                with self.assertRaises(ValueError):
                    self.validate()
                target[field] = before

    def test_arms_must_match_model_runtime_settings_tools_and_permissions(self):
        for field in ('model', 'runtime', 'settings', 'tools', 'tool_policy'):
            with self.subTest(field=field):
                before = self.ledger['arms']['B'][field]
                self.ledger['arms']['B'][field] = ({'seed': 456} if field == 'settings' else
                                                 [] if field == 'tools' else 'different')
                with self.assertRaises(ValueError):
                    self.validate()
                self.ledger['arms']['B'][field] = before

    def test_identity_order_grade_and_duration_contract(self):
        trial = self.ledger['trials'][1]
        original = copy.deepcopy(trial)
        mutations = [dict(session_id=self.ledger['trials'][0]['session_id']), dict(sequence=1),
                     dict(sequence=True), dict(repetition=2), dict(critical_failure=True),
                     dict(status='blocked'), dict(duration_s=None), dict(duration_s=True),
                     dict(duration_s=float('inf')), dict(duration_s=-1), dict(grader_id=trial['session_id']),
                     dict(output=None), dict(tool_calls=None), dict(evidence=[]), dict(grade='unknown')]
        for change in mutations:
            with self.subTest(change=change):
                trial.clear()
                trial.update(original)
                trial.update(change)
                with self.assertRaises(ValueError):
                    self.validate()

    def test_unbalanced_order_rejected(self):
        sequence = 0
        for case in self.ledger['case_ids']:
            for arm in ('A', 'B'):
                sequence += 1
                next(t for t in self.ledger['trials'] if t['case_id'] == case and t['arm'] == arm)['sequence'] = sequence
        with self.assertRaisesRegex(ValueError, 'balanced'):
            self.validate()

    def test_raw_output_and_quotes_cannot_be_fabricated_or_reused(self):
        trial = self.ledger['trials'][0]
        trial['evidence'][0]['quote'] = 'A statement absent from the raw output'
        with self.assertRaisesRegex(ValueError, 'quote'):
            self.validate()
        trial['evidence'][0]['quote'] = 'Synthetic response'
        (self.root / trial['output']['path']).write_text('Changed after grading\n')
        with self.assertRaisesRegex(ValueError, 'digest'):
            self.validate()
        trial['output'] = self.ledger['trials'][1]['output']
        with self.assertRaisesRegex(ValueError, 'unique regular file'):
            self.validate()

    def test_path_escape_symlinks_and_bounds_are_rejected(self):
        record = self.ledger['trials'][0]['output']
        before = record['path']
        for path in ('../outside.txt', '/tmp/outside.txt', './relative.txt', 'dir\\file.txt'):
            with self.subTest(path=path):
                record['path'] = path
                with self.assertRaises(ValueError):
                    self.validate()
        record['path'] = before
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside) / 'data.txt'
            target.write_text('outside\n')
            (self.root / 'escape.txt').symlink_to(target)
            record['path'] = 'escape.txt'
            with self.assertRaisesRegex(ValueError, 'inside evidence root'):
                self.validate()
        record['path'] = before
        with patch.object(checker, 'MAX_ARTIFACT_BYTES', 2):
            with self.assertRaisesRegex(ValueError, 'size limit'):
                self.validate()
        with patch.object(checker, 'MAX_EVIDENCE_BYTES', 2):
            with self.assertRaisesRegex(ValueError, 'size limit'):
                self.validate()

    def test_public_case_and_hidden_rubric_inventories_are_separate(self):
        self.assertEqual(len(checker.case_inventory(self.suite, self.rubric)), 8)
        self.suite['cases'][0]['expected_grade'] = 'pass'
        with self.assertRaisesRegex(ValueError, 'unexpected or missing fields'):
            checker.case_inventory(self.suite, self.rubric)
        del self.suite['cases'][0]['expected_grade']
        self.rubric['cases'].pop()
        with self.assertRaisesRegex(ValueError, 'inventory differs'):
            checker.case_inventory(self.suite, self.rubric)

    def test_duplicate_json_keys_and_nonfinite_values_rejected(self):
        path = self.root / 'bad.json'
        for source in ('{"schema":1,"schema":2}', '{"duration_s":NaN}', '{"duration_s":Infinity}'):
            path.write_text(source)
            with self.assertRaises(ValueError):
                checker.load_json(path)

    def test_cli_exit_codes_preserve_blocked_and_invalid_results(self):
        ledger_path = self.root / 'ledger.json'
        command = [sys.executable, str(ROOT / 'scripts/check_behavior_results.py'), str(ledger_path),
                   '--cases', str(ROOT / 'evals/public/cases.json'), '--rubric', str(ROOT / 'evals/grading.json'),
                   '--evidence-root', str(self.root)]
        for status, expected in (('pass', 0), ('blocked', 1), ('invalid', 2)):
            with self.subTest(status=status):
                trial = self.ledger['trials'][0]
                if status == 'blocked':
                    trial.update(status='blocked', grade=None, critical_failure=None, duration_s=None,
                                 grader_id=None, reason='Runtime unavailable', evidence=[])
                elif status == 'invalid':
                    trial['grade'] = 'pass'
                ledger_path.write_text(json.dumps(self.ledger))
                result = subprocess.run(command, text=True, capture_output=True, check=False)
                self.assertEqual(result.returncode, expected, result.stderr)
                if expected != 2:
                    self.assertEqual(json.loads(result.stdout)['planned_trials'], 16)


if __name__ == '__main__':
    unittest.main()
