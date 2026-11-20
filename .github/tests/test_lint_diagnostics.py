"""A slow lint observation must preserve results, containment and private data."""
from __future__ import annotations

from contextlib import redirect_stderr
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".github/lint_diagnostics.py"
BUDGET = ROOT / "skills/ci-optimiser/scripts/run_budget.py"
SPEC = importlib.util.spec_from_file_location("repository_lint_diagnostics", SCRIPT)
lint = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lint)
SECRET = "DO_NOT_RECORD_argv_env_cwd_stack_descriptor_secret"


def stat_line(pid, pgid, *, comm="shellcheck", start=30):
    fields = ["0"] * 22
    for index, value in {0: "S", 1: 1, 2: pgid, 3: pgid, 11: 7, 12: 2,
                         17: 1, 19: start, 21: 40}.items():
        fields[index] = str(value)
    return f"{pid} ({comm}) " + " ".join(fields) + "\n"


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux /proc fixture contract")
class ProcSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def process(self, pid, pgid=100, *, comm="shellcheck"):
        directory = self.root / str(pid)
        directory.mkdir()
        (directory / "stat").write_text(stat_line(pid, pgid, comm=comm))
        (directory / "wchan").write_text("pipe_read\n")
        task = directory / "task" / str(pid)
        task.mkdir(parents=True)
        (task / "wchan").write_text("futex_wait_queue\n")
        (directory / "fd").mkdir()
        (directory / "fdinfo").mkdir()
        pipe = directory / "fixture-pipe"
        os.mkfifo(pipe)
        for name, flags in (("0", "00"), ("1", "01"), ("2", "02")):
            (directory / "fd" / name).symlink_to(pipe)
            (directory / "fdinfo" / name).write_text(f"pos:\t0\nflags:\t{flags}\n")
        return directory

    def snapshot(self):
        return lint.collect_snapshot(100, 101, 100, 4.0, proc_root=self.root)

    def test_only_owned_group_allowlisted_fields_and_pipe_counts_are_emitted(self):
        self.process(100, comm="python3")
        child = self.process(101, comm=SECRET)
        self.process(102)
        self.process(200, pgid=200, comm=SECRET)
        for name in ("cmdline", "environ", "stack"):
            (child / name).write_text(SECRET)
        (self.root / SECRET).write_text(SECRET)
        (child / "cwd").symlink_to(self.root / SECRET)
        (child / "fd" / "3").symlink_to(self.root / SECRET)
        (child / "wchan").write_text(SECRET)
        snapshot = self.snapshot()
        text = lint.render_snapshot(snapshot)
        self.assertNotIn(SECRET, text)
        self.assertEqual({row["pid"] for row in snapshot["processes"]}, {100, 101, 102})
        command = snapshot["processes"][1]
        self.assertEqual(command["role"], "actionlint")
        self.assertEqual(command["cpu_user_ticks"], 7)
        self.assertEqual(command["cpu_system_ticks"], 2)
        self.assertEqual(command["rss_pages"], 40)
        self.assertEqual(command["start_ticks"], 30)
        self.assertEqual(command["wait_channel"], "unknown")
        self.assertEqual(command["thread_wait_channels"]["observed"], {"futex_wait_queue": 1})
        self.assertEqual(command["pipe_endpoints"], {
            "observed": {"read": 1, "write": 1, "read_write": 1},
            "unknown_entries": 0, "complete": True,
        })
        self.assertEqual(snapshot["processes"][2]["role"], "shellcheck")

    def test_missing_denied_and_reused_process_state_is_unknown_not_zero(self):
        self.process(100)
        child = self.process(101)
        (child / "wchan").unlink()
        real_open = os.open
        def denied(path, *args, **kwargs):
            if Path(path) == child / "task/101/wchan":
                raise PermissionError(SECRET)
            return real_open(path, *args, **kwargs)
        with patch.object(lint.os, "open", side_effect=denied):
            snapshot = self.snapshot()
        row = snapshot["processes"][1]
        self.assertEqual(row["wait_channel"], "unknown")
        self.assertEqual(row["thread_wait_channels"]["observed"], {"unknown": 1})
        self.assertFalse(row["thread_wait_channels"]["complete"])
        self.assertNotIn(SECRET, lint.render_snapshot(snapshot))
        probe = lint.Probe()
        self.assertEqual(lint.process_row(probe, self.root, 999, 100, "actionlint")["status"], "unknown")
        first = lint.parse_stat(stat_line(101, 100), 101)
        (child / "stat").write_text(stat_line(101, 100, start=31))
        row = lint.process_row(lint.Probe(), self.root, 101, 100, "actionlint", first)
        self.assertEqual(row, {"pid": 101, "role": "actionlint", "status": "unknown"})

    def test_fd_race_and_unknown_flags_do_not_become_exact_zero_counts(self):
        directory = self.process(100)
        (directory / "fdinfo/0").unlink()
        (directory / "fdinfo/1").write_text(SECRET)
        with patch.object(lint.os, "stat", side_effect=FileNotFoundError(SECRET)):
            result = lint.pipe_endpoints(lint.Probe(), directory)
        self.assertFalse(result["complete"])
        self.assertEqual(result["unknown_entries"], 3)
        result = lint.pipe_endpoints(lint.Probe(), directory)
        self.assertFalse(result["complete"])
        self.assertEqual(result["unknown_entries"], 2)
        self.assertEqual(result["observed"]["read_write"], 1)
        self.assertNotIn(SECRET, json.dumps(result))

    def test_reused_pipe_descriptor_is_unknown(self):
        directory = self.process(100)
        real_stat = os.stat
        second_pipe = directory / "reused-pipe"
        os.mkfifo(second_pipe)
        reads = 0
        def changed(path):
            nonlocal reads
            if Path(path) == directory / "fd/0":
                reads += 1
                return real_stat(path) if reads == 1 else real_stat(second_pipe)
            return real_stat(path)
        with patch.object(lint.os, "stat", side_effect=changed):
            result = lint.pipe_endpoints(lint.Probe(), directory)
        self.assertEqual(result["unknown_entries"], 1)
        self.assertFalse(result["complete"])

    def test_process_fd_thread_read_and_output_limits_are_explicit(self):
        self.process(100)
        self.process(101)
        self.process(102)
        with patch.object(lint, "MAX_PROCESSES", 2), patch.object(lint, "MAX_FDS", 1), \
                patch.object(lint, "MAX_THREADS", 0):
            snapshot = self.snapshot()
        self.assertTrue(snapshot["truncated"])
        self.assertEqual(set(snapshot["limits_reached"]), {"processes", "descriptors", "threads"})
        self.assertFalse(snapshot["processes"][0]["pipe_endpoints"]["complete"])
        with patch.object(lint, "MAX_READ_BYTES", 32):
            short = self.snapshot()
        self.assertTrue(short["truncated"])
        self.assertIn("read_bytes", short["limits_reached"])
        with patch.object(lint, "MAX_OUTPUT_BYTES", 450):
            text = lint.render_snapshot(snapshot)
        self.assertLessEqual(len(text.encode()), 450)
        self.assertIn("output_bytes", text)

    def test_unowned_group_and_unavailable_proc_root_are_unknown(self):
        snapshot = lint.collect_snapshot(100, 101, 99, 4, proc_root=self.root)
        self.assertEqual(snapshot["status"], "unknown")
        self.assertEqual(snapshot["processes"], [])
        missing = lint.collect_snapshot(100, 101, 100, 4, proc_root=self.root / "missing")
        self.assertFalse(missing["scan_complete"])
        self.assertTrue(all(row["status"] == "unknown" for row in missing["processes"]))

    def test_slow_collector_has_a_bounded_wait_and_one_unknown_record(self):
        release = threading.Event()
        output = io.StringIO()
        try:
            with patch.object(lint, "collect_snapshot", side_effect=lambda *args: release.wait(2)), \
                    redirect_stderr(output):
                started = time.monotonic()
                lint.emit_snapshot(os.getpid(), 1, started)
                elapsed = time.monotonic() - started
        finally:
            release.set()
        self.assertLess(elapsed, 1)
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0].removeprefix(lint.PREFIX))
        self.assertEqual(record["status"], "unknown")
        self.assertEqual(record["limits_reached"], ["collection_time"])


class SupervisorTest(unittest.TestCase):
    def test_production_workflow_keeps_exact_command_budget_and_observation_policy(self):
        workflow = (ROOT / ".github/workflows/ci.yml").read_text()
        self.assertIn('run_budget.py --seconds 5 --label workflow-lint -- python3 '
                      '.github/lint_diagnostics.py -- "$RUNNER_TEMP/actionlint" -color', workflow)
        self.assertEqual(lint.SNAPSHOT_AFTER_SECONDS, 4.0)

    def test_success_and_nonzero_status_and_streams_are_preserved_without_snapshot(self):
        for code in (0, 17):
            child = [sys.executable, "-c", "import sys; assert sys.argv[1] == " + repr(SECRET)
                     + "; print('child-out'); print('child-err', file=sys.stderr); sys.exit(" + str(code) + ")", SECRET]
            with self.subTest(code=code):
                result = subprocess.run([sys.executable, str(SCRIPT), "--", *child],
                                        capture_output=True, text=True, timeout=3)
                self.assertEqual(result.returncode, code)
                self.assertEqual(result.stdout, "child-out\n")
                self.assertEqual(result.stderr, "child-err\n")

    @unittest.skipUnless(os.name == "posix", "POSIX signal identity")
    def test_signal_exit_is_preserved_for_outer_supervisor(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "--", sys.executable, "-c",
                                 "import os, signal; os.kill(os.getpid(), signal.SIGTERM)"],
                                capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, -signal.SIGTERM)
        self.assertEqual(result.stderr, "")

    def test_observation_never_retries_or_captures_the_command(self):
        proc = unittest.mock.Mock()
        proc.pid = 123
        proc.wait.side_effect = [subprocess.TimeoutExpired("command", 0.01), 23]
        proc.poll.return_value = None
        command = ["actionlint", "-color"]
        with patch.object(lint.subprocess, "Popen", return_value=proc) as spawn, \
                patch.object(lint, "emit_snapshot") as observe:
            self.assertEqual(lint.supervise(command, snapshot_after=0.01), 23)
        spawn.assert_called_once_with(command)
        observe.assert_called_once()
        self.assertEqual(proc.wait.call_count, 2)

    def test_spawn_failure_does_not_expose_exception_or_arguments(self):
        output = io.StringIO()
        with patch.object(lint.subprocess, "Popen", side_effect=OSError(SECRET)), redirect_stderr(output):
            self.assertEqual(lint.supervise([SECRET]), 127)
        self.assertNotIn(SECRET, output.getvalue())

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux process-group control")
    def test_hung_tree_emits_once_then_outer_budget_fails_124_and_cleans_group(self):
        with tempfile.TemporaryDirectory() as directory:
            pids = Path(directory) / "identities.json"
            evidence = Path(directory) / "budget.json"
            # A shorter test policy exercises the unchanged budget implementation;
            # neither the production CLI nor workflow exposes this override.
            child = ("import json, os, pathlib, time; child = os.fork(); "
                     "pathlib.Path(" + repr(str(pids)) + ").write_text(json.dumps("
                     "{'pid':os.getpid(),'pgid':os.getpgrp(),'child':child})) if child else None; time.sleep(60)")
            launch = ("import importlib.util, sys; spec=importlib.util.spec_from_file_location('diag', "
                      + repr(str(SCRIPT)) + "); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m); "
                      "sys.exit(m.supervise([sys.executable,'-c'," + repr(child) + "," + repr(SECRET)
                      + "], snapshot_after=0.15))")
            result = subprocess.run([sys.executable, str(BUDGET), "--seconds", "0.7", "--label", "lint-control",
                                     "--json", str(evidence), "--", sys.executable, "-c", launch],
                                    capture_output=True, text=True, timeout=4, env={**os.environ, "LINT_TEST_SECRET": SECRET})
            self.assertEqual(result.returncode, 124, result.stderr)
            budget = json.loads(evidence.read_text())
            self.assertEqual(budget["status"], "budget_exceeded")
            self.assertTrue(budget["cleanup"]["complete"])
            self.assertGreaterEqual(budget["elapsed_seconds"], 0.7)
            self.assertLess(budget["elapsed_seconds"], 2)
            lines = [line for line in result.stderr.splitlines() if line.startswith(lint.PREFIX)]
            self.assertEqual(len(lines), 1, result.stderr)
            self.assertNotIn(SECRET, result.stderr)
            snapshot = json.loads(lines[0].removeprefix(lint.PREFIX))
            identities = json.loads(pids.read_text())
            self.assertEqual(snapshot["process_group_id"], identities["pgid"])
            rows = {row["pid"]: row for row in snapshot["processes"]}
            for pid in (identities["pid"], identities["child"]):
                self.assertEqual(rows[pid]["pgid"], identities["pgid"])
                path = Path("/proc") / str(pid) / "stat"
                try:
                    self.assertIn(lint.parse_stat(path.read_text(), pid)["state"], {"Z", "X"})
                except FileNotFoundError:
                    pass


if __name__ == "__main__":
    unittest.main()
