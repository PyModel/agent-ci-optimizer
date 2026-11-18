#!/usr/bin/env python3
"""Inventory a repository's CI, toolchain and test stack from file names.

Opens only package.json and GitHub workflow files, using bounded reads of
regular files within the requested root. Symlinks are not followed. Package
script bodies and workflow command/environment values are not emitted.
Everything else is inventoried by filename alone. Output is advisory JSON:
file names and line markers do not prove coverage, enforcement or readiness.

Usage: python3 scripts/discover_repo.py --root REPO > discovery.json
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import stat
import sys
from pathlib import Path

SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build",
    "target", "vendor", ".next", ".nx", ".turbo", ".ci-audit", "coverage",
    ".gradle", ".terraform", "Pods", ".cache",
}
MAX_FILES = 200_000
MAX_READ_BYTES = 1_000_000
MAX_LANGUAGE_EXAMPLES = 20

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
        "Package.resolved", "flake.lock", "actions.lock",
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
    "test_sources": ("*_test.go", "test_*.py", "*_test.py", "*.test.*", "*.spec.*", "*Test.java", "*Tests.cs"),
    "integration_candidates": (
        "tests/integration/*", "test/integration/*", "integration/*", "tests/e2e/*", "e2e/*",
        "cypress/e2e/*", "*.integration.test.*", "*.int.test.*", "*_integration_test.go",
        "playwright.config.*", "cypress.config.*",
    ),
    "suite_entrypoints": (
        "Makefile", "GNUmakefile", "justfile", "Justfile", "Taskfile.yml", "Taskfile.yaml",
        "tox.ini", "noxfile.py", "pytest.ini", "phpunit.xml*", "*.runsettings",
        "scripts/test-all*", "scripts/full-test*", "scripts/verify*", "scripts/check*",
    ),
    "validation_policy": (
        "ci-map.json", ".github/rulesets/*", ".github/*ruleset*.json", "ci/*",
        "*validation*.md", "*testing*.md", "*TESTING*.md", "*release-policy*",
    ),
    "release": (
        ".goreleaser.yml", ".goreleaser.yaml", ".releaserc*", "release.config.*",
        "release-please-config.json", ".changeset/config.json", "fastlane/Fastfile",
        "fastlane/Appfile", ".github/workflows/*release*", "scripts/release*",
    ),
    "deployment": (
        "vercel.json", "netlify.toml", "firebase.json", ".firebaserc", "fly.toml", "render.yaml",
        "app.yaml", "app.yml", "skaffold.yaml", "skaffold.yml", "Chart.yaml",
        "kustomization.yaml", "kustomization.yml", "helmfile.yaml", "helmfile.yml",
        "serverless.yml", "serverless.yaml", "*.tf", "Pulumi.yaml", "cloudbuild.yaml",
        "deploy/*", "deployment/*", ".github/workflows/*deploy*", "scripts/deploy*",
    ),
    "rollout_policy": (
        "*rollout*.yaml", "*rollout*.yml", "*rollout*.json", "*rollout*.md",
        "*rollback*.md", "*rollback*.yaml", "*rollback*.yml", "*canary*.yaml", "*canary*.yml",
        "*slo*.yaml", "*slo*.yml", "*slo*.md", "*runbook*.md", "*release-checklist*.md",
        "*readiness*.md", "argo-rollouts/*", "argocd/*", "flagger/*",
    ),
    "action_locks": ("actions.lock",),
}

# Line markers for human review, not a YAML parser or a security verdict.
# Do not emit source lines: a matching line can also contain a literal credential.
WORKFLOW_PATTERNS = {
    "pull_request_target": re.compile(r"\bpull_request_target\b"),
    "workflow_run": re.compile(r"\bworkflow_run\b"),
    "continue_on_error": re.compile(r"continue-on-error:\s*true"),
    "or_true": re.compile(r"\|\|\s*(true|:)\s*$"),
    "runs_on": re.compile(r"^\s*runs-on:\s*(.+)$"),
    "needs": re.compile(r"^\s*needs:\s*(.+)$"),
    "paths_filter": re.compile(r"^\s*(paths|paths-ignore):"),
    "if_condition": re.compile(r"^\s*if:\s*(.+)$"),
    "cache": re.compile(r"actions/cache|cache:\s*['\"]?(npm|pnpm|yarn|pip|poetry|gradle|maven)"),
    "fetch_depth": re.compile(r"^\s*fetch-depth:\s*(\S+)"),
    "blob_filter": re.compile(r"filter:\s*blob:none"),
    "permissions": re.compile(r"^\s*permissions:\s*(.*)$"),
    "secret_names": re.compile(r"secrets\.([A-Za-z0-9_]+)"),
    "concurrency": re.compile(r"^\s*concurrency:"),
    "timeout": re.compile(r"^\s*timeout-minutes:\s*(\S+)"),
    "merge_group": re.compile(r"\bmerge_group\b"),
    "ready_for_review": re.compile(r"\bready_for_review\b"),
    "background": re.compile(r"^\s*(?:-\s*)?background:\s*(true|false)?"),
    "parallel": re.compile(r"^\s*(?:-\s*)?parallel:"),
    "cache_mode": re.compile(r"^\s*cache-mode:\s*['\"]?(read|write-only|write|none)\b"),
    "environment": re.compile(r"^\s*environment:"),
    "services": re.compile(r"^\s*services:"),
    "pr_types": re.compile(r"^\s*types:\s*(.*\b(?:edited|labeled|unlabeled|assigned|review_requested)\b.*)$"),
    "draft_gate": re.compile(r"pull_request\.draft"),
    "submodules_or_lfs": re.compile(r"^\s*(?:submodules:\s*(?:true|recursive)|lfs:\s*true)\b"),
    "max_parallel": re.compile(r"^\s*max-parallel:\s*(\S+)"),
    "fail_fast": re.compile(r"^\s*fail-fast:\s*(\S+)"),
    "cancel_in_progress_false": re.compile(r"cancel-in-progress:\s*false\b"),
    "cron": re.compile(r"cron:\s*(.+)$"),
    "image_latest": re.compile(r"(?:image:\s*['\"]?|docker://)([\w./-]+:latest)\b"),
    "sleep": re.compile(r"\bsleep\s+(\d+)"),
    "id_token_write": re.compile(r"id-token:\s*write"),
    "no_cache_or_clean": re.compile(r"(--no-cache\b|\brm -rf\s+\S+)"),
}
USES = re.compile(r'''^\s*(?:-\s*)?uses:\s*(['"]?)([\w.-]+/[\w./-]+)@([^\s'"#{}\[\],]+)\1\s*$''')
USES_KEY = re.compile(r"^\s*(?:-\s*)?uses:")
SHA = re.compile(r"^[0-9a-fA-F]{40}$")
DEP_HINTS = (
    "eslint", "oxlint", "oxfmt", "@biomejs/biome", "typescript", "@typescript/native-preview",
    "vitest", "jest", "mocha", "playwright", "@playwright/test", "cypress", "nx",
    "turbo", "lerna", "husky", "lefthook", "lint-staged", "prettier", "bun-types",
    "@types/bun", "tsx", "esbuild", "vite", "next", "webpack", "rollup",
)
LANGUAGE_INPUTS = {
    "javascript/typescript": (("package.json", "deno.json", "deno.jsonc"),
                              (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts")),
    "python": (("pyproject.toml", "setup.py", "requirements*.txt", "uv.lock", "poetry.lock"), (".py", ".pyi")),
    "go": (("go.mod",), (".go",)),
    "rust": (("Cargo.toml",), (".rs",)),
    "jvm": (("pom.xml", "build.gradle", "build.gradle.kts"), (".java", ".kt", ".kts", ".scala")),
    "ruby": (("Gemfile",), (".rb",)),
    "php": (("composer.json",), (".php",)),
    "swift": (("Package.swift",), (".swift",)),
    "elixir": (("mix.exs",), (".ex", ".exs")),
    "dotnet": (("*.csproj", "*.fsproj", "*.vbproj"), (".cs", ".fs", ".fsx", ".vb")),
    "c/c++": ((), (".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".hxx")),
}


def walk(root: Path, issues: list[dict] | None = None) -> tuple[list[str], bool]:
    files: list[str] = []

    def skipped(path: Path, reason: str) -> None:
        if issues is not None:
            issues.append({"path": os.path.relpath(path, root).replace(os.sep, "/"), "reason": reason})

    try:
        if not stat.S_ISDIR(root.lstat().st_mode):
            skipped(root, "root is not a regular directory (symlinks are not followed)")
            return files, False
    except OSError:
        skipped(root, "root metadata unavailable")
        return files, False

    def walk_error(exc: OSError) -> None:
        skipped(Path(exc.filename) if exc.filename else root, "directory unreadable")

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False, onerror=walk_error):
        kept = []
        for name in sorted(dirnames):
            directory = Path(dirpath) / name
            if name in SKIP_DIRS:
                continue
            try:
                if not stat.S_ISDIR(directory.lstat().st_mode):
                    skipped(directory, "symlink or non-directory excluded")
                    continue
            except OSError:
                skipped(directory, "directory metadata unavailable")
                continue
            kept.append(name)
        dirnames[:] = kept
        for f in sorted(filenames):
            path = Path(dirpath) / f
            try:
                if not stat.S_ISREG(path.lstat().st_mode):
                    skipped(path, "symlink or nonregular file excluded")
                    continue
            except OSError:
                skipped(path, "file metadata unavailable")
                continue
            files.append(os.path.relpath(os.path.join(dirpath, f), root).replace(os.sep, "/"))
            if len(files) >= MAX_FILES:
                return files, True
    return files, False


def matches(rel: str, pattern: str) -> bool:
    if "/" in pattern:
        return fnmatch.fnmatchcase(rel, pattern) or fnmatch.fnmatchcase(rel, "*/" + pattern)
    return fnmatch.fnmatchcase(rel.rsplit("/", 1)[-1], pattern)


def read_text(path: Path, root: Path | None = None) -> str | None:
    """Bounded, descriptor-relative read; unsupported safe access stays unknown.

    Opening each path component without following symlinks also covers a file
    replaced between inventory and reading. No repository code is executed.
    """
    root = Path(os.path.abspath(root if root is not None else path.parent))
    path = Path(os.path.abspath(path))
    descriptors: list[int] = []
    try:
        relative = path.relative_to(root)
        if not relative.parts or any(part in {".", ".."} for part in relative.parts):
            return None
        if os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
            return None
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        descriptors.append(os.open(root.anchor, directory_flags))
        for part in root.parts[1:]:
            descriptors.append(os.open(part, directory_flags, dir_fd=descriptors[-1]))
        for part in relative.parts[:-1]:
            descriptors.append(os.open(part, directory_flags, dir_fd=descriptors[-1]))
        fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0),
                     dir_fd=descriptors[-1])
        descriptors.append(fd)
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_READ_BYTES:
            return None
        with os.fdopen(os.dup(fd), "rb") as source:
            data = source.read(MAX_READ_BYTES + 1)
        return data.decode("utf-8", errors="replace") if len(data) <= MAX_READ_BYTES else None
    except (OSError, ValueError):
        return None
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def package_meta(path: Path, root: Path | None = None) -> dict:
    text = read_text(path, root)
    if text is None:
        return {"error": "unreadable, too large, or safe no-follow access unavailable"}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return {"error": f"invalid JSON: {exc.msg}"}
    if not isinstance(data, dict):
        return {"error": "package.json must contain an object"}
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
    on pull_request: possible duplicate same-repo PR work. Line heuristics, not YAML."""
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


def marker_value(key: str, match: re.Match) -> str:
    """Keep useful scalars, never an arbitrary matching source/command line."""
    if key in {"if_condition", "permissions", "pr_types"}:
        return "present; inspect source"
    if key == "no_cache_or_clean":
        return "--no-cache" if "--no-cache" in match.group(0) else "rm -rf"
    if match.groups() and match.group(1):
        value = match.group(1).strip()
        if key in {"runs_on", "needs", "cron"} and ("${{" in value or ";" in value or "$" in value or "`" in value):
            return "expression or unparsed value; inspect source"
        return value[:200]
    return key


def workflow_meta(path: Path, root: Path | None = None) -> dict:
    text = read_text(path, root)
    if text is None:
        return {"error": "unreadable, too large, or safe no-follow access unavailable"}
    hits: dict[str, list] = {}
    pins = [0, 0]  # [pinned to a full SHA, total remote `uses:`]
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.split(" #", 1)[0] if not line.lstrip().startswith("#") else ""
        if not stripped.strip():
            continue
        m = USES.fullmatch(stripped)
        if m:
            action, reference = m.group(2), m.group(3)
            pins[1] += 1
            pins[0] += bool(SHA.fullmatch(reference))
            if not SHA.fullmatch(reference):
                hits.setdefault("unpinned_action", []).append({"line": lineno, "value": f"{action}@{reference}"[:200]})
            if action == "actions/checkout":
                hits.setdefault("checkout", []).append({"line": lineno, "value": reference[:200]})
        elif USES_KEY.match(stripped):
            hits.setdefault("unresolved_action_reference", []).append({"line": lineno, "value": "not a resolved remote action scalar"})
        for key, rx in WORKFLOW_PATTERNS.items():
            for m in rx.finditer(stripped):
                hits.setdefault(key, []).append({"line": lineno, "value": marker_value(key, m)})
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


def language_evidence(files: list[str]) -> dict:
    """Inspect filename signals, including source-only repos; do not guess tools."""
    evidence = {}
    for language, (patterns, extensions) in sorted(LANGUAGE_INPUTS.items()):
        manifests = [path for path in files if any(matches(path, pattern) for pattern in patterns)]
        sources = [path for path in files if extensions and path.endswith(extensions)]
        if manifests or sources:
            evidence[language] = {
                "manifest_examples": manifests[:MAX_LANGUAGE_EXAMPLES],
                "source_examples": sources[:MAX_LANGUAGE_EXAMPLES],
                "inventoried_source_files": len(sources),
                "examples_truncated": len(manifests) > MAX_LANGUAGE_EXAMPLES or len(sources) > MAX_LANGUAGE_EXAMPLES,
                "meaning": "filename evidence only; toolchain and validation commands unconfirmed",
            }
    return evidence


def provider_for(path: str) -> str:
    """Name the provider from the config path, without interpreting its gates."""
    checks = (
        ("github_actions", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
        ("gitlab_ci", (".gitlab-ci.yml",)),
        ("circleci", (".circleci/config.yml",)),
        ("azure_pipelines", ("azure-pipelines.yml",)),
        ("jenkins", ("Jenkinsfile",)),
        ("buildkite", (".buildkite/*.yml",)),
        ("bitbucket_pipelines", ("bitbucket-pipelines.yml",)),
        ("travis_ci", (".travis.yml",)),
        ("drone", (".drone.yml",)),
        ("aws_codebuild", ("buildspec.yml",)),
        ("google_cloud_build", ("cloudbuild.yaml",)),
        ("woodpecker", (".woodpecker.yml",)),
    )
    return next((name for name, patterns in checks if any(matches(path, pattern) for pattern in patterns)), "unknown")


def adapter_candidates(files: list[str], packages: dict, found: dict) -> list[dict]:
    """Route positive evidence to an adapter without reading or running more code."""
    definitions = (
        ("go", (), ("go.mod", "go.work", "*_test.go"), "go"),
        ("nextjs", ("next",), ("next.config.*",), "nextjs"),
        ("vitest", ("vitest",), ("vitest.config.*", "vitest.workspace.*"), "vitest-transform-and-worker-reuse"),
        ("playwright", ("playwright", "@playwright/test"), ("playwright.config.*",), "playwright"),
        ("typed-lint", ("typescript", "oxlint", "@typescript/native-preview"),
         ("tsconfig*.json", ".oxlintrc.json", "oxlint.config.*"), "typed-lint-and-typescript"),
        ("formatting", ("prettier", "oxfmt", "@biomejs/biome"),
         (".prettierrc*", "prettier.config.*", ".oxfmtrc*", "biome.json*"), "formatting"),
        ("remote-task-cache", ("nx", "turbo"), ("nx.json", "turbo.json"), "nx-and-turborepo-remote-caches"),
        ("docker", (), CATEGORIES["containers"], "docker-and-buildkit"),
    )
    candidates = []
    for name, dependencies, patterns, anchor in definitions:
        evidence = [{"path": path, "basis": "filename"} for path in files
                    if any(matches(path, pattern) for pattern in patterns)]
        for path, package in sorted(packages.items()):
            matched = sorted(set(package.get("tooling_deps", [])) & set(dependencies))
            if matched:
                evidence.append({"path": path, "basis": "dependency names", "dependencies": matched})
        if evidence:
            candidates.append({"adapter": name, "status": "candidate_requires_review",
                               "evidence": evidence[:MAX_LANGUAGE_EXAMPLES],
                               "evidence_truncated": len(evidence) > MAX_LANGUAGE_EXAMPLES,
                               "version_support": "unknown", "measured_benefit": "unknown",
                               "reference": f"references/10-stack-adapters.md#{anchor}"})
    if found["ci"]:
        candidates.append({"adapter": "runner-and-artifact-transport", "status": "candidate_requires_review",
                           "evidence": [{"path": path, "basis": "CI filename"} for path in found["ci"][:MAX_LANGUAGE_EXAMPLES]],
                           "version_support": "unknown", "measured_benefit": "unknown",
                           "reference": "references/10-stack-adapters.md#artifact-transport"})
    return candidates


def validation_topology(found: dict, packages: dict, workflows: dict, truncated: bool, issues: list[dict]) -> dict:
    """Locate evidence for an agent's assessment; never assign checks by name.

    The four questions are assessment targets, not four required workflows.
    Neither a file, a script name nor a workflow marker answers them alone.
    """
    evidence_files = {
        "hooks_and_rules": sorted(set(found["hooks"] + found["lint_typecheck"] + found["policy"])),
        "test_configuration_and_sources": sorted(set(found["test_config"] + found["test_sources"])),
        "dependency_graph_inputs": sorted(set(found["monorepo"] + found["manifests"] + found["lockfiles"])),
        "integration_candidates": found["integration_candidates"],
        "full_suite_entrypoint_candidates": found["suite_entrypoints"],
        "workflow_definitions": found["ci"],
        "validation_policy_candidates": found["validation_policy"],
        "release_candidates": found["release"],
        "deployment_candidates": found["deployment"],
        "rollout_policy_candidates": found["rollout_policy"],
        "action_lockfiles": found["action_locks"],
    }
    providers: dict[str, list[str]] = {}
    for path in found["ci"]:
        providers.setdefault(provider_for(path), []).append(path)
    markers = []
    for path, metadata in sorted(workflows.items()):
        for name, values in sorted(metadata.items()):
            if isinstance(values, list) and values and all(isinstance(v, dict) and "line" in v for v in values):
                markers.append({"path": path, "marker": name, "lines": sorted({v["line"] for v in values}),
                                "meaning": "text occurrence only; semantics unconfirmed"})
    unknowns = [
        "Commands, conditions, reusable/composite workflows, matrices and provider includes are not expanded.",
        "Category assignments and correctness/coverage equivalence require inspection of the actual checks.",
        "Required-check rules, branch policy, merge-queue configuration and enforcement are unknown.",
        "Dependency-graph completeness, affected selection and full-suite coverage are unknown.",
        "Deployment approvals, artifact provenance, rollout/rollback policy and release readiness are unknown.",
        "Runner capacity, timings, reliability, validation budgets and cost require telemetry.",
        "File or marker absence does not prove that a capability or gate is absent; configuration may be external.",
        "Action pin ratios describe recognized direct scalars only; lockfiles and transitive references are not resolved.",
    ]
    content_errors = [{"path": path, "error": meta["error"]}
                      for path, meta in sorted({**packages, **workflows}.items()) if "error" in meta]
    return {
        "schema": "validation-topology/1",
        "status": "needs_review",
        "required_questions": {
            "rules": {"question": "Does the code follow the repository's rules?", "status": "unknown"},
            "change": {"question": "Does this change work within its actual dependency and risk scope?", "status": "unknown"},
            "integration": {"question": "Does the integrated main branch work?", "status": "unknown"},
            "release": {"question": "Is the product ready to release under this repository's policy?", "status": "unknown"},
        },
        "category_assignments": "unknown",
        "evidence_files": evidence_files,
        "package_script_names": [{"path": path, "names": meta.get("scripts", [])}
                                 for path, meta in sorted(packages.items()) if "error" not in meta],
        "workflow_markers": markers,
        "providers": [{"provider": provider, "files": sorted(paths),
                       "analysis": "line_markers_only" if any(p in workflows and "error" not in workflows[p] for p in paths) else "filenames_only",
                       "inspected_files": sorted(p for p in paths if p in workflows and "error" not in workflows[p]),
                       "expanded": False} for provider, paths in sorted(providers.items())],
        "inventory": {"truncated": truncated, "excluded_or_unreadable": issues, "content_errors": content_errors,
                      "coverage": "partial" if truncated or issues or content_errors else "filename_inventory_only"},
        "unknowns": unknowns,
        "next_step": "Inspect the evidence, expand the real check graph and confirm policy and telemetry before designing validation placement.",
    }


def discover(root: Path) -> dict:
    root = Path(os.path.abspath(root))
    issues: list[dict] = []
    files, truncated = walk(root, issues)
    found: dict[str, list[str]] = {c: [] for c in CATEGORIES}
    for rel in files:
        for cat, pats in CATEGORIES.items():
            if any(matches(rel, p) for p in pats):
                found[cat].append(rel)
    packages = {
        rel: package_meta(root / rel, root)
        for rel in found["manifests"] if rel.rsplit("/", 1)[-1] == "package.json"
    }
    workflows = {
        rel: workflow_meta(root / rel, root) for rel in found["ci"] if rel.startswith(".github/workflows/")
    }
    detected_languages = language_evidence(files)
    languages = sorted(detected_languages)
    js = "javascript/typescript" in languages
    return {
        "schema": "ci-discovery/1",
        "root": str(root),
        "file_count": len(files),
        "truncated": truncated,
        "languages": languages,
        "language_evidence": detected_languages,
        "found": found,
        "package_json": packages,
        "github_workflows": workflows,
        "adapter_candidates": adapter_candidates(files, packages, found),
        "validation_topology": validation_topology(found, packages, workflows, truncated, issues),
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
            "Validation topology is advisory: names and markers do not establish category, coverage, permission or release readiness.",
            "Content reads require descriptor-relative no-follow access; unsupported platforms leave contents unknown.",
            f"Directories pruned: {sorted(SKIP_DIRS)}; only package.json and GitHub workflow files are opened.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", required=True, type=Path, help="repository root to inspect")
    args = ap.parse_args(argv)
    if not args.root.is_dir() or args.root.is_symlink():
        print(f"error: --root {args.root} must be a directory, not a symlink", file=sys.stderr)
        return 2
    json.dump(discover(args.root), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
