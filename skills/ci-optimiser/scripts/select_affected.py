#!/usr/bin/env python3
"""Fail-closed reference selector: changed files -> targets to test, or FULL.

A reference implementation for adversarial tests and for repos without a real
project graph; prefer the build tool's own graph (Nx, Turborepo, Bazel, ...)
when it exists. Returns FULL whenever it cannot prove a narrower selection is
safe: missing base, git failure, empty diff, a file matching a full trigger,
an unmapped file, or a rename crossing mapped target boundaries. Deletions
and type changes also use the conservative FULL fallback.

Map file (JSON):
  {
    "full_triggers": ["infra/**"],                 # added to DEFAULT_FULL_TRIGGERS
    "rules": [{"paths": ["packages/api/**"], "targets": ["api"]}],
    "dependents": {"api": ["web"]},                 # target -> targets that depend on it
    "known_targets": ["api", "web"]                # optional inventory, not required
  }
Globs use fnmatch: `*` also matches `/`, so "packages/api/*" covers nested files.

Usage:
  python3 scripts/select_affected.py --map ci-map.json --base origin/main [--head HEAD]
  python3 scripts/select_affected.py --map ci-map.json --changed a.ts b.ts   # no git
Exit 0 with JSON {"mode": "FULL"|"SELECTED", ...}; exit 2 on a bad map.
Exit 0 validates only the selection plan: FULL must execute all required checks,
and the caller must propagate their failures. --changed supplies paths only;
use --base for rename/deletion-aware selection.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

DEFAULT_FULL_TRIGGERS = (
    ".github/*", ".gitlab-ci.yml", ".circleci/*", "azure-pipelines.yml", "Jenkinsfile", ".buildkite/*",
    "package.json", "*/package.json", "package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "pnpm-workspace.yaml", "yarn.lock",
    "bun.lock", "bun.lockb", ".npmrc", ".yarnrc*", "uv.lock", "poetry.lock", "requirements*.txt",
    "pyproject.toml", "*/pyproject.toml", "uv.toml", "Pipfile", "requirements*.in", "pylock*.toml",
    "Cargo.lock", "Cargo.toml", "*/Cargo.toml", "Package.resolved",
    "go.mod", "*/go.mod", "go.sum", "go.work", "go.work.sum", "Gemfile.lock",
    "tsconfig*.json", "*/tsconfig*.json", "*.config.js", "*.config.cjs", "*.config.mjs", "*.config.ts",
    "nx.json", "turbo.json", "project.json", "*/project.json", "WORKSPACE*", "MODULE.bazel", "*.bzl",
    ".nvmrc", ".node-version", ".python-version", ".tool-versions", "rust-toolchain*", "mise.toml",
    "Dockerfile*", "*/Dockerfile*", "docker-compose*", "Makefile", "justfile",
    "*migrations/*", "*migration*", "*schema*", "*.proto", "*.graphql", "openapi*", "*/openapi*",
    "*generated*", "*.snap", "CODEOWNERS", "SECURITY.md", "*.env.example",
    "*security*", "*policy*", "*.lock", "AGENTS.md", "CLAUDE.md", "CONTRIBUTING*",
    "ci-map.json", "select_affected.py",
)


class MapError(ValueError):
    pass


def valid_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and "\0" not in value


def string_list(value: object, label: str, *, nonempty: bool = False) -> list[str]:
    if (not isinstance(value, list) or (nonempty and not value)
            or any(not valid_string(item) for item in value)):
        raise MapError(f"{label} must be {'a non-empty' if nonempty else 'an'} array of non-empty strings")
    return value


def validate_map(data: object) -> dict:
    """Validate nested values before any selection; cycles and dependent-only targets are valid."""
    if not isinstance(data, dict) or not isinstance(data.get("rules"), list):
        raise MapError("map must be an object with a 'rules' list")
    referenced: set[str] = set()
    for i, rule in enumerate(data["rules"]):
        if not isinstance(rule, dict):
            raise MapError(f"rules[{i}] must be an object")
        string_list(rule.get("paths"), f"rules[{i}].paths", nonempty=True)
        referenced.update(string_list(rule.get("targets"), f"rules[{i}].targets", nonempty=True))
    if not isinstance(data.get("dependents", {}), dict):
        raise MapError("'dependents' must be an object")
    for target, dependents in data.get("dependents", {}).items():
        if not valid_string(target):
            raise MapError("dependents keys must be non-empty strings")
        referenced.add(target)
        referenced.update(string_list(dependents, f"dependents[{target!r}]"))
    string_list(data.get("full_triggers", []), "full_triggers")
    if "known_targets" in data:
        inventory = string_list(data["known_targets"], "known_targets", nonempty=True)
        if len(set(inventory)) != len(inventory):
            raise MapError("known_targets must not contain duplicate names")
        unknown = referenced - set(inventory)
        if unknown:
            raise MapError(f"targets absent from known_targets: {', '.join(sorted(unknown))}")
    return data


def load_map(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MapError(f"cannot read map {path}: {exc}") from exc
    return validate_map(data)


class ChangedPaths(list[str]):
    """A list-compatible path set retaining Git status and rename pairs for the selector."""

    def __init__(self, records: list[tuple[str, tuple[str, ...]]]):
        super().__init__(sorted({path for _, paths in records for path in paths}))
        self.records = records


def relative_path(path: str) -> str | None:
    if not valid_string(path):
        return None
    try:
        path.encode("utf-8")
    except UnicodeError:
        return None
    p = PurePosixPath(path)
    return p.as_posix() if p.parts and not p.is_absolute() and ".." not in p.parts else None


def parse_git_changes(raw: bytes) -> ChangedPaths:
    """Parse complete NUL records, refusing undecodable paths instead of losing changes."""
    if not raw:
        return ChangedPaths([])
    if not raw.endswith(b"\0"):
        raise ValueError("truncated Git change record")
    fields, records, i = raw[:-1].split(b"\0"), [], 0
    while i < len(fields):
        status = fields[i].decode("ascii")
        if not re.fullmatch(r"[ADMTUXB]|[RCM](?:100|[0-9]{1,2})", status):
            raise ValueError(f"unsupported Git change status {status!r}")
        count = 2 if status[0] in {"R", "C"} else 1
        if i + count >= len(fields):
            raise ValueError("truncated Git change record")
        paths = tuple(field.decode("utf-8") for field in fields[i + 1:i + 1 + count])
        if any(relative_path(path) != path for path in paths):
            raise ValueError("invalid repository-relative path in Git change record")
        records.append((status, paths))
        i += count + 1
    return ChangedPaths(records)


def diagnostic(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace").strip()[:200]


def git_changed(base: str, head: str, cwd: Path) -> tuple[list[str] | None, str]:
    """Paths changed between merge-base(base, head) and head, both sides of renames."""
    if base.startswith("-") or head.startswith("-"):
        return None, "base and head must be refs, not Git options"
    try:
        mb = subprocess.run(["git", "merge-base", base, head], cwd=cwd, capture_output=True, timeout=30)
        if mb.returncode != 0:
            return None, f"no merge-base for {base}..{head} (shallow clone or missing ref): {diagnostic(mb.stderr)}"
        merge_base = mb.stdout.decode("ascii").strip()
        if not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", merge_base):
            return None, "Git returned an invalid merge-base"
        diff = subprocess.run(["git", "diff", "--no-ext-diff", "--no-textconv", "--no-relative",
                               "--name-status", "-M", "-z", merge_base, head, "--"],
                              cwd=cwd, capture_output=True, timeout=60)
    except (OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
        return None, f"git failed: {exc}"
    if diff.returncode != 0:
        return None, f"git diff failed: {diagnostic(diff.stderr)}"
    try:
        return parse_git_changes(diff.stdout), ""
    except (UnicodeError, ValueError) as exc:
        return None, f"cannot safely parse Git changes: {exc}"


def protected_inputs(map_path: Path, cwd: Path) -> set[str]:
    """Protect the actual map and selector, including in-repository symlink aliases."""
    root = cwd.resolve()
    try:
        proc = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=root,
                              capture_output=True, timeout=10)
        if proc.returncode == 0:
            root = Path(proc.stdout.decode("utf-8").strip()).resolve()
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        pass  # Explicit --changed also supports directories without a Git repository.
    protected = set()
    for path in (map_path, Path(__file__)):
        for candidate in (path.absolute(), path.resolve()):
            try:
                protected.add(candidate.relative_to(root).as_posix())
            except ValueError:
                pass  # An installed selector/map outside this repository is not in its diff.
    return protected


def closure(targets: set[str], dependents: dict) -> set[str]:
    seen, stack = set(targets), list(targets)
    while stack:
        for dep in dependents.get(stack.pop(), []):
            if dep not in seen:
                seen.add(dep)
                stack.append(dep)
    return seen


def mapped_targets(path: str, mapping: dict) -> set[str]:
    return {target for rule in mapping["rules"] if any(fnmatch.fnmatchcase(path, p) for p in rule["paths"])
            for target in rule["targets"]}


def select(changed: list[str] | None, mapping: dict, reason_if_none: str = "", *,
           protected_paths: set[str] | None = None) -> dict:
    validate_map(mapping)
    records = getattr(changed, "records", ())
    full = lambda reasons: {"mode": "FULL", "targets": [], "reasons": reasons, "changed": changed or []}
    if changed is None:
        return full([reason_if_none or "change set unknown"])
    if not changed:
        return full(["empty change set: cannot prove nothing needs testing"])
    normalized = [relative_path(path) for path in changed]
    if any(path is None for path in normalized):
        return full(["change set contains an invalid repository-relative or non-UTF-8 path"])
    changed = sorted(set(normalized))
    reasons, targets = [], set()
    for path in changed:
        if path in (protected_paths or set()):
            reasons.append(f"{path}: selector or map input changed")
            continue
        # Basename-only control patterns apply at every package depth.
        hit = next((t for t in DEFAULT_FULL_TRIGGERS if fnmatch.fnmatchcase(path, t)
                    or ("/" not in t and fnmatch.fnmatchcase(path.rsplit("/", 1)[-1], t))), None)
        if hit is None:
            hit = next((t for t in mapping.get("full_triggers", []) if fnmatch.fnmatchcase(path, t)), None)
        if hit:
            reasons.append(f"{path}: full trigger {hit!r}")
            continue
        selected = mapped_targets(path, mapping)
        if not selected:
            reasons.append(f"{path}: no rule maps it")
            continue
        targets.update(selected)
    for status, paths in records:
        if status[0] in {"D", "T", "U", "X", "B"}:
            reasons.append(f"{paths[0]}: change status {status} requires the conservative full suite")
        elif status[0] in {"R", "C"} and mapped_targets(paths[0], mapping) != mapped_targets(paths[1], mapping):
            reasons.append(f"{paths[0]} -> {paths[1]}: rename/copy crosses mapped target boundaries")
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
        protected = protected_inputs(args.map, args.cwd)
        if args.changed is not None:
            result = select(sorted(set(args.changed)), mapping, protected_paths=protected)
        else:
            changed, why = git_changed(args.base, args.head, args.cwd)
            result = select(changed, mapping, why, protected_paths=protected)
    except MapError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
