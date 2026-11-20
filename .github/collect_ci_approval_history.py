#!/usr/bin/env python3
"""Collect bounded approval-only attempt receipts with the CI job's read token.

Repository plumbing, not an installed skill command. Run inside the existing
10-second timing-read budget. Uses authenticated GitHub.com GETs, no cache/retries,
no approvals, reruns or provider mutations. The offline budget verifier decides
whether these unchanged receipts prove that no earlier attempt executed a job.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/ci-optimiser/scripts"))
from check_job_budgets import MAX_APPROVAL_ATTEMPT, MAX_INPUT_BYTES, SHA, approval_context, positive_int
from collect_gh import GhClient, GhError


def context_from_environment(environment):
    if environment.get("GITHUB_EVENT_NAME") != "pull_request" or not environment.get("GH_TOKEN"):
        raise ValueError("approval collection requires a PR and its scoped read token")
    context = {
        "repository": environment.get("GITHUB_REPOSITORY"),
        "repository_id": int(environment.get("GITHUB_REPOSITORY_ID", "")),
        "workflow_ref": environment.get("GITHUB_WORKFLOW_REF"),
        "pull_request": int(environment.get("EXPECTED_PR_NUMBER", "")),
        "run_id": int(environment.get("GITHUB_RUN_ID", "")),
        "attempt": int(environment.get("GITHUB_RUN_ATTEMPT", "")),
        "head_sha": environment.get("EXPECTED_API_HEAD_SHA"),
    }
    approval_context(context["repository"], context["repository_id"], context["workflow_ref"], context["pull_request"])
    if (not positive_int(context["run_id"]) or not 2 <= context["attempt"] <= MAX_APPROVAL_ATTEMPT
            or not isinstance(context["head_sha"], str) or SHA.fullmatch(context["head_sha"]) is None):
        raise ValueError("approval collection needs exact run/head and attempt 2..10")
    return context


def collect(get, context):
    base = f"repos/{context['repository']}/actions/runs/{context['run_id']}/attempts/"
    attempt = context["attempt"]
    history = {"schema": "ci-pr-approval-history/1", "current_run": get(base + str(attempt)),
               "previous_attempts": []}
    for number in range(1, attempt):
        history["previous_attempts"].append({
            "run": get(base + str(number)),
            "jobs": get(base + f"{number}/jobs?per_page=100"),
        })
    # This repository has four expected jobs. A nonzero prior total or an
    # incomplete current page fails verification; it is never treated as empty.
    jobs = get(base + f"{attempt}/jobs?per_page=100")
    return history, jobs


def encoded(value):
    payload = (json.dumps(value, indent=2, allow_nan=False) + "\n").encode("utf-8")
    if len(payload) > MAX_INPUT_BYTES:
        raise ValueError("approval receipt exceeds the verifier input bound")
    return payload


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--jobs-out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        context = context_from_environment(os.environ)
        get = GhClient(timeout=8, retries=0, max_wait=0, cache_dir=None)
        history, jobs = collect(get, context)
        history_bytes, job_bytes = encoded(history), encoded(jobs)
        # Retain provider outcomes unchanged, even when later validation rejects
        # a failed/executed prior attempt. Never overwrite an earlier receipt.
        with args.out.open("xb") as stream:
            stream.write(history_bytes)
        with args.jobs_out.open("xb") as stream:
            stream.write(job_bytes)
    except (ValueError, OSError, GhError, OverflowError, RecursionError):
        print("approval receipt collection failed; no retry or attempt normalization", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
