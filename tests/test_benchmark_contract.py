"""Behavioral contracts for provenance, controlled comparisons and child cleanup."""
from __future__ import annotations

import copy
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import benchmark  # noqa: E402

META = {"repository_id": "example/test", "workload_id": "tests-and-build-v1",
        "toolchain": {"python": "fixture"}, "cache_state": "disabled", "runner_class": "fixture"}


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=Benchmark Test", "-c", "user.email=test@example.invalid", *args],
                   cwd=root, check=True, capture_output=True)


def repository(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "input.txt").write_text("original\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "fixture")
    return repo


def run_main(repo: Path, out: Path, code: str = "pass", *options: str) -> tuple[int, dict]:
    args = ["--label", "baseline", "--runs", "2", "--quiet", "--cwd", str(repo), "--out", str(out),
            *options, "--", sys.executable, "-c", code]
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        rc = benchmark.main(args)
    return rc, json.loads(out.read_text())


def arm(times: list[float]) -> dict:
    env = {key: "fixture" for key in benchmark.ENV_KEYS}
    env.update(cpu_count=2, cpu_affinity=[0, 1])
    snap = {"git": {"sha": "a" * 40, "fingerprint": "sha256:" + "a" * 64, "dirty": False},
            "metadata": copy.deepcopy(META), "env": env}
    return {"label": "fixture", "command": ["fixture"], "runs": [
            {"seconds": t, "exit": 0, "pair": i + 1, "status": "success", "cleanup": {"complete": True}}
            for i, t in enumerate(times)], "summary": benchmark.summarize(times), "failures": 0,
            "warmup": 0, "warmup_runs": [], "runs_requested": len(times),
            "completed": True, "aborted": False, "metadata": copy.deepcopy(META), "env": env,
            "provenance": {"stable": True, "before": snap, "after": copy.deepcopy(snap)}}


@unittest.skipUnless(os.name == "posix", "execution explicitly requires POSIX containment")
class ExecutionContractTest(unittest.TestCase):
    def test_failed_warmup_is_recorded_and_stops_timed_trials(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            marker = root / "first"
            code = f"from pathlib import Path; p=Path({str(marker)!r}); existed=p.exists(); p.touch(); raise SystemExit(0 if existed else 7)"
            rc, result = run_main(root, root / "result.json", code, "--warmup", "1")
            self.assertEqual(rc, 1)
            self.assertEqual(result["warmup_runs"][0]["exit"], 7)
            self.assertEqual(result["runs"], [])
            self.assertEqual(result["failures"], 1)
            self.assertTrue(result["aborted"])
            self.assertEqual(result["summary"], {"n": 0})

    def test_allow_failures_preserves_warmup_evidence_and_can_continue(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            marker = root / "first"
            code = f"from pathlib import Path; p=Path({str(marker)!r}); existed=p.exists(); p.touch(); raise SystemExit(0 if existed else 7)"
            rc, result = run_main(root, root / "result.json", code, "--warmup", "1", "--allow-failures")
            self.assertEqual(rc, 0)
            self.assertEqual((result["warmup_failures"], result["timed_failures"], result["summary"]["n"]), (1, 0, 2))
            self.assertFalse(benchmark.compare(result, result)["comparable"])

    def test_default_stops_at_first_measured_failure(self):
        with tempfile.TemporaryDirectory() as td:
            rc, result = run_main(Path(td), Path(td) / "result.json", "raise SystemExit(9)")
            self.assertEqual((rc, result["failures"], len(result["runs"])), (1, 1, 1))

    def test_timeout_kills_descendant_before_next_sample(self):
        with tempfile.TemporaryDirectory() as td:
            root, marker = Path(td), Path(td) / "descendant-marker"
            child = f"import signal,time; from pathlib import Path; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(.8); Path({str(marker)!r}).touch()"
            parent = f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{child!r}]); time.sleep(10)"
            sample = benchmark.run_trial([sys.executable, "-c", parent], root, .15, True)
            self.assertEqual((sample["exit"], sample["status"]), (124, "timeout"))
            self.assertTrue(sample["cleanup"]["complete"])
            self.assertLess(sample["seconds"], 2)
            probe = f"import time; from pathlib import Path; time.sleep(.9); raise SystemExit(8 if Path({str(marker)!r}).exists() else 0)"
            self.assertEqual(benchmark.run_once([sys.executable, "-c", probe], root, 2, True)[1], 0)
            self.assertFalse(marker.exists())

    def test_successful_parent_does_not_leave_background_children_or_stdout_open(self):
        with tempfile.TemporaryDirectory() as td:
            root, marker = Path(td), Path(td) / "marker"
            child = f"import time; from pathlib import Path; time.sleep(.7); Path({str(marker)!r}).touch()"
            code = f"import subprocess,sys; subprocess.Popen([sys.executable,'-c',{child!r}])"
            command = [sys.executable, str(ROOT / "scripts/benchmark.py"), "--label", "background", "--runs", "2",
                       "--cwd", str(root), "--out", str(root / "result.json"), "--", sys.executable, "-c", code]
            proc = subprocess.run(command, capture_output=True, text=True, timeout=5)
            result = json.loads((root / "result.json").read_text())
            self.assertEqual(proc.returncode, 1)
            self.assertEqual(result["runs"][0]["status"], "background descendants")
            self.assertTrue(result["runs"][0]["cleanup"]["complete"])
            self.assertEqual(len(result["runs"]), 1)
            time.sleep(.8)
            self.assertFalse(marker.exists())

    def test_interrupted_warmup_cleans_children_and_writes_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ready, marker, out = root / "ready", root / "late", root / "result.json"
            child = f"import time; from pathlib import Path; time.sleep(1); Path({str(marker)!r}).touch()"
            code = f"import subprocess,sys,time; from pathlib import Path; subprocess.Popen([sys.executable,'-c',{child!r}]); Path({str(ready)!r}).touch(); time.sleep(10)"
            argv = [sys.executable, str(ROOT / "scripts/benchmark.py"), "--label", "interrupt", "--runs", "2",
                    "--warmup", "1", "--cwd", str(root), "--out", str(out), "--", sys.executable, "-c", code]
            proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 5
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(ready.exists(), "warmup did not start")
                proc.send_signal(signal.SIGINT)
                proc.communicate(timeout=5)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.communicate()
            result = json.loads(out.read_text())
            self.assertEqual(proc.returncode, 130)
            self.assertEqual(result["warmup_runs"][0]["exit"], 130)
            self.assertTrue(result["warmup_runs"][0]["cleanup"]["complete"])
            self.assertEqual(result["runs"], [])
            self.assertTrue(result["aborted"])
            time.sleep(1.1)
            self.assertFalse(marker.exists())

    def test_incomplete_cleanup_never_allows_another_sample(self):
        with tempfile.TemporaryDirectory() as td:
            sample = {"seconds": .1, "exit": 124, "cleanup": {"complete": False}}
            with patch.object(benchmark, "run_trial", return_value=sample) as run:
                rc, result = run_main(Path(td), Path(td) / "result.json", "pass", "--allow-failures")
            self.assertEqual((rc, run.call_count), (1, 1))
            self.assertTrue(result["aborted"])


@unittest.skipUnless(os.name == "posix", "execution explicitly requires POSIX containment")
class ProvenanceContractTest(unittest.TestCase):
    def test_start_precedes_warmup_and_source_mutation_is_not_relabelled(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = repository(root)
            sha = benchmark.git_state(repo)["sha"]
            code = "from pathlib import Path; p=Path('input.txt'); p.write_text(p.read_text()+'changed\\n')"
            rc, result = run_main(repo, root / "result.json", code, "--warmup", "1")
            self.assertEqual(rc, 0)
            before, after = result["provenance"]["before"], result["provenance"]["after"]
            self.assertEqual(result["git_sha"], sha)
            self.assertFalse(before["git"]["dirty"])
            self.assertTrue(after["git"]["dirty"])
            self.assertNotEqual(before["git"]["fingerprint"], after["git"]["fingerprint"])
            self.assertFalse(result["provenance"]["stable"])
            self.assertLessEqual(datetime.fromisoformat(result["started_at"]), datetime.fromisoformat(result["warmup_runs"][0]["started_at"]))
            self.assertGreaterEqual(datetime.fromisoformat(result["finished_at"]), datetime.fromisoformat(result["runs"][-1]["finished_at"]))

    def test_different_dirty_contents_have_different_fingerprints(self):
        with tempfile.TemporaryDirectory() as td:
            repo = repository(Path(td))
            (repo / "input.txt").write_text("one\n")
            first = benchmark.git_state(repo)
            (repo / "input.txt").write_text("two\n")
            second = benchmark.git_state(repo)
            self.assertEqual(first["sha"], second["sha"])
            self.assertTrue(first["dirty"] and second["dirty"])
            self.assertNotEqual(first["fingerprint"], second["fingerprint"])

    def test_metadata_change_during_command_invalidates_comparison_and_no_env_dump(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, metadata = repository(root), root / "metadata.json"
            metadata.write_text(json.dumps(META))
            changed = {**META, "workload_id": "different"}
            code = f"from pathlib import Path; Path({str(metadata)!r}).write_text({json.dumps(changed)!r})"
            with patch.dict(os.environ, {"BENCH_SECRET_TEST": "must-not-be-collected"}):
                _, result = run_main(repo, root / "result.json", code, "--metadata", str(metadata))
            self.assertFalse(result["provenance"]["stable"])
            self.assertIn("metadata", result["provenance"]["changes"])
            self.assertNotIn("must-not-be-collected", json.dumps(result))

    def test_paired_cli_records_balanced_order_and_separate_warmups(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, log, metadata, command = repository(root), root / "order", root / "meta.json", root / "candidate.json"
            metadata.write_text(json.dumps(META))
            a = f"from pathlib import Path; p=Path({str(log)!r}); p.open('a').write('A')"
            b = f"from pathlib import Path; p=Path({str(log)!r}); p.open('a').write('B')"
            command.write_text(json.dumps([sys.executable, "-c", b]))
            rc, result = run_main(repo, root / "result.json", a, "--runs", "4", "--warmup", "1",
                                  "--metadata", str(metadata), "--candidate-command", str(command))
            self.assertEqual(rc, 0)
            self.assertEqual((result["summary"]["n"], result["candidate"]["summary"]["n"]), (4, 4))
            self.assertEqual(len(result["candidate"]["warmup_runs"]), 1)
            orders = result["paired_design"]["orders"]
            self.assertEqual(orders.count(["baseline", "candidate"]), 2)
            self.assertEqual(orders.count(["candidate", "baseline"]), 2)
            expected = "AB" + "".join("A" if label == "baseline" else "B" for order in orders for label in order)
            self.assertEqual(log.read_text(), expected)
            self.assertEqual(result["comparison"]["decision"], "INSUFFICIENT EVIDENCE")


class ComparisonContractTest(unittest.TestCase):
    def assert_insufficient(self, candidate, baseline=None, *, paired=False):
        result = benchmark.compare(candidate, baseline or arm([10.0] * 50), paired=paired)
        self.assertFalse(result["comparable"])
        self.assertEqual(result["decision"], "INSUFFICIENT EVIDENCE")
        self.assertNotIn("confidence", result)
        self.assertNotIn("p50_delta_pct", result)

    def test_failed_raw_trials_cannot_be_hidden_by_counters_or_pair_filtering(self):
        for phase in ("runs", "warmup_runs"):
            for counters in ("zero", "missing", "accurate"):
                with self.subTest(phase=phase, counters=counters):
                    candidate = arm([8.0] * 50)
                    candidate[phase].append({"seconds": 1.0, "exit": 7, "status": "failed",
                                             "cleanup": {"complete": True}, "pair": 51})
                    candidate["runs_requested" if phase == "runs" else "warmup"] += 1
                    if counters == "missing":
                        candidate.pop("failures")
                    elif counters == "accurate":
                        candidate["failures"] = 1
                    self.assert_insufficient(candidate, paired=True)

    def test_cleanup_and_status_are_observed_evidence_not_exit_zero(self):
        for kind in ("incomplete", "missing", "status"):
            with self.subTest(kind=kind):
                candidate = arm([8.0] * 50)
                if kind == "incomplete":
                    candidate["runs"][0]["cleanup"]["complete"] = False
                elif kind == "missing":
                    candidate["runs"][0].pop("cleanup")
                else:
                    candidate["runs"][0]["status"] = "timeout"
                self.assert_insufficient(candidate)

    def test_declared_summary_and_completion_must_match_raw_samples(self):
        for key, value in (("n", 51), ("p50", 1), ("mean", True), ("p95", float("nan"))):
            with self.subTest(key=key):
                candidate = arm([8.0] * 50)
                candidate["summary"][key] = value
                self.assert_insufficient(candidate)
        for field in ("runs_requested", "warmup"):
            candidate = arm([8.0] * 50)
            candidate[field] += 1
            self.assert_insufficient(candidate)
        candidate = arm([8.0] * 50)
        candidate["aborted"] = True
        self.assert_insufficient(candidate)

    def test_imported_stable_flag_cannot_override_actual_snapshot_changes(self):
        for section in ("git", "env", "metadata"):
            with self.subTest(section=section):
                candidate = arm([8.0] * 50)
                after = candidate["provenance"]["after"]
                if section == "git":
                    after[section]["fingerprint"] = "sha256:" + "b" * 64
                elif section == "env":
                    after[section]["cpu_count"] = 8
                else:
                    after[section]["workload_id"] = "other-work"
                self.assert_insufficient(candidate)

    def test_declared_metadata_must_match_captured_provenance(self):
        baseline, candidate = arm([10.0] * 50), arm([8.0] * 50)
        for value in (baseline, candidate):
            value["metadata"]["workload_id"] = "detached-declaration"
        self.assert_insufficient(candidate, baseline)
        for missing in ("before", "after"):
            candidate = arm([8.0] * 50)
            candidate["provenance"].pop(missing)
            self.assert_insufficient(candidate)

    def test_partial_or_conflicting_git_identity_is_incomplete_evidence(self):
        for field in ("sha", "dirty", "fingerprint"):
            with self.subTest(field=field):
                candidate = arm([8.0] * 50)
                for point in ("before", "after"):
                    candidate["provenance"][point]["git"].pop(field)
                self.assert_insufficient(candidate)
        candidate = arm([8.0] * 50)
        candidate["git_sha"] = "b" * 40
        self.assert_insufficient(candidate)

    def test_nested_types_and_hash_formats_cannot_masquerade_as_known(self):
        for value in (True, 123, [], {}, "sha256:abc", "anything"):
            with self.subTest(fingerprint=value):
                baseline, candidate = arm([10.0] * 50), arm([8.0] * 50)
                for item in (baseline, candidate):
                    for point in ("before", "after"):
                        item["provenance"][point]["git"]["fingerprint"] = value
                self.assert_insufficient(candidate, baseline)
        for section, key, value in (("metadata", "toolchain", False), ("metadata", "workload_id", 1),
                                    ("metadata", "toolchain", {"python": 3}), ("env", "cpu_count", True),
                                    ("env", "cpu_affinity", [False]), ("env", "system", [])):
            with self.subTest(section=section, key=key):
                baseline, candidate = arm([10.0] * 50), arm([8.0] * 50)
                for item in (baseline, candidate):
                    item[section][key] = value
                    for point in ("before", "after"):
                        item["provenance"][point][section][key] = value
                self.assert_insufficient(candidate, baseline)

    def test_pair_identity_must_be_unique_positive_integers(self):
        for pair in (True, 0, "1", [], 2):
            with self.subTest(pair=pair):
                candidate = arm([8.0] * 50)
                candidate["runs"][0]["pair"] = pair
                self.assert_insufficient(candidate, paired=True)

    def test_malformed_nested_evidence_fails_closed_without_traceback(self):
        for key, value in (("env", None), ("metadata", []), ("provenance", {"before": None}),
                           ("runs", [{"exit": 0, "seconds": float("nan")}])):
            with self.subTest(key=key):
                invalid = arm([2.0] * 50)
                invalid[key] = value
                result = benchmark.compare(arm([1.0] * 50), invalid)
                self.assertFalse(result["comparable"])
                self.assertEqual(result["decision"], "INSUFFICIENT EVIDENCE")

    def test_missing_metadata_and_legacy_records_never_get_a_speed_claim(self):
        legacy = {"label": "old", "command": ["x"], "summary": {"n": 100, "p50": 10}, "runs": []}
        result = benchmark.compare(arm([1.0] * 50), legacy)
        self.assertFalse(result["comparable"])
        self.assertEqual(result["decision"], "INSUFFICIENT EVIDENCE")
        self.assertNotIn("p50_delta_pct", result)

    def test_workload_toolchain_cache_platform_and_source_mismatches_reject(self):
        for section, key, value in (("metadata", "workload_id", "new"), ("metadata", "toolchain", {"python": "new"}),
                                    ("metadata", "cache_state", "cold"), ("env", "system", "another-os"),
                                    ("env", "cpu_quota", "100000 100000")):
            with self.subTest(section=section, key=key):
                baseline, candidate = arm([10.0] * 50), arm([1.0] * 50)
                candidate[section][key] = value
                result = benchmark.compare(candidate, baseline)
                self.assertFalse(result["comparable"])
                self.assertNotIn("confidence", result)
        baseline, candidate = arm([10.0] * 50), arm([1.0] * 50)
        candidate["provenance"]["before"]["git"]["fingerprint"] = "sha256:" + "b" * 64
        self.assertFalse(benchmark.compare(candidate, baseline)["comparable"])

    def test_reviewed_source_difference_must_bind_both_actual_fingerprints(self):
        baseline, candidate = arm([10.0] * 50), arm([8.0] * 50)
        for point in ("before", "after"):
            candidate["provenance"][point]["git"]["fingerprint"] = "sha256:" + "b" * 64
        review = {"experiment_id": "cache-only-1", "baseline_fingerprint": "sha256:" + "a" * 64, "candidate_fingerprint": "sha256:" + "b" * 64,
                  "allowed_difference": "reviewed workflow-only cache setting", "review_reference": "review/123"}
        for value in (baseline, candidate):
            value["metadata"]["reviewed_comparison"] = copy.deepcopy(review)
            for point in ("before", "after"):
                value["provenance"][point]["metadata"]["reviewed_comparison"] = copy.deepcopy(review)
        self.assertTrue(benchmark.compare(candidate, baseline)["comparable"])
        self.assertFalse(benchmark.compare(candidate, baseline, paired=True)["comparable"])
        candidate["metadata"]["reviewed_comparison"]["candidate_fingerprint"] = "sha256:other"
        self.assertFalse(benchmark.compare(candidate, baseline)["comparable"])

    def test_low_sample_and_noise_remain_insufficient(self):
        self.assertEqual(benchmark.compare(arm([1.0] * 3), arm([10.0] * 3))["decision"], "INSUFFICIENT EVIDENCE")
        noisy = [1.0, 3.0, 12.0, 50.0, 2.0] * 10
        result = benchmark.compare(arm(noisy), arm(noisy), paired=True)
        self.assertEqual(result["decision"], "INSUFFICIENT EVIDENCE")
        self.assertTrue(any("noise" in warning for warning in result["warnings"]))

    def test_paired_confidence_is_reproducible_and_never_automatic_adoption(self):
        baseline, candidate = arm([10.0] * 50), arm([8.0] * 50)
        result = benchmark.compare(candidate, baseline, paired=True, seed=4)
        self.assertEqual(result["confidence"], benchmark.compare(candidate, baseline, paired=True, seed=4)["confidence"])
        self.assertEqual(result["confidence"]["method"], "paired percentile bootstrap")
        self.assertEqual(result["confidence"]["intervals"]["p50_delta_s"], [-2.0, -2.0])
        self.assertEqual(result["decision"], "PERFORMANCE IMPROVEMENT SIGNAL")
        self.assertIn("correctness", result["adoption"])


class InputContractTest(unittest.TestCase):
    def test_help_does_not_require_a_command_delimiter(self):
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as exc:
            benchmark.main(["--help"])
        self.assertEqual(exc.exception.code, 0)
        self.assertIn("--candidate-command", output.getvalue())

    def test_invalid_timeouts_and_baseline_json_fail_before_execution(self):
        with tempfile.TemporaryDirectory() as td:
            root, invalid = Path(td), Path(td) / "invalid.json"
            invalid.write_text("{")
            options = [["--timeout", value] for value in ("0", "-1", "nan", "inf")]
            options.append(["--baseline", str(invalid)])
            for option in options:
                with self.subTest(option=option), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exc:
                    benchmark.main(["--label", "x", "--out", str(root / "out.json"), *option, "--", sys.executable, "-c", "pass"])
                self.assertEqual(exc.exception.code, 2)
            self.assertFalse((root / "out.json").exists())

    def test_nested_malformed_baseline_is_a_cli_error_before_execution(self):
        with tempfile.TemporaryDirectory() as td:
            root, path = Path(td), Path(td) / "baseline.json"
            path.write_text(json.dumps({"summary": {}, "runs": [], "provenance": None}))
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exc:
                benchmark.main(["--label", "x", "--baseline", str(path), "--out", str(root / "out.json"), "--", "true"])
            self.assertEqual(exc.exception.code, 2)
            self.assertFalse((root / "out.json").exists())

    def test_unknown_metadata_keys_and_shell_command_strings_are_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "input.json"
            path.write_text(json.dumps({**META, "SECRET_TOKEN": "not-for-recording"}))
            with self.assertRaises(ValueError):
                benchmark.read_metadata(path)
            path.write_text(json.dumps("echo hello && do-something"))
            with self.assertRaises(ValueError):
                benchmark.read_command(path)

    def test_windows_fails_explicitly_without_starting_a_command(self):
        with patch.object(benchmark.os, "name", "nt"), patch.object(benchmark.subprocess, "Popen") as spawn:
            with self.assertRaisesRegex(ValueError, "Windows is unsupported"):
                benchmark.run_trial(["echo", "test"], ROOT, 1, True)
            spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
