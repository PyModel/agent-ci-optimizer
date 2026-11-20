"""Internal typed cache-state validation; supplied evidence is never authenticated.

Used by cache economics and benchmark preparation. This module has no CLI and
never inspects disks, executes a command, follows a reference or reads secrets.
"""
from __future__ import annotations

import re

LAYERS = ('runner_snapshot', 'toolchain', 'dependencies', 'build_outputs',
          'test_results', 'action_archive')
STATES = {'cold', 'warm', 'disabled', 'unknown'}
STATE_FIELDS = {'state', 'identity', 'evidence'}
CONTEXT_FIELDS = {'repository', 'revision', 'workflow', 'run_id', 'job_id', 'attempt',
                  'config_digest', 'action_digest', 'toolchain_digest',
                  'runner_image_digest', 'workload_digest', 'states'}
MATCH_FIELDS = ('repository', 'revision', 'workflow', 'action_digest',
                'toolchain_digest', 'runner_image_digest', 'workload_digest', 'attempt')
DIGEST_FIELDS = {'config_digest', 'action_digest', 'toolchain_digest',
                 'runner_image_digest', 'workload_digest'}
MAX_REFERENCE = 1024


def reference(value):
    """Return a bounded opaque evidence reference, or explicit unknown."""
    if value is None:
        return None
    if (not isinstance(value, str) or not 1 <= len(value) <= MAX_REFERENCE
            or value != value.strip() or not value.isprintable()):
        raise ValueError('evidence references must be bounded nonblank printable strings or null')
    return value


def unknown_states():
    return {layer: {'state': 'unknown', 'identity': None, 'evidence': None} for layer in LAYERS}


def validate_states(value):
    """Validate the exact layer map; known labels without evidence stay unresolved."""
    if not isinstance(value, dict) or set(value) != set(LAYERS):
        raise ValueError('cache states require exactly: ' + ', '.join(LAYERS))
    result = {}
    for layer in LAYERS:
        row = value[layer]
        if not isinstance(row, dict) or set(row) != STATE_FIELDS:
            raise ValueError('each cache state requires exactly state, identity and evidence')
        state = row['state']
        if not isinstance(state, str) or state not in STATES:
            raise ValueError('cache state must be cold, warm, disabled or unknown')
        result[layer] = {'state': state, 'identity': reference(row['identity']),
                         'evidence': reference(row['evidence'])}
    return result


def unresolved_states(states):
    """List layers whose state/equivalence cannot be established from supplied data."""
    return [layer for layer, row in validate_states(states).items()
            if row['state'] == 'unknown' or row['identity'] is None or row['evidence'] is None]


def validate_context(value):
    if not isinstance(value, dict) or set(value) - CONTEXT_FIELDS:
        raise ValueError('invalid cache comparison context fields')
    result = {}
    for key in sorted(CONTEXT_FIELDS - {'states', 'attempt'}):
        item = reference(value.get(key))
        if key in DIGEST_FIELDS and item is not None and not re.fullmatch(r'[0-9a-f]{64}', item):
            raise ValueError(key + ' must be a SHA-256 fingerprint or null')
        if key == 'revision' and item is not None and not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', item):
            raise ValueError('revision must be a full immutable Git revision or null')
        result[key] = item
    attempt = value.get('attempt')
    if attempt is not None and (type(attempt) is not int or not 1 <= attempt <= 1000):
        raise ValueError('attempt must be an integer from 1 to 1000 or null')
    result['attempt'] = attempt
    result['states'] = validate_states(value['states']) if value.get('states') is not None else unknown_states()
    return result


def compare_contexts(value):
    """Resolve structural comparability, without accepting a caller's pass boolean.

The review reference asserts that any configuration difference changes only the
named layer. Fingerprints and references remain caller-supplied evidence.
"""
    fields = {'layer_under_test', 'baseline', 'candidate', 'review_reference'}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError('comparison requires layer_under_test, baseline, candidate and review_reference')
    layer = value['layer_under_test']
    if not isinstance(layer, str) or layer not in LAYERS:
        raise ValueError('layer_under_test must name a supported cache layer')
    baseline, candidate = (validate_context(value[name]) for name in ('baseline', 'candidate'))
    review = reference(value['review_reference'])
    unknown, incompatible = [], []
    if review is None:
        unknown.append('review_reference')
    for name, context in (('baseline', baseline), ('candidate', candidate)):
        unknown.extend(name + '.' + key for key in sorted(CONTEXT_FIELDS - {'states'}) if context[key] is None)
        unknown.extend(name + '.states.' + missing for missing in unresolved_states(context['states']))
    for key in MATCH_FIELDS:
        if baseline[key] is not None and candidate[key] is not None and baseline[key] != candidate[key]:
            incompatible.append(key)
    for name in LAYERS:
        if name == layer:
            continue
        a, b = baseline['states'][name], candidate['states'][name]
        if a['state'] != 'unknown' and b['state'] != 'unknown' and a['state'] != b['state']:
            incompatible.append('states.' + name + '.state')
        if a['identity'] is not None and b['identity'] is not None and a['identity'] != b['identity']:
            incompatible.append('states.' + name + '.identity')
    observation = ('repository', 'run_id', 'job_id', 'attempt')
    if all(baseline[key] is not None and candidate[key] is not None for key in observation):
        if all(baseline[key] == candidate[key] for key in observation):
            incompatible.append('same_observation')
    # The selected layer must actually differ; identical records are not a control.
    before, after = baseline['states'][layer], candidate['states'][layer]
    if (before['state'] != 'unknown' and after['state'] != 'unknown'
            and before['identity'] is not None and after['identity'] is not None
            and (before['state'], before['identity']) == (after['state'], after['identity'])):
        incompatible.append('layer_under_test_unchanged')
    status = 'incompatible' if incompatible else ('unknown' if unknown else 'comparable_supplied_evidence')
    return {'layer_under_test': layer, 'baseline': baseline, 'candidate': candidate,
            'review_reference': review, 'status': status, 'unknown_fields': sorted(unknown),
            'incompatible_fields': sorted(incompatible), 'authenticated': False}
