"""Behavioral checks for a strict, single-command CI duration budget."""
from __future__ import annotations

from contextlib import redirect_stderr
import io
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import run_budget  # noqa: E402


class BudgetInputTest(unittest.TestCase):
    def test_invalid_budgets_do_not_run_command(self):
        for seconds in ("0", "-1", "nan", "inf", "-inf", "1e9999", "not-a-number"):
            with self.subTest(seconds=seconds), patch.object(run_budget, "run_command") as run:
                with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exc:
                    run_budget.main(["--seconds=" + seconds, "--label", "check", "--", "unused"])
                self.assertEqual(exc.exception.code, 2)
                run.assert_not_called()

    def test_separator_and_command_are_required(self):
        for command in ([], ["--"], ["echo", "hello"]):
            with self.subTest(command=command), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exc:
                run_budget.main(["--seconds", "1", "--label", "check", *command])
            self.assertEqual(exc.exception.code, 2)

    def test_exact_boundary_is_failure_even_when_child_exits_zero(self):
        for elapsed, expected in ((math.nextafter(1.0, 0), 0), (1.0, 124), (1.0001, 124)):
            proc = Mock(pid=12345, returncode=0)
            proc.wait.return_value = 0
            with self.subTest(elapsed=elapsed), patch.object(run_budget.subprocess, "Popen", return_value=proc):
                with patch.object(run_budget, "group_alive", return_value=False), patch.object(run_budget.time, "monotonic", side_effect=[0.0, 0.0, elapsed]):
                    result = run_budget.run_command(["unused"], 1, "boundary")
            self.assertEqual(result["exit_code"], expected)
            self.assertEqual(result["over_budget"], elapsed >= 1)
            self.assertEqual(result["command_exit"], 0)

    def test_signal_exit_never_becomes_success(self):
        self.assertEqual(run_budget.command_exit_code(-signal.SIGTERM), 143)
        self.assertEqual(run_budget.command_exit_code(256), 1)

    def test_evidence_write_failure_cannot_pass(self):
        result = {"exit_code": 0}
        with patch.object(run_budget, "run_command", return_value=result), patch.object(run_budget, "write_evidence", side_effect=OSError):
            with redirect_stderr(io.StringIO()):
                rc = run_budget.main(["--seconds", "1", "--label", "check", "--json", "unused.json", "--", "unused"])
        self.assertEqual(rc, 125)


@unittest.skipUnless(os.name == "posix", "POSIX process-group execution")
class BudgetExecutionTest(unittest.TestCase):
    def cli(self, seconds: float, code: str, *options: str):
        return subprocess.run(
            [sys.executable, str(ROOT / "scripts/run_budget.py"), "--seconds", str(seconds),
             "--label", "test", *options, "--", sys.executable, "-c", code],
            capture_output=True, text=True, timeout=6,
        )

    def test_passing_command_preserves_streams_and_evidence_omits_argv(self):
        secret = "fake-secret-never-log-this-argv"
        with tempfile.TemporaryDirectory() as td:
            evidence = Path(td) / "result.json"
            proc = self.cli(2, f"import sys; secret={secret!r}; print('stdout'); print('stderr',file=sys.stderr)", "--json", str(evidence))
            result = json.loads(evidence.read_text())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "stdout\n")
        self.assertIn("stderr\n", proc.stderr)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["command_exit"], 0)
        self.assertFalse(result["over_budget"])
        self.assertNotIn(secret, proc.stdout + proc.stderr + json.dumps(result))
        self.assertNotIn("command", result)
        self.assertNotIn("environment", result)

    def test_child_failure_is_preserved_without_retry(self):
        with tempfile.TemporaryDirectory() as td:
            marker, evidence = Path(td) / "calls", Path(td) / "result.json"
            code = f"from pathlib import Path; p=Path({str(marker)!r}); p.open('a').write('once\\n'); raise SystemExit(7)"
            proc = self.cli(2, code, "--json", str(evidence))
            result = json.loads(evidence.read_text())
            self.assertEqual(marker.read_text(), "once\n")
        self.assertEqual(proc.returncode, 7)
        self.assertEqual(result["status"], "command_failed")
        self.assertEqual(result["command_exit"], 7)

    def test_timeout_cannot_pass_when_term_handler_exits_zero(self):
        code = "import signal,time; signal.signal(signal.SIGTERM,lambda *_:exit(0)); time.sleep(20)"
        with tempfile.TemporaryDirectory() as td:
            evidence = Path(td) / "result.json"
            proc = self.cli(.3, code, "--json", str(evidence))
            result = json.loads(evidence.read_text())
        self.assertEqual(proc.returncode, 124)
        self.assertEqual(result["status"], "budget_exceeded")
        self.assertTrue(result["over_budget"])
        self.assertTrue(result["cleanup"]["complete"])

    def test_missing_executable_does_not_echo_path(self):
        secret = "/no-such-executable/fake-sensitive-path"
        result = run_budget.run_command([secret], 2, "spawn")
        self.assertEqual(result["exit_code"], 127)
        self.assertEqual(result["status"], "spawn_error")
        self.assertNotIn(secret, json.dumps(result))

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux /proc verifies orphaned descendants including zombies")
    def test_timeout_kills_term_ignoring_descendant(self):
        with tempfile.TemporaryDirectory() as td:
            ready, evidence = Path(td) / "child-pid", Path(td) / "result.json"
            child = ("import os,signal,time; from pathlib import Path; "
                     "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
                     f"Path({str(ready)!r}).write_text(str(os.getpid())); time.sleep(20)")
            parent = f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{child!r}]); time.sleep(20)"
            proc = self.cli(1, parent, "--json", str(evidence))
            result = json.loads(evidence.read_text())
            self.assertTrue(ready.exists(), "descendant did not start within the test budget")
            pid = int(ready.read_text())
            live = False
            try:
                stat_path = Path(f"/proc/{pid}/stat")
                try:
                    live = stat_path.read_text().rsplit(")", 1)[1].split()[0] not in {"Z", "X"}
                except FileNotFoundError:
                    pass  # Reaped between wrapper completion and inspection.
                self.assertFalse(live, "descendant survived the deadline and cleanup")
            finally:
                if live:
                    os.kill(pid, signal.SIGKILL)
        self.assertEqual(proc.returncode, 124)
        self.assertTrue(result["cleanup"]["complete"])
        self.assertTrue(result["cleanup"]["tree_verified"])
        self.assertLess(result["elapsed_seconds"], 3)

    def test_successful_parent_cannot_leave_background_children(self):
        child = "import time; time.sleep(20)"
        code = f"import subprocess,sys; subprocess.Popen([sys.executable,'-c',{child!r}])"
        with tempfile.TemporaryDirectory() as td:
            evidence = Path(td) / "result.json"
            proc = self.cli(2, code, "--json", str(evidence))
            result = json.loads(evidence.read_text())
        self.assertEqual(proc.returncode, 125)
        self.assertEqual(result["status"], "background_descendants")
        self.assertTrue(result["cleanup"]["complete"])


if __name__ == "__main__":
    unittest.main()
