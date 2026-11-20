"""Layered cache counterfactuals, independent lifecycle outcomes and unknowns."""
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import analyze_cache as cache
import cache_evidence as evidence


def context(job):
    states = {layer: {'state': 'warm', 'identity': 'snapshot-' + layer,
                      'evidence': 'manifest/' + job + '/' + layer}
              for layer in evidence.LAYERS}
    states['test_results']['state'] = 'disabled'
    return {'repository': 'example/project', 'revision': 'a' * 40, 'workflow': '.github/workflows/ci.yml',
            'run_id': 'run-1', 'job_id': job, 'attempt': 1, 'config_digest': 'a' * 64,
            'action_digest': 'b' * 64, 'toolchain_digest': 'c' * 64, 'runner_image_digest': 'd' * 64,
            'workload_digest': 'e' * 64, 'states': states}


def comparison():
    baseline, candidate = context('archive-off'), context('archive-on')
    baseline['states']['action_archive']['state'] = 'disabled'
    candidate['config_digest'] = 'f' * 64
    return {'layer_under_test': 'action_archive', 'baseline': baseline, 'candidate': candidate,
            'review_reference': 'review/only-archive-toggle'}


def event(**changes):
    value = {'id': 'event-1', 'cache': 'go', 'cohort': 'linux/warm-snapshot/revision-a',
             'restore_outcome': 'exact_hit', 'save_outcome': 'not_needed',
             'comparison': comparison(), 'timing_model': 'sequential_components',
             'timing_scope': 'review/setup-through-post-save',
             'restore_s': 32, 'save_s': 0, 'work_s': 35, 'uncached_work_s': 35}
    value.update(changes)
    return value


def analyze(*events):
    return cache.analyze({'schema': 'ci-cache-events/2', 'events': list(events)})


class StateContract(unittest.TestCase):
    def test_exact_six_layers_and_unresolved_identity_or_evidence(self):
        states = context('job')['states']
        normalized = evidence.validate_states(states)
        self.assertEqual(normalized, states)
        self.assertIsNot(normalized['runner_snapshot'], states['runner_snapshot'])
        self.assertEqual(evidence.unresolved_states(states), [])
        for key in ('identity', 'evidence'):
            value = copy.deepcopy(states)
            value['runner_snapshot'][key] = None
            self.assertEqual(evidence.unresolved_states(value), ['runner_snapshot'])
        states['toolchain']['state'] = 'unknown'
        self.assertEqual(evidence.unresolved_states(states), ['toolchain'])
        self.assertEqual(set(evidence.unresolved_states(evidence.unknown_states())), set(evidence.LAYERS))

    def test_malformed_and_unbounded_state_data_is_rejected(self):
        states = context('job')['states']
        mutations = [None, [], {**states, 'archive': states['action_archive']},
                     {k: v for k, v in states.items() if k != 'toolchain'}]
        for key, bad in (('state', True), ('state', 'hot'), ('identity', ''), ('identity', 'x' * 1025),
                         ('evidence', '\nreference'), ('evidence', 'a\x00b'), ('evidence', 1)):
            value = copy.deepcopy(states)
            value['toolchain'][key] = bad
            mutations.append(value)
        value = copy.deepcopy(states)
        value['toolchain']['verified'] = True
        mutations.append(value)
        for value in mutations:
            with self.subTest(value=value):
                with self.assertRaises(ValueError): evidence.validate_states(value)

    def test_context_identity_fields_are_bounded_and_typed(self):
        for key, bad in (('revision', 'main'), ('revision', True), ('attempt', True), ('attempt', 0),
                         ('attempt', 1001), ('job_id', ' x'), ('run_id', 'x\n'),
                         ('config_digest', 'tag'), ('action_digest', 'A' * 64),
                         ('runner_image_digest', 3), ('workload_digest', 'x' * 64)):
            value = context('job')
            value[key] = bad
            with self.subTest(key=key, bad=bad):
                with self.assertRaises(ValueError): evidence.validate_context(value)
        with self.assertRaises(ValueError): evidence.validate_context({'verified': True})
        self.assertEqual(evidence.validate_context({})['states'], evidence.unknown_states())


class LayeredEconomics(unittest.TestCase):
    def test_matched_warm_snapshot_detects_archive_overhead(self):
        result = analyze(event())
        self.assertEqual(result['schema'], 'ci-cache-analysis/2')
        group = result['groups'][0]
        observed = group['events'][0]
        self.assertEqual(observed['comparison']['status'], 'comparable_supplied_evidence')
        self.assertFalse(observed['comparison']['authenticated'])
        self.assertEqual(observed['comparison']['unknown_fields'], [])
        self.assertEqual(observed['net_saved_s'], -32)
        self.assertEqual(group['negative_savings_event_ids'], ['event-1'])
        self.assertEqual(group['observed_hit_rate'], 1)
        self.assertEqual(group['recommendation'], 'investigate_negative_savings')

    def test_cold_disk_versus_warm_disk_cannot_isolate_archive(self):
        value = event()
        value['comparison']['baseline']['states']['runner_snapshot']['state'] = 'cold'
        observed = analyze(value)['groups'][0]['events'][0]
        self.assertEqual(observed['comparison']['status'], 'incompatible')
        self.assertIn('states.runner_snapshot.state', observed['comparison']['incompatible_fields'])
        self.assertIsNone(observed['net_saved_s'])

    def test_missing_evidence_never_inherits_a_comparability_assertion(self):
        for arm, key in (('candidate', 'states'), ('baseline', 'run_id'), ('candidate', 'config_digest')):
            value = event()
            value['comparison'][arm].pop(key)
            group = analyze(value)['groups'][0]
            self.assertEqual(group['events'][0]['comparison']['status'], 'unknown')
            self.assertIsNone(group['total_net_saved_s'])
            self.assertEqual(group['negative_savings_event_count'], 0)
        for key in ('identity', 'evidence'):
            value = event()
            value['comparison']['candidate']['states']['runner_snapshot'][key] = None
            self.assertIsNone(analyze(value)['groups'][0]['events'][0]['net_saved_s'])
        value = event()
        value['comparison']['review_reference'] = None
        self.assertEqual(analyze(value)['groups'][0]['events'][0]['comparison']['status'], 'unknown')
        self.assertIsNone(analyze(event(comparison=None))['groups'][0]['total_net_saved_s'])

    def test_context_drift_and_replayed_or_identical_control_are_incompatible(self):
        changes = [('repository', 'another/project'), ('revision', 'b' * 40), ('workflow', 'other.yml'),
                   ('action_digest', '1' * 64), ('toolchain_digest', '2' * 64),
                   ('runner_image_digest', '3' * 64), ('workload_digest', '4' * 64), ('attempt', 2)]
        for key, changed in changes:
            value = event()
            value['comparison']['candidate'][key] = changed
            observed = analyze(value)['groups'][0]['events'][0]
            self.assertEqual(observed['comparison']['status'], 'incompatible')
            self.assertIn(key, observed['comparison']['incompatible_fields'])
            self.assertIsNone(observed['net_saved_s'])
        value = event()
        value['comparison']['candidate']['states']['dependencies']['identity'] = 'other-deps'
        self.assertIn('states.dependencies.identity', analyze(value)['groups'][0]['events'][0]['comparison']['incompatible_fields'])
        value = event()
        value['comparison']['candidate']['job_id'] = 'archive-off'
        self.assertIn('same_observation', analyze(value)['groups'][0]['events'][0]['comparison']['incompatible_fields'])
        value = event()
        value['comparison']['candidate']['states']['action_archive']['state'] = 'disabled'
        self.assertIn('layer_under_test_unchanged', analyze(value)['groups'][0]['events'][0]['comparison']['incompatible_fields'])

    def test_restore_and_save_lifecycle_failures_coexist(self):
        group = analyze(event(restore_outcome='fallback_hit', save_outcome='save_failure'))['groups'][0]
        self.assertEqual(group['restore_outcome_counts'], {'fallback_hit': 1})
        self.assertEqual(group['save_outcome_counts'], {'save_failure': 1})
        self.assertTrue(group['events'][0]['hit'])
        group = analyze(event(restore_outcome='restore_failure', save_outcome='write_disabled'))['groups'][0]
        self.assertEqual(group['unknown_hits'], 1)
        self.assertEqual(group['save_outcome_counts'], {'write_disabled': 1})

    def test_elapsed_totals_do_not_double_count_overlapping_components(self):
        group = analyze(event(timing_model='elapsed_total', elapsed_s=67, uncached_elapsed_s=35,
                              restore_s=40, save_s=12, work_s=60))['groups'][0]
        self.assertEqual(group['total_net_saved_s'], -32)
        self.assertEqual(group['events'][0]['restore_s'], 40)
        value = event(timing_model='elapsed_total', elapsed_s=67, uncached_elapsed_s=35)
        for key in cache.COMPONENTS:
            value.pop(key)
        self.assertEqual(analyze(value)['groups'][0]['total_net_saved_s'], -32)

    def test_unknown_timing_model_scope_or_required_costs_cannot_measure(self):
        for changes in ({'timing_model': 'unknown'}, {'timing_scope': None}, {'save_s': None},
                        {'timing_model': 'elapsed_total', 'elapsed_s': 35}):
            observed = analyze(event(**changes))['groups'][0]['events'][0]
            self.assertEqual(observed['economics_status'], 'incomplete_timing')
            self.assertIsNone(observed['net_saved_s'])

    def test_rejects_invalid_or_conflicting_contracts_and_nonfinite_costs(self):
        changes = [{'baseline_comparable': True}, {'hit': True}, {'outcome': 'exact_hit'},
                   {'restore_outcome': 'save_failure'}, {'save_outcome': 'exact_hit'},
                   {'restore_outcome': None}, {'timing_model': True}, {'timing_scope': False},
                   {'restore_s': -1}, {'save_s': True}, {'elapsed_s': float('nan')},
                   {'uncached_elapsed_s': float('inf')}, {'saved_bytes': True}, {'id': 'a\n'}]
        for change in changes:
            with self.subTest(change=change):
                with self.assertRaises(ValueError): analyze(event(**change))
        for comp in ({}, {'layer_under_test': 'disk'}, {**comparison(), 'verified': True}):
            with self.assertRaises(ValueError): analyze(event(comparison=comp))
        with self.assertRaises(ValueError): analyze(event(), event())

    def test_inputs_and_output_are_deterministic_without_mutation(self):
        first, second = event(id='z'), event(id='a', comparison=None)
        before = copy.deepcopy(first)
        self.assertEqual(analyze(first, second), analyze(second, first))
        self.assertEqual(first, before)
        result = analyze({'id': 'unknown', 'cache': 'deps', 'cohort': 'unknown'})['groups'][0]
        self.assertIsNone(result['total_net_saved_s'])
        self.assertEqual(result['restore_outcome_counts'], {'unknown': 1})
        self.assertEqual(result['save_outcome_counts'], {'unknown': 1})


if __name__ == '__main__':
    unittest.main()
