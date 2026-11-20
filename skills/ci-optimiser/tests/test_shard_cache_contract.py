"""Inventory preservation, unknown handling and explicit cost evidence."""
import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import plan_shards as shards
import analyze_cache as cache


def fixture():
    return ({'schema': 'ci-test-inventory/1', 'required_coverage': ['unit', 'integration'],
             'tests': [{'id': 'a', 'serial_group': 'serial', 'covers': ['unit']},
                       {'id': 'b', 'serial_group': 'serial'},
                       {'id': 'c', 'covers': ['integration']}, {'id': 'd'}]},
            {'schema': 'ci-test-timings/1', 'tests': [{'id': 'a', 'duration_s': 8},
                                                    {'id': 'b', 'duration_s': 4},
                                                    {'id': 'c', 'duration_s': 10}]})


def event(**changes):
    value = {'id': '1', 'cache': 'deps', 'cohort': 'linux/config1/work1', 'hit': True,
             'restore_s': 3, 'save_s': 0, 'work_s': 2, 'uncached_work_s': 20,
             'baseline_comparable': True}
    value.update(changes)
    return value


def analyze(*events):
    return cache.analyze({'schema': 'ci-cache-events/1', 'events': list(events)})['groups']


class Shards(unittest.TestCase):
    def test_preserves_every_id_once_and_groups(self):
        inv, times = fixture()
        result = shards.plan(inv, times, 2)
        ids = [t['id'] for s in result['shards'] for t in s['tests']]
        self.assertEqual(sorted(ids), ['a', 'b', 'c', 'd'])
        self.assertEqual(len(ids), len(set(ids)))
        group = next(s for s in result['shards'] if any(t['id'] == 'a' for t in s['tests']))
        self.assertIn('b', [t['id'] for t in group['tests']])
        self.assertEqual(result['coverage_status'], 'declared_not_verified')
        self.assertEqual(result['required_coverage'], ['integration', 'unit'])

    def test_deterministic_input_permutation(self):
        inv, times = fixture()
        expected = shards.plan(inv, times, 3)
        inv['tests'].reverse()
        times['tests'].reverse()
        self.assertEqual(shards.plan(inv, times, 3), expected)

    def test_unknown_is_not_zero_or_estimated_runtime(self):
        inv, times = fixture()
        result = shards.plan(inv, times, 1)['shards'][0]
        self.assertIsNone(result['duration_s'])
        self.assertEqual(result['known_duration_s'], 22)
        self.assertEqual(result['unknown_timing_ids'], ['d'])
        times['tests'] = []
        result = shards.plan(inv, times, 2)
        self.assertTrue(all(s['duration_s'] is None for s in result['shards']))

    def test_zero_and_empty_shards_are_observed(self):
        inv = {'schema': 'ci-test-inventory/1', 'tests': [{'id': 'a'}]}
        times = {'schema': 'ci-test-timings/1', 'tests': [{'id': 'a', 'duration_s': 0}]}
        result = shards.plan(inv, times, 3)
        self.assertEqual([s['duration_s'] for s in result['shards']], [0, 0, 0])

    def test_rejects_ambiguous_or_invalid_timings(self):
        for entry in ({'id': 'z', 'duration_s': 1}, {'id': 'a', 'duration_s': 1},
                      {'id': 'd', 'duration_s': -1}, {'id': 'd', 'duration_s': True},
                      {'id': 'd', 'duration_s': float('nan')}):
            with self.subTest(entry=entry):
                inv, times = fixture()
                times['tests'].append(entry)
                with self.assertRaises(ValueError): shards.plan(inv, times, 2)

    def test_missing_coverage_and_duplicate_inventory_fail(self):
        inv, times = fixture()
        inv['required_coverage'].append('missing')
        with self.assertRaises(ValueError): shards.plan(inv, times, 2)
        inv, times = fixture()
        inv['tests'].append(copy.deepcopy(inv['tests'][0]))
        with self.assertRaises(ValueError): shards.plan(inv, times, 2)

    def test_bounds_and_cli_error(self):
        inv, times = fixture()
        for count in (0, True, 1025):
            with self.assertRaises(ValueError): shards.plan(inv, times, count)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'data.json'
            path.write_text('{"schema":1,"schema":2}')
            with self.assertRaises(ValueError): shards.load_json(path)
            path.write_text(' ' * 33)
            with patch.object(shards, 'MAX_BYTES', 32):
                with self.assertRaises(ValueError): shards.load_json(path)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(shards.main(['--inventory', '/nonexistent/inventory', '--timings', '/nonexistent/times', '--shards', '2']), 2)


class Cache(unittest.TestCase):
    def test_negative_events_remain_visible_in_partial_and_positive_cohorts(self):
        negative = event(id='loss', restore_s=30)
        for other, total in ((None, -12), (event(id='unknown', save_s=None), None),
                             (event(id='gain'), 3)):
            with self.subTest(other=other):
                result = analyze(negative, *([other] if other else []))[0]
                self.assertEqual(result['negative_savings_event_count'], 1)
                self.assertEqual(result['negative_savings_event_ids'], ['loss'])
                self.assertEqual(result['warnings'], ['measured_negative_cache_events'])
                self.assertEqual(result['total_net_saved_s'], total)
                self.assertEqual(result['recommendation'], 'investigate_negative_savings'
                                 if other is None else 'measure_before_changing')

    def test_negative_warning_requires_a_measured_loss_and_has_stable_ids(self):
        result = analyze(event(id='z', restore_s=30), event(id='a', restore_s=40))[0]
        self.assertEqual(result['negative_savings_event_ids'], ['a', 'z'])
        self.assertEqual(result['negative_savings_event_count'], 2)
        result = analyze(event(), event(id='unknown', work_s=None),
                         event(id='unverified', restore_s=30, baseline_comparable=False))[0]
        self.assertEqual(result['negative_savings_event_count'], 0)
        self.assertEqual(result['negative_savings_event_ids'], [])
        self.assertEqual(result['warnings'], [])

    def test_costs_not_hit_rate_determine_economics(self):
        result = analyze(event(restore_s=30))[0]
        self.assertEqual(result['observed_hit_rate'], 1)
        self.assertEqual(result['total_net_saved_s'], -12)
        self.assertEqual(result['recommendation'], 'investigate_negative_savings')
        miss = analyze(event(hit=False, restore_s=2, save_s=4, work_s=20))[0]
        self.assertEqual(miss['total_net_saved_s'], -6)

    def test_unknown_cost_is_not_zero(self):
        result = analyze(event(), event(id='2', save_s=None, hit=None))[0]
        self.assertEqual(result['known_net_saved_s'], 15)
        self.assertIsNone(result['total_net_saved_s'])
        self.assertEqual(result['unknown_economics'], 1)
        self.assertEqual(result['unknown_hits'], 1)
        result = analyze(event(hit=None, baseline_comparable=False))[0]
        self.assertIsNone(result['known_net_saved_s'])
        self.assertIsNone(result['observed_hit_rate'])

    def test_cohorts_never_mix_and_order_deterministic(self):
        first, second = event(), event(id='2', cohort='other')
        self.assertEqual(analyze(first, second), analyze(second, first))
        self.assertEqual(len(analyze(first, second)), 2)

    def test_invalid_events_rejected(self):
        for changes in ({'hit': 1}, {'restore_s': -1}, {'save_s': True},
                        {'work_s': float('inf')}, {'baseline_comparable': 'yes'}):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError): analyze(event(**changes))
        with self.assertRaises(ValueError): analyze(event(), event())

    def test_outcomes_and_bytes_are_explicit(self):
        result = analyze(event(hit=None, outcome='exact_hit', restored_bytes=100, saved_bytes=0))[0]
        self.assertIsNone(result['observed_hit_rate'])
        self.assertEqual(result['outcome_counts'], {'exact_hit': 1})
        self.assertEqual(result['total_restored_bytes'], 100)
        self.assertEqual(result['total_saved_bytes'], 0)
        result = analyze(event(), event(id='2', restored_bytes=5))[0]
        self.assertIsNone(result['total_restored_bytes'])
        for change in ({'outcome': 'invalid'}, {'outcome': 'miss'},
                       {'restored_bytes': -1}, {'saved_bytes': True}):
            with self.assertRaises(ValueError): analyze(event(**change))

    def test_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'events.json'
            path.write_text(json.dumps({'schema': 'ci-cache-events/1', 'events': [event()]}))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(cache.main([str(path)]), 0)
            self.assertEqual(json.loads(output.getvalue())['schema'], 'ci-cache-analysis/1')


if __name__ == '__main__':
    unittest.main()
