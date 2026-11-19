#!/usr/bin/env python3
"""Compare same-revision selected and FULL test reports; never grants gate eligibility."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

MAX_BYTES = 16 * 1024 * 1024
MAX_TESTS = 100_000
REVISION = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
OUTCOMES = {"passed", "failed", "skipped", "cancelled", "error"}
SCHEMA = "ci-selector-shadow/1"


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def ids(values):
    if not isinstance(values, list) or not 0 < len(values) <= MAX_TESTS:
        return None
    if any(not isinstance(x, str) or not x.strip() or len(x) > 1024 for x in values):
        return None
    unique = set(values)
    return unique if len(unique) == len(values) else None


def evaluate(data):
    result = {"status": "unknown", "recommendation": "FULL", "gaps": [],
              "missed_failures": [], "disagreements": [], "selection_fraction": "unknown"}
    gaps = result["gaps"]
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        gaps.append("unsupported evidence schema")
        return result
    inventory = ids(data.get("inventory"))
    selection = ids(data.get("selection"))
    if inventory is None or selection is None or not selection <= inventory:
        gaps.append("selection and inventory must be unique, nonempty, and selection a subset")
        return result
    if data.get("inventory_complete") is not True:
        gaps.append("complete expected test inventory not established")
    arms = {}
    identities = []
    for name, expected in (("full", inventory), ("selected", selection)):
        arm = data.get(name)
        if not isinstance(arm, dict):
            gaps.append(f"{name}: absent report")
            continue
        revision, context = arm.get("revision"), arm.get("context_digest")
        if (not isinstance(revision, str) or not REVISION.fullmatch(revision)
                or not isinstance(context, str) or not DIGEST.fullmatch(context)):
            gaps.append(f"{name}: invalid revision or comparison-context digest")
        identities.append((revision, context))
        if arm.get("complete") is not True or arm.get("state") != "completed":
            gaps.append(f"{name}: execution/report not complete")
        outcomes = arm.get("outcomes")
        if not isinstance(outcomes, dict) or outcomes.keys() != expected:
            gaps.append(f"{name}: executed test IDs do not match expected IDs")
            continue
        if any(not isinstance(x, str) or x not in OUTCOMES for x in outcomes.values()):
            gaps.append(f"{name}: invalid test outcome")
            continue
        if any(x not in {"passed", "failed"} for x in outcomes.values()):
            gaps.append(f"{name}: skipped, cancelled or errored tests")
        arms[name] = outcomes
    if len(identities) == 2 and identities[0] != identities[1]:
        gaps.append("revision or comparison context differs between arms")
    if gaps:
        return result
    full, selected = arms["full"], arms["selected"]
    missed = sorted(test for test in inventory - selection if full[test] == "failed")
    disagreements = sorted(test for test in selection if full[test] != selected[test])
    status = "missed_failure" if missed else "outcome_disagreement" if disagreements else (
        "matched_failure" if "failed" in full.values() else "observed_clean")
    result.update(status=status, missed_failures=missed, disagreements=disagreements,
                  selection_fraction=len(selection) / len(inventory))
    # Even a clean pair cannot authorize selection. Compare cohorts and enforce
    # the repository's separately reviewed selection and release policies.
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help=f"normalized {SCHEMA} JSON, max {MAX_BYTES} bytes")
    args = parser.parse_args(argv)
    try:
        if not args.input.is_file():
            raise ValueError("input must be a regular file")
        with args.input.open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("input exceeds size budget")
        result = evaluate(json.loads(raw, object_pairs_hook=unique_keys))
    except (ValueError, OSError, RecursionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0 if result["status"] == "observed_clean" else 3 if result["status"] == "unknown" else 1


if __name__ == "__main__":
    raise SystemExit(main())
