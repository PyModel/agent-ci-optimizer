#!/usr/bin/env python3
"""Diagnostic cache economics from ci-cache-events/1 or /2; never a CI gate.

Version 1 retains supplied comparability and generic hit diagnostics. Version 2
separates restore/save outcomes, compares typed initial states and immutable
context, and requires explicit timing semantics. See references/13-measured-adapters.md.
Supplied evidence is not authenticated. Missing data stays unknown.
"""
import argparse
import json
import math
import sys
from collections import defaultdict
from cache_evidence import compare_contexts, reference
from plan_shards import label, load_json, number, rows

COMPONENTS = ('restore_s', 'save_s', 'work_s', 'uncached_work_s')
RESTORE_OUTCOMES = {'exact_hit', 'fallback_hit', 'miss', 'restore_failure', 'disabled', 'unknown'}
SAVE_OUTCOMES = {'saved', 'not_needed', 'write_disabled', 'save_failure', 'unknown'}
V2_FIELDS = {'id', 'cache', 'cohort', 'restore_outcome', 'save_outcome', 'comparison',
             'timing_model', 'timing_scope', 'elapsed_s', 'uncached_elapsed_s',
             'restored_bytes', 'saved_bytes', *COMPONENTS}


def timings(event, keys):
    values = {key: event.get(key) for key in keys}
    if any(v is not None and not number(v) for v in values.values()):
        raise ValueError('costs must be finite nonnegative seconds or null')
    return values


def legacy_event(event):
    hit, comparable = event.get('hit'), event.get('baseline_comparable')
    if any(v is not None and not isinstance(v, bool) for v in (hit, comparable)):
        raise ValueError('hit and baseline_comparable must be boolean or null')
    outcome = event.get('outcome', 'unknown')
    if not isinstance(outcome, str) or outcome not in {'exact_hit', 'fallback_hit', 'miss', 'restore_failure',
                       'save_failure', 'write_disabled', 'unknown'}:
        raise ValueError('invalid cache outcome')
    if (outcome in {'exact_hit', 'fallback_hit'} and hit is False) or (outcome == 'miss' and hit is True):
        raise ValueError('contradictory hit and outcome')
    costs = timings(event, COMPONENTS)
    net = None
    if comparable is True and all(v is not None for v in costs.values()):
        net = costs['uncached_work_s'] - costs['work_s'] - costs['restore_s'] - costs['save_s']
    return {'hit': hit, 'outcome': outcome, 'net_saved_s': net}


def layered_event(event):
    if set(event) - V2_FIELDS:
        raise ValueError('unknown version 2 cache event fields')
    for key in ('id', 'cache', 'cohort'):
        reference(event[key])
    restore, save = event.get('restore_outcome', 'unknown'), event.get('save_outcome', 'unknown')
    if not isinstance(restore, str) or restore not in RESTORE_OUTCOMES:
        raise ValueError('invalid restore outcome')
    if not isinstance(save, str) or save not in SAVE_OUTCOMES:
        raise ValueError('invalid save outcome')
    comparison = compare_contexts(event['comparison']) if event.get('comparison') is not None else None
    status = comparison['status'] if comparison else 'unknown'
    model, scope = event.get('timing_model', 'unknown'), reference(event.get('timing_scope'))
    if not isinstance(model, str) or model not in {'sequential_components', 'elapsed_total', 'unknown'}:
        raise ValueError('invalid timing model')
    costs = timings(event, (*COMPONENTS, 'elapsed_s', 'uncached_elapsed_s'))
    required = COMPONENTS if model == 'sequential_components' else ('elapsed_s', 'uncached_elapsed_s')
    measured = model != 'unknown' and scope is not None and all(costs[k] is not None for k in required)
    net = None
    economics_status = status + '_comparison'
    if status == 'comparable_supplied_evidence':
        economics_status = 'incomplete_timing'
        if measured:
            net = (costs['uncached_elapsed_s'] - costs['elapsed_s'] if model == 'elapsed_total' else
                   costs['uncached_work_s'] - costs['work_s'] - costs['restore_s'] - costs['save_s'])
            economics_status = 'measured_supplied_comparison'
    hit = True if restore in {'exact_hit', 'fallback_hit'} else (False if restore == 'miss' else None)
    return {'hit': hit, 'restore_outcome': restore, 'save_outcome': save,
            'comparison': comparison, 'timing_model': model, 'timing_scope': scope, **costs,
            'economics_status': economics_status, 'net_saved_s': net}


def analyze(data):
    schema = data.get('schema') if isinstance(data, dict) else None
    if schema not in ('ci-cache-events/1', 'ci-cache-events/2'):
        raise ValueError('unsupported cache event schema')
    version = schema.rsplit('/', 1)[1]
    if version == '2' and set(data) - {'schema', 'events'}:
        raise ValueError('unknown version 2 document fields')
    groups = defaultdict(list)
    seen = set()
    for event in rows(data, schema, 'events'):
        if not isinstance(event, dict) or not all(label(event.get(k)) for k in ('id', 'cache', 'cohort')):
            raise ValueError('event id, cache, and cohort required')
        if event['id'] in seen:
            raise ValueError('duplicate event ID')
        seen.add(event['id'])
        byte_counts = {}
        for key in ('restored_bytes', 'saved_bytes'):
            value = event.get(key)
            if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
                raise ValueError('byte counts must be nonnegative integers or null')
            byte_counts[key] = value
        normalized = legacy_event(event) if version == '1' else layered_event(event)
        if normalized['net_saved_s'] is not None and not math.isfinite(normalized['net_saved_s']):
            raise ValueError('cost overflow')
        groups[(event['cache'], event['cohort'])].append({'id': event['id'], **normalized, **byte_counts})
    result = []
    for (cache, cohort), events in sorted(groups.items()):
        events = sorted(events, key=lambda e: e['id'])
        hits = sum(e['hit'] is True for e in events)
        misses = sum(e['hit'] is False for e in events)
        measured = [e['net_saved_s'] for e in events if e['net_saved_s'] is not None]
        total = sum(measured)
        if not math.isfinite(total):
            raise ValueError('cost overflow')
        complete = len(measured) == len(events)
        negative_ids = [e['id'] for e in events if e['net_saved_s'] is not None and e['net_saved_s'] < 0]
        outcomes = ('outcome',) if version == '1' else ('restore_outcome', 'save_outcome')
        counts = {key + '_counts': {name: sum(e[key] == name for e in events) for name in sorted({e[key] for e in events})}
                  for key in outcomes}
        result.append({'cache': cache, 'cohort': cohort, 'events': events, **counts,
                       'total_restored_bytes': sum(e['restored_bytes'] for e in events) if all(e['restored_bytes'] is not None for e in events) else None,
                       'total_saved_bytes': sum(e['saved_bytes'] for e in events) if all(e['saved_bytes'] is not None for e in events) else None,
                       'sample_count': len(events), 'known_hits': hits, 'known_misses': misses,
                       'unknown_hits': len(events) - hits - misses,
                       'observed_hit_rate': hits / (hits + misses) if hits + misses else None,
                       'economics_samples': len(measured), 'unknown_economics': len(events) - len(measured),
                       'known_net_saved_s': total if measured else None,
                       'total_net_saved_s': total if complete else None,
                       'mean_net_saved_s': total / len(measured) if measured else None,
                       'negative_savings_event_count': len(negative_ids),
                       'negative_savings_event_ids': negative_ids,
                       'warnings': ['measured_negative_cache_events'] if negative_ids else [],
                       'recommendation': 'investigate_negative_savings' if complete and total < 0 else 'measure_before_changing'})
    return {'schema': 'ci-cache-analysis/' + version, 'groups': result,
            'limitations': ['Baselines and cohort comparability are supplied, not verified.',
                           'Savings are runner seconds, not critical-path or monetary savings.',
                           'Partial economics cannot establish total savings.',
                           'Negative-event warnings describe observations, not established population regressions or automatic cache-removal decisions.',
                           *(['Layer identities, fingerprints, timing scopes and evidence references are supplied, never authenticated.',
                              'Sequential components must not overlap; elapsed totals must cover the same complete scope, including post-job saves.',
                              'Cache economics does not establish fresh full-suite execution or merge eligibility.'] if version == '2' else [])]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('events')
    args = parser.parse_args(argv)
    try:
        print(json.dumps(analyze(load_json(args.events)), indent=2, allow_nan=False))
        return 0
    except (ValueError, OSError, RecursionError, OverflowError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
