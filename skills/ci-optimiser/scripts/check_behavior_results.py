#!/usr/bin/env python3
"""Check an offline behavior ledger's completeness and local evidence integrity.

This does not execute or grade a model, authenticate provenance, or prove behavior.
Exit 0: valid selected trials all pass; 1: valid fail/blocked; 2: invalid evidence.
"""
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path, PurePosixPath

from plan_shards import label, load_json, number

MAX_ARTIFACT_BYTES = 1024 * 1024
MAX_EVIDENCE_BYTES = 32 * MAX_ARTIFACT_BYTES
HEX = re.compile(r"[0-9a-f]{64}\Z")
REVISION = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(value):
    """Canonical JSON SHA-256; artifact hashes instead cover original bytes."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def fields(value, expected, context):
    require(isinstance(value, dict) and set(value) == set(expected.split()),
            context + ': unexpected or missing fields')


def positive_integer(value, maximum):
    return type(value) is int and 1 <= value <= maximum


def hash_value(value):
    return isinstance(value, str) and HEX.fullmatch(value) is not None


def case_inventory(suite, rubric):
    fields(suite, 'schema cases', 'suite')
    fields(rubric, 'schema cases', 'rubric')
    require(suite['schema'] == 'ci-behavior-cases/1', 'unsupported suite schema')
    require(rubric['schema'] == 'ci-behavior-rubric/1', 'unsupported rubric schema')
    inventories = []
    for document, grading in ((suite, False), (rubric, True)):
        require(isinstance(document['cases'], list) and 1 <= len(document['cases']) <= 64,
                'cases must contain 1..64 records')
        inventory = {}
        for case in document['cases']:
            fields(case, 'id criteria critical_failures' if grading else 'id prompt input', 'case')
            require(label(case['id']) and case['id'] not in inventory, 'invalid or duplicate case ID')
            if grading:
                for key in ('criteria', 'critical_failures'):
                    require(isinstance(case[key], list) and 1 <= len(case[key]) <= 32
                            and all(label(v) for v in case[key]), 'invalid grading criteria')
            else:
                require(label(case['prompt']) and isinstance(case['input'], dict), 'invalid public case')
            inventory[case['id']] = case
        inventories.append(inventory)
    require(set(inventories[0]) == set(inventories[1]), 'suite/rubric case inventory differs')
    return inventories[0]


def validate(ledger, suite, rubric, evidence_root):
    cases = case_inventory(suite, rubric)
    fields(ledger, 'schema suite_sha256 rubric_sha256 baseline_arm candidate_arm arms '
           'case_ids repetitions trials', 'ledger')
    require(ledger['schema'] == 'ci-behavior-results/1', 'unsupported ledger schema')
    require(ledger['suite_sha256'] == digest(suite) and ledger['rubric_sha256'] == digest(rubric),
            'suite or rubric digest mismatch')
    baseline, candidate = ledger['baseline_arm'], ledger['candidate_arm']
    require(label(baseline) and label(candidate) and baseline != candidate, 'two distinct arms required')
    require(isinstance(ledger['arms'], dict) and set(ledger['arms']) == {baseline, candidate},
            'arm inventory mismatch')
    for arm in ledger['arms'].values():
        fields(arm, 'skill_revision skill_snapshot_sha256 model runtime settings tools tool_policy', 'arm')
        require(isinstance(arm['skill_revision'], str) and REVISION.fullmatch(arm['skill_revision'])
                and hash_value(arm['skill_snapshot_sha256']), 'exact skill revision and snapshot required')
        require(all(label(arm[key]) for key in ('model', 'runtime', 'tool_policy'))
                and isinstance(arm['settings'], dict), 'model, runtime, settings and permissions required')
        require(isinstance(arm['tools'], list) and len(arm['tools']) <= 128, 'bounded tool inventory required')
        names = set()
        for tool in arm['tools']:
            fields(tool, 'name version', 'tool')
            require(label(tool['name']) and label(tool['version']) and tool['name'] not in names,
                    'unique exact tool identities required')
            names.add(tool['name'])
    for key in ('model', 'runtime', 'settings', 'tools', 'tool_policy'):
        require(ledger['arms'][baseline][key] == ledger['arms'][candidate][key],
                'unmatched comparison setting: ' + key)
    selected = ledger['case_ids']
    require(isinstance(selected, list) and selected and all(label(v) for v in selected)
            and len(set(selected)) == len(selected) and set(selected) <= set(cases), 'invalid selected cases')
    repetitions = ledger['repetitions']
    require(positive_integer(repetitions, 16), 'repetitions must be 1..16')
    expected = {(case, arm, rep) for case in selected for arm in (baseline, candidate)
                for rep in range(1, repetitions + 1)}
    trials = ledger['trials']
    require(isinstance(trials, list) and len(trials) == len(expected) <= 512,
            'all planned trials, including blocked trials, are required (maximum 512)')
    root = Path(evidence_root).resolve(strict=True)
    require(root.is_dir(), 'evidence root must be a directory')
    paths, sessions, sequences, records = set(), set(), set(), {}
    total_bytes = 0

    def read_artifact(record):
        nonlocal total_bytes
        fields(record, 'path sha256', 'artifact')
        name = record['path']
        require(label(name) and '\\' not in name, 'artifact path must be relative POSIX')
        relative = PurePosixPath(name)
        require(not relative.is_absolute() and '..' not in relative.parts and str(relative) == name,
                'artifact path must be canonical and inside evidence root')
        path = (root / name).resolve(strict=True)
        require(path.is_relative_to(root) and path.is_file() and path not in paths,
                'artifact must be a unique regular file inside evidence root')
        paths.add(path)
        with path.open('rb') as stream:
            raw = stream.read(MAX_ARTIFACT_BYTES + 1)
        total_bytes += len(raw)
        require(len(raw) <= MAX_ARTIFACT_BYTES and total_bytes <= MAX_EVIDENCE_BYTES,
                'raw evidence exceeds size limit')
        require(hash_value(record['sha256']) and hashlib.sha256(raw).hexdigest() == record['sha256'],
                'raw artifact digest mismatch')
        return raw.decode('utf-8').splitlines()

    for trial in trials:
        fields(trial, 'case_id arm repetition sequence session_id input_sha256 status grade '
               'critical_failure grader_id reason duration_s output tool_calls evidence', 'trial')
        require(label(trial['case_id']) and label(trial['arm'])
                and positive_integer(trial['repetition'], repetitions), 'invalid trial identity')
        key = (trial['case_id'], trial['arm'], trial['repetition'])
        require(key in expected and key not in records, 'unknown or duplicate planned trial')
        require(trial['input_sha256'] == digest(cases[trial['case_id']]), 'public input digest mismatch')
        require(label(trial['session_id']) and trial['session_id'] not in sessions, 'fresh session ID required')
        require(positive_integer(trial['sequence'], len(expected)) and trial['sequence'] not in sequences,
                'execution sequence must be a complete permutation')
        sessions.add(trial['session_id'])
        sequences.add(trial['sequence'])
        require(label(trial['reason']), 'grade or blocker reason required')
        require(trial['status'] in ('graded', 'blocked'), 'status must be graded or blocked')
        require(trial['duration_s'] is None or number(trial['duration_s']), 'invalid duration')
        if trial['status'] == 'graded':
            require(trial['grade'] in ('pass', 'fail') and type(trial['critical_failure']) is bool,
                    'graded outcome and critical-failure flag required')
            require(not trial['critical_failure'] or trial['grade'] == 'fail', 'critical failure cannot pass')
            require(label(trial['grader_id']) and trial['grader_id'] != trial['session_id'],
                    'independent grader identity required')
            require(number(trial['duration_s']) and trial['output'] is not None
                    and trial['tool_calls'] is not None, 'graded trial needs duration and raw evidence')
        else:
            require(trial['grade'] is None and trial['critical_failure'] is None
                    and (trial['grader_id'] is None or label(trial['grader_id'])),
                    'blocked is not a grade or a critical-failure assessment')
        artifacts = {kind: read_artifact(trial[kind]) for kind in ('output', 'tool_calls')
                     if trial[kind] is not None}
        evidence = trial['evidence']
        require(isinstance(evidence, list) and len(evidence) <= 32
                and (trial['status'] == 'blocked' or evidence), 'graded trial needs quoted evidence')
        for quote in evidence:
            fields(quote, 'artifact start_line end_line quote', 'evidence quote')
            require(isinstance(quote['artifact'], str) and quote['artifact'] in artifacts,
                    'quote refers to an unavailable artifact')
            lines = artifacts[quote['artifact']]
            start, end = quote['start_line'], quote['end_line']
            require(positive_integer(start, len(lines)) and positive_integer(end, len(lines))
                    and start <= end and label(quote['quote'])
                    and quote['quote'] in '\n'.join(lines[start - 1:end]), 'quote does not match raw evidence')
        records[key] = trial
    require(set(records) == expected, 'incomplete planned trial inventory')
    pairs = dict(both_pass=0, regression=0, improvement=0, both_fail=0, inconclusive=0)
    first_baseline = 0
    for case in selected:
        for rep in range(1, repetitions + 1):
            left, right = records[(case, baseline, rep)], records[(case, candidate, rep)]
            first_baseline += left['sequence'] < right['sequence']
            pair = ('inconclusive' if 'blocked' in (left['status'], right['status']) else
                    {('pass', 'pass'): 'both_pass', ('pass', 'fail'): 'regression',
                     ('fail', 'pass'): 'improvement', ('fail', 'fail'): 'both_fail'}[(left['grade'], right['grade'])])
            pairs[pair] += 1
    require(abs(2 * first_baseline - len(selected) * repetitions) <= 1, 'arm order is not balanced')
    outcomes = {arm: {status: sum(t['arm'] == arm and (t['grade'] if t['status'] == 'graded' else 'blocked')
                                      == status for t in trials) for status in ('pass', 'fail', 'blocked')}
                for arm in (baseline, candidate)}
    return {'schema': 'ci-behavior-summary/1', 'selected_cases': len(selected), 'suite_cases': len(cases),
            'complete_suite': set(selected) == set(cases), 'planned_trials': len(expected),
            'outcomes': outcomes, 'paired': pairs,
            'critical_failures': sum(t['critical_failure'] is True for t in trials),
            'all_selected_passed': all(t['grade'] == 'pass' for t in trials),
            'scope': 'Validates declared human grades, inventory and local byte integrity only; '
                     'does not execute or semantically grade agents, authenticate identities, '
                     'prove independent grading, or authorize CI adoption.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('ledger')
    parser.add_argument('--cases', required=True)
    parser.add_argument('--rubric', required=True)
    parser.add_argument('--evidence-root', required=True)
    args = parser.parse_args(argv)
    try:
        result = validate(load_json(args.ledger), load_json(args.cases), load_json(args.rubric),
                          args.evidence_root)
    except (OSError, ValueError, TypeError, OverflowError, RecursionError) as error:
        print('Invalid behavior evidence: ' + str(error), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0 if result['all_selected_passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
