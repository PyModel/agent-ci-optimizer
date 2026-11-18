#!/usr/bin/env python3
"""Advisory GitHub workflow inspection; JSON normalized with YAML 1.2 is required.

Usage: inspect_workflows.py --workflow ci.json --path .github/workflows/ci.yml
       [--lockfile actions-lock.json] [--context context.json] [--out report.json]

This stdlib helper neither parses YAML nor evaluates expressions, executes steps,
fetches actions, expands matrices/reusable workflows, or changes YAML/lockfiles.
Do not normalize with a YAML 1.1 loader that changes `on` into boolean true.

Context keys: trigger_cache_default (read/write/unknown), trigger_trust
(trusted/low-trust/unknown), caller_cache_limit (an EXPLICIT calling-job limit,
or null when inspection established no explicit limit),
event_name, platform (github.com/ghec/ghes/unknown), host, features. The optional
features map has actions_lockfile, same_repo_refs, parallel_steps, cache_mode,
queue_max booleans or 'unknown'. Do not infer an ordinary PR is cache-read-only.
Absent platform availability and trigger defaults remain unknown. A caller's
implicit trigger default is not a caller_cache_limit.

Checks reflect official docs read 2026-11-17 (America/New_York); this is not the provider's validator.
Exit 1 means a definite static error, 2 malformed input, 0 advisory findings only.
No exit code certifies a safe workflow or a release-eligible artifact.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path, PurePosixPath
import re
import sys

UNKNOWN = "unknown"
CAPABILITIES = {"read": {"restore"}, "write": {"restore", "save"}, "write-only": {"save"}, "none": set()}
FEATURES = {"actions_lockfile", "same_repo_refs", "parallel_steps", "cache_mode", "queue_max"}
CONTEXT_KEYS = {"trigger_cache_default", "trigger_trust", "caller_cache_limit", "event_name", "platform", "host", "features"}
REMOTE = re.compile(r"^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)(?:/([^@\s]+))?@([^@\s]+)$")
FULL_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
LOCK_SHA = re.compile(r"^sha1-[0-9a-fA-F]{40}$")
DOCKER_DIGEST = re.compile(r"^docker://[^@\s]+@sha256:[0-9a-fA-F]{64}$")
SOURCES = ["https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax",
           "https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching",
           "https://docs.github.com/en/actions/concepts/security/workflow-dependency-lockfiles",
           "https://docs.github.com/en/actions/how-tos/secure-your-work/lock-workflow-dependencies",
           "https://github.com/github/actions-lockfile"]


def text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and "\0" not in value


def expression(value: object) -> bool:
    return isinstance(value, str) and "${{" in value


def repo_path(value: object) -> bool:
    if not text(value):
        return False
    path = PurePosixPath(value)
    return bool(path.parts) and not path.is_absolute() and ".." not in path.parts and path.as_posix() == value


def pin_key(reference: object) -> str | None:
    match = REMOTE.fullmatch(reference) if isinstance(reference, str) else None
    return f"{match[1].lower()}/{match[2].lower()}@{match[4]}" if match else None


def workflow_file(value: object) -> bool:
    """Reusable calls name a single YAML basename, not a subdirectory or @ref."""
    if not repo_path(value) or "@" in value or "\\" in value:
        return False
    path = PurePosixPath(value)
    return len(path.parts) == 3 and path.parts[:2] == (".github", "workflows") and path.suffix in {".yml", ".yaml"}


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_json(path: Path) -> object:
    with path.open("rb") as stream:
        raw = stream.read(8 * 1024 * 1024 + 1)
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError("input exceeds the 8 MiB inspection limit")
    return json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                      parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("non-finite JSON number")))


def validate_context(context: object) -> dict:
    if not isinstance(context, dict) or set(context) - CONTEXT_KEYS:
        raise ValueError("context must use only documented keys")
    for key in ("event_name", "platform", "host"):
        if key in context and not text(context[key]):
            raise ValueError(f"context.{key} must be a non-empty string")
    if context.get("trigger_cache_default", UNKNOWN) not in ("read", "write", UNKNOWN):
        raise ValueError("trigger_cache_default must be read, write or unknown")
    if context.get("trigger_trust", UNKNOWN) not in ("trusted", "low-trust", UNKNOWN):
        raise ValueError("trigger_trust must be trusted, low-trust or unknown")
    if "caller_cache_limit" in context:
        limit = context["caller_cache_limit"]
        if limit is not None and (not isinstance(limit, str) or (limit not in CAPABILITIES and limit != UNKNOWN and not expression(limit))):
            raise ValueError("caller_cache_limit must be an explicit cache mode or unknown")
    default_for_trust = {"trusted": "write", "low-trust": "read"}.get(context.get("trigger_trust"))
    if default_for_trust and context.get("trigger_cache_default", UNKNOWN) not in (UNKNOWN, default_for_trust):
        raise ValueError("trigger_trust and trigger_cache_default contradict each other")
    features = context.get("features", {})
    if not isinstance(features, dict) or set(features) - FEATURES or any(type(v) is not bool and v != UNKNOWN for v in features.values()):
        raise ValueError("features must map documented names to booleans or unknown")
    return context


class Inspector:
    def __init__(self, workflow: dict, path: str, lockfile: object, context: dict):
        self.workflow, self.path, self.context = workflow, path, context
        self.findings, self.actions, self.steps, self.jobs, self.concurrency = [], [], [], [], []
        self.features_used, self.pins, self.listed = {}, {}, set()
        events = workflow.get("on", [])
        self.caller_unknown = (isinstance(events, (dict, list)) and "workflow_call" in events or events == "workflow_call") and "caller_cache_limit" not in context
        self.lock = {"provided": lockfile is not None, "format_valid": UNKNOWN, "onboarded": False,
                     "transitive_coverage": UNKNOWN, "reachability_verified": False, "repository_identity_verified": False}
        self.read_lockfile(lockfile)

    def finding(self, severity: str, code: str, location: str, message: str) -> None:
        self.findings.append({"severity": severity, "code": code, "location": location, "message": message})

    def feature(self, name: str, location: str):
        available = self.context.get("features", {}).get(name, UNKNOWN)
        if self.context.get("platform") in {"ghes", "github-enterprise-server"} and name in {"same_repo_refs", "actions_lockfile"}:
            available = False
        if name not in self.features_used:
            self.features_used[name] = available
            if available is False:
                self.finding("error", "feature_unavailable", location, f"{name} is unavailable in the supplied platform context.")
            elif available == UNKNOWN:
                self.finding("unknown", "feature_availability", location, f"Verify {name} availability for this repository/platform; syntax alone does not establish support.")
        return available

    def read_lockfile(self, data: object) -> None:
        if data is None:
            return
        errors = []
        if not isinstance(data, dict) or not text(data.get("version")):
            self.lock["format_valid"] = False
            self.finding("error", "lockfile_format", "lockfile", "Lockfile must be an object with a version string.")
            return
        if data["version"] != "v0.0.3":
            self.finding("unknown", "lockfile_version", "lockfile", "Only native v0.0.3 is understood; no pin coverage is inferred.")
            return
        self.lock["version"] = data["version"]
        workflows, dependencies = data.get("workflows"), data.get("dependencies")
        if not isinstance(workflows, dict) or not isinstance(dependencies, dict):
            errors.append("workflows and dependencies must be objects")
        else:
            for key, entry in dependencies.items():
                canonical = pin_key(key)
                match = REMOTE.fullmatch(key) if isinstance(key, str) else None
                if not match or match[3] or not isinstance(entry, dict):
                    errors.append("dependency keys must be OWNER/REPO@REF with object values")
                    continue
                if canonical in self.pins:
                    errors.append("duplicate case-normalized dependency key")
                if (entry.get("ref") != match[4] or not isinstance(entry.get("commit"), str)
                        or not LOCK_SHA.fullmatch(entry["commit"])
                        or any(type(entry.get(name)) is not int or entry[name] <= 0 for name in ("owner_id", "repo_id"))):
                    errors.append("dependency needs matching ref, sha1-40hex commit and positive numeric owner_id/repo_id")
                    continue
                host = entry.get("hostname")
                if host is not None and (not isinstance(host, str) or not re.fullmatch(r"github\.com|[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.ghe\.com", host)):
                    errors.append("dependency hostname must be a canonical GitHub hostname")
                if self.context.get("host") == "github.com" and host not in (None, "github.com"):
                    errors.append("a github.com run cannot use a tenant-host lock entry")
                uses = entry.get("uses", [])
                if not isinstance(uses, list) or any(pin_key(ref) is None for ref in uses):
                    errors.append("dependency uses must be an array of remote action pin references")
                self.pins[canonical] = entry
            normalized_workflows = {}
            for path, refs in workflows.items():
                if not repo_path(path) or not isinstance(refs, list) or any(pin_key(ref) is None for ref in refs):
                    errors.append("workflow entries must contain repository-relative paths and arrays of pin references")
                    continue
                keys = [pin_key(ref) for ref in refs]
                if len(keys) != len(set(keys)):
                    errors.append("duplicate workflow dependency reference")
                if any(key not in self.pins for key in keys):
                    errors.append("workflow references a missing dependency entry")
                normalized_workflows[path] = set(keys)
            for refs in normalized_workflows.values():
                for key in refs:
                    entry = self.pins.get(key, {})
                    for used in entry.get("uses", []) if isinstance(entry.get("uses", []), list) else []:
                        if pin_key(used) not in refs:
                            errors.append("workflow's flat transitive list omits a recorded dependency")
            self.listed = normalized_workflows.get(self.path, set())
            self.lock["onboarded"] = self.path in normalized_workflows
            self.lock["listed_dependencies"] = len(self.listed)
        self.lock["format_valid"] = not errors
        for message in sorted(set(errors)):
            self.finding("error", "lockfile_format", "lockfile", message)
        if errors:
            self.pins, self.listed = {}, set()
        elif self.lock["onboarded"]:
            self.lock["feature_available"] = self.feature("actions_lockfile", "lockfile")
            self.finding("unknown", "lockfile_live_verification", "lockfile", "Pin metadata is structurally checked only; actual transitive graph, commit reachability and repository ownership were not verified.")

    def cache(self, declared: object, inherited: object, location: str, *, explicit: bool) -> dict:
        limit = self.context.get("caller_cache_limit")
        mode, source = declared, "explicit" if explicit else "inherited"
        if not explicit:
            mode = inherited
            if mode is None:
                mode, source = (limit, "explicit caller limit") if limit is not None else (self.context.get("trigger_cache_default", UNKNOWN), "trigger context")
                if mode == UNKNOWN and self.context.get("trigger_trust") in {"trusted", "low-trust"}:
                    mode = "write" if self.context["trigger_trust"] == "trusted" else "read"
        if explicit:
            self.feature("cache_mode", location)
        if not isinstance(mode, str) or (mode not in CAPABILITIES and mode != UNKNOWN and not expression(mode)):
            self.finding("error", "invalid_cache_mode", location, "cache-mode must be read, write, write-only or none.")
            mode = UNKNOWN
        if expression(mode):
            self.finding("unknown", "cache_expression", location, "Cache-mode expression was not evaluated.")
            mode = UNKNOWN
        requested, permitted = mode, True
        if self.caller_unknown:
            permitted = UNKNOWN
            self.finding("unknown", "caller_context_missing", location, "This workflow can be called by another workflow; its explicit caller limit was not supplied. Use null only after establishing that no explicit caller limit exists.")
        elif limit is not None:
            if limit not in CAPABILITIES or mode == UNKNOWN:
                permitted = UNKNOWN
            elif not CAPABILITIES[mode] <= CAPABILITIES[limit]:
                permitted = False
                self.finding("error", "cache_caller_overrequest", location, "The callee requests capabilities beyond the explicit caller limit; provider validation fails, rather than silently intersecting modes.")
        effective = mode if permitted is True else UNKNOWN
        if requested in {"write", "write-only"} and self.context.get("trigger_trust") == "low-trust":
            self.finding("warning", "low_trust_cache_write", location, "Write-capable cache access overrides the low-trust default; verify that no untrusted inputs can poison a subsequently trusted cache.")
        if effective == UNKNOWN:
            self.finding("unknown", "cache_access_unknown", location, "Effective cache access requires resolved mode, trigger context and any explicit caller limit.")
        return {"requested": requested, "effective": effective, "source": source, "within_caller_limit": permitted,
                "restore": "restore" in CAPABILITIES[effective] if effective in CAPABILITIES else UNKNOWN,
                "save": "save" in CAPABILITIES[effective] if effective in CAPABILITIES else UNKNOWN,
                "caller_limit": limit if limit is not None else ("no explicit limit" if "caller_cache_limit" in self.context else "not supplied")}

    def inspect_concurrency(self, value: object, location: str) -> None:
        if isinstance(value, str):
            value = {"group": value}
        if not isinstance(value, dict) or not text(value.get("group")):
            self.finding("error", "invalid_concurrency", location, "Concurrency must specify a group string/expression.")
            return
        queue, cancel = value.get("queue", "single"), value.get("cancel-in-progress", False)
        if expression(value["group"]):
            self.finding("unknown", "concurrency_group_expression", location, "Concurrency group expression and cross-run collisions are unresolved.")
        if expression(queue):
            queue = UNKNOWN
        elif queue not in ("single", "max"):
            self.finding("error", "invalid_concurrency_queue", location, "queue must be single or max.")
            queue = UNKNOWN
        if expression(cancel):
            cancel = UNKNOWN
        elif type(cancel) is not bool:
            self.finding("error", "invalid_cancellation", location, "cancel-in-progress must be boolean or an expression.")
            cancel = UNKNOWN
        if queue == "max":
            self.feature("queue_max", location)
            if cancel is True:
                self.finding("error", "queue_max_cancellation", location, "queue: max and cancel-in-progress: true are incompatible.")
        if UNKNOWN in (queue, cancel):
            self.finding("unknown", "concurrency_unresolved", location, "Queue/cancellation expressions were not evaluated; compatibility is unknown.")
        self.concurrency.append({"location": location, "group": UNKNOWN if expression(value["group"]) else value["group"],
                                 "queue": queue, "cancel_in_progress": cancel})

    def reference(self, value: object, location: str, reusable: bool = False) -> dict:
        item = {"location": location, "scope": "reusable_workflow" if reusable else "action_step",
                "reference": value if isinstance(value, str) and not expression(value) else UNKNOWN,
                "classification": UNKNOWN, "lockfile_covered": False, "live_verified": False}
        if not text(value) or expression(value):
            self.finding("unknown", "uses_unresolved", location, "Action/workflow reference is malformed or an unevaluated expression.")
        elif value.startswith("$/"):
            if not repo_path(value[2:]) or "@" in value:
                self.finding("error", "invalid_same_repo_ref", location, "A $/ reference needs a repository-relative path and cannot specify @ref.")
            elif reusable and not workflow_file(value[2:]):
                self.finding("error", "invalid_reusable_path", location, "Same-repository reusable calls need one .yml/.yaml basename directly under .github/workflows, without @ref.")
            else:
                item.update(classification="same_repo", resolution="defining repository at running workflow/action commit",
                            feature_available=self.feature("same_repo_refs", location))
        elif value.startswith("./"):
            if not repo_path(value[2:]):
                self.finding("error", "invalid_local_ref", location, "Local reference must stay inside the repository/workspace.")
            elif reusable and not workflow_file(value[2:]):
                self.finding("error", "invalid_reusable_path", location, "Local reusable calls need one .yml/.yaml basename directly under .github/workflows, without @ref.")
            else:
                item.update(classification="local_path", resolution="same workflow commit" if reusable else "checked-out workspace; not pinned by lockfile")
                if not reusable:
                    self.finding("unknown", "local_action_expansion", location, "Inspect checkout identity and local action dependencies; workspace code and transitive actions are not resolved here.")
        elif value.startswith("docker://"):
            if reusable:
                self.finding("error", "docker_reusable_workflow", location, "A Docker action is not a reusable workflow reference.")
            item["classification"] = "docker_digest" if DOCKER_DIGEST.fullmatch(value) else "mutable"
        else:
            match = REMOTE.fullmatch(value)
            if not match:
                self.finding("unknown", "uses_unrecognized", location, "Reference syntax is outside the supported forms.")
            elif reusable and not workflow_file(match[3]):
                self.finding("error", "invalid_reusable_path", location, "Remote reusable calls need one .yml/.yaml basename directly under .github/workflows plus @ref.")
            else:
                key = pin_key(value)
                item["pin_key"] = key
                covered = not reusable and self.lock["format_valid"] is True and self.lock["onboarded"] and key in self.listed
                item["lockfile_covered"] = covered
                if FULL_SHA.fullmatch(match[4]):
                    item.update(classification="direct_full_sha", commit=match[4].lower())
                elif covered:
                    item.update(classification="lockfile_covered", commit=self.pins[key]["commit"][5:].lower(),
                                feature_available=self.lock.get("feature_available", UNKNOWN))
                else:
                    item["classification"] = "mutable"
                if not reusable and self.lock["format_valid"] is True and self.lock["onboarded"] and not covered:
                    self.finding("error", "lockfile_missing_action", location, "This onboarded workflow has no lock entry for the action reference; another workflow's entry does not cover it.")
        if item["classification"] == "mutable":
            self.finding("warning", "mutable_reference", location, "The reference has no static immutable resolution proven for this scope; review direct SHA/digest pinning or applicable action lock coverage.")
        if reusable:
            self.finding("unknown", "reusable_not_expanded", location, "Reusable workflow jobs, expressions and dependencies are unexpanded; caller lockfiles do not pin job-level uses calls.")
        self.actions.append(item)
        return item

    def walk_steps(self, steps: object, location: str, *, parallel: bool = False, group_verified: bool = True, depth: int = 0) -> None:
        if depth > 32:
            self.finding("error", "step_depth", location, "Step nesting exceeds the inspection limit.")
            return
        if not isinstance(steps, list):
            self.finding("unknown" if expression(steps) else "error", "steps_unresolved", location, "Expected a concrete step array; it was not expanded.")
            return
        for index, step in enumerate(steps):
            loc = f"{location}/{index}"
            if not isinstance(step, dict):
                self.finding("error", "invalid_step", loc, "Each step must be an object.")
                continue
            kinds = [kind for kind in ("run", "uses", "parallel", "wait", "wait-all", "cancel") if kind in step]
            if len(kinds) != 1:
                self.finding("error", "step_operation", loc, "A step must declare one supported operation.")
            kind = kinds[0] if kinds else UNKNOWN
            supported_member = not parallel or (group_verified and kind in {"run", "uses"})
            if parallel and not supported_member:
                self.finding("unknown", "parallel_member_unverified", loc, "Nested groups or synchronization inside a parallel group are inventoried only; provider syntax/semantics have not been established for this shape.")
            background = step.get("background", False)
            if expression(background):
                background = UNKNOWN
                self.finding("unknown", "background_expression", loc, "Background execution expression is unresolved.")
            elif type(background) is not bool:
                self.finding("error", "invalid_background", loc, "background must be boolean or an expression.")
                background = UNKNOWN
            if "background" in step or kind in {"parallel", "wait", "wait-all", "cancel"}:
                self.feature("parallel_steps", loc)
            if "background" in step and kind not in {"run", "uses"}:
                self.finding("error", "background_operation", loc, "background applies to run/uses steps.")
            condition = step.get("if", True)
            if type(condition) is not bool:
                condition = UNKNOWN
                self.finding("unknown", "step_condition", loc, "Step condition was not evaluated.")
            if kind in {"wait", "wait-all", "cancel"} and "if" in step:
                self.finding("error", "conditional_synchronization", loc, "Native wait/wait-all/cancel steps do not support if conditions.")
            step_id = step.get("id", UNKNOWN)
            if not text(step_id):
                self.finding("error", "invalid_step_id", loc, "Step ID must be a non-empty string.")
                step_id = UNKNOWN
            if kind == "run" and not isinstance(step[kind], str):
                self.finding("error", "invalid_run", loc, "run must be a command string; it is never executed by this inspector.")
            item = {"location": loc, "kind": kind, "id": step_id, "condition": condition,
                    "background": (True if supported_member else UNKNOWN) if parallel else background,
                    "parallel_member": parallel, "parallel_context_verified": group_verified and supported_member}
            if kind == "parallel":
                item["implicit_wait"] = True if supported_member else UNKNOWN
            elif kind == "wait-all":
                if step[kind] is not None:
                    self.finding("error", "wait_all_arguments", loc, "wait-all takes no arguments (normalize it as null).")
            elif kind in {"wait", "cancel"}:
                targets = step[kind] if isinstance(step[kind], list) else [step[kind]]
                if not targets or any(not text(target) for target in targets) or (kind == "cancel" and not isinstance(step[kind], str)):
                    self.finding("error", "invalid_sync_target", loc, "wait requires one or more IDs; cancel requires one ID.")
                elif any(expression(target) for target in targets):
                    item["targets"] = UNKNOWN
                    self.finding("unknown", "sync_target_expression", loc, "Synchronization targets were not evaluated.")
                else:
                    item["targets"] = targets
            if step.get("continue-on-error") is True:
                self.finding("warning", "continued_failure", loc, "This step can fail without failing its job; verify it is not uniquely required validation.")
            elif expression(step.get("continue-on-error")):
                self.finding("unknown", "continued_failure_expression", loc, "Failure propagation depends on an unevaluated continue-on-error expression.")
            self.steps.append(item)
            if "uses" in step:
                self.reference(step["uses"], loc + "/uses")
            if "parallel" in step:
                self.walk_steps(step["parallel"], loc + "/parallel", parallel=True,
                                group_verified=group_verified and not parallel, depth=depth + 1)

    def inspect(self) -> dict:
        workflow_mode = self.workflow.get("cache-mode")
        workflow_cache = self.cache(workflow_mode, None, "/cache-mode", explicit="cache-mode" in self.workflow)
        if "concurrency" in self.workflow:
            self.inspect_concurrency(self.workflow["concurrency"], "/concurrency")
        jobs = self.workflow.get("jobs")
        if not isinstance(jobs, dict) or not jobs:
            raise ValueError("workflow.jobs must be a non-empty object")
        for job_id, job in jobs.items():
            loc = f"/jobs/{job_id}"
            if not isinstance(job, dict):
                self.finding("error", "invalid_job", loc, "A job must be an object.")
                continue
            inherited = workflow_cache["requested"] if "cache-mode" in self.workflow else None
            cache = self.cache(job.get("cache-mode"), inherited, loc + "/cache-mode", explicit="cache-mode" in job)
            matrix = (isinstance(job.get("strategy"), dict) and "matrix" in job["strategy"]) or ("strategy" in job and not isinstance(job["strategy"], dict))
            if matrix:
                self.finding("unknown", "matrix_not_expanded", loc + "/strategy/matrix", "Matrix instances/include/exclude were not expanded; job counts and each instance's effective settings remain unknown.")
            if "if" in job and type(job["if"]) is not bool:
                self.finding("unknown", "job_condition", loc + "/if", "Job condition was not evaluated.")
            needs = job.get("needs", [])
            needs = [needs] if isinstance(needs, str) else needs
            if not isinstance(needs, list) or any(not text(item) or expression(item) for item in needs):
                needs = UNKNOWN
                self.finding("unknown", "needs_unresolved", loc + "/needs", "Dependency expression/shape was not resolved.")
            self.jobs.append({"id": job_id, "needs": needs, "cache": cache,
                              "matrix_expansion": UNKNOWN if matrix else "not applicable", "reusable_expansion": UNKNOWN if "uses" in job else "not applicable"})
            if "concurrency" in job:
                self.inspect_concurrency(job["concurrency"], loc + "/concurrency")
            if "uses" in job:
                self.reference(job["uses"], loc + "/uses", reusable=True)
            if "steps" in job:
                self.walk_steps(job["steps"], loc + "/steps")
        events = self.workflow.get("on", UNKNOWN)
        if isinstance(events, dict):
            events = list(events)
        elif isinstance(events, str) and not expression(events):
            events = [events]
        if not isinstance(events, list) or any(not text(event) for event in events):
            events = UNKNOWN
            self.finding("unknown", "events_unresolved", "/on", "Trigger inventory is missing or unresolved.")
        return {"schema": "ci-workflow-inspection/1", "workflow_path": self.path, "advisory_only": True,
                "has_errors": any(item["severity"] == "error" for item in self.findings), "complete_expansion": False,
                "events": events, "cache": workflow_cache, "jobs": self.jobs, "steps": self.steps,
                "action_references": self.actions, "concurrency": self.concurrency, "lockfile": self.lock,
                "features_used": self.features_used, "findings": self.findings,
                "limitations": ["YAML must be normalized correctly before inspection; this is not the GitHub validator.",
                  "Expressions, matrix instances, action metadata and reusable workflows are not expanded.",
                  "Commands, environment values and secrets are not inventoried or executed.",
                  "Feature availability is caller-supplied; no hosting plan is inferred from public syntax.",
                  "Native background work has an implicit job-end wait; overlapping durations must not be summed.",
                  "No live commit reachability, owner identity or release eligibility was verified."],
                "sources_checked": "2026-11-17", "sources_timezone": "America/New_York", "sources": SOURCES}


def inspect_workflow(workflow: object, path: str, lockfile: object = None, context: object = None) -> dict:
    if not isinstance(workflow, dict):
        raise ValueError("workflow must be a YAML-1.2-normalized JSON object")
    if True in workflow or "true" in workflow:
        raise ValueError("boolean true/on key corruption: re-normalize workflow with YAML 1.2")
    if not repo_path(path):
        raise ValueError("--path must be the original repository-relative workflow path")
    return Inspector(workflow, path, lockfile, validate_context({} if context is None else context)).inspect()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workflow", type=Path, required=True, help="YAML 1.2 normalized JSON file")
    ap.add_argument("--path", required=True, help="original workflow path, e.g. .github/workflows/ci.yml")
    ap.add_argument("--lockfile", type=Path, help="native actions.lock normalized as JSON")
    ap.add_argument("--context", type=Path, help="explicit platform, trigger and caller-limit JSON")
    ap.add_argument("--out", type=Path)
    args = ap.parse_args(argv)
    try:
        result = inspect_workflow(load_json(args.workflow), args.path,
                                  load_json(args.lockfile) if args.lockfile else None,
                                  load_json(args.context) if args.context else None)
        rendered = json.dumps(result, indent=2, allow_nan=False) + "\n"
        if args.out:
            if any(args.out.resolve() == path.resolve() for path in (args.workflow, args.lockfile, args.context) if path):
                raise ValueError("--out must not overwrite an input")
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(rendered, encoding="utf-8")
        else:
            print(rendered, end="")
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 1 if result["has_errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
