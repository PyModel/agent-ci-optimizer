#!/usr/bin/env python3
"""Fail-closed reference selector: changed files -> targets to test, or FULL.

A reference implementation for adversarial tests and for repos without a real
project graph; prefer the build tool's own graph (Nx, Turborepo, Bazel, ...)
when it exists. Returns FULL whenever it cannot prove a narrower selection is
safe: missing base, git failure, empty diff, a file matching a full trigger,
or a file no rule maps.

Map file (JSON):
  {
    "full_triggers": ["infra/**"],                 # added to DEFAULT_FULL_TRIGGERS
    "rules": [{"paths": ["packages/api/**"], "targets": ["api"]}],
    "dependents": {"api": ["web"]}                  # target -> targets that depend on it
  }
Globs use fnmatch: `*` also matches `/`, so "packages/api/*" covers nested files.

Usage:
  python3 scripts/select_affected.py --map ci-map.json --base origin/main [--head HEAD]
  python3 scripts/select_affected.py --map ci-map.json --changed a.ts b.ts   # no git
Exit 0 with JSON {"mode": "FULL"|"SELECTED", ...}; exit 2 on a bad map.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import subprocess
import sys
from pathlib import Path

DEFAULT_FULL_TRIGGERS = (
    ".github/*", ".gitlab-ci.yml", ".circleci/*", "azure-pipelines.yml", "Jenkinsfile", ".buildkite/*",
    "package.json", "*/package.json", "package-lock.json", "pnpm-lock.yaml", "pnpm-workspace.yaml", "yarn.lock",
    "bun.lock", "bun.lockb", ".npmrc", ".yarnrc*", "uv.lock", "poetry.lock", "requirements*.txt",
    "pyproject.toml", "*/pyproject.toml", "Cargo.lock", "Cargo.toml", "*/Cargo.toml",
    "go.mod", "*/go.mod", "go.sum", "Gemfile.lock",
    "tsconfig*.json", "*/tsconfig*.json", "*.config.js", "*.config.cjs", "*.config.mjs", "*.config.ts",
    "nx.json", "turbo.json", "project.json", "*/project.json", "WORKSPACE*", "MODULE.bazel", "*.bzl",
    ".nvmrc", ".node-version", ".python-version", ".tool-versions", "rust-toolchain*", "mise.toml",
    "Dockerfile*", "*/Dockerfile*", "docker-compose*", "Makefile", "justfile",
    "*migrations/*", "*migration*", "*schema*", "*.proto", "*.graphql", "openapi*", "*/openapi*",
    "*generated*", "*.snap", "CODEOWNERS", "SECURITY.md", "*.env.example",
    "*security*", "*policy*", "*.lock",
)


class MapError(ValueError):
    pass


def load_map(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise MapError(f"cannot read map {path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("rules"), list):
        raise MapError("map must be an object with a 'rules' list")
    for i, rule in enumerate(data["rules"]):
        if not (isinstance(rule, dict) and isinstance(rule.get("paths"), list) and rule["paths"]
                and isinstance(rule.get("targets"), list) and rule["targets"]):
            raise MapError(f"rules[{i}] needs non-empty 'paths' and 'targets' lists")
    if not isinstance(data.get("dependents", {}), dict):
        raise MapError("'dependents' must be an object")
    if not isinstance(data.get("full_triggers", []), list):
        raise MapError("'full_triggers' must be a list")
    return data


def git_changed(base: str, head: str, cwd: Path) -> tuple[list[str] | None, str]:
    """Paths changed between merge-base(base, head) and head, both sides of renames."""
    try:
        mb = subprocess.run(["git", "merge-base", base, head], cwd=cwd, capture_output=True, text=True, timeout=30)
        if mb.returncode != 0:
            return None, f"no merge-base for {base}..{head} (shallow clone or missing ref): {mb.stderr.strip()[:200]}"
        diff = subprocess.run(["git", "diff", "--name-status", "-M", "-z", mb.stdout.strip(), head],
                              cwd=cwd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"git failed: {exc}"
    if diff.returncode != 0:
        return None, f"git diff failed: {diff.stderr.strip()[:200]}"
    fields = [f for f in diff.stdout.split("\0") if f]
    paths, i = [], 0
    while i < len(fields):
        status = fields[i]
        n = 2 if status[:1] in {"R", "C"} else 1
        paths.extend(fields[i + 1:i + 1 + n])
        i += 1 + n
    return sorted(set(paths)), ""


def closure(targets: set[str], dependents: dict) -> set[str]:
    seen, stack = set(targets), list(targets)
    while stack:
        for dep in dependents.get(stack.pop(), []):
            if dep not in seen:
                seen.add(dep)
                stack.append(dep)
    return seen


def select(changed: list[str] | None, mapping: dict, reason_if_none: str = "") -> dict:
    full = lambda reasons: {"mode": "FULL", "targets": [], "reasons": reasons, "changed": changed or []}
    if changed is None:
        return full([reason_if_none or "change set unknown"])
    if not changed:
        return full(["empty change set: cannot prove nothing needs testing"])
    triggers = list(DEFAULT_FULL_TRIGGERS) + list(mapping.get("full_triggers", []))
    reasons, targets = [], set()
    for path in changed:
        hit = next((t for t in triggers if fnmatch.fnmatchcase(path, t)), None)
        if hit:
            reasons.append(f"{path}: full trigger {hit!r}")
            continue
        mapped = [r for r in mapping["rules"] if any(fnmatch.fnmatchcase(path, p) for p in r["paths"])]
        if not mapped:
            reasons.append(f"{path}: no rule maps it")
            continue
        for r in mapped:
            targets.update(r["targets"])
    if reasons:
        return full(reasons)
    return {"mode": "SELECTED", "targets": sorted(closure(targets, mapping.get("dependents", {}))),
            "reasons": [], "changed": changed}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Fail-closed affected-target selector.")
    ap.add_argument("--map", type=Path, required=True)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--base", help="base ref, e.g. origin/main (merge-base with --head is used)")
    src.add_argument("--changed", nargs="*", help="explicit changed paths instead of git")
    ap.add_argument("--head", default="HEAD")
    ap.add_argument("--cwd", type=Path, default=Path.cwd())
    args = ap.parse_args(argv)
    try:
        mapping = load_map(args.map)
    except MapError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.changed is not None:
        result = select(sorted(set(args.changed)), mapping)
    else:
        changed, why = git_changed(args.base, args.head, args.cwd)
        result = select(changed, mapping, why)
    print(json.dumps(result, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
