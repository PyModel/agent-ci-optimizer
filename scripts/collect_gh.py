#!/usr/bin/env python3
"""Collect recent completed GitHub Actions runs, their jobs, and required-check
metadata through the authenticated `gh` CLI. Read-only: GET requests only.

Writes:
  OUT/raw/runs.json            list of workflow runs (newest first)
  OUT/raw/jobs/<run_id>.json   jobs (with steps) for each run
  OUT/raw/required_checks.json branch-protection + ruleset required checks (best effort)
  OUT/raw/collect_meta.json    what was asked, what was fetched, every error

Needs `gh auth status` OK with actions:read on the repo. Never prints tokens.

Usage: python3 scripts/collect_gh.py --repo OWNER/REPO --limit 150 --out .ci-audit
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import quote, urlencode

REPO_RX = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$")
PER_PAGE = 100
MAX_LIMIT = 1000

Gh = Callable[[str], object]


class GhError(RuntimeError):
    pass


def gh_api(path: str) -> object:
    """GET one REST path via gh. Raises GhError with gh's stderr (never the token)."""
    proc = subprocess.run(
        ["gh", "api", "-X", "GET", "-H", "Accept: application/vnd.github+json", path],
        capture_output=True, text=True, timeout=120, check=False,
    )
    if proc.returncode != 0:
        raise GhError(proc.stderr.strip()[:500] or f"gh exited {proc.returncode}")
    return json.loads(proc.stdout)


def fetch_runs(gh: Gh, repo: str, limit: int, branch: str | None, event: str | None) -> list[dict]:
    runs: list[dict] = []
    page = 1
    while len(runs) < limit:
        q = {"status": "completed", "per_page": PER_PAGE, "page": page}
        if branch:
            q["branch"] = branch
        if event:
            q["event"] = event
        data = gh(f"repos/{repo}/actions/runs?{urlencode(q)}")
        batch = data.get("workflow_runs", []) if isinstance(data, dict) else []
        runs.extend(batch)
        if len(batch) < PER_PAGE:
            break
        page += 1
    return runs[:limit]


def fetch_jobs(gh: Gh, repo: str, run_id: int) -> list[dict]:
    jobs: list[dict] = []
    page = 1
    while True:
        # filter=all keeps jobs from earlier attempts, so reruns stay visible.
        data = gh(f"repos/{repo}/actions/runs/{run_id}/jobs?filter=all&per_page={PER_PAGE}&page={page}")
        batch = data.get("jobs", []) if isinstance(data, dict) else []
        jobs.extend(batch)
        if len(batch) < PER_PAGE:
            return jobs
        page += 1


def fetch_required_checks(gh: Gh, repo: str, branch: str, errors: list[str]) -> dict:
    out: dict = {"branch": branch, "classic_protection": None, "rulesets": None}
    b = quote(branch, safe="")
    try:
        out["classic_protection"] = gh(f"repos/{repo}/branches/{b}/protection/required_status_checks")
    except GhError as exc:  # 404 = no classic protection; 403 = needs admin. Both are data.
        out["classic_protection"] = {"unavailable": str(exc)[:300]}
    try:
        rules = gh(f"repos/{repo}/rules/branches/{b}")
        out["rulesets"] = [r for r in rules if isinstance(r, dict) and r.get("type") in
                           {"required_status_checks", "merge_queue", "workflows"}] if isinstance(rules, list) else rules
    except GhError as exc:
        out["rulesets"] = {"unavailable": str(exc)[:300]}
        errors.append(f"rulesets: {exc}")
    return out


def collect(gh: Gh, repo: str, limit: int, out: Path, branch: str | None = None,
            event: str | None = None) -> dict:
    raw = out / "raw"
    (raw / "jobs").mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    repo_info = gh(f"repos/{repo}")
    default_branch = repo_info.get("default_branch", "main") if isinstance(repo_info, dict) else "main"
    runs = fetch_runs(gh, repo, limit, branch, event)
    (raw / "runs.json").write_text(json.dumps(runs, indent=1))
    jobs_ok = 0
    for run in runs:
        try:
            jobs = fetch_jobs(gh, repo, run["id"])
        except GhError as exc:
            errors.append(f"jobs {run['id']}: {exc}")
            continue
        (raw / "jobs" / f"{run['id']}.json").write_text(json.dumps(jobs, indent=1))
        jobs_ok += 1
    required = fetch_required_checks(gh, repo, branch or default_branch, errors)
    (raw / "required_checks.json").write_text(json.dumps(required, indent=1))
    meta = {
        "schema": "ci-collect/1",
        "source": "github-rest via gh api",
        "repo": repo,
        "default_branch": default_branch,
        "filters": {"status": "completed", "branch": branch, "event": event, "limit": limit},
        "collected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "runs": len(runs),
        "runs_with_jobs": jobs_ok,
        "errors": errors,
        "private": repo_info.get("private") if isinstance(repo_info, dict) else None,
    }
    (raw / "collect_meta.json").write_text(json.dumps(meta, indent=1))
    return meta


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Collect GitHub Actions run/job data (read-only).")
    ap.add_argument("--repo", required=True, help="OWNER/REPO")
    ap.add_argument("--limit", type=int, default=150, help=f"completed runs to fetch (1-{MAX_LIMIT})")
    ap.add_argument("--out", type=Path, default=Path(".ci-audit"))
    ap.add_argument("--branch", help="only runs on this branch")
    ap.add_argument("--event", help="only runs for this event (push, pull_request, merge_group, ...)")
    args = ap.parse_args(argv)
    if not REPO_RX.match(args.repo):
        ap.error("--repo must look like OWNER/REPO")
    if not 1 <= args.limit <= MAX_LIMIT:
        ap.error(f"--limit must be 1..{MAX_LIMIT}")
    try:
        meta = collect(gh_api, args.repo, args.limit, args.out, args.branch, args.event)
    except GhError as exc:
        print(f"error: gh api failed: {exc}\nCheck `gh auth status` and repo access.", file=sys.stderr)
        return 1
    except FileNotFoundError:
        print("error: `gh` not found on PATH", file=sys.stderr)
        return 1
    print(json.dumps({k: meta[k] for k in ("repo", "runs", "runs_with_jobs", "errors")}, indent=1))
    if meta["runs_with_jobs"] < meta["runs"]:
        print("warning: some runs have no job data; metrics from this set are partial", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
