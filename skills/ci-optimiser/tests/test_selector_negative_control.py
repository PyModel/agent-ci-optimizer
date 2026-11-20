"""Exercise a real hidden regression through selection, test execution and the shadow CLI."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parent.parent
SELECTOR = ROOT / "scripts" / "select_affected.py"
SHADOW = ROOT / "scripts" / "evaluate_shadow.py"

RUNNER = """\
import argparse
import json
from pathlib import Path
import sys
import unittest

parser = argparse.ArgumentParser()
parser.add_argument("report")
parser.add_argument("modules", nargs="*")
args = parser.parse_args()
loader = unittest.TestLoader()
suite = (loader.loadTestsFromNames(args.modules) if args.modules else
         loader.discover(".", pattern="test_*.py"))

def test_ids(tests):
    for test in tests:
        if isinstance(test, unittest.TestSuite):
            yield from test_ids(test)
        else:
            yield test.id()

inventory = list(test_ids(suite))

class Result(unittest.TestResult):
    def __init__(self):
        super().__init__()
        self.outcomes = {}

    def addSuccess(self, test):
        super().addSuccess(test)
        self.outcomes[test.id()] = "passed"

    def addFailure(self, test, error):
        super().addFailure(test, error)
        self.outcomes[test.id()] = "failed"

    def addError(self, test, error):
        super().addError(test, error)
        self.outcomes[test.id()] = "error"

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.outcomes[test.id()] = "skipped"

result = Result()
suite.run(result)
report = {"inventory": inventory, "outcomes": result.outcomes,
          "inventory_complete": not args.modules and not loader.errors and bool(inventory)
                                and len(inventory) == len(set(inventory)),
          "complete": not loader.errors and result.testsRun == len(inventory)
                      and set(result.outcomes) == set(inventory),
          "state": "completed"}
Path(args.report).write_text(json.dumps(report), encoding="utf-8")
raise SystemExit(0 if result.wasSuccessful() else 1)
"""


class SelectorNegativeControlTest(unittest.TestCase):
    def test_hidden_source_change_fails_the_real_shadow_gate(self):
        with tempfile.TemporaryDirectory(prefix="ci-selector-control-") as directory:
            root = Path(directory)
            # Prevent an immediate same-size edit from reusing timestamp-based .pyc.
            env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}

            def command(argv, *, expected):
                result = subprocess.run(argv, cwd=root, env=env, capture_output=True,
                                        text=True, timeout=10, check=False)
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                return result

            def git(*args):
                return command(["git", "-c", "user.name=Selector Fixture",
                                "-c", "user.email=selector@example.invalid",
                                "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null",
                                *args], expected=0).stdout.strip()

            def write(name, content):
                (root / name).write_text(textwrap.dedent(content), encoding="utf-8")

            write("calculator.py", "def total(quantity):\n    return quantity * 2\n")
            write("test_smoke.py", """\
                import unittest
                from calculator import total

                class SmokeTests(unittest.TestCase):
                    def test_zero_quantity(self):
                        self.assertEqual(total(0), 0)
                """)
            write("test_checkout.py", """\
                import unittest
                from calculator import total

                class CheckoutTests(unittest.TestCase):
                    def test_order_total(self):
                        self.assertEqual(total(2), 4)
                """)
            write("run_tests.py", RUNNER)
            # Deliberately omit the calculator -> checkout dependency. The map
            # remains syntactically valid, exactly as a stale graph can be.
            mapping = {"rules": [{"paths": ["calculator.py"], "targets": ["test_smoke"]}],
                       "dependents": {}, "known_targets": ["test_smoke", "test_checkout"]}
            write("ci-map.json", json.dumps(mapping))
            git("init", "-q", "-b", "main")
            git("add", "calculator.py", "test_smoke.py", "test_checkout.py", "run_tests.py", "ci-map.json")
            git("commit", "-qm", "good control")
            baseline = git("rev-parse", "HEAD")

            def execute(label, modules, expected):
                path = root / (label + ".json")
                command([sys.executable, "run_tests.py", str(path), *modules], expected=expected)
                report = json.loads(path.read_text(encoding="utf-8"))
                self.assertTrue(report["complete"])
                # Both arms attest the actual immutable fixture revision and the
                # same source/runtime comparison context, never guessed outcomes.
                report["revision"] = git("rev-parse", "HEAD")
                context = hashlib.sha256(sys.version.encode("utf-8"))
                for source in sorted(root.glob("*.py")):
                    context.update(source.name.encode("utf-8") + b"\0" + source.read_bytes() + b"\0")
                report["context_digest"] = "sha256:" + context.hexdigest()
                return report

            def pair(selected, full):
                return {"schema": "ci-selector-shadow/1", "inventory_complete": full["inventory_complete"],
                        "inventory": full["inventory"], "selection": selected["inventory"],
                        "selected": selected, "full": full}

            def shadow(label, evidence, expected):
                path = root / (label + ".json")
                write(path.name, json.dumps(evidence))
                result = command([sys.executable, str(SHADOW), str(path)], expected=expected)
                return json.loads(result.stdout)

            with self.subTest(control="unbroken source"):
                selected = execute("good-selected", ["test_smoke"], 0)
                full = execute("good-full", [], 0)
                result = shadow("good-shadow", pair(selected, full), 0)
                self.assertEqual(result["status"], "observed_clean")
                self.assertEqual(result["recommendation"], "FULL")

            # This is an actual source change, committed and diffed by Git. The
            # downstream assertion is untouched; only the implementation breaks.
            write("calculator.py", "def total(quantity):\n    return quantity * 3\n")
            git("add", "calculator.py")
            git("commit", "-qm", "real regression hidden from selected target")
            selection = json.loads(command([sys.executable, str(SELECTOR), "--map", str(root / "ci-map.json"),
                                            "--base", baseline, "--head", "HEAD", "--cwd", str(root)],
                                           expected=0).stdout)
            self.assertEqual(selection["changed"], ["calculator.py"])
            self.assertEqual((selection["mode"], selection["targets"]), ("SELECTED", ["test_smoke"]))

            with self.subTest(control="hidden downstream failure"):
                selected = execute("broken-selected", selection["targets"], 0)
                full = execute("broken-full", [], 1)
                evidence = pair(selected, full)
                result = shadow("broken-shadow", evidence, 1)
                actual_failures = sorted(test for test, outcome in full["outcomes"].items() if outcome == "failed")
                self.assertTrue(actual_failures)
                self.assertEqual(result["status"], "missed_failure")
                self.assertEqual(result["missed_failures"], actual_failures)
                self.assertTrue(set(actual_failures).isdisjoint(selected["inventory"]))

            with self.subTest(control="complete selection sees the same regression"):
                complete = execute("broken-complete-selection", mapping["known_targets"], 1)
                result = shadow("complete-selection-shadow", pair(complete, full), 1)
                self.assertEqual(result["status"], "matched_failure")

            with self.subTest(control="incomplete evidence"):
                incomplete = copy.deepcopy(evidence)
                incomplete["full"]["complete"] = False
                result = shadow("incomplete-shadow", incomplete, 3)
                self.assertEqual(result["status"], "unknown")

            with self.subTest(control="wrong revision evidence"):
                wrong = copy.deepcopy(evidence)
                wrong["full"]["revision"] = baseline
                result = shadow("wrong-revision-shadow", wrong, 3)
                self.assertEqual(result["status"], "unknown")


if __name__ == "__main__":
    unittest.main()
