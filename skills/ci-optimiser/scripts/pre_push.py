#!/usr/bin/env python3
"""Fast local code rules for this stdlib skill; the full suite remains CI's gate.

Invoked by .githooks/pre-push under run_budget.py's strict nine-second limit.
Consume Git's ref-update stdin exactly once. Check committed diff whitespace,
changed Python syntax, existing packaging/feature-register contracts, and changed
workflows with installed actionlint. Syntax compilation is NOT static typechecking;
this repository has no configured Python formatter or static typechecker.

Support branch updates whose candidate is the clean current HEAD, including new
branches (whole tree), force updates and multiple destinations for that HEAD.
Reject unavailable bases, other candidate revisions, tags and malformed input.
Deletion-only/no-update pushes have no source candidate and are reported explicitly.
Never fetch, install tools, rewrite files, retry checks, or expand paths in a shell.
"""
from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import unittest

BUNDLE_ROOT = Path(__file__).resolve().parent.parent
BUNDLE_PATH = Path("skills/ci-optimiser")
MAX_INPUT = 1024 * 1024
MAX_SOURCE = 2 * 1024 * 1024
SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
CONTRACTS = ("test_skill_docs.py", "test_feature_matrix.py")


def git(root, *args, data=b""):
    result = subprocess.run(["git", "--no-optional-locks", "-c", "core.fsmonitor=false", *args],
                            cwd=root, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise ValueError("Git evidence/check failed; verify local commits and diff whitespace")
    return result.stdout


def source_repository():
    """Bind this maintainer hook to its source checkout, never an installed copy."""
    root = Path(git(Path.cwd(), "rev-parse", "--show-toplevel").decode("utf-8").strip()).resolve()
    if (root / BUNDLE_PATH).resolve() != BUNDLE_ROOT:
        raise ValueError("run the pre-push hook from this skill's source checkout, not an installed copy")
    return root


def parse_updates(raw):
    if len(raw) > MAX_INPUT:
        raise ValueError("hook input exceeds its limit")
    records, destinations = [], set()
    for line in raw.splitlines():
        fields = line.split()
        if len(fields) != 4:
            raise ValueError("malformed pre-push ref input")
        local, sha, remote, old = [item.decode("utf-8") for item in fields]
        if not SHA.fullmatch(sha) or not SHA.fullmatch(old) or len(sha) != len(old):
            raise ValueError("invalid pushed commit identity")
        if set(sha) == {"0"} and (local != "(delete)" or set(old) == {"0"}):
            raise ValueError("invalid branch-deletion evidence")
        if not remote.startswith("refs/heads/") or (set(sha) != {"0"} and not local.startswith("refs/heads/")):
            raise ValueError("this hook supports branch updates only")
        if remote in destinations:
            raise ValueError("duplicate destination ref")
        destinations.add(remote)
        if set(sha) != {"0"}:
            records.append((sha, old))
    return records


def safe_path(name):
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts or ".." in path.parts or "\\" in name or path.as_posix() != name:
        raise ValueError("unsupported changed path")
    return path


def changed_paths(root, records):
    head = git(root, "rev-parse", "--verify", "HEAD^{commit}").decode("ascii").strip()
    if any(sha != head for sha, _ in records):
        raise ValueError("check out the exact pushed candidate; all updated branches must point to HEAD")
    if git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all"):
        raise ValueError("commit or stash local changes before checking the pushed candidate")
    changed = set()
    for sha, old in sorted(set(records)):
        if set(old) == {"0"}:
            base = git(root, "hash-object", "-t", "tree", "--stdin").decode("ascii").strip()
        else:
            git(root, "cat-file", "-e", old + "^{commit}")
            base = old
        git(root, "-c", "core.whitespace=blank-at-eol,blank-at-eof,space-before-tab", "diff",
            "--no-ext-diff", "--check", base, sha, "--")
        names = git(root, "diff", "--no-ext-diff", "--no-renames", "--name-only", "--diff-filter=ACMT", "-z", base, sha, "--")
        for raw in names.split(b"\0"):
            if raw:
                name = raw.decode("utf-8")
                safe_path(name)
                changed.add(name)
    return head, changed


def syntax_checks(root, head, changed):
    entries = git(root, "ls-tree", "-r", "-z", head).split(b"\0")
    objects = {}
    for entry in entries:
        if entry:
            header, name = entry.split(b"\t", 1)
            mode, kind, oid = header.split()
            objects[name.decode("utf-8")] = (mode, kind, oid.decode("ascii"))
    for name in sorted(changed):
        mode, kind, oid = objects[name]
        if kind != b"blob" or mode not in {b"100644", b"100755"}:
            raise ValueError("changed symlinks/submodules need a separate reviewed check")
        if name.endswith(".py"):
            if int(git(root, "cat-file", "-s", oid)) > MAX_SOURCE:
                raise ValueError("changed Python file exceeds the syntax-check limit")
            source = git(root, "cat-file", "blob", oid)
            try:
                compile(source, name, "exec", dont_inherit=True)
            except (SyntaxError, ValueError) as exc:
                raise ValueError("Python syntax failed for " + json.dumps(name)) from exc


def repository_contracts(root):
    suite, loader = unittest.TestSuite(), unittest.TestLoader()
    for pattern in CONTRACTS:
        tests = loader.discover(str(root / BUNDLE_PATH / "tests"), pattern=pattern)
        if tests.countTestCases() == 0:
            raise ValueError("required repository contract tests are missing")
        suite.addTests(tests)
    if not unittest.TextTestRunner(stream=sys.stderr).run(suite).wasSuccessful():
        raise ValueError("repository contracts failed")


def check_push(root, raw):
    records = parse_updates(raw)
    if not records:
        print("pre-push: deletion-only/no-update push; no candidate source checks apply.", file=sys.stderr)
        return
    head, changed = changed_paths(root, records)
    syntax_checks(root, head, changed)
    repository_contracts(root)
    workflows = [name for name in sorted(changed) if name.startswith(".github/workflows/") and name.endswith((".yml", ".yaml"))]
    if any(name in changed for name in (".github/actionlint.yaml", ".github/actionlint.yml")):
        workflows = [p.relative_to(root).as_posix() for p in (root / ".github/workflows").glob("*") if p.suffix in {".yml", ".yaml"}]
    if workflows:
        actionlint = shutil.which("actionlint")
        if actionlint is None:
            raise ValueError("actionlint is required for pushed workflow changes; install the repo's reviewed version")
        result = subprocess.run([actionlint, *[str(root / name) for name in workflows]], cwd=root)
        if result.returncode:
            raise ValueError("workflow lint failed")
    if git(root, "rev-parse", "HEAD").decode("ascii").strip() != head or git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all"):
        raise ValueError("candidate/worktree changed while the hook was checking it")
    print("pre-push: whitespace, Python syntax and repository contracts passed; "
          "applicable workflow lint passed. No static typecheck is configured; full CI remains required.", file=sys.stderr)


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print("pre-push: expected Git's two remote arguments and ref-update stdin.", file=sys.stderr)
        return 2
    try:
        check_push(source_repository(), sys.stdin.buffer.read(MAX_INPUT + 1))
    except (OSError, UnicodeError, ValueError, KeyError) as exc:
        message = str(exc) if type(exc) is ValueError else "could not safely read/check repository evidence"
        print("pre-push: " + message, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
