#!/usr/bin/env python3
"""Time a command N times and write wall-clock stats as JSON.

Runs exactly the argv after `--` (no shell). Review the command for side
effects first: never benchmark deploys, migrations, payments or anything
destructive. Exits non-zero when any timed run fails, unless --allow-failures.

Usage:
  python3 scripts/benchmark.py --label baseline --runs 10 --out .ci-audit/baseline.json -- npm test
  python3 scripts/benchmark.py --label candidate --runs 10 --baseline .ci-audit/baseline.json \
      --out .ci-audit/candidate.json -- npm test
"""
from __future__ import annotations

import argparse
import json
import math
import os
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

MIN_RUNS_FOR_DECISION = 10


def pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q / 100 * len(ordered)) - 1)]


def git_sha(cwd: Path) -> str | None:
    try:
        p = subprocess.run(["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True, text=True, timeout=10)
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=cwd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode != 0:
        return None
    return p.stdout.strip() + ("-dirty" if dirty.stdout.strip() else "")


def run_once(cmd: list[str], cwd: Path, timeout: float | None, quiet: bool) -> tuple[float, int]:
    sink = subprocess.DEVNULL if quiet else None
    start = time.perf_counter()
    try:
        code = subprocess.run(cmd, cwd=cwd, stdout=sink, stderr=sink, timeout=timeout, check=False).returncode
    except subprocess.TimeoutExpired:
        code = 124
    return time.perf_counter() - start, code


def summarize(times: list[float]) -> dict:
    if not times:
        return {"n": 0}
    return {"n": len(times), "min": round(min(times), 3), "p50": round(pct(times, 50), 3),
            "p95": round(pct(times, 95), 3), "max": round(max(times), 3),
            "mean": round(sum(times) / len(times), 3)}


def compare(current: dict, baseline: dict) -> dict:
    out: dict = {"baseline_label": baseline.get("label")}
    for key in ("p50", "p95"):
        a, b = baseline["summary"].get(key), current["summary"].get(key)
        if a and b:
            out[f"{key}_delta_s"] = round(b - a, 3)
            out[f"{key}_delta_pct"] = round((b - a) / a * 100, 1)
    warnings = []
    if baseline.get("command") != current["command"]:
        warnings.append("commands differ")
    for k in ("machine", "cpu_count", "python"):
        if baseline.get("env", {}).get(k) != current["env"].get(k):
            warnings.append(f"env.{k} differs")
    if min(baseline["summary"].get("n", 0), current["summary"]["n"]) < MIN_RUNS_FOR_DECISION:
        warnings.append(f"fewer than {MIN_RUNS_FOR_DECISION} runs: preliminary only")
    if baseline.get("failures") or current["failures"]:
        warnings.append("failed runs present")
    out["warnings"] = warnings
    return out


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--" not in argv:
        print("error: put the command after `--`", file=sys.stderr)
        return 2
    split = argv.index("--")
    cmd = argv[split + 1:]
    ap = argparse.ArgumentParser(description="Benchmark a command (no shell).")
    ap.add_argument("--label", required=True)
    ap.add_argument("--runs", type=int, default=10)
    ap.add_argument("--warmup", type=int, default=0, help="untimed runs first (warm caches)")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--cwd", type=Path, default=Path.cwd())
    ap.add_argument("--timeout", type=float, help="seconds per run; timeout counts as exit 124")
    ap.add_argument("--baseline", type=Path, help="earlier benchmark JSON to compare against")
    ap.add_argument("--quiet", action="store_true", help="discard the command's output")
    ap.add_argument("--allow-failures", action="store_true")
    args = ap.parse_args(argv[:split])
    if not cmd:
        ap.error("empty command after `--`")
    if args.runs < 1 or args.warmup < 0:
        ap.error("--runs must be >= 1 and --warmup >= 0")
    baseline = json.loads(args.baseline.read_text()) if args.baseline else None

    for _ in range(args.warmup):
        run_once(cmd, args.cwd, args.timeout, args.quiet)
    times, codes = [], []
    for i in range(args.runs):
        t, code = run_once(cmd, args.cwd, args.timeout, args.quiet)
        times.append(t)
        codes.append(code)
        print(f"[{args.label}] run {i + 1}/{args.runs}: {t:.3f}s exit={code}", file=sys.stderr)
    ok = [t for t, c in zip(times, codes) if c == 0]
    result = {
        "schema": "ci-benchmark/1",
        "label": args.label,
        "command": cmd,
        "cwd": str(args.cwd.resolve()),
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "git_sha": git_sha(args.cwd),
        "env": {"machine": platform.machine(), "system": platform.system(), "cpu_count": os.cpu_count(),
                "python": platform.python_version(), "ci": bool(os.environ.get("CI"))},
        "warmup": args.warmup,
        "runs": [{"seconds": round(t, 3), "exit": c} for t, c in zip(times, codes)],
        "failures": len(times) - len(ok),
        "summary": summarize(ok),
    }
    if baseline:
        result["comparison"] = compare(result, baseline)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2))
    print(json.dumps({"summary": result["summary"], "failures": result["failures"],
                      **({"comparison": result["comparison"]} if baseline else {})}, indent=1))
    return 0 if args.allow_failures or result["failures"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
