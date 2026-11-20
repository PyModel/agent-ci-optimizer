"""Per-trial preparation must retain cost, reject stale state and preserve failures."""
from __future__ import annotations

import copy
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import benchmark  # noqa: E402
import benchmark_preparation as preparation  # noqa: E402
from cache_evidence import LAYERS, unknown_states  # noqa: E402
from test_benchmark_contract import META, arm, repository, run_main  # noqa: E402


def prepared_arm(seconds, preparation_seconds, identity="same-starting-state"):
    """Synthetic timing/evidence fixture, not measurements of the execution host."""
    result = arm([seconds] * 40)
    states = {layer: {"state": "disabled", "identity": identity, "evidence": "fixture/review"} for layer in LAYERS}
    protocol = preparation.protocol({"schema": "ci-benchmark-preparation/1", "review_reference": "fixture/review",
                                     "command": ["reviewed-fixture-argv"], "timeout_s": 10, "expected_states": states})
    records = []
    for index, sample in enumerate(result["runs"], 1):
        nonce = f"{(int(seconds * 1000) << 32) + index:032x}"
        sample.update(sequence=index, position=1, preparation_sequence=index, provenance=copy.deepcopy(result["provenance"]))
        records.append({"protocol_sha256": protocol["sha256"], "nonce": nonce, "states": copy.deepcopy(states),
                        "state_evidence": "recorded_by_reviewed_protocol", "evidence_sha256": preparation.digest(
                            {"schema": "ci-benchmark-state/1", "nonce": nonce, "states": states}),
                        "issues": [], "execution": {"seconds": preparation_seconds, "exit": 0, "status": "success", "cleanup": {"complete": True}},
                        "wall_seconds": preparation_seconds, "provenance": copy.deepcopy(result["provenance"]),
                        "sequence": index, "pair": index, "position": 1, "phase": "runs"})
    result.update(preparation_protocol=protocol, preparation_runs=records, preparation_failures=0,
                  preparation_summary=benchmark.preparation_summary(records))
    return result


class PreparationComparisonTest(unittest.TestCase):
    def test_faster_work_cannot_hide_slower_preparation_plus_work(self):
        result = benchmark.compare(prepared_arm(8, 4), prepared_arm(10, 1))
        self.assertTrue(result["comparable"])
        self.assertEqual(result["p50_delta_s"], -2)
        self.assertEqual(result["preparation_cost"]["preparation_plus_command"]["p50_delta_s"], 1)
        self.assertEqual(result["decision"], "INSUFFICIENT EVIDENCE")
        self.assertTrue(any("preparation plus command" in warning for warning in result["warnings"]))

    def test_changed_states_missing_protocol_and_source_drift_block_comparison(self):
        baseline = prepared_arm(10, 1)
        self.assertFalse(benchmark.compare(prepared_arm(8, 1, "different-starting-state"), baseline)["comparable"])
        self.assertFalse(benchmark.compare(arm([8]*40), baseline)["comparable"])
        changed = prepared_arm(8, 1)
        changed["preparation_runs"][0]["provenance"]["before"]["git"]["sha"] = "b"*40
        self.assertFalse(benchmark.compare(changed, baseline)["comparable"])

    def test_reusing_the_same_observations_is_not_an_independent_comparison(self):
        baseline = prepared_arm(10, 1)
        result = benchmark.compare(copy.deepcopy(baseline), baseline)
        self.assertFalse(result["comparable"])
        self.assertIn("preparation reuses a trial nonce across compared arms", result["warnings"])


@unittest.skipUnless(os.name == "posix", "preparation uses the existing POSIX containment contract")
class PerTrialPreparationTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = repository(self.root)
        self.out = self.root / "result.json"
        self.script = self.repo / "prepare.py"
        self.path = self.root / "preparation.json"
        self.metadata = self.root / "metadata.json"
        self.metadata.write_text(json.dumps(META))
        self.marker = self.root / "work-ran"
        self.cache = self.root / "cache-state"
        self.states = {layer: {"state": "disabled", "identity": f"fixture-disabled/{layer}", "evidence": "fixture/review"} for layer in LAYERS}

    def configure(self, before="", response="path.write_text(json.dumps(value))", *, expected=None, timeout=2):
        self.script.write_text("import json, os\nfrom pathlib import Path\n"
                               "path = Path(os.environ['CI_OPTIMISER_TRIAL_EVIDENCE'])\n"
                               f"value = {{'schema':'ci-benchmark-state/1','nonce':os.environ['CI_OPTIMISER_TRIAL_NONCE'],'states':{self.states!r}}}\n"
                               + before + "\n" + response + "\n")
        self.protocol = {"schema": "ci-benchmark-preparation/1", "review_reference": "fixture/reviewed-exact-argv",
                         "command": [sys.executable, str(self.script)], "timeout_s": timeout,
                         "expected_states": expected or self.states}
        self.path.write_text(json.dumps(self.protocol))

    def execute(self, *options, code=None):
        work = code or f"from pathlib import Path; Path({str(self.marker)!r}).touch()"
        return run_main(self.repo, self.out, work, "--metadata", str(self.metadata), "--preparation", str(self.path), *options)

    def test_every_warmup_and_paired_trial_prepares_the_cache_again(self):
        self.states["dependencies"] = {"state": "cold", "identity": "fixture-empty-v1", "evidence": "fixture/reset"}
        self.configure(before=f"Path({str(self.cache)!r}).write_text('cold')")
        work = f"from pathlib import Path; p=Path({str(self.cache)!r}); assert p.read_text() == 'cold'; p.write_text('warm')"
        candidate = self.root / "candidate.json"
        candidate.write_text(json.dumps([sys.executable, "-c", work]))
        # This protocol fixture supplies an explicitly known platform; the host's
        # cgroup quota is allowed to be unknown and must not be assumed here.
        with patch.object(benchmark, "environment", return_value=arm([1])["env"]):
            rc, result = self.execute("--warmup", "1", "--candidate-command", str(candidate), code=work)
        self.assertEqual(rc, 0)
        self.assertTrue(result["comparison"]["comparable"], result["comparison"])
        self.assertIn("preparation_cost", result["comparison"])
        nonces = []
        for value in (result, result["candidate"]):
            benchmark.validate_benchmark(value)
            self.assertEqual(len(value["preparation_runs"]), 3)
            self.assertEqual(value["preparation_summary"]["n"], 3)
            self.assertGreater(value["preparation_summary"]["total_s"], 0)
            self.assertTrue(value["preparation_summary"]["includes_failed"])
            by_sequence = {record["sequence"]: record for record in value["preparation_runs"]}
            for sample in value["runs"] + value["warmup_runs"]:
                record = by_sequence[sample["preparation_sequence"]]
                self.assertEqual(record["states"]["dependencies"]["state"], "cold")
                self.assertTrue(record["provenance"]["stable"])
                self.assertTrue(sample["provenance"]["stable"])
                nonces.append(record["nonce"])
        self.assertEqual(len(set(nonces)), 6)

    def test_preparation_failure_stops_even_with_allow_failures_and_keeps_time(self):
        self.configure(before="raise SystemExit(7)")
        rc, result = self.execute("--allow-failures", "--warmup", "1")
        self.assertEqual(rc, 1)
        self.assertFalse(self.marker.exists())
        self.assertEqual(result["runs"], [])
        self.assertEqual(result["warmup_runs"], [])
        self.assertEqual((result["failures"], result["preparation_failures"]), (1, 1))
        self.assertEqual(result["preparation_runs"][0]["execution"]["exit"], 7)
        self.assertGreater(result["preparation_summary"]["total_s"], 0)
        benchmark.validate_benchmark(result)

    def test_timeout_uses_the_same_containment_and_prevents_work(self):
        self.configure(before="import time; time.sleep(10)", timeout=.05)
        rc, result = self.execute("--allow-failures")
        self.assertEqual(rc, 1)
        record = result["preparation_runs"][0]
        self.assertEqual(record["execution"]["exit"], 124)
        self.assertTrue(record["execution"]["cleanup"]["complete"])
        self.assertFalse(self.marker.exists())
        self.assertEqual(len(result["preparation_runs"]), 1)

    def test_stale_missing_malformed_oversized_and_symlink_responses_fail_closed(self):
        cases = ["value['nonce']='0'*32; path.write_text(json.dumps(value))", "pass",
                 "path.write_text('{broken')", "path.write_text('x'*65537)",
                 "other=path.with_name('other.json'); other.write_text(json.dumps(value)); path.symlink_to(other)",
                 "path.write_text('{\"schema\":1,\"schema\":2}')", "path.write_text('['*2000+'0'+']'*2000)"]
        for response in cases:
            with self.subTest(response=response):
                self.configure(response=response)
                rc, result = self.execute("--allow-failures")
                self.assertEqual(rc, 1)
                self.assertEqual(result["runs"], [])
                self.assertEqual(result["preparation_failures"], 1)
                self.assertEqual(result["preparation_runs"][0]["state_evidence"], "unknown")
                self.assertFalse(self.marker.exists())
                benchmark.validate_benchmark(result)

    def test_wrong_prepared_state_is_retained_and_work_never_starts(self):
        expected = copy.deepcopy(self.states)
        expected["dependencies"]["state"] = "cold"
        self.configure(expected=expected)
        rc, result = self.execute()
        self.assertEqual(rc, 1)
        record = result["preparation_runs"][0]
        self.assertEqual(record["states"]["dependencies"]["state"], "disabled")
        self.assertIn("dependencies.state", record["issues"][0])
        self.assertFalse(self.marker.exists())
        benchmark.validate_benchmark(result)

    def test_unknown_layers_remain_unresolved_and_protocol_does_not_clean_caches(self):
        self.states = unknown_states()
        self.configure()
        self.cache.write_text("preserve-existing-cache")
        rc, result = self.execute()
        self.assertEqual(rc, 0)  # Diagnostic work may run; no comparable cache-state claim.
        self.assertEqual(self.cache.read_text(), "preserve-existing-cache")
        comparison = benchmark.compare(result, result)
        self.assertFalse(comparison["comparable"])
        self.assertTrue(any("cache-layer state is unresolved" in issue for issue in comparison["warnings"]))

    def test_source_mutation_in_preparation_is_not_laundered_by_later_trials(self):
        self.configure(before="Path('input.txt').write_text('changed')")
        rc, result = self.execute("--allow-failures")
        self.assertEqual(rc, 1)
        self.assertFalse(self.marker.exists())
        self.assertIn("git", result["preparation_runs"][0]["provenance"]["changes"])
        self.assertEqual(result["preparation_failures"], 1)

    def test_source_mutation_in_work_stops_before_the_next_preparation(self):
        self.configure()
        rc, result = self.execute("--allow-failures", code="from pathlib import Path; Path('input.txt').write_text('changed')")
        self.assertEqual(rc, 1)
        self.assertEqual(len(result["preparation_runs"]), 1)
        self.assertEqual(len(result["runs"]), 1)
        self.assertIn("git", result["runs"][0]["provenance"]["changes"])

    def test_exact_argv_is_not_interpolated_or_sent_to_a_shell(self):
        literal = "$(touch SHOULD_NOT_EXIST); `touch ALSO_NOT_CREATED`"
        self.configure(before=f"import sys; assert sys.argv[1] == {literal!r}")
        self.protocol["command"].append(literal)
        self.path.write_text(json.dumps(self.protocol))
        rc, result = self.execute()
        self.assertEqual(rc, 0)
        self.assertEqual(result["preparation_protocol"]["command"][-1], literal)
        self.assertFalse((self.repo / "SHOULD_NOT_EXIST").exists())
        self.assertFalse((self.repo / "ALSO_NOT_CREATED").exists())

    def test_imported_records_cannot_hide_failed_prep_or_reuse_links(self):
        self.configure()
        rc, result = self.execute()
        self.assertEqual(rc, 0)
        mutations = [lambda row: row["preparation_runs"][0].update(execution=[]),
                     lambda row: row["preparation_runs"][0].update(evidence_sha256="0"*64),
                     lambda row: row["preparation_runs"][0]["execution"].update(exit=9, status="failed"),
                     lambda row: row["preparation_summary"].update(total_s=0),
                     lambda row: row["preparation_protocol"].update(command=["unreviewed"]),
                     lambda row: row["runs"][1].update(preparation_sequence=row["runs"][0]["preparation_sequence"])]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                changed = copy.deepcopy(result)
                mutate(changed)
                self.assertFalse(benchmark.compare(changed, result)["comparable"])

    def test_legacy_execution_and_comparison_keep_explicit_unverified_state(self):
        rc, result = run_main(self.repo, self.out, "pass", "--metadata", str(self.metadata))
        self.assertEqual(rc, 0)
        self.assertEqual(result["cache_state_evidence"], "unverified")
        self.assertNotIn("preparation_runs", result)
        comparison = benchmark.compare(arm([1]*40), arm([2]*40))
        self.assertTrue(comparison["comparable"])
        self.assertEqual(comparison["cache_state_evidence"], "unverified_declarations")

    def test_invalid_protocol_is_rejected_before_any_command(self):
        self.configure()
        cases = [None, {**self.protocol, "timeout_s": True}, {**self.protocol, "timeout_s": 10**400},
                 {**self.protocol, "timeout_s": float("inf")},
                 {**self.protocol, "command": "shell command"}, {**self.protocol, "extra": "unreviewed"}]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(SystemExit) as stopped, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.path.write_text(json.dumps(value))
                self.execute()
            self.assertEqual(stopped.exception.code, 2)
            self.assertFalse(self.marker.exists())
        self.path.write_text(json.dumps(self.protocol).replace('"timeout_s": 2', '"timeout_s": 1e999'))
        with self.assertRaises(ValueError):
            preparation.load_protocol(self.path)


if __name__ == "__main__":
    unittest.main()
