"""Adversarial coverage for the reference selector's conservative selection contract."""
from __future__ import annotations

import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import select_affected as selector  # noqa: E402


MAP = {
    "rules": [
        {"paths": ["packages/api/*"], "targets": ["api"]},
        {"paths": ["packages/ui/*"], "targets": ["ui"]},
    ],
    "dependents": {"api": ["web"], "ui": ["web"], "web": ["e2e"]},
}
BROAD_MAP = {"rules": [{"paths": ["*"], "targets": ["all"]}]}


def run_cli(*args: str) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = selector.main(list(args))
    return code, stdout.getvalue(), stderr.getvalue()


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.email=selector-test@example.invalid", "-c", "user.name=Selector Test",
                    *args], cwd=cwd, capture_output=True, check=True)


def init_repo(root: Path) -> None:
    git(root, "init", "-q", "-b", "main")
    (root / "packages/ui").mkdir(parents=True)
    (root / "packages/api").mkdir(parents=True)
    (root / "packages/ui/original.txt").write_text("stable rename content\n" * 20, encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-qm", "baseline")


class SelectorMapContractTest(unittest.TestCase):
    def test_rejects_invalid_rule_values(self):
        invalid = [None, 3, False, {}, [], "", "  ", "bad\0value"]
        for field in ("paths", "targets"):
            for value in invalid:
                with self.subTest(field=field, value=value):
                    mapping = copy.deepcopy(MAP)
                    mapping["rules"][0][field] = [value]
                    with self.assertRaises(selector.MapError):
                        selector.select(["packages/api/main.py"], mapping)
        for field in ("paths", "targets"):
            for value in ([], None, "api"):
                with self.subTest(field=field, container=value):
                    mapping = copy.deepcopy(MAP)
                    mapping["rules"][0][field] = value
                    with self.assertRaises(selector.MapError):
                        selector.validate_map(mapping)

    def test_rejects_malformed_dependent_arrays_and_names(self):
        for dependents in ("web", None, 7, {"web": True}, [None], [3], [""], ["   "], [["web"]]):
            with self.subTest(dependents=dependents):
                mapping = copy.deepcopy(MAP)
                mapping["dependents"] = {"api": dependents}
                with self.assertRaises(selector.MapError):
                    selector.validate_map(mapping)
        for name in ("", " ", "bad\0key", None, 3):
            with self.subTest(name=name):
                mapping = copy.deepcopy(MAP)
                mapping["dependents"] = {name: ["web"]}
                with self.assertRaises(selector.MapError):
                    selector.validate_map(mapping)

    def test_rejects_invalid_full_trigger_values(self):
        for value in ("infra/*", None, [None], [2], [""], ["\0"]):
            with self.subTest(value=value):
                mapping = {**MAP, "full_triggers": value}
                with self.assertRaises(selector.MapError):
                    selector.validate_map(mapping)

    def test_cli_bad_map_is_nonzero_without_a_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "map.json"
            for invalid in ({**MAP, "dependents": {"api": "web"}},
                            {"rules": [{"paths": ["*"], "targets": [None]}]},
                            {"rules": [{"paths": ["*"], "targets": [3]}]}):
                with self.subTest(mapping=invalid):
                    path.write_text(json.dumps(invalid), encoding="utf-8")
                    code, stdout, stderr = run_cli("--map", str(path), "--changed", "packages/api/main.py")
                    self.assertEqual((code, stdout), (2, ""))
                    self.assertIn("error:", stderr)
            path.write_bytes(b"\xff")
            self.assertEqual(run_cli("--map", str(path), "--changed", "a")[0:2], (2, ""))

    def test_existing_maps_and_dependent_only_targets_need_no_inventory(self):
        result = selector.select(["packages/api/main.py"], MAP)
        self.assertEqual((result["mode"], result["targets"]), ("SELECTED", ["api", "e2e", "web"]))

    def test_optional_inventory_checks_all_references(self):
        mapping = {**MAP, "known_targets": ["api", "ui", "web", "e2e"]}
        self.assertEqual(selector.select(["packages/ui/button.ts"], mapping)["targets"], ["e2e", "ui", "web"])
        for inventory in ([], None, ["api", "ui"], ["api", "ui", "web", "e2e", "web"], [None]):
            with self.subTest(inventory=inventory), self.assertRaises(selector.MapError):
                selector.validate_map({**MAP, "known_targets": inventory})
        with self.assertRaises(selector.MapError):
            selector.validate_map({**mapping, "dependents": {"unknown": []}})

    def test_cycles_and_empty_dependent_lists_are_valid(self):
        mapping = {**MAP, "dependents": {"api": ["web"], "web": ["api", "e2e"], "e2e": []}}
        self.assertEqual(selector.select(["packages/api/main.py"], mapping)["targets"], ["api", "e2e", "web"])


class SelectorControlInputTest(unittest.TestCase):
    def test_nested_lockfiles_and_controls_force_full(self):
        names = ("go.sum", "go.mod", "go.work.sum", "package-lock.json", "pnpm-lock.yaml", "bun.lockb",
                 "npm-shrinkwrap.json", "requirements.txt", "requirements-dev.in", "Pipfile.lock", "uv.lock",
                 "pylock.toml", "Cargo.lock", "Package.resolved", ".npmrc", ".tool-versions",
                 "AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md", "CODEOWNERS", "select_affected.py", "ci-map.json")
        for name in names:
            with self.subTest(name=name):
                result = selector.select(["packages/api/deep/" + name], BROAD_MAP)
                self.assertEqual((result["mode"], result["targets"]), ("FULL", []))

    def test_explicit_custom_trigger_patterns_keep_their_existing_scope(self):
        mapping = {**BROAD_MAP, "full_triggers": ["special.txt"]}
        self.assertEqual(selector.select(["special.txt"], mapping)["mode"], "FULL")
        self.assertEqual(selector.select(["packages/api/special.txt"], mapping)["mode"], "SELECTED")

    def test_actual_map_path_is_protected_under_broad_rule(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "config/selection.json"
            path.parent.mkdir()
            path.write_text(json.dumps(BROAD_MAP), encoding="utf-8")
            code, stdout, _ = run_cli("--map", str(path), "--cwd", str(root), "--changed", "config/selection.json")
            result = json.loads(stdout)
            self.assertEqual((code, result["mode"], result["targets"]), (0, "FULL", []))
            self.assertIn("map input changed", result["reasons"][0])

    def test_map_protection_uses_repository_root_from_subdirectory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            path = root / "selection.json"
            path.write_text(json.dumps(BROAD_MAP), encoding="utf-8")
            code, stdout, _ = run_cli("--map", str(path), "--cwd", str(root / "packages/ui"),
                                      "--changed", "selection.json")
            self.assertEqual((code, json.loads(stdout)["mode"]), (0, "FULL"))

    @unittest.skipUnless(os.name == "posix", "symlink fixture uses POSIX semantics")
    def test_map_alias_and_resolved_path_are_both_protected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real = root / "selection.json"
            real.write_text(json.dumps(BROAD_MAP), encoding="utf-8")
            alias = root / "selection.link"
            alias.symlink_to(real)
            for changed in ("selection.json", "selection.link"):
                with self.subTest(changed=changed):
                    code, stdout, _ = run_cli("--map", str(alias), "--cwd", str(root), "--changed", changed)
                    self.assertEqual((code, json.loads(stdout)["mode"]), (0, "FULL"))

    def test_ci_paths_cannot_bypass_controls_with_dot_prefix(self):
        self.assertEqual(selector.select(["./.github/workflows/ci.yml"], BROAD_MAP)["mode"], "FULL")

    def test_invalid_explicit_paths_choose_full_not_empty_selection(self):
        for changed in (["../outside"], ["/absolute"], [""], ["\udcff"], [None], []):
            with self.subTest(changed=changed):
                result = selector.select(changed, BROAD_MAP)
                self.assertEqual((result["mode"], result["targets"]), ("FULL", []))
                self.assertTrue(result["reasons"])


class SelectorGitContractTest(unittest.TestCase):
    def test_cross_target_rename_is_full_and_preserves_both_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            git(root, "mv", "packages/ui/original.txt", "packages/api/moved.txt")
            git(root, "commit", "-qm", "cross-package rename")
            changed, why = selector.git_changed("HEAD~1", "HEAD", root)
            self.assertEqual(why, "")
            self.assertEqual(changed, ["packages/api/moved.txt", "packages/ui/original.txt"])
            result = selector.select(changed, MAP)
            self.assertEqual(result["mode"], "FULL")
            self.assertTrue(any("crosses mapped target boundaries" in reason for reason in result["reasons"]))

    def test_same_target_rename_retains_narrow_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            git(root, "mv", "packages/ui/original.txt", "packages/ui/moved.txt")
            git(root, "commit", "-qm", "same-package rename")
            changed, why = selector.git_changed("HEAD~1", "HEAD", root)
            self.assertEqual(why, "")
            result = selector.select(changed, MAP)
            self.assertEqual((result["mode"], result["targets"]), ("SELECTED", ["e2e", "ui", "web"]))

    def test_deleted_and_type_changed_paths_use_conservative_fallback(self):
        for status in (b"D", b"T", b"U", b"X", b"B"):
            with self.subTest(status=status):
                changed = selector.parse_git_changes(status + b"\0packages/ui/original.txt\0")
                self.assertEqual(selector.select(changed, MAP)["mode"], "FULL")

    def test_git_records_preserve_valid_spaces_tabs_and_newlines(self):
        raw = b"M\0packages/api/a b\tc\nd.txt\0"
        changed = selector.parse_git_changes(raw)
        self.assertEqual(changed, ["packages/api/a b\tc\nd.txt"])
        self.assertEqual(selector.select(changed, MAP)["mode"], "SELECTED")

    def test_truncated_and_unsupported_git_records_are_rejected(self):
        for raw in (b"M\0file", b"R100\0old\0", b"Z\0file\0", b"R101\0old\0new\0",
                    b"M\0../outside\0", b"M\0\0", b"M\0file\0\0"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                selector.parse_git_changes(raw)

    @unittest.skipUnless(os.name == "posix", "Git byte-filename fixture requires POSIX")
    def test_non_utf8_git_filename_falls_back_without_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            filename = os.fsencode(root / "packages/api") + b"/bad-\xff.txt"
            with open(filename, "wb") as output:
                output.write(b"nonsecret fixture\n")
            git(root, "add", ".")
            git(root, "commit", "-qm", "byte filename")
            changed, why = selector.git_changed("HEAD~1", "HEAD", root)
            self.assertIsNone(changed)
            self.assertIn("cannot safely parse Git changes", why)
            self.assertEqual(selector.select(changed, MAP, why)["mode"], "FULL")

    def test_missing_base_is_full_with_diagnostic(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            changed, why = selector.git_changed("origin/missing", "HEAD", root)
            result = selector.select(changed, MAP, why)
            self.assertEqual((result["mode"], result["targets"]), ("FULL", []))
            self.assertIn("no merge-base", result["reasons"][0])


if __name__ == "__main__":
    unittest.main()
