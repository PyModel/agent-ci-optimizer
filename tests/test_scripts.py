"""One focused test class per script. Run: python3 -m unittest discover -s tests -v"""
from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import analyze_gh  # noqa: E402
import benchmark  # noqa: E402
import collect_gh  # noqa: E402
import discover_repo  # noqa: E402
import select_affected  # noqa: E402

SENTINEL = "TOPSECRET_VALUE_7f3a"


def write(root: Path, rel: str, text: str = "") -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


class DiscoverRepoTest(unittest.TestCase):
    def test_detects_stack_and_never_reads_secrets(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            write(root, ".env", f"API_KEY={SENTINEL}")
            write(root, "config/credentials.json", SENTINEL)
            write(root, "node_modules/x/package.json", "{}")
            write(root, "package.json", json.dumps({"scripts": {"test": "vitest"}, "devDependencies": {"vitest": "5", "left-pad": "1"}}))
            write(root, "pnpm-lock.yaml")
            write(root, "nx.json", "{}")
            write(root, ".github/workflows/ci.yml",
                  "on: [pull_request_target]\njobs:\n  t:\n    runs-on: ubuntu-latest\n    steps:\n"
                  "      - uses: actions/checkout@v7\n      - run: npm test || true\n"
                  f"        env:\n          TOKEN: ${{{{ secrets.NPM_TOKEN }}}}\n")
            write(root, ".github/workflows/secrets-scan.yml", "on: push\n")
            opened = []
            real = Path.read_text
            def spy(self, *a, **k):
                opened.append(self.relative_to(root).as_posix())
                return real(self, *a, **k)
            with mock.patch.object(Path, "read_text", spy):
                out = discover_repo.discover(root)
            self.assertEqual(sorted(opened), [".github/workflows/ci.yml", ".github/workflows/secrets-scan.yml", "package.json"])
            dumped = json.dumps(out)
            self.assertNotIn(SENTINEL, dumped)
            self.assertIn("javascript/typescript", out["languages"])
            self.assertEqual(out["found"]["lockfiles"], ["pnpm-lock.yaml"])
            self.assertTrue(out["hints"]["project_graph_present"])
            self.assertEqual(list(out["package_json"]), ["package.json"])  # node_modules pruned
            self.assertEqual(out["package_json"]["package.json"]["tooling_deps"], ["vitest"])
            wf = out["github_workflows"][".github/workflows/ci.yml"]
            for key in ("pull_request_target", "or_true", "unpinned_action"):
                self.assertIn(key, wf)
            self.assertEqual(wf["secret_names"], ["NPM_TOKEN"])


def fake_gh(routes: dict):
    calls = []

    def gh(path: str):
        calls.append(path)
        for prefix, value in routes.items():
            if path == prefix[:-1] if prefix.endswith("$") else path.startswith(prefix):
                if isinstance(value, Exception):
                    raise value
                return value(path) if callable(value) else value
        raise collect_gh.GhError(f"HTTP 404 {path}")
    gh.calls = calls
    return gh


class CollectGhTest(unittest.TestCase):
    def test_paginates_respects_limit_and_records_errors(self):
        def runs_page(path):
            page = int(parse_qs(urlsplit(path).query)["page"][0])
            base = (page - 1) * 100
            return {"workflow_runs": [{"id": base + i} for i in range(100 if page < 3 else 5)]}
        gh = fake_gh({
            "repos/o/r/actions/runs?": runs_page,
            "repos/o/r/actions/runs/7/jobs": collect_gh.GhError("HTTP 500"),
            "repos/o/r/actions/runs/": {"jobs": [{"id": 1, "steps": []}]},
            "repos/o/r/rules/": [{"type": "required_status_checks", "parameters": {}}, {"type": "deletion"}],
            "repos/o/r$": {"default_branch": "trunk", "private": True},
        })
        with tempfile.TemporaryDirectory() as d:
            meta = collect_gh.collect(gh, "o/r", 150, Path(d))
            runs = json.loads((Path(d) / "raw/runs.json").read_text())
            required = json.loads((Path(d) / "raw/required_checks.json").read_text())
        self.assertEqual(len(runs), 150)
        self.assertEqual(meta["runs_with_jobs"], 149)
        self.assertTrue(any("jobs 7" in e for e in meta["errors"]))
        self.assertEqual(required["branch"], "trunk")
        self.assertEqual([r["type"] for r in required["rulesets"]], ["required_status_checks"])
        self.assertIn("unavailable", required["classic_protection"])
        self.assertTrue(any("/branches/trunk/protection" in c for c in gh.calls))

    def test_main_exits_3_on_partial_job_data(self):
        gh = fake_gh({
            "repos/o/r/actions/runs?": {"workflow_runs": [{"id": 1}, {"id": 2}]},
            "repos/o/r/actions/runs/2/jobs": collect_gh.GhError("HTTP 500"),
            "repos/o/r/actions/runs/": {"jobs": []},
            "repos/o/r/rules/": [],
            "repos/o/r$": {"default_branch": "main"},
        })
        with tempfile.TemporaryDirectory() as d, mock.patch.object(collect_gh, "gh_api", gh), \
                redirect_stdout(io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(collect_gh.main(["--repo", "o/r", "--out", d]), 3)

    def test_rejects_bad_repo_and_limit(self):
        for argv in (["--repo", "o/r; rm -rf /"], ["--repo", "o/r", "--limit", "0"]):
            with self.assertRaises(SystemExit), redirect_stdout(io.StringIO()):
                collect_gh.main(argv)


def job(run_id, name, created, started, completed, conclusion="success", attempt=1, steps=None, labels=("ubuntu-latest",)):
    return {"run_id": run_id, "name": name, "created_at": created, "started_at": started,
            "completed_at": completed, "conclusion": conclusion, "run_attempt": attempt,
            "labels": list(labels), "steps": steps or []}


class AnalyzeGhTest(unittest.TestCase):
    def test_metrics_and_unknowns(self):
        T = "2026-10-01T00:{:02d}:00Z".format
        runs = [
            {"id": 1, "name": "CI", "event": "push", "head_sha": "a", "workflow_id": 9, "conclusion": "failure",
             "created_at": T(0), "updated_at": T(5), "run_attempt": 1},
            {"id": 2, "name": "CI", "event": "push", "head_sha": "a", "workflow_id": 9, "conclusion": "success",
             "created_at": T(10), "updated_at": T(20), "run_attempt": 2},
            {"id": 3, "name": "CI", "event": "push", "head_sha": "b", "workflow_id": 9, "conclusion": "success",
             "created_at": T(30), "updated_at": T(59), "run_attempt": 1},
        ]
        steps = [{"name": "Set up job", "started_at": T(11), "completed_at": T(12)},
                 {"name": "Run tests", "started_at": T(12), "completed_at": T(15)}]
        jobs = {
            2: [job(2, "test (1)", T(10), T(11), T(15), steps=steps, attempt=2),
                job(2, "test (2)", T(10), T(11), T(19), attempt=2),
                job(2, "test (3)", T(10), T(11), T(13), attempt=2),
                job(2, "old", T(0), T(1), T(50), attempt=1, conclusion="failure")],
            3: [job(3, "lint", T(30), T(32), T(40))],
        }
        m = analyze_gh.analyze(runs, jobs, {"repo": "o/r"})
        g = m["workflows"]["CI [push]"]
        self.assertEqual(g["runs"], 3)
        self.assertEqual(g["run_wall_s_success"]["p50"], 540.0)  # run 2: created 10 -> latest attempt ends 19
        self.assertTrue(g["run_wall_s_success"]["low_sample"])
        self.assertEqual(g["shard_skew"]["max"], 2.0)  # 8 min / median 4 min
        self.assertEqual(g["setup_share"]["p50"], 0.25)
        self.assertEqual(g["rerun_rate"]["value"], round(1 / 3, 4))
        self.assertEqual(g["jobs_missing_for_runs"], 1)
        self.assertEqual(m["flaky_sha_rate"]["num"], 1)
        self.assertEqual(m["cache_hit_rate"], "unknown")
        self.assertIsInstance(m["push_to_green_s"], dict)

    def test_empty_is_unknown_not_zero(self):
        m = analyze_gh.analyze([], {}, {})
        self.assertEqual(m["push_to_green_s"], "unknown")
        self.assertEqual(m["flaky_sha_rate"], "unknown")
        self.assertEqual(m["window"], "unknown")
        self.assertEqual(m["billable_minutes_by_label"], "unknown")


class BenchmarkTest(unittest.TestCase):
    def run_bench(self, *extra, code="pass"):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "b.json"
            argv = ["--label", "x", "--runs", "3", "--quiet", "--out", str(out), *extra, "--",
                    sys.executable, "-c", code]
            with redirect_stdout(io.StringIO()):
                rc = benchmark.main(argv)
            return rc, (json.loads(out.read_text()) if out.exists() else None)

    def test_success_and_compare(self):
        rc, res = self.run_bench()
        self.assertEqual(rc, 0)
        self.assertEqual(res["summary"]["n"], 3)
        self.assertEqual(res["command"][-1], "pass")
        with tempfile.TemporaryDirectory() as d:
            base = Path(d) / "base.json"
            base.write_text(json.dumps(res))
            rc, res2 = self.run_bench("--baseline", str(base))
        self.assertIn("p50_delta_pct", res2["comparison"])
        self.assertTrue(any("fewer than" in w for w in res2["comparison"]["warnings"]))

    def test_failure_exits_nonzero(self):
        rc, res = self.run_bench(code="raise SystemExit(3)")
        self.assertEqual(rc, 1)
        self.assertEqual(res["failures"], 3)
        self.assertEqual(res["summary"]["n"], 0)

    def test_rejects_zero_runs_and_missing_command(self):
        with self.assertRaises(SystemExit):
            benchmark.main(["--label", "x", "--runs", "0", "--out", "/dev/null", "--", "true"])
        self.assertEqual(benchmark.main(["--label", "x", "--out", "/dev/null"]), 2)


MAP = {"rules": [{"paths": ["packages/api/*"], "targets": ["api"]},
                 {"paths": ["packages/ui/*"], "targets": ["ui"]}],
       "dependents": {"ui": ["web"], "web": ["e2e"]}}


class SelectAffectedTest(unittest.TestCase):
    def sel(self, changed, why=""):
        return select_affected.select(changed, MAP, why)

    def test_selects_with_transitive_dependents(self):
        r = self.sel(["packages/ui/src/button.tsx"])
        self.assertEqual((r["mode"], r["targets"]), ("SELECTED", ["e2e", "ui", "web"]))

    def test_fails_closed(self):
        cases = {
            "unmapped": ["README.md"],
            "ci file": ["packages/api/x.ts", ".github/workflows/ci.yml"],
            "lockfile": ["pnpm-lock.yaml"],
            "nested manifest": ["packages/api/package.json"],
            "migration": ["packages/api/db/migrations/001.sql"],
            "schema": ["packages/api/schema.graphql"],
            "empty": [],
            "unknown": None,
        }
        for name, changed in cases.items():
            with self.subTest(name):
                self.assertEqual(self.sel(changed, "no base")["mode"], "FULL")

    def test_git_rename_and_missing_base(self):
        with tempfile.TemporaryDirectory() as d:
            g = lambda *a: subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *a], cwd=d,
                                         check=True, capture_output=True)
            g("init", "-q", "-b", "main")
            write(Path(d), "packages/ui/a.ts", "x\n" * 20)
            g("add", "."), g("commit", "-qm", "1")
            g("mv", "packages/ui/a.ts", "README.md"), g("commit", "-qm", "2")
            changed, _ = select_affected.git_changed("HEAD~1", "HEAD", Path(d))
            self.assertEqual(changed, ["README.md", "packages/ui/a.ts"])  # both rename sides
            self.assertEqual(self.sel(changed)["mode"], "FULL")
            changed, why = select_affected.git_changed("origin/nope", "HEAD", Path(d))
            self.assertIsNone(changed)
            self.assertEqual(self.sel(changed, why)["mode"], "FULL")

    def test_bad_map_exits_2(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "m.json"
            p.write_text(json.dumps({"rules": [{"paths": [], "targets": ["x"]}]}))
            self.assertEqual(select_affected.main(["--map", str(p), "--changed", "a"]), 2)


if __name__ == "__main__":
    unittest.main()
