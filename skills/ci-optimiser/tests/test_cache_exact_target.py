"""Exact-hit targets use explicit complete PR populations, independently of cost."""
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import analyze_cache as cache


def event(name, outcome='exact_hit', eligible=True, **changes):
    return {'id': name, 'cache': 'deps', 'cohort': 'PR-window-a', 'eligible_pr_restore': eligible,
            'restore_outcome': outcome, **changes}


def population(count, **changes):
    return {'cache': 'deps', 'cohort': 'PR-window-a', 'eligible_count': count, 'complete': True,
            'scope': 'repo-a/pull-requests/attempt-1/2026-11-01..20',
            'evidence': 'review/all-eligible-PR-restore-events', **changes}


def analyze(events, pop):
    return cache.analyze({'schema': 'ci-cache-events/2', 'events': events,
                          'populations': [] if pop is None else [pop]})['groups'][0]


class ExactTarget(unittest.TestCase):
    def test_generic_all_hits_cannot_hide_fallback_or_unknown(self):
        group = analyze([event('exact'), event('fallback', 'fallback_hit'), event('unknown', 'unknown')], population(3))
        self.assertEqual(group['observed_hit_rate'], 1)
        result = group['exact_hit_target']
        self.assertEqual(result['known_exact_fraction'], 1 / 3)
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(result['eligible_outcomes'], {'exact_hit': 1, 'fallback_hit': 1, 'miss': 0,
                                                      'restore_failure': 0, 'disabled': 0, 'unknown': 1})

    def test_strict_boundary_does_not_round_95_percent_up(self):
        for count, misses, fraction, status in ((19, 1, 0.95, 'not_met'), (20, 1, 20 / 21, 'met'),
                                                (1, 0, 1, 'met')):
            events = [event(str(i)) for i in range(count)]
            events.extend(event('lockfile-change-' + str(i), 'miss') for i in range(misses))
            result = analyze(events, population(len(events)))['exact_hit_target']
            self.assertEqual(result['known_exact_fraction'], fraction)
            self.assertEqual(result['status'], status)
            self.assertEqual(result['operator'], '>')
            self.assertFalse(result['authenticated'])

    def test_errors_remain_in_denominator_and_unknowns_prevent_met(self):
        events = [event(str(i)) for i in range(99)]
        error = analyze([*events, event('error', 'restore_failure')], population(100))['exact_hit_target']
        self.assertEqual(error['eligible_outcomes']['restore_failure'], 1)
        self.assertEqual(error['known_exact_fraction'], 0.99)
        self.assertEqual(error['status'], 'met')
        for outcome in ('unknown', 'disabled'):
            result = analyze([*events, event('unresolved', outcome)], population(100))['exact_hit_target']
            self.assertEqual(result['known_exact_fraction'], 0.99)
            self.assertEqual(result['status'], 'unknown')

    def test_missing_population_scope_evidence_or_completeness_cannot_pass(self):
        events = [event('a')]
        self.assertEqual(analyze(events, None)['exact_hit_target']['status'], 'unassessed')
        for changes in ({'complete': False}, {'complete': None}, {'eligible_count': None},
                        {'scope': None}, {'evidence': None}, {'eligible_count': 2}, {'eligible_count': 0}):
            with self.subTest(changes=changes):
                result = analyze(events, population(1, **changes))['exact_hit_target']
                self.assertEqual(result['status'], 'unknown')
                self.assertTrue(result['reasons'])
        result = analyze(events, population(0))['exact_hit_target']
        self.assertIsNone(result['known_exact_fraction'])

    def test_eligibility_is_not_inferred_from_saving_or_restore_labels(self):
        for save in ('saved', 'save_failure', 'write_disabled', 'not_needed'):
            unresolved = event('unknown-eligibility', eligible=None, save_outcome=save)
            result = analyze([event('a'), unresolved], population(1))['exact_hit_target']
            self.assertEqual(result['status'], 'unknown')
            self.assertEqual(result['unknown_eligibility_count'], 1)
            unresolved['eligible_pr_restore'] = False
            result = analyze([event('a'), unresolved], population(1))['exact_hit_target']
            self.assertEqual(result['status'], 'met')
            self.assertEqual(result['observed_ineligible_count'], 1)
            self.assertEqual(result['eligible_outcomes']['exact_hit'], 1)
        value = event('absent')
        value.pop('eligible_pr_restore')
        self.assertEqual(analyze([value], population(1))['exact_hit_target']['status'], 'unknown')

    def test_population_without_events_stays_visible_and_not_zero_cost(self):
        group = analyze([], population(100))
        self.assertEqual(group['sample_count'], 0)
        self.assertEqual(group['exact_hit_target']['status'], 'unknown')
        self.assertIsNone(group['total_net_saved_s'])
        self.assertIsNone(group['total_restored_bytes'])
        result = analyze([], population(0))['exact_hit_target']
        self.assertEqual(result['status'], 'unassessed')
        self.assertIsNone(result['known_exact_fraction'])
        self.assertEqual(result['reasons'], ['empty_eligible_population'])

    def test_legacy_hit_diagnostics_are_not_a_target_assessment(self):
        result = cache.analyze({'schema': 'ci-cache-events/1', 'events': [
            {'id': 'one', 'cache': 'deps', 'cohort': 'legacy', 'hit': True, 'outcome': 'exact_hit'}]})['groups'][0]
        self.assertEqual(result['observed_hit_rate'], 1)
        self.assertEqual(result['exact_hit_target']['status'], 'unassessed')
        self.assertIsNone(result['exact_hit_target']['known_exact_fraction'])

    def test_high_exact_hit_rate_does_not_override_measured_loss(self):
        from test_cache_evidence import event as measured_event
        value = measured_event(eligible_pr_restore=True)
        pop = population(1, cache=value['cache'], cohort=value['cohort'])
        result = analyze([value], pop)
        self.assertEqual(result['exact_hit_target']['status'], 'met')
        self.assertEqual(result['total_net_saved_s'], -32)
        self.assertEqual(result['negative_savings_event_count'], 1)
        self.assertEqual(result['recommendation'], 'investigate_negative_savings')

    def test_population_scopes_never_mix_and_input_order_is_stable(self):
        first, second = event('a'), event('b', 'miss', cohort='PR-window-b')
        data = {'schema': 'ci-cache-events/2', 'events': [first, second],
                'populations': [population(1), population(1, cohort='PR-window-b')]}
        original = copy.deepcopy(data)
        result = cache.analyze(data)
        self.assertEqual(data, original)
        self.assertEqual([g['exact_hit_target']['status'] for g in result['groups']], ['met', 'not_met'])
        data['events'].reverse()
        data['populations'].reverse()
        self.assertEqual(cache.analyze(data), result)

    def test_population_fields_are_bounded_and_ambiguous_input_is_rejected(self):
        for changes in ({'eligible_count': True}, {'eligible_count': -1}, {'eligible_count': 100001},
                        {'eligible_count': 1.0}, {'complete': 1}, {'scope': ''}, {'evidence': 'a\n'},
                        {'cache': None}, {'cohort': 'x' * 1025}, {'extra': True}):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError): analyze([event('a')], population(1, **changes))
        for eligibility in (1, 0, 'true', [], {}):
            with self.assertRaises(ValueError): analyze([event('a', eligible=eligibility)], population(1))
        for pops in (None, {}, [population(1), population(1)]):
            with self.assertRaises(ValueError):
                cache.analyze({'schema': 'ci-cache-events/2', 'events': [], 'populations': pops})


if __name__ == '__main__':
    unittest.main()
