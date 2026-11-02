#!/usr/bin/env python3
"""Inventory a repository's CI, toolchain and test stack from file names.

Opens only two kinds of file: package.json (script names, dependency names)
and .github/workflows/*.yml (lines matching fixed risk/perf patterns; secret
*names* only). Everything else is seen by name alone, so .env files, keys and
credentials are never read. Output is JSON on stdout; every finding is a hint
to verify by hand, not a conclusion.

Usage: python3 scripts/discover_repo.py --root REPO > discovery.json
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import sys
from pathlib import Path

SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build",
    "target", "vendor", ".next", ".nx", ".turbo", ".ci-audit", "coverage",
    ".gradle", ".terraform", "Pods", ".cache",
}
MAX_FILES = 200_000
MAX_READ_BYTES = 1_000_000

CATEGORIES: dict[str, tuple[str, ...]] = {
    "ci": (
        ".github/workflows/*.yml", ".github/workflows/*.yaml", ".gitlab-ci.yml",
        ".circleci/config.yml", "azure-pipelines.yml", "Jenkinsfile",
        ".buildkite/*.yml", "bitbucket-pipelines.yml", ".travis.yml",
        ".drone.yml", "buildspec.yml", "cloudbuild.yaml", ".woodpecker.yml",
    ),
    "lockfiles": (
        "package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock",
        "bun.lock", "bun.lockb", "uv.lock", "poetry.lock", "Pipfile.lock",
        "pdm.lock", "requirements*.txt", "Cargo.lock", "go.sum", "Gemfile.lock",
        "composer.lock", "gradle.lockfile", "packages.lock.json", "mix.lock",
        "Package.resolved", "flake.lock",
    ),
    "manifests": (
        "package.json", "pyproject.toml", "setup.py", "Cargo.toml", "go.mod",
        "pom.xml", "build.gradle", "build.gradle.kts", "Gemfile", "composer.json",
        "*.csproj", "mix.exs", "Package.swift", "deno.json", "deno.jsonc",
    ),
    "toolchain_pins": (
        ".nvmrc", ".node-version", ".python-version", ".tool-versions",
        "rust-toolchain", "rust-toolchain.toml", ".go-version", "mise.toml",
        ".mise.toml", "global.json", ".bun-version",
    ),
    "monorepo": (
        "nx.json", "turbo.json", "pnpm-workspace.yaml", "lerna.json", "rush.json",
        "WORKSPACE", "WORKSPACE.bazel", "MODULE.bazel", "pants.toml", "BUCK",
        "settings.gradle", "settings.gradle.kts", "moon.yml", ".moon/workspace.yml",
    ),
    "lint_typecheck": (
        "eslint.config.*", ".eslintrc*", ".oxlintrc.json", "oxlint.json",
        "biome.json", "biome.jsonc", "tsconfig*.json", ".prettierrc*",
        "ruff.toml", ".ruff.toml", "mypy.ini", ".flake8", ".golangci.yml",
        ".golangci.yaml", "clippy.toml", ".rubocop.yml", "dprint.json",
    ),
    "test_config": (
        "vitest.config.*", "vitest.workspace.*", "jest.config.*",
        "playwright.config.*", "cypress.config.*", "pytest.ini", "tox.ini",
        "noxfile.py", "conftest.py", "karma.conf.*", ".mocharc*",
    ),
    "hooks": (
        ".husky/*", "lefthook.yml", "lefthook.yaml", ".pre-commit-config.yaml",
        ".lintstagedrc*", "lint-staged.config.*", ".simple-git-hooks*",
    ),
    "policy": (
        "AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md", "CODEOWNERS",
        ".github/CODEOWNERS", "SECURITY.md", ".github/dependabot.yml",
        "renovate.json", ".github/renovate.json",
    ),
    "containers": ("Dockerfile", "Dockerfile.*", "*.Dockerfile", "docker-compose*.yml", "compose*.yaml"),
}

# Workflow lines worth a human look. Values printed are config, never secret values:
# `secrets.NAME` captures the name only.
WORKFLOW_PATTERNS = {
    "pull_request_target": re.compile(r"\bpull_request_target\b"),
    "workflow_run": re.compile(r"\bworkflow_run\b"),
    "continue_on_error": re.compile(r"continue-on-error:\s*true"),
    "or_true": re.compile(r"\|\|\s*(true|:)\s*$"),
    "runs_on": re.compile(r"runs-on:\s*(.+)$"),
    "needs": re.compile(r"^\s*needs:\s*(.+)$"),
    "paths_filter": re.compile(r"^\s*(paths|paths-ignore):"),
    "if_condition": re.compile(r"^\s*if:\s*(.+)$"),
    "cache": re.compile(r"actions/cache|cache:\s*['\"]?(npm|pnpm|yarn|pip|poetry|gradle|maven)"),
    "checkout": re.compile(r"actions/checkout@(\S+)"),
    "fetch_depth": re.compile(r"fetch-depth:\s*(\S+)"),
    "blob_filter": re.compile(r"filter:\s*blob:none"),
    "permissions": re.compile(r"^\s*permissions:\s*(.*)$"),
    "secret_names": re.compile(r"secrets\.([A-Za-z0-9_]+)"),
    "concurrency": re.compile(r"^\s*concurrency:"),
    "timeout": re.compile(r"timeout-minutes:\s*(\S+)"),
    "unpinned_action": re.compile(r"uses:\s*([\w.-]+/[\w./-]+)@(?![0-9a-f]{40}\b)(\S+)"),
    "merge_group": re.compile(r"\bmerge_group\b"),
    "pr_types": re.compile(r"^\s*types:\s*(.*\b(?:edited|labeled|unlabeled|assigned|review_requested)\b.*)$"),
    "draft_gate": re.compile(r"pull_request\.draft"),
    "submodules_or_lfs": re.compile(r"^\s*(?:submodules:\s*(?:true|recursive)|lfs:\s*true)\b"),
    "max_parallel": re.compile(r"max-parallel:\s*(\S+)"),
    "fail_fast": re.compile(r"fail-fast:\s*(\S+)"),
    "cancel_in_progress_false": re.compile(r"cancel-in-progress:\s*false\b"),
    "cron": re.compile(r"cron:\s*(.+)$"),
    "image_latest": re.compile(r"(?:image:\s*['\"]?|docker://)([\w./-]+:latest)\b"),
    "sleep": re.compile(r"\bsleep\s+(\d+)"),
    "id_token_write": re.compile(r"id-token:\s*write"),
    "no_cache_or_clean": re.compile(r"(--no-cache\b|\brm -rf\s+\S+)"),
}
USES = re.compile(r"uses:\s*([\w.-]+/[\w./-]+)@(\S+)")
SHA = re.compile(r"^[0-9a-f]{40}$")
DEP_HINTS = (
    "eslint", "oxlint", "@biomejs/biome", "typescript", "@typescript/native-preview",
    "vitest", "jest", "mocha", "playwright", "@playwright/test", "cypress", "nx",
    "turbo", "lerna", "husky", "lefthook", "lint-staged", "prettier", "bun-types",
    "@types/bun", "tsx", "esbuild", "vite", "webpack", "rollup",
)


def walk(root: Path) -> tuple[list[str], bool]:
    files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not os.path.islink(os.path.join(dirpath, d)))
        for f in sorted(filenames):
            files.append(os.path.relpath(os.path.join(dirpath, f), root).replace(os.sep, "/"))
            if len(files) >= MAX_FILES:
                return files, True
    return files, False


def matches(rel: str, pattern: str) -> bool:
    if "/" in pattern:
        return fnmatch.fnmatchcase(rel, pattern) or fnmatch.fnmatchcase(rel, "*/" + pattern)
    return fnmatch.fnmatchcase(rel.rsplit("/", 1)[-1], pattern)


def read_text(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_READ_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def package_meta(path: Path) -> dict:
    text = read_text(path)
    if text is None:
        return {"error": "unreadable or too large"}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return {"error": f"invalid JSON: {exc.msg}"}
    deps: set[str] = set()
    for key in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
        if isinstance(data.get(key), dict):
            deps.update(data[key])
    return {
        "scripts": sorted(data["scripts"]) if isinstance(data.get("scripts"), dict) else [],
        "workspaces": bool(data.get("workspaces")),
        "packageManager": data.get("packageManager") if isinstance(data.get("packageManager"), str) else None,
        "tooling_deps": sorted(d for d in deps if d in DEP_HINTS),
    }


ON_FLOW = re.compile(r"""^['"]?on['"]?:\s*(\[.*\]|\w+)\s*$""")
PUSH_KEY = re.compile(r"^(\s*)push:\s*(\{\s*\}|null|~)?\s*$")
PR_EVENT = re.compile(r"\bpull_request\b(?!_target)")
PUSH_FILTER = re.compile(r"^\s*(branches|branches-ignore|tags|tags-ignore):")


def unfiltered_push(lines: list[str]) -> int | None:
    """Line of a `push` trigger with no branch/tag filter in a workflow that also runs
    on pull_request: every PR commit then runs twice. Line heuristics, not YAML."""
    if not any(PR_EVENT.search(l.split("#", 1)[0]) for l in lines):
        return None
    for i, line in enumerate(lines):
        flow = ON_FLOW.match(line)
        if flow and re.search(r"\bpush\b", flow.group(1)):
            return i + 1
        key = PUSH_KEY.match(line)
        if not key:
            continue
        indent, children = len(key.group(1)), []
        for child in lines[i + 1:]:
            if child.strip() and len(child) - len(child.lstrip()) <= indent:
                break
            children.append(child)
        if not any(PUSH_FILTER.match(c) for c in children):
            return i + 1
    return None


def workflow_meta(path: Path) -> dict:
    text = read_text(path)
    if text is None:
        return {"error": "unreadable or too large"}
    hits: dict[str, list] = {}
    pins = [0, 0]  # [pinned to a full SHA, total remote `uses:`]
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.split(" #", 1)[0] if not line.lstrip().startswith("#") else ""
        if not stripped.strip():
            continue
        for m in USES.finditer(stripped):
            pins[1] += 1
            pins[0] += bool(SHA.match(m.group(2)))
        for key, rx in WORKFLOW_PATTERNS.items():
            for m in rx.finditer(stripped):
                value = m.group(1).strip() if m.groups() and m.group(1) else stripped.strip()
                if key == "unpinned_action":
                    value = f"{m.group(1)}@{m.group(2)}"
                hits.setdefault(key, []).append({"line": lineno, "value": value[:200]})
    if "secret_names" in hits:
        hits["secret_names"] = sorted({h["value"] for h in hits["secret_names"]})
    push_line = unfiltered_push(text.splitlines())
    if push_line:
        hits["push_and_pr_unfiltered"] = [{"line": push_line, "value": "push without branches/tags + pull_request"}]
    if pins[1]:
        hits["action_pins"] = {"pinned": pins[0], "total": pins[1]}
    return hits


def pinned_ratio(workflows: dict) -> dict | str:
    pinned = sum(w.get("action_pins", {}).get("pinned", 0) for w in workflows.values())
    total = sum(w.get("action_pins", {}).get("total", 0) for w in workflows.values())
    return {"pinned": pinned, "total": total, "value": round(pinned / total, 4)} if total else "unknown"


def discover(root: Path) -> dict:
    files, truncated = walk(root)
    found: dict[str, list[str]] = {c: [] for c in CATEGORIES}
    for rel in files:
        for cat, pats in CATEGORIES.items():
            if any(matches(rel, p) for p in pats):
                found[cat].append(rel)
    packages = {
        rel: package_meta(root / rel)
        for rel in found["manifests"] if rel.rsplit("/", 1)[-1] == "package.json"
    }
    workflows = {
        rel: workflow_meta(root / rel) for rel in found["ci"] if rel.startswith(".github/workflows/")
    }
    names = {rel.rsplit("/", 1)[-1] for rel in files}
    languages = sorted({
        lang for lang, marks in {
            "javascript/typescript": ("package.json", "deno.json"),
            "python": ("pyproject.toml", "setup.py", "requirements.txt", "uv.lock", "poetry.lock"),
            "rust": ("Cargo.toml",), "go": ("go.mod",),
            "jvm": ("pom.xml", "build.gradle", "build.gradle.kts"),
            "ruby": ("Gemfile",), "php": ("composer.json",), "swift": ("Package.swift",),
            "elixir": ("mix.exs",),
        }.items() if names & set(marks)
    } | ({"dotnet"} if any(n.endswith(".csproj") for n in names) else set()))
    js = "javascript/typescript" in languages
    return {
        "schema": "ci-discovery/1",
        "root": str(root.resolve()),
        "file_count": len(files),
        "truncated": truncated,
        "languages": languages,
        "found": found,
        "package_json": packages,
        "github_workflows": workflows,
        "hints": {
            "js_modules_applicable": js,
            "project_graph_present": bool(found["monorepo"]),
            "hooks_present": bool(found["hooks"]),
            "ci_provider_count": len({p.split("/")[0] for p in found["ci"]}),
            "pinned_action_ratio": pinned_ratio(workflows),
        },
        "limitations": [
            "File-name and regex heuristics only; verify every finding by reading the file.",
            "Workflow YAML is not parsed: anchors, reusable workflows and matrix expansion are not resolved.",
            f"Directories pruned: {sorted(SKIP_DIRS)}; only package.json and GitHub workflow files are opened.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", required=True, type=Path, help="repository root to inspect")
    args = ap.parse_args(argv)
    if not args.root.is_dir():
        print(f"error: --root {args.root} is not a directory", file=sys.stderr)
        return 2
    json.dump(discover(args.root), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
