#!/usr/bin/env python3
"""Structured cache economics; never infer hit rate from log wording.

Schema ci-cache-events/1: events [{id, cache, cohort, hit: bool|null,
restore_s, save_s, work_s, uncached_work_s, baseline_comparable: bool,
outcome?, restored_bytes?, saved_bytes?}]. Outcome is explicitly reported, never
inferred; allowed values are exact_hit/fallback_hit/miss/restore_failure/
save_failure/write_disabled/unknown. Byte counts are optional nonnegative integers.
Cohort qualifies workload/config/runner. Baseline comparability is an adapter
assertion, not verified causality. Costs need explicit zero or remain unknown.
"""
import argparse
import json
import math
import sys
from collections import defaultdict
from plan_shards import label, load_json, number, rows


def analyze(data):
    groups = defaultdict(list)
    seen = set()
    for event in rows(data, 'ci-cache-events/1', 'events'):
        if not isinstance(event, dict) or not all(label(event.get(k)) for k in ('id', 'cache', 'cohort')):
            raise ValueError('event id, cache, and cohort required')
        if event['id'] in seen:
            raise ValueError('duplicate event ID')
        seen.add(event['id'])
        hit = event.get('hit')
        comparable = event.get('baseline_comparable')
        if any(v is not None and not isinstance(v, bool) for v in (hit, comparable)):
            raise ValueError('hit and baseline_comparable must be boolean or null')
        outcome = event.get('outcome', 'unknown')
        if not isinstance(outcome, str) or outcome not in {'exact_hit', 'fallback_hit', 'miss', 'restore_failure',
                           'save_failure', 'write_disabled', 'unknown'}:
            raise ValueError('invalid cache outcome')
        if (outcome in {'exact_hit', 'fallback_hit'} and hit is False) or (outcome == 'miss' and hit is True):
            raise ValueError('contradictory hit and outcome')
        byte_counts = {}
        for key in ('restored_bytes', 'saved_bytes'):
            value = event.get(key)
            if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
                raise ValueError('byte counts must be nonnegative integers or null')
            byte_counts[key] = value
        costs = [event.get(k) for k in ('restore_s', 'save_s', 'work_s', 'uncached_work_s')]
        if any(v is not None and not number(v) for v in costs):
            raise ValueError('costs must be finite nonnegative seconds or null')
        net = None
        if comparable is True and all(v is not None for v in costs):
            restore, save, work, baseline = costs
            net = baseline - work - restore - save
            if not math.isfinite(net):
                raise ValueError('cost overflow')
        groups[(event['cache'], event['cohort'])].append({'id': event['id'], 'hit': hit, 'outcome': outcome, 'net_saved_s': net, **byte_counts})
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
        result.append({'cache': cache, 'cohort': cohort, 'events': sorted(events, key=lambda e: e['id']),
                       'outcome_counts': {name: sum(e['outcome'] == name for e in events) for name in sorted({e['outcome'] for e in events})},
                       'total_restored_bytes': sum(e['restored_bytes'] for e in events) if all(e['restored_bytes'] is not None for e in events) else None,
                       'total_saved_bytes': sum(e['saved_bytes'] for e in events) if all(e['saved_bytes'] is not None for e in events) else None,
                       'sample_count': len(events), 'known_hits': hits, 'known_misses': misses,
                       'unknown_hits': len(events) - hits - misses,
                       'observed_hit_rate': hits / (hits + misses) if hits + misses else None,
                       'economics_samples': len(measured), 'unknown_economics': len(events) - len(measured),
                       'known_net_saved_s': total if measured else None,
                       'total_net_saved_s': total if complete else None,
                       'mean_net_saved_s': total / len(measured) if measured else None,
                       'recommendation': 'investigate_negative_savings' if complete and total < 0 else 'measure_before_changing'})
    return {'schema': 'ci-cache-analysis/1', 'groups': result,
            'limitations': ['Baselines and cohort comparability are supplied, not verified.',
                           'Savings are runner seconds, not critical-path or monetary savings.',
                           'Partial economics cannot establish total savings.']}


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
