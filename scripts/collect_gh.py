#!/usr/bin/env python3
"""Collect read-only GitHub Actions evidence into immutable local snapshots.

OUT/current.json atomically selects OUT/snapshots/ID/raw/, containing runs,
jobs, attempts, required checks, collection metadata and a manifest. A failed
run enumeration never replaces the pointer. Missing per-run payloads are
recorded, never inherited. All API requests are GETs through authenticated gh;
conditional reuse requires a server 304. Diagnostics never include raw stderr.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable
from urllib.parse import quote, urlencode

REPO_RX = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$")
API_VERSION = "2026-03-10"  # Official GitHub API versions; verified 2026-11-17.
PER_PAGE = 100
MAX_LIMIT = 1000
MAX_PAGES = 100
MAX_ATTEMPTS_PER_RUN = 100
MAX_RESPONSE_BYTES = 20_000_000
DEFAULT_TIMEOUT = 30
Gh = Callable[[str], object]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class GhError(RuntimeError):
    """Safe structured diagnostic; raw CLI/server text is never retained."""

    def __init__(self, message: str = "", *, kind: str | None = None, status_code: int | None = None,
                 retry_after: float | None = None):
        if status_code is None:
            match = re.search(r"\bHTTP[ /]*(\d{3})\b", message)
            status_code = int(match.group(1)) if match else None
        self.kind = kind or ("http_error" if status_code else "api_error")
        self.status_code, self.retry_after = status_code, retry_after
        safe = self.kind.replace("_", " ")
        super().__init__(safe + (f" (HTTP {status_code})" if status_code is not None else ""))

    def record(self, stage: str, **identity) -> dict:
        result = {"stage": stage, "kind": self.kind, "status_code": self.status_code, **identity}
        if self.retry_after is not None:
            result["retry_after_seconds"] = self.retry_after
        return result


def json_bytes(data: object) -> bytes:
    return (json.dumps(data, indent=1, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")


def write_json(path: Path, data: object) -> None:
    """Atomic file replacement; callers own the parent directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(json_bytes(data))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def parse_http(output: str) -> tuple[int | None, dict[str, str], str]:
    remaining = output.replace("\r\n", "\n")
    status_code, headers = None, {}
    while remaining.startswith("HTTP/"):
        head, separator, body = remaining.partition("\n\n")
        if not separator:
            raise GhError(kind="malformed_http_headers")
        lines = head.splitlines()
        match = re.match(r"HTTP/\S+\s+(\d{3})\b", lines[0])
        if not match:
            raise GhError(kind="malformed_http_status")
        status_code, headers = int(match.group(1)), {}
        for line in lines[1:]:
            key, sep, value = line.partition(":")
            if sep:
                headers[key.strip().lower()] = value.strip()
        remaining = body
    return status_code, headers, remaining


def retry_delay(headers: dict[str, str], now: float, fallback: float) -> float:
    value = headers.get("retry-after")
    if value:
        try:
            delay = float(value)
        except ValueError:
            try:
                delay = parsedate_to_datetime(value).timestamp() - now
            except (ValueError, TypeError, OverflowError):
                delay = fallback
        if math.isfinite(delay):
            return max(0.0, delay)
    if headers.get("x-ratelimit-remaining") == "0":
        try:
            delay = float(headers["x-ratelimit-reset"]) - now
            if math.isfinite(delay):
                return max(0.0, delay)
        except (KeyError, ValueError, OverflowError):
            pass
    return fallback


class GhClient:
    """Serial, bounded GET transport with optional authenticated ETag reuse."""

    def __init__(self, *, timeout: float = DEFAULT_TIMEOUT, retries: int = 2, max_wait: float = 5,
                 api_version: str = API_VERSION, cache_dir: Path | None = None,
                 runner=None, sleeper=None, clock=None):
        if not math.isfinite(timeout) or timeout <= 0 or not 0 <= retries <= 5:
            raise ValueError("timeout must be positive and retries must be 0..5")
        if not math.isfinite(max_wait) or not 0 <= max_wait <= 30:
            raise ValueError("max_wait must be finite and 0..30 seconds")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", api_version):
            raise ValueError("API version must use YYYY-MM-DD")
        self.timeout, self.retries, self.max_wait = timeout, retries, max_wait
        self.api_version, self.cache_dir = api_version, cache_dir
        self.runner, self.sleeper, self.clock = runner or subprocess.run, sleeper or time.sleep, clock or time.time
        self.stats = {"requests": 0, "retries": 0, "conditional_hits": 0, "cache_write_errors": 0}

    def _cache_path(self, path: str) -> Path | None:
        if self.cache_dir is None:
            return None
        key = hashlib.sha256((self.api_version + "\n" + path).encode()).hexdigest()
        return self.cache_dir / f"{key}.json"

    def _cached(self, path: str) -> dict | None:
        target = self._cache_path(path)
        if target is None:
            return None
        try:
            info = target.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_RESPONSE_BYTES:
                return None
            with target.open("rb") as stream:
                payload = stream.read(MAX_RESPONSE_BYTES + 1)
            if len(payload) > MAX_RESPONSE_BYTES:
                return None
            cached = json.loads(payload)
            if not isinstance(cached, dict) or cached.get("schema") != "ci-http-cache/1":
                return None
            if cached.get("endpoint") != path or cached.get("api_version") != self.api_version:
                return None
            etag = cached.get("etag")
            if not isinstance(etag, str) or not etag or len(etag) > 512 or "\n" in etag or "\r" in etag:
                return None
            if hashlib.sha256(json_bytes(cached.get("body"))).hexdigest() != cached.get("body_sha256"):
                return None
            return cached
        except (OSError, ValueError, TypeError, UnicodeError):
            return None

    def __call__(self, path: str) -> object:
        if not path.startswith("repos/") or any(c in path for c in "\r\n\0"):
            raise GhError(kind="invalid_api_path")
        cached = self._cached(path)
        command = ["gh", "api", "--hostname", "github.com", "--method", "GET", "--include",
                   "-H", "Accept: application/vnd.github+json",
                   "-H", f"X-GitHub-Api-Version: {self.api_version}"]
        if cached:
            command.extend(["-H", f"If-None-Match: {cached['etag']}"])
        command.append(path)
        waited = 0.0
        for attempt in range(self.retries + 1):
            self.stats["requests"] += 1
            headers: dict[str, str] = {}
            try:
                proc = self.runner(command, capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=self.timeout, check=False)
            except FileNotFoundError as exc:
                raise GhError(kind="gh_not_found") from exc
            except subprocess.TimeoutExpired:
                error, transient, delay = GhError(kind="timeout"), True, float(2 ** attempt)
            except OSError:
                error, transient, delay = GhError(kind="transport_error"), True, float(2 ** attempt)
            else:
                if len(proc.stdout.encode("utf-8")) > MAX_RESPONSE_BYTES:
                    raise GhError(kind="response_too_large")
                status_code, headers, body = parse_http(proc.stdout)
                if status_code is None and proc.returncode:
                    match = re.search(r"\bHTTP[ /]*(\d{3})\b", proc.stderr or "")
                    status_code = int(match.group(1)) if match else None
                if status_code == 304:
                    if not cached:
                        raise GhError(kind="unusable_not_modified", status_code=304)
                    self.stats["conditional_hits"] += 1
                    return cached["body"]
                if proc.returncode == 0 and (status_code is None or 200 <= status_code < 300):
                    try:
                        data = json.loads(body, parse_constant=lambda value: (_ for _ in ()).throw(ValueError()))
                    except (ValueError, RecursionError) as exc:
                        raise GhError(kind="malformed_json", status_code=status_code) from exc
                    target = self._cache_path(path)
                    if target and headers.get("etag"):
                        record = {"schema": "ci-http-cache/1", "api_version": self.api_version, "endpoint": path,
                                  "etag": headers["etag"], "body": data,
                                  "body_sha256": hashlib.sha256(json_bytes(data)).hexdigest(), "validated_at": utc_now()}
                        try:
                            write_json(target, record)
                        except OSError:
                            self.stats["cache_write_errors"] += 1
                    return data
                rate_limited = status_code == 429 or (status_code == 403 and (
                    "retry-after" in headers or headers.get("x-ratelimit-remaining") == "0"
                    or "rate limit" in body.lower() or "abuse detection" in body.lower()))
                if rate_limited:
                    delay = retry_delay(headers, self.clock(), 60.0)
                    error, transient = GhError(kind="rate_limit", status_code=status_code, retry_after=delay), True
                else:
                    error = GhError(kind="http_error" if status_code else "gh_command_failed", status_code=status_code)
                    transient = status_code in {408, 500, 502, 503, 504}
                    delay = retry_delay(headers, self.clock(), float(2 ** attempt))
            # Never shorten a server deadline to fit the wait budget.
            if not transient or attempt == self.retries or delay > self.max_wait or waited + delay > self.max_wait:
                raise error
            self.stats["retries"] += 1
            self.sleeper(delay)
            waited += delay
        raise GhError(kind="retry_budget_exhausted")


_default_client: GhClient | None = None


def gh_api(path: str) -> object:
    """Compatibility wrapper; main configures one serial client per collection."""
    global _default_client
    if _default_client is None:
        _default_client = GhClient()
    return _default_client(path)


def identity(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def object_list(data: object, key: str) -> list[dict]:
    if not isinstance(data, dict) or not isinstance(data.get(key), list):
        raise GhError(kind=f"malformed_{key}_response")
    batch = data[key]
    if not all(isinstance(item, dict) for item in batch):
        raise GhError(kind=f"malformed_{key}_record")
    return batch


def check_source_identity(record: dict, repo: str, repo_info: dict, report: dict, stage: str,
                          *, expected_run: dict | None = None, **context) -> None:
    """Check owning identities only; head_repository may legitimately be a fork."""
    report["checked_records"][stage] += 1
    gaps = []

    def repository_id(value: object, field: str) -> None:
        if value is None:
            gaps.append(field)
        elif not identity(value) or value < 1:
            raise GhError(kind="malformed_source_repository_identity")
        elif not identity(repo_info.get("id")) or repo_info["id"] < 1:
            gaps.append(field + ".expected_unknown")
        elif value != repo_info["id"]:
            raise GhError(kind="source_repository_identity_mismatch")

    owning = record if stage == "metadata" else record.get("repository")
    if owning is None:
        if stage != "jobs" or "repository" in record:
            gaps.append("repository")
    elif not isinstance(owning, dict):
        raise GhError(kind="malformed_source_repository_identity")
    else:
        repository_id(owning.get("id"), "repository.id")
        full_name = owning.get("full_name")
        if full_name is None:
            gaps.append("repository.full_name")
        elif not isinstance(full_name, str) or not REPO_RX.fullmatch(full_name):
            raise GhError(kind="malformed_source_repository_identity")
        elif full_name.casefold() != repo.casefold():
            raise GhError(kind="source_repository_identity_mismatch")
        owner_name, repo_name = repo.split("/")
        if "name" in owning and owning["name"] is not None:
            if not isinstance(owning["name"], str) or owning["name"].casefold() != repo_name.casefold():
                raise GhError(kind="source_repository_identity_mismatch")
        owner = owning.get("owner")
        if owner is not None:
            if not isinstance(owner, dict):
                raise GhError(kind="malformed_source_repository_identity")
            if owner.get("login") is not None and (not isinstance(owner["login"], str)
                    or owner["login"].casefold() != owner_name.casefold()):
                raise GhError(kind="source_repository_identity_mismatch")
    if "repository_id" in record:
        repository_id(record["repository_id"], "repository_id")
    if stage == "jobs":
        value = record.get("run_id")
        if value is None:
            gaps.append("run_id")
        elif not identity(value) or value != context["run_id"]:
            raise GhError(kind="job_run_identity_mismatch")
    if stage != "metadata":
        value = record.get("head_sha")
        if value is None:
            gaps.append("head_sha")
        elif not isinstance(value, str) or not value:
            raise GhError(kind="malformed_source_head_identity")
        elif expected_run is not None:
            expected = expected_run.get("head_sha")
            if not isinstance(expected, str) or not expected:
                gaps.append("head_sha.expected_unknown")
            elif value != expected:
                raise GhError(kind="source_head_identity_mismatch")
    if gaps:
        report["state"] = "unknown"
        report["gaps"].append({"stage": stage, **context, "fields": gaps})


def source_payload_gap(report: dict | None, stage: str, **context) -> None:
    if report is not None:
        report["state"] = "unknown"
        report["gaps"].append({"stage": stage, **context, "fields": ["payload_unavailable"]})


def fetch_runs(gh: Gh, repo: str, limit: int, branch: str | None, event: str | None, *,
               created: str | None = None, workflow: str | None = None, include_incomplete: bool = False,
               enumeration: dict | None = None) -> list[dict]:
    runs: list[dict] = []
    seen: set[int] = set()
    total_count, total_changed, exhausted = None, False, False
    page, pages_fetched = 1, 0
    endpoint = f"repos/{repo}/actions"
    endpoint += f"/workflows/{quote(str(workflow), safe='')}/runs" if workflow else "/runs"
    while len(runs) < limit:
        query: dict[str, object] = {"per_page": PER_PAGE, "page": page}
        if not include_incomplete:
            query["status"] = "completed"
        for name, value in (("branch", branch), ("event", event), ("created", created)):
            if value:
                query[name] = value
        data = gh(f"{endpoint}?{urlencode(query)}")
        pages_fetched += 1
        batch = object_list(data, "workflow_runs")
        reported_total = data.get("total_count")
        if identity(reported_total):
            total_changed |= total_count is not None and total_count != reported_total
            total_count = reported_total
        for run in batch:
            run_id = run.get("id")
            if not identity(run_id) or run_id in seen:
                raise GhError(kind="invalid_or_duplicate_run_identity")
            seen.add(run_id)
        runs.extend(batch)
        exhausted = len(batch) < PER_PAGE or (total_count is not None and len(runs) >= total_count)
        if exhausted:
            break
        page += 1
    selected = runs[:limit]
    limit_reached = len(runs) > limit or not exhausted
    if enumeration is not None:
        enumeration.update({"complete": exhausted and not limit_reached and not total_changed,
                            "selection_complete": True, "limit_reached": limit_reached,
                            "server_total_count": total_count if total_count is not None else "unknown",
                            "total_count_changed_during_pagination": total_changed,
                            "pages": pages_fetched, "returned_runs": len(selected), "requested_limit": limit,
                            "api_filtered_search_limit": 1000,
                            "retention": "unknown; deleted or expired evidence cannot be inferred from the returned list",
                            "include_incomplete": include_incomplete})
    return selected


def fetch_jobs(gh: Gh, repo: str, run_id: int, *, expected_run: dict | None = None,
               repo_info: dict | None = None, source_identity: dict | None = None) -> list[dict]:
    jobs: list[dict] = []
    seen = set()
    for page in range(1, MAX_PAGES + 1):
        data = gh(f"repos/{repo}/actions/runs/{run_id}/jobs?filter=all&per_page={PER_PAGE}&page={page}")
        batch = object_list(data, "jobs")
        for job in batch:
            job_id = job.get("id")
            if not identity(job_id) or job_id in seen:
                raise GhError(kind="invalid_or_duplicate_job_identity")
            if job.get("run_id") is not None and (not identity(job["run_id"]) or job["run_id"] != run_id):
                raise GhError(kind="job_run_identity_mismatch")
            if source_identity is not None:
                check_source_identity(job, repo, repo_info or {}, source_identity, "jobs",
                                      expected_run=expected_run, run_id=run_id, job_id=job_id)
            seen.add(job_id)
        jobs.extend(batch)
        if len(batch) < PER_PAGE:
            return jobs
    raise GhError(kind="jobs_pagination_limit")


def fetch_attempts(gh: Gh, repo: str, run: dict, errors: list[dict], *, repo_info: dict | None = None,
                   source_identity: dict | None = None) -> tuple[list[dict], bool]:
    run_id, latest = run["id"], run.get("run_attempt")
    if not identity(latest) or latest < 1:
        errors.append(GhError(kind="missing_run_attempt").record("attempts", run_id=run_id))
        source_payload_gap(source_identity, "attempts", run_id=run_id)
        return [run], False
    records, complete = [], True
    if latest > MAX_ATTEMPTS_PER_RUN:
        errors.append(GhError(kind="attempt_collection_limit").record("attempts", run_id=run_id,
                                                                    latest_attempt=latest, limit=MAX_ATTEMPTS_PER_RUN))
        complete = False
    for number in range(1, min(latest, MAX_ATTEMPTS_PER_RUN)):
        try:
            attempt = gh(f"repos/{repo}/actions/runs/{run_id}/attempts/{number}")
            if (not isinstance(attempt, dict) or not identity(attempt.get("id")) or attempt["id"] != run_id
                    or not identity(attempt.get("run_attempt")) or attempt["run_attempt"] != number):
                raise GhError(kind="malformed_attempt_response")
            if source_identity is not None:
                check_source_identity(attempt, repo, repo_info or {}, source_identity, "attempts",
                                      expected_run=run, run_id=run_id, run_attempt=number)
        except GhError as exc:
            errors.append(exc.record("attempts", run_id=run_id, run_attempt=number))
            source_payload_gap(source_identity, "attempts", run_id=run_id, run_attempt=number)
            complete = False
            continue
        records.append(attempt)
    if source_identity is not None:
        check_source_identity(run, repo, repo_info or {}, source_identity, "attempts",
                              expected_run=run, run_id=run_id, run_attempt=latest)
    records.append(run)
    return records, complete


def required_identities(classic: dict, rules: list[dict]) -> list[dict]:
    result, known = [], set()

    def add(record: dict) -> None:
        key = json.dumps(record, sort_keys=True)
        if key not in known:
            known.add(key)
            result.append(record)

    checks = classic.get("checks", [])
    for check in checks if isinstance(checks, list) else []:
        if isinstance(check, dict) and isinstance(check.get("context"), str):
            add({"kind": "status_check", "context": check["context"], "app_id": check.get("app_id"),
                 "source": "classic_protection"})
    contexts = classic.get("contexts", [])
    with_identity = {r["context"] for r in result}
    for context in contexts if isinstance(contexts, list) else []:
        if isinstance(context, str) and context not in with_identity:
            add({"kind": "status_check", "context": context, "app_id": None, "source": "classic_protection"})
    for rule in rules:
        parameters = rule.get("parameters", {})
        if not isinstance(parameters, dict):
            continue
        if rule.get("type") == "required_status_checks":
            checks = parameters.get("required_status_checks", [])
            for check in checks if isinstance(checks, list) else []:
                if isinstance(check, dict) and isinstance(check.get("context"), str):
                    add({"kind": "status_check", "context": check["context"], "app_id": check.get("integration_id"),
                         "source": "rulesets", "ruleset_id": rule.get("ruleset_id")})
        if rule.get("type") == "workflows":
            workflows = parameters.get("workflows", [])
            for workflow in workflows if isinstance(workflows, list) else []:
                if isinstance(workflow, dict):
                    add({"kind": "workflow", "path": workflow.get("path"), "repository_id": workflow.get("repository_id"),
                         "ref": workflow.get("ref"), "sha": workflow.get("sha"), "source": "rulesets",
                         "ruleset_id": rule.get("ruleset_id")})
    return result


def valid_required_rule(rule: dict) -> bool:
    if rule.get("type") not in {"required_status_checks", "workflows"}:
        return True
    parameters = rule.get("parameters")
    if not isinstance(parameters, dict):
        return False
    if rule["type"] == "required_status_checks":
        checks = parameters.get("required_status_checks")
        return isinstance(checks, list) and all(isinstance(check, dict) and isinstance(check.get("context"), str)
                                               and bool(check["context"]) for check in checks)
    workflows = parameters.get("workflows")
    return isinstance(workflows, list) and all(isinstance(workflow, dict) and isinstance(workflow.get("path"), str)
                                              and bool(workflow["path"]) for workflow in workflows)


def fetch_required_checks(gh: Gh, repo: str, branch: str | None, errors: list[dict]) -> dict:
    result: dict = {"schema": "ci-required-checks/2", "branch": branch or "unknown",
                    "classic_protection": {"unavailable": "unknown"}, "rulesets": {"unavailable": "unknown"},
                    "source_status": {}}
    if not branch:
        for source in ("classic_protection", "rulesets"):
            result["source_status"][source] = {"complete": False, "state": "unknown", "status_code": None}
        result.update(completeness={"complete": False}, required_identities=[])
        return result
    encoded = quote(branch, safe="")
    try:
        classic = gh(f"repos/{repo}/branches/{encoded}/protection/required_status_checks")
        if not isinstance(classic, dict) or not ({"contexts", "checks"} & set(classic)):
            raise GhError(kind="malformed_classic_protection")
        if not isinstance(classic.get("contexts", []), list) or not isinstance(classic.get("checks", []), list):
            raise GhError(kind="malformed_classic_protection")
        if not all(isinstance(context, str) and context for context in classic.get("contexts", [])):
            raise GhError(kind="malformed_classic_protection")
        if not all(isinstance(check, dict) and isinstance(check.get("context"), str) and check["context"]
                   for check in classic.get("checks", [])):
            raise GhError(kind="malformed_classic_protection")
        result["classic_protection"] = classic
        result["source_status"]["classic_protection"] = {"complete": True, "state": "complete", "status_code": 200}
    except GhError as exc:
        errors.append(exc.record("classic_protection", branch=branch))
        result["classic_protection"] = {"unavailable": "unknown", "http_status": exc.status_code,
                                        "reason": "not_found_or_inaccessible" if exc.status_code == 404 else exc.kind}
        result["source_status"]["classic_protection"] = {"complete": False, "state": "unknown", "status_code": exc.status_code}
    rules, pages, rules_complete, failure = [], 0, False, None
    for page in range(1, MAX_PAGES + 1):
        try:
            batch = gh(f"repos/{repo}/rules/branches/{encoded}?per_page={PER_PAGE}&page={page}")
            if not isinstance(batch, list) or not all(isinstance(rule, dict) for rule in batch):
                raise GhError(kind="malformed_branch_rules")
        except GhError as exc:
            failure = exc
            break
        pages += 1
        rules.extend(r for r in batch if r.get("type") in {"required_status_checks", "merge_queue", "workflows"})
        if len(batch) < PER_PAGE:
            rules_complete = True
            break
    if rules_complete and not all(valid_required_rule(rule) for rule in rules):
        rules_complete = False
        failure = GhError(kind="malformed_required_rule", status_code=200)
    if not rules_complete:
        failure = failure or GhError(kind="rules_pagination_limit")
        errors.append(failure.record("rulesets", branch=branch))
    result["rulesets"] = rules if rules or rules_complete else {
        "unavailable": "unknown", "http_status": failure.status_code if failure else None,
        "reason": failure.kind if failure else "unknown"}
    result["source_status"]["rulesets"] = {
        "complete": rules_complete, "state": "complete" if rules_complete else ("partial" if rules else "unknown"),
        "status_code": 200 if rules_complete else failure.status_code, "pages": pages}
    result["completeness"] = {"complete": all(v["complete"] for v in result["source_status"].values())}
    result["required_identities"] = required_identities(result["classic_protection"], rules)
    return result


def resolve_target_branch(runs: list[dict], default_branch: str | None, explicit: str | None) -> tuple[str | None, dict]:
    bases, unresolved = set(), []
    for run in runs:
        if run.get("event") != "pull_request":
            continue
        found = set()
        for pr in run.get("pull_requests", []) if isinstance(run.get("pull_requests", []), list) else []:
            base = pr.get("base", {}) if isinstance(pr, dict) else {}
            if isinstance(base, dict) and isinstance(base.get("ref"), str) and base["ref"]:
                found.add(base["ref"])
        bases.update(found)
        if not found:
            unresolved.append(run["id"])
    if explicit:
        return explicit, {"source": "explicit_target_branch", "observed_pr_bases": sorted(bases),
                          "unresolved_pr_run_ids": unresolved, "applicability": "only the explicitly requested target"}
    if len(bases) == 1 and not unresolved:
        return next(iter(bases)), {"source": "observed_pr_base", "observed_pr_bases": sorted(bases),
                                  "unresolved_pr_run_ids": [], "applicability": "single observed PR target"}
    if bases or unresolved:
        return None, {"source": "unknown", "observed_pr_bases": sorted(bases), "unresolved_pr_run_ids": unresolved,
                      "applicability": "mixed or unavailable PR bases; provide --target-branch"}
    return default_branch, {"source": "repository_default_branch", "observed_pr_bases": [],
                            "unresolved_pr_run_ids": [],
                            "applicability": "default branch policy only; run --branch is a separate history filter"}


def collect(gh: Gh, repo: str, limit: int, out: Path, branch: str | None = None,
            event: str | None = None, *, target_branch: str | None = None, created: str | None = None,
            workflow: str | None = None, include_incomplete: bool = False) -> dict:
    if not REPO_RX.fullmatch(repo) or not 1 <= limit <= MAX_LIMIT:
        raise ValueError("invalid repository or collection limit")
    started_at = utc_now()
    errors: list[dict] = []
    repo_info = gh(f"repos/{repo}")
    if not isinstance(repo_info, dict):
        raise GhError(kind="malformed_repository_metadata")
    source_identity = {"schema": "ci-source-identity/1", "state": "verified",
                       "expected_repository": repo, "expected_repository_id": repo_info.get("id", "unknown"),
                       "checked_records": {"metadata": 0, "runs": 0, "jobs": 0, "attempts": 0}, "gaps": []}
    check_source_identity(repo_info, repo, repo_info, source_identity, "metadata")
    if repo_info.get("id") is None:
        source_identity["expected_repository_id"] = "unknown"
    default_branch = repo_info.get("default_branch")
    default_branch = default_branch if isinstance(default_branch, str) and default_branch else None
    enumeration: dict = {}
    # Enumeration must finish before a new snapshot can become publishable.
    runs = fetch_runs(gh, repo, limit, branch, event, created=created, workflow=workflow,
                      include_incomplete=include_incomplete, enumeration=enumeration)
    for run in runs:
        check_source_identity(run, repo, repo_info, source_identity, "runs", run_id=run["id"])
    protected_branch, branch_resolution = resolve_target_branch(runs, default_branch, target_branch)
    snapshot_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:12]
    snapshot_path = f"snapshots/{snapshot_id}"
    staging = out / ".staging" / snapshot_id
    destination = out / snapshot_path
    raw = staging / "raw"
    raw.mkdir(parents=True, exist_ok=False)
    jobs_ok, attempts_ok, complete_attempts = [], [], []
    try:
        write_json(raw / "runs.json", runs)
        for run in runs:
            run_id = run["id"]
            try:
                jobs = fetch_jobs(gh, repo, run_id, expected_run=run, repo_info=repo_info,
                                  source_identity=source_identity)
            except GhError as exc:
                errors.append(exc.record("jobs", run_id=run_id))
                source_payload_gap(source_identity, "jobs", run_id=run_id)
            else:
                write_json(raw / "jobs" / f"{run_id}.json", jobs)
                jobs_ok.append(run_id)
                latest = run.get("run_attempt")
                if identity(latest) and any(identity(j.get("run_attempt")) and j["run_attempt"] > latest for j in jobs):
                    errors.append(GhError(kind="run_changed_during_collection").record("freshness", run_id=run_id))
            attempts, complete = fetch_attempts(gh, repo, run, errors, repo_info=repo_info,
                                               source_identity=source_identity)
            write_json(raw / "attempts" / f"{run_id}.json", attempts)
            attempts_ok.append(run_id)
            if complete:
                complete_attempts.append(run_id)
        required = fetch_required_checks(gh, repo, protected_branch, errors)
        required["branch_resolution"] = branch_resolution
        write_json(raw / "required_checks.json", required)
        completeness = {"jobs": len(jobs_ok) == len(runs), "attempts": len(complete_attempts) == len(runs),
                        "required_checks": required["completeness"]["complete"]}
        freshness = {"payloads": "fetched or authenticated-server-revalidated during this collection",
                     "point_in_time_transaction": False,
                     "source_identity": source_identity,
                     "consistent_observed_attempts": not any(e["stage"] == "freshness" for e in errors)}
        meta = {"schema": "ci-collect/2", "source": "github-rest via gh api", "repo": repo,
                "repository_id": repo_info.get("id", "unknown"), "default_branch": default_branch or "unknown",
                "snapshot_id": snapshot_id, "snapshot_path": snapshot_path,
                "protected_target_branch": protected_branch or "unknown", "branch_resolution": branch_resolution,
                "filters": {"status": "all" if include_incomplete else "completed", "branch": branch,
                            "target_branch": target_branch, "event": event, "created": created,
                            "workflow": workflow, "limit": limit},
                "started_at": started_at, "collected_at": utc_now(), "runs": len(runs),
                "runs_with_jobs": len(jobs_ok), "runs_with_attempts": len(attempts_ok),
                "runs_with_complete_attempts": len(complete_attempts),
                "errors": errors, "completeness": completeness, "run_enumeration": enumeration,
                "freshness": freshness, "private": repo_info.get("private", "unknown"),
                "retention": {"state": "unknown", "note": "Visible retained runs are not proof of full history; deleted runs, expired logs and artifacts may be unavailable."}}
        client = gh if isinstance(gh, GhClient) else (_default_client if gh is gh_api else None)
        if client is not None:
            meta["transport"] = {"api_version": client.api_version, "conditional_cache": client.cache_dir is not None,
                                 **client.stats}
        write_json(raw / "collect_meta.json", meta)
        files = {}
        for path in sorted(raw.rglob("*.json")):
            payload = path.read_bytes()
            files[path.relative_to(raw).as_posix()] = {"sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload)}
        manifest = {"schema": "ci-snapshot-manifest/1", "repo": repo, "snapshot_id": snapshot_id,
                    "collected_at": meta["collected_at"], "selected_run_ids": [r["id"] for r in runs],
                    "jobs_payload_run_ids": jobs_ok, "attempts_payload_run_ids": attempts_ok,
                    "attempts_complete_run_ids": complete_attempts, "required_checks_payload": True,
                    "run_enumeration": enumeration, "completeness": completeness,
                    "freshness": freshness, "errors": errors, "files": files}
        write_json(raw / "manifest.json", manifest)
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging, destination)
        write_json(out / "current.json", {"schema": "ci-snapshot-pointer/1", "snapshot_id": snapshot_id,
                                         "snapshot_path": snapshot_path, "repo": repo})
        return meta
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def main(argv: list[str] | None = None) -> int:
    global _default_client
    ap = argparse.ArgumentParser(description="Collect GitHub Actions evidence into immutable snapshots (GET only).")
    ap.add_argument("--repo", required=True, help="OWNER/REPO")
    ap.add_argument("--limit", type=int, default=150, help=f"retained runs to sample (1-{MAX_LIMIT})")
    ap.add_argument("--out", type=Path, default=Path(".ci-audit"))
    ap.add_argument("--branch", help="filter run history head branch; does not select branch protection")
    ap.add_argument("--target-branch", help="protected merge target whose required rules should be collected")
    ap.add_argument("--event", help="filter event (push, pull_request, merge_group, ...)")
    ap.add_argument("--created", help="server-side creation date/range, e.g. 2026-11-01..2026-11-17")
    ap.add_argument("--workflow", help="workflow ID or workflow filename for server-side filtering")
    ap.add_argument("--include-incomplete", action="store_true", help="include queued/running/waiting runs; handle censoring in analysis")
    ap.add_argument("--incremental", action="store_true", help="authenticated ETag requests; never reuse failed fetches")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="seconds per API request")
    ap.add_argument("--retries", type=int, default=2, help="maximum GET retries (0-5)")
    ap.add_argument("--max-rate-wait", type=float, default=5, help="total retry-wait budget per request, at most 30 seconds")
    ap.add_argument("--api-version", default=API_VERSION)
    args = ap.parse_args(argv)
    if not REPO_RX.fullmatch(args.repo):
        ap.error("--repo must look like OWNER/REPO")
    if not 1 <= args.limit <= MAX_LIMIT:
        ap.error(f"--limit must be 1..{MAX_LIMIT}")
    try:
        _default_client = GhClient(timeout=args.timeout, retries=args.retries, max_wait=args.max_rate_wait,
                                   api_version=args.api_version, cache_dir=args.out / ".http-cache" if args.incremental else None)
    except ValueError as exc:
        ap.error(str(exc))
    try:
        meta = collect(gh_api, args.repo, args.limit, args.out, args.branch, args.event,
                       target_branch=args.target_branch, created=args.created, workflow=args.workflow,
                       include_incomplete=args.include_incomplete)
    except GhError as exc:
        print(f"error: collection failed: {exc}; check gh authentication and repository access", file=sys.stderr)
        return 1
    except OSError:
        print("error: snapshot could not be saved; previous current pointer was not intentionally replaced", file=sys.stderr)
        return 1
    summary = {key: meta[key] for key in ("repo", "snapshot_id", "runs", "runs_with_jobs", "completeness", "errors")}
    summary["source_identity"] = meta["freshness"]["source_identity"]["state"]
    print(json.dumps(summary, indent=1))
    if (not all(meta["completeness"].values()) or not meta["freshness"]["consistent_observed_attempts"]
            or summary["source_identity"] != "verified"):
        print("warning: snapshot contains explicit evidence gaps; affected metrics remain unknown", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
