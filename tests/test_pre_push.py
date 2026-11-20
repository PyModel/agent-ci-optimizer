"""The local hook checks actual push refs and preserves failures and input."""
from __future__ import annotations

from contextlib import redirect_stderr
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import pre_push  # noqa: E402


def git(root, *args):
    return subprocess.check_output(["git", "-c", "user.name=Hook Test", "-c", "user.email=hook@example.invalid", *args], cwd=root, stderr=subprocess.DEVNULL).decode().strip()


def update(sha, old="0" * 40, branch="feature"):
    return f"refs/heads/{branch} {sha} refs/heads/{branch} {old}\n".encode()


class PrePushTest(unittest.TestCase):
    def test_parse_supports_multiple_refs_and_explicit_deletions(self):
        sha = "a" * 40
        raw = update(sha) + update(sha, branch="second") + b"(delete) " + b"0" * 40 + b" refs/heads/old " + b"b" * 40 + b"\n"
        self.assertEqual(pre_push.parse_updates(raw), [(sha, "0" * 40)] * 2)
        with patch.object(pre_push, "changed_paths") as checks, redirect_stderr(io.StringIO()) as log:
            pre_push.check_push(ROOT, b"(delete) " + b"0" * 40 + b" refs/heads/old " + b"b" * 40 + b"\n")
        checks.assert_not_called()
        self.assertIn("deletion-only", log.getvalue())

    def test_invalid_refs_and_paths_fail(self):
        for raw in (b"bad input", update("bad"), update("a" * 40) * 2,
                    update("a" * 40).replace(b"refs/heads/", b"refs/tags/"), update("0" * 40),
                    b"x" * (pre_push.MAX_INPUT + 1)):
            with self.assertRaises(ValueError):
                pre_push.parse_updates(raw)
        for path in ("/outside.py", "../outside.py", "a/../outside.py", "a\\outside.py"):
            with self.assertRaises(ValueError):
                pre_push.safe_path(path)
        self.assertEqual(str(pre_push.safe_path("scripts/a $(touch marker).py")), "scripts/a $(touch marker).py")

    def test_real_commit_checks_include_new_branch_and_reject_false_green(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            git(root, "init", "-q")
            name = "a $(touch marker).py"
            (root / name).write_text("value = 1\n")
            git(root, "add", "--", name)
            git(root, "commit", "-qm", "fixture")
            first = git(root, "rev-parse", "HEAD")
            with patch.object(pre_push, "repository_contracts"), redirect_stderr(io.StringIO()):
                pre_push.check_push(root, update(first))
                self.assertFalse((root / "marker").exists())
                (root / name).write_text("value =\n")
                with self.assertRaisesRegex(ValueError, "commit or stash"):
                    pre_push.check_push(root, update(first))
                git(root, "add", "--", name)
                git(root, "commit", "-qm", "invalid syntax")
                second = git(root, "rev-parse", "HEAD")
                with self.assertRaisesRegex(ValueError, "syntax failed"):
                    pre_push.check_push(root, update(second, first))
                with self.assertRaisesRegex(ValueError, "exact pushed candidate"):
                    pre_push.check_push(root, update(first))
                with self.assertRaisesRegex(ValueError, "Git evidence"):
                    pre_push.check_push(root, update(second, "b" * 40))
                (root / name).write_text("value = 2 \n")
                git(root, "add", "--", name)
                git(root, "commit", "-qm", "invalid whitespace")
                third = git(root, "rev-parse", "HEAD")
                with self.assertRaisesRegex(ValueError, "diff whitespace"):
                    pre_push.check_push(root, update(third, second))
                git(root, "checkout", "--detach", first)
                pre_push.check_push(root, update(first, third))  # Force update to a valid older candidate.

    def test_missing_actionlint_and_failing_contracts_block_push(self):
        with patch.object(pre_push, "changed_paths", return_value=("a" * 40, {".github/workflows/ci.yml"})), \
                patch.object(pre_push, "syntax_checks"), patch.object(pre_push, "repository_contracts"), \
                patch.object(pre_push.shutil, "which", return_value=None):
            with self.assertRaisesRegex(ValueError, "actionlint is required"):
                pre_push.check_push(ROOT, update("a" * 40))
        with patch.object(pre_push, "changed_paths", return_value=("a" * 40, set())), \
                patch.object(pre_push, "syntax_checks"), patch.object(pre_push, "repository_contracts", side_effect=ValueError("fixture contract failed")):
            with self.assertRaisesRegex(ValueError, "fixture contract failed"):
                pre_push.check_push(ROOT, update("a" * 40))

    @unittest.skipUnless(os.name == "posix", "executable POSIX Git hook")
    def test_executable_wrapper_preserves_stdin_arguments_and_failure(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "scripts").mkdir()
            shutil.copyfile(ROOT / "scripts/run_budget.py", root / "scripts/run_budget.py")
            hook = root / "pre-push"
            shutil.copyfile(ROOT / ".githooks/pre-push", hook)
            hook.chmod(0o755)
            child = root / "scripts/pre_push.py"
            child.write_text("import json,sys; print(json.dumps([sys.stdin.read(),sys.argv[1:]])); raise SystemExit(7)\n")
            raw = update("a" * 40).decode()
            proc = subprocess.run([str(hook), "origin", "fixture-remote"], cwd=root, input=raw,
                                  capture_output=True, text=True, timeout=3)
            self.assertEqual(proc.returncode, 7)
            self.assertEqual(json.loads(proc.stdout), [raw, ["origin", "fixture-remote"]])
            self.assertIn("budget=9s", proc.stderr)
        self.assertTrue(os.access(ROOT / ".githooks/pre-push", os.X_OK))


if __name__ == "__main__":
    unittest.main()
