#!/usr/bin/env python3
"""Collect exact-revision GitHub check evidence using authenticated, GET-only gh.

This deliberately emits ci-github-check-evidence/1, NOT validation eligibility.
Actions cannot prove historical policy/build-input identity, artifact provenance,
actual push receipt or rollout health. Those capabilities remain unknown. No flag
or input file can assert verification. A reviewed expectation file contains only
repository ID, workflow ID, event, max_check_age_s and expected {name, app_id}
job identities.

Usage: collect_gh_evidence.py --repo OWNER/REPO --sha SHA --expect FILE --out FILE
Exit 0: all expected checks observed successful. Exit 1: blocked/unknown. Exit 2:
invalid input. This is a time-bounded observation, not authorization to release.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

try:
    from .collect_gh import GhClient, GhError, write_json
except ImportError:
    from collect_gh import GhClient, GhError, write_json

MAX_PAGES = 10
MAX_CALLS = 128
MAX_INPUT_BYTES = 65536


class EvidenceError(ValueError):
    pass


def positive(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except ValueError:
        return None


def fingerprint(value):
    return 'sha256:' + hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                               allow_nan=False).encode()).hexdigest()


def validate(repo, sha, expect):
    if not isinstance(repo, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo):
        raise ValueError('repository must be OWNER/REPO')
    if not isinstance(sha, str) or not re.fullmatch(r'[a-f0-9]{40}|[a-f0-9]{64}', sha):
        raise ValueError('exact immutable SHA required')
    if not isinstance(expect, dict) or expect.get('schema') != 'ci-github-expectations/1':
        raise ValueError('expectation schema must be ci-github-expectations/1')
    if set(expect) != {'schema', 'repository_id', 'workflow_id', 'event', 'jobs', 'max_check_age_s'}:
        raise ValueError('unsupported or missing expectation fields')
    if not positive(expect['repository_id']) or not positive(expect['workflow_id']):
        raise ValueError('positive repository/workflow IDs required')
    if expect['event'] not in {'push', 'pull_request', 'merge_group', 'workflow_dispatch'}:
        raise ValueError('unsupported event')
    if not positive(expect['max_check_age_s']):
        raise ValueError('max_check_age_s must be a positive integer')
    jobs = expect['jobs']
    if not isinstance(jobs, list) or not 1 <= len(jobs) <= 40:
        raise ValueError('expect 1..40 explicit jobs')
    names = set()
    for job in jobs:
        if (not isinstance(job, dict) or set(job) != {'name', 'app_id'}
                or not isinstance(job['name'], str) or not job['name'].strip()
                or len(job['name']) > 256 or not positive(job['app_id']) or job['name'] in names):
            raise ValueError('jobs require unique names and positive app IDs')
        names.add(job['name'])


def collect(repo, sha, expect, api=None, *, clock=None, monotonic=None, budget_s=120):
    """Live source adapter; injected transports/clocks exist for deterministic tests."""
    validate(repo, sha, expect)
    if isinstance(budget_s, bool) or not isinstance(budget_s, (int, float)) or not 1 <= budget_s <= 600:
        raise ValueError('budget must be 1..600 seconds')
    api = api or GhClient(timeout=10, retries=0)
    clock = clock or (lambda: datetime.now(timezone.utc))
    monotonic = monotonic or time.monotonic
    began, start = clock(), monotonic()
    if not isinstance(began, datetime) or began.tzinfo is None:
        raise ValueError('timezone-aware clock required')
    report = {'schema': 'ci-github-check-evidence/1', 'repository': repo, 'revision': sha,
              'expectations_digest': fingerprint(expect), 'state': 'unknown', 'gaps': [], 'checks': [],
              'started_at': began.isoformat(), 'observed_at': None, 'expires_at': None,
              'complete': False, 'sources': [],
              'capabilities': {'check_identity': 'unknown', 'policy_binding': 'unknown',
                               'build_inputs': 'unknown', 'artifact_provenance': 'unknown',
                               'push_receipt': 'unknown', 'release_eligibility': 'unknown'}}
    calls = 0

    def get(path):
        nonlocal calls
        if calls >= MAX_CALLS or monotonic() - start >= budget_s:
            raise EvidenceError('collection budget exhausted')
        calls += 1
        payload = api(path)
        if monotonic() - start > budget_s:
            raise EvidenceError('collection budget exceeded during request')
        report['sources'].append({'url': 'https://api.github.com/' + path,
                                  'sha256': fingerprint(payload)})
        return payload

    def pages(path, key):
        rows, total, seen = [], None, set()
        for page in range(1, MAX_PAGES + 1):
            payload = get(path + ('&' if '?' in path else '?') + f'per_page=100&page={page}')
            if not isinstance(payload, dict) or not isinstance(payload.get(key), list):
                raise EvidenceError('malformed paginated response')
            count = payload.get('total_count')
            if not isinstance(count, int) or isinstance(count, bool) or count < 0 or count > 1000:
                raise EvidenceError('unknown or capped enumeration count')
            if total is not None and count != total:
                raise EvidenceError('enumeration changed during collection')
            total = count
            chunk = payload[key]
            if len(chunk) > 100:
                raise EvidenceError('oversized page')
            for row in chunk:
                if not isinstance(row, dict) or not positive(row.get('id')) or row['id'] in seen:
                    raise EvidenceError('invalid or duplicate record identity')
                seen.add(row['id'])
                rows.append(row)
            if len(rows) == total:
                return rows
            if len(rows) > total or len(chunk) != 100:
                raise EvidenceError('missing or inconsistent page')
        raise EvidenceError('pagination limit reached')

    def run_identity(run):
        if (not isinstance(run, dict) or run.get('head_sha') != sha
                or run.get('workflow_id') != expect['workflow_id'] or run.get('event') != expect['event']
                or not positive(run.get('id')) or not positive(run.get('run_attempt'))
                or not positive(run.get('check_suite_id'))
                or not isinstance(run.get('repository'), dict)
                or run['repository'].get('id') != expect['repository_id']
                or run['repository'].get('full_name', '').lower() != repo.lower()):
            raise EvidenceError('run source identity mismatch')
        created, updated = timestamp(run.get('created_at')), timestamp(run.get('updated_at'))
        if created is None or updated is None or not created <= updated <= clock():
            raise EvidenceError('invalid run chronology')

    try:
        metadata = get(f'repos/{repo}')
        if (not isinstance(metadata, dict) or metadata.get('id') != expect['repository_id']
                or metadata.get('full_name', '').lower() != repo.lower()):
            raise EvidenceError('repository identity mismatch')
        path = f"repos/{repo}/actions/workflows/{expect['workflow_id']}/runs?" + urlencode(
            {'head_sha': sha, 'event': expect['event']})
        runs = pages(path, 'workflow_runs')
        if not runs:
            raise EvidenceError('no runs for expected exact revision')
        for run in runs:
            run_identity(run)
        newest = max(timestamp(r['created_at']) for r in runs)
        candidates = [r for r in runs if timestamp(r['created_at']) == newest]
        if len(candidates) != 1:
            raise EvidenceError('ambiguous latest run')
        selected = candidates[0]
        # A later update/rerun of an older execution makes latest-run precedence ambiguous.
        if any(timestamp(r['updated_at']) > timestamp(selected['updated_at']) for r in runs):
            raise EvidenceError('older run updated after selected run')
        run_path = f"repos/{repo}/actions/runs/{selected['id']}"
        current = get(run_path)
        run_identity(current)
        def run_version(run):
            return {key: run.get(key) for key in ('id', 'head_sha', 'workflow_id', 'event', 'run_attempt',
                    'check_suite_id', 'status', 'conclusion', 'created_at', 'updated_at')}

        if run_version(current) != run_version(selected):
            raise EvidenceError('run changed between list and detail')
        attempt = current['run_attempt']
        jobs = pages(f'{run_path}/attempts/{attempt}/jobs', 'jobs')
        expected = {j['name']: j for j in expect['jobs']}
        found = {}
        for job in jobs:
            if (job.get('run_id') != current['id'] or job.get('run_attempt') != attempt
                    or job.get('head_sha') != sha or not isinstance(job.get('name'), str)):
                raise EvidenceError('job source identity mismatch')
            name = job['name']
            if name not in expected:
                continue
            if name in found:
                raise EvidenceError('duplicate expected job name')
            found[name] = job
            url = job.get('check_run_url', '')
            match = re.fullmatch(re.escape(f'https://api.github.com/repos/{repo}/check-runs/') + r'([1-9][0-9]*)', url)
            if not match:
                raise EvidenceError('invalid check URL')
            check_path = f"repos/{repo}/check-runs/{match[1]}"
            check = get(check_path)
            if (not isinstance(check, dict) or check.get('id') != int(match[1])
                    or check.get('name') != name or check.get('head_sha') != sha
                    or not isinstance(check.get('app'), dict) or check['app'].get('id') != expected[name]['app_id']
                    or not isinstance(check.get('check_suite'), dict)
                    or check['check_suite'].get('id') != current['check_suite_id']
                    or check.get('status') != job.get('status') or check.get('conclusion') != job.get('conclusion')):
                raise EvidenceError('check source identity or result mismatch')
            end = timestamp(check.get('completed_at'))
            if check.get('status') == 'completed' and (end is None or not newest <= end <= clock()):
                raise EvidenceError('invalid check completion chronology')
            if end is not None and (clock() - end).total_seconds() > expect['max_check_age_s']:
                raise EvidenceError('check evidence is stale')
            report['checks'].append({'identity': f"github:{expect['repository_id']}/workflow:{expect['workflow_id']}"
                                    f"/event:{expect['event']}/app:{expected[name]['app_id']}/job:{name}",
                                    'name': name, 'revision': sha, 'run_id': current['id'], 'attempt': attempt,
                                    'conclusion': check.get('conclusion'), 'status': check.get('status'),
                                    'completed_at': check.get('completed_at'), 'source': url})
            if get(check_path) != check:
                raise EvidenceError('check changed during collection')
        if set(found) != set(expected):
            raise EvidenceError('expected job missing from current attempt; partial rerun requires explicit reconciliation')
        if get(run_path) != current or pages(path, 'workflow_runs') != runs:
            raise EvidenceError('new run or attempt appeared during collection')
        ended = clock()
        if ended < began or (ended - began).total_seconds() > budget_s:
            raise EvidenceError('clock changed or snapshot too old')
        expiry = min([began + timedelta(seconds=budget_s)] +
                     [timestamp(c['completed_at']) + timedelta(seconds=expect['max_check_age_s'])
                      for c in report['checks'] if timestamp(c['completed_at']) is not None])
        if ended >= expiry:
            raise EvidenceError('snapshot or check evidence expired during collection')
        report.update(complete=True, observed_at=ended.isoformat(), expires_at=expiry.isoformat())
        report['capabilities']['check_identity'] = 'observed_from_github_api'
        report['state'] = ('pass' if current.get('status') == 'completed' and current.get('conclusion') == 'success'
                           and all(c['status'] == 'completed' and c['conclusion'] == 'success'
                                   for c in report['checks']) else 'blocked')
        if report['state'] == 'blocked':
            report['gaps'].append('latest run or expected checks are not explicitly successful')
    except (EvidenceError, GhError, OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
        # No raw provider response/error, token, or URL-query content is copied into errors.
        report['gaps'].append(str(exc) if isinstance(exc, EvidenceError) else
                              'collection incomplete, inconsistent, or source verification failed')
    report['requests'] = calls
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', required=True)
    parser.add_argument('--sha', required=True)
    parser.add_argument('--expect', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        info = args.expect.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_INPUT_BYTES:
            raise ValueError('expectation must be a bounded regular file')
        with args.expect.open('rb') as stream:
            raw = stream.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES:
            raise ValueError('expectation file too large')
        expect = json.loads(raw)
        report = collect(args.repo, args.sha, expect)
        write_json(args.out, report)
    except (OSError, ValueError, UnicodeError):
        print('invalid input or output unavailable', file=sys.stderr)
        return 2
    return 0 if report['state'] == 'pass' else 1


if __name__ == '__main__':
    sys.exit(main())
