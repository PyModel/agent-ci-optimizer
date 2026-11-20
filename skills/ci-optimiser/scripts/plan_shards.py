#!/usr/bin/env python3
"""Normalized JSON timing import and deterministic inventory-preserving shard plan.

Inventory schema ci-test-inventory/1: tests [{id, serial_group?, covers?: [str]}],
required_coverage: [str]. Timing schema ci-test-timings/1: tests [{id, duration_s}].
Use exact runner dispatch IDs and one reviewed aggregate per ID (not retries).
Null/missing timing stays unknown. Serial groups must execute sequentially.
This does not import JUnit XML, execute tests, or verify coverage declarations.
"""
import argparse
import json
import math
import statistics
import sys
from pathlib import Path

MAX_BYTES = 8 * 1024 * 1024
MAX_ROWS = 100000


def load_json(path):
    with Path(path).open('rb') as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError('input exceeds 8 MiB')
    def pairs(items):
        result = {}
        for k, v in items:
            if k in result:
                raise ValueError('duplicate JSON key')
            result[k] = v
        return result
    def constant(value):
        raise ValueError('nonfinite JSON number')
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def label(v):
    return isinstance(v, str) and bool(v.strip()) and len(v) <= 1024


def number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0


def rows(data, schema, key):
    if not isinstance(data, dict) or data.get('schema') != schema:
        raise ValueError('schema must be ' + schema)
    value = data.get(key)
    if not isinstance(value, list) or len(value) > MAX_ROWS:
        raise ValueError('expected bounded record list')
    return value


def labels(value):
    if not isinstance(value, list) or len(value) > MAX_ROWS or not all(label(v) for v in value):
        raise ValueError('invalid labels')
    if len(value) != len(set(value)):
        raise ValueError('duplicate labels')
    return value


def plan(inventory, timing, shard_count):
    if not isinstance(shard_count, int) or isinstance(shard_count, bool) or not 1 <= shard_count <= 1024:
        raise ValueError('shards must be 1..1024')
    tests = {}
    for item in rows(inventory, 'ci-test-inventory/1', 'tests'):
        if not isinstance(item, dict) or not label(item.get('id')):
            raise ValueError('inventory ID required')
        ident = item['id']
        if ident in tests:
            raise ValueError('duplicate inventory ID')
        group = item.get('serial_group')
        if group is not None and not label(group):
            raise ValueError('invalid serial group')
        tests[ident] = {'id': ident, 'serial_group': group, 'covers': sorted(labels(item.get('covers', [])))}
    if not tests:
        raise ValueError('empty inventory')
    required = labels(inventory.get('required_coverage', []))
    covered = {c for t in tests.values() for c in t['covers']}
    if set(required) - covered:
        raise ValueError('missing required coverage declaration')
    timings = {}
    for item in rows(timing, 'ci-test-timings/1', 'tests'):
        if not isinstance(item, dict) or not label(item.get('id')):
            raise ValueError('timing ID required')
        ident = item['id']
        if ident not in tests or ident in timings:
            raise ValueError('unknown or duplicate timing ID')
        duration = item.get('duration_s')
        if duration is not None and not number(duration):
            raise ValueError('duration must be finite nonnegative seconds or null')
        timings[ident] = duration
    known = [v for v in timings.values() if v is not None]
    fallback = max(1.0, statistics.median(known)) if known else 1.0
    groups = {}
    for ident, item in tests.items():
        key = ('serial', item['serial_group']) if item['serial_group'] is not None else ('test', ident)
        groups.setdefault(key, []).append(ident)
    def weight(ids):
        total = sum(timings[i] if timings.get(i) is not None else fallback for i in ids)
        if not math.isfinite(total):
            raise ValueError('duration overflow')
        return total
    groups = [sorted(ids) for ids in groups.values()]
    shards = [{'index': i, 'tests': [], 'scheduling_weight': 0.0} for i in range(shard_count)]
    for ids in sorted(groups, key=lambda x: (-weight(x), tuple(x))):
        target = min(shards, key=lambda s: (s['scheduling_weight'], len(s['tests']), s['index']))
        target['tests'].extend(ids)
        target['scheduling_weight'] += weight(ids)
        if not math.isfinite(target['scheduling_weight']):
            raise ValueError('duration overflow')
    for shard in shards:
        ids = shard['tests']
        unknown = sorted(i for i in ids if timings.get(i) is None)
        shard['unknown_timing_ids'] = unknown
        shard['known_duration_s'] = sum(timings[i] for i in ids if timings.get(i) is not None)
        shard['duration_s'] = None if unknown else shard['known_duration_s']
        shard['tests'] = [tests[i] for i in ids]
    return {'schema': 'ci-shard-plan/1', 'inventory_count': len(tests), 'shards': shards,
            'required_coverage': sorted(required), 'declared_coverage': sorted(covered),
            'coverage_status': 'declared_not_verified', 'serial_group_execution': 'sequential_required',
            'unknown_weight': fallback, 'algorithm': 'deterministic_grouped_lpt',
            'limitations': ['Unknown weight is a scheduling heuristic, not predicted seconds.',
                           'Runner must preserve IDs, execute serial groups sequentially, and merge every shard coverage report.',
                           'No execution, measured runtime guarantee, or required-check verdict.']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inventory', required=True)
    parser.add_argument('--timings', required=True)
    parser.add_argument('--shards', required=True, type=int)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(plan(load_json(args.inventory), load_json(args.timings), args.shards), indent=2, allow_nan=False))
        return 0
    except (ValueError, OSError, RecursionError, OverflowError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
