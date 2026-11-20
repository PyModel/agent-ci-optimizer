#!/usr/bin/env python3
"""Benchmark reviewed argv with provenance and bounded POSIX process-group cleanup.

The existing interface is --label LABEL --out FILE -- COMMAND ARG... .
--metadata FILE declares repository_id, workload_id, toolchain (version map),
cache_state and runner_class. Missing declarations remain unknown. Never put
secrets in metadata/argv. No project command discovers versions; no env dump.
Git fingerprints cover HEAD, index and non-ignored worktree changes.

--candidate-command FILE reads a reviewed JSON argv array for balanced randomized
AB/BA pairs in one worktree. --candidate-metadata optionally overrides metadata.
An optional metadata.reviewed_comparison object can authorize a different-source
comparison: experiment_id, baseline_fingerprint, candidate_fingerprint,
allowed_difference and review_reference. All five must be non-empty strings,
the immutable fingerprints must match the actual arms, and both arms must carry
the same assertion and workload/toolchain/platform/cache declarations. This is
an explicit reviewed assertion, never inferred equivalence or a correctness gate.

Warmups are recorded. Default: stop on the first failure. --allow-failures never
overrides an interruption or incomplete cleanup. Windows execution is rejected
until process-tree containment is supported. POSIX commands must not daemonize
or escape their owned process group. Surviving background descendants invalidate
even a successful direct child. Cleanup precedes the next sample.
Never benchmark deploys, payments, migrations or destructive commands. Performance
signals require independent correctness/security and representative CI evidence.

Optional --preparation FILE (and --candidate-preparation FILE) runs exact reviewed
argv before every warmup/trial. ci-benchmark-preparation/1 declares expected typed
cache states and an explicit timeout. The command writes ci-benchmark-state/1 to
CI_OPTIMISER_TRIAL_EVIDENCE with CI_OPTIMISER_TRIAL_NONCE. Both are fresh per trial.
No cache is implicitly deleted. Preparation failures abort even with --allow-failures;
preparation time and state/source evidence are retained separately from timed work.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import math
import os
import platform
import random
import re
import secrets
import signal
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import benchmark_preparation as preparation
from cache_evidence import LAYERS, unknown_states, unresolved_states, validate_states

UNKNOWN = "unknown"
MIN_RUNS_FOR_DECISION = 10
MIN_TAIL_RUNS = 40  # Conservative policy, not a guarantee about a population p95.
BOOTSTRAP_SAMPLES = 2000
CLEANUP_GRACE_S = 0.3
CLEANUP_KILL_S = 1.0
MAX_FINGERPRINT_BYTES = 256 * 1024 * 1024
METADATA_KEYS = {"repository_id", "workload_id", "toolchain", "cache_state", "runner_class"}
REVIEW_KEYS = {"experiment_id", "baseline_fingerprint", "candidate_fingerprint", "allowed_difference", "review_reference"}
ENV_KEYS = ("system", "release", "machine", "cpu_count", "cpu_model", "cpu_affinity", "cpu_quota", "python", "runner_class")
FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")
REVISION = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


def stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q / 100 * len(ordered)) - 1)]


def known(value: object) -> bool:
    if value is None or value == UNKNOWN or value == "":
        return False
    if isinstance(value, dict):
        return bool(value) and all(known(v) for v in value.values())
    if isinstance(value, list):
        return bool(value) and all(known(v) for v in value)
    return True


def valid_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and "\0" not in value


def validate_metadata(data: object) -> dict:
    if not isinstance(data, dict) or set(data) - METADATA_KEYS - {"reviewed_comparison"}:
        raise ValueError("metadata accepts only " + ", ".join(sorted(METADATA_KEYS)) + ", reviewed_comparison")
    result = {key: data.get(key, UNKNOWN) for key in sorted(METADATA_KEYS)}
    for key, value in result.items():
        if key == "toolchain" and isinstance(value, dict):
            if not value or any(not valid_text(k) or not valid_text(v) for k, v in value.items()):
                raise ValueError("toolchain must map non-empty tool names to explicit version strings")
        elif not valid_text(value) or (key == "toolchain" and value != UNKNOWN):
            raise ValueError(f"metadata.{key} must be a non-empty string (toolchain: version map or 'unknown')")
    if "reviewed_comparison" in data:
        review = data["reviewed_comparison"]
        if not isinstance(review, dict) or set(review) != REVIEW_KEYS or any(not valid_text(v) or not known(v) for v in review.values()):
            raise ValueError("reviewed_comparison needs exactly " + ", ".join(sorted(REVIEW_KEYS)))
        if any(not FINGERPRINT.fullmatch(review[key]) for key in ("baseline_fingerprint", "candidate_fingerprint")):
            raise ValueError("reviewed_comparison fingerprints must be SHA-256 identities")
        result["reviewed_comparison"] = review
    return result


def read_metadata(path: Path | None) -> dict:
    return validate_metadata(json.loads(path.read_text(encoding="utf-8")) if path is not None else {})


def read_command(path: Path) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not data or any(not isinstance(arg, str) or "\0" in arg for arg in data) or not data[0]:
        raise ValueError("candidate command must be a non-empty JSON argv array with no NUL bytes")
    return data


def environment(metadata: dict) -> dict:
    cpu_model, quota = platform.processor().strip() or UNKNOWN, UNKNOWN
    if sys.platform.startswith("linux"):
        try:
            for line in Path("/proc/cpuinfo").read_text().splitlines():
                if line.startswith("model name"):
                    cpu_model = line.split(":", 1)[1].strip()
                    break
        except OSError:
            pass
        try:
            quota = Path("/sys/fs/cgroup/cpu.max").read_text().strip() or UNKNOWN
        except OSError:
            try:
                quota = " ".join(Path("/sys/fs/cgroup/cpu/" + name).read_text().strip()
                                 for name in ("cpu.cfs_quota_us", "cpu.cfs_period_us"))
            except OSError:
                pass
    try:
        affinity: list[int] | str = sorted(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        affinity = UNKNOWN
    return {"machine": platform.machine() or UNKNOWN, "system": platform.system() or UNKNOWN,
            "release": platform.release() or UNKNOWN, "cpu_count": os.cpu_count() or UNKNOWN,
            "cpu_model": cpu_model, "cpu_affinity": affinity, "cpu_quota": quota,
            "python": platform.python_version(), "runner_class": metadata["runner_class"],
            "ci": bool(os.environ.get("CI"))}


def _git(cwd: Path, *args: str) -> bytes:
    proc = subprocess.run(["git", "--no-optional-locks", "-c", "core.fsmonitor=false", *args],
                          cwd=cwd, capture_output=True, timeout=10, check=False)
    if proc.returncode:
        raise ValueError("Git provenance unavailable")
    return proc.stdout


def _status_paths(raw: bytes) -> list[bytes]:
    fields, paths, i = raw.split(b"\0"), [], 0
    while i < len(fields) - 1:
        entry = fields[i]
        if len(entry) < 4 or entry[2:3] != b" ":
            raise ValueError("unrecognized Git status record")
        paths.append(entry[3:])
        if b"R" in entry[:2] or b"C" in entry[:2]:
            i += 1
            if i >= len(fields) - 1:
                raise ValueError("truncated Git rename record")
            paths.append(fields[i])
        i += 1
    if fields[-1]:
        raise ValueError("truncated Git status")
    return sorted(set(paths))


def _hash_changed_path(root: Path, raw: bytes, digest, remaining: int) -> int:
    rel = Path(os.fsdecode(raw))
    if rel.is_absolute() or not rel.parts or ".." in rel.parts:
        raise ValueError("invalid Git path")
    parent = root
    for part in rel.parts[:-1]:
        parent /= part
        if parent.is_symlink():
            raise ValueError("symlink parent prevents a safe worktree fingerprint")
    path = root / rel
    digest.update(raw + b"\0")
    try:
        before = path.lstat()
    except FileNotFoundError:
        digest.update(b"missing\0")
        return remaining
    digest.update(str(stat.S_IMODE(before.st_mode)).encode() + b"\0")
    if stat.S_ISLNK(before.st_mode):
        digest.update(b"link\0" + os.fsencode(os.readlink(path)) + b"\0")
    elif stat.S_ISREG(before.st_mode):
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_ino, opened.st_dev) != (before.st_ino, before.st_dev):
                raise ValueError("worktree changed while fingerprinting")
            while block := stream.read(1024 * 1024):
                remaining -= len(block)
                if remaining < 0:
                    raise ValueError("changed files exceed the 256 MiB fingerprint limit")
                digest.update(block)
        after = path.lstat()
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError("worktree changed while fingerprinting")
    else:
        raise ValueError("changed directory/submodule or special file needs external provenance")
    digest.update(b"\0")
    return remaining


def git_state(cwd: Path) -> dict:
    result = {"sha": UNKNOWN, "dirty": UNKNOWN, "fingerprint": UNKNOWN}
    try:
        root = Path(os.fsdecode(_git(cwd, "rev-parse", "--show-toplevel").rstrip(b"\n")))
        sha = _git(root, "rev-parse", "HEAD").decode("ascii").strip()
        if len(sha) not in (40, 64) or any(c not in "0123456789abcdef" for c in sha):
            raise ValueError("invalid Git revision")
        status = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=none")
        index = _git(root, "ls-files", "--stage", "-z")
        result.update(sha=sha, dirty=bool(status), root=str(root))
        digest = hashlib.sha256(sha.encode() + b"\0" + index + b"\0" + status)
        remaining = MAX_FINGERPRINT_BYTES
        for raw in _status_paths(status):
            remaining = _hash_changed_path(root, raw, digest, remaining)
        if status != _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=none"):
            raise ValueError("worktree status changed while fingerprinting")
        result["fingerprint"] = "sha256:" + digest.hexdigest()
    except (OSError, ValueError, UnicodeError, subprocess.TimeoutExpired) as exc:
        result["error"] = str(exc)
    return result


def git_sha(cwd: Path) -> str:
    """Compatibility helper; experiments record full before/after state."""
    state = git_state(cwd)
    return state["sha"] + ("-dirty" if state["dirty"] is True else "")


def snapshot(cwd: Path, metadata_path: Path | None) -> dict:
    errors = []
    try:
        metadata = read_metadata(metadata_path)
    except (OSError, ValueError, UnicodeError) as exc:
        metadata = read_metadata(None)
        errors.append("metadata unavailable: " + str(exc))
    return {"captured_at": stamp(), "git": git_state(cwd), "env": environment(metadata),
            "metadata": metadata, "errors": errors}


def provenance(before: dict, after: dict) -> dict:
    changes = [key for key in ("git", "env", "metadata") if before[key] != after[key]]
    verifiable = known(before["git"]["fingerprint"]) and known(after["git"]["fingerprint"])
    return {"before": before, "after": after, "stable": not changes if verifiable else UNKNOWN,
            "changes": changes, "scope": "HEAD, index, non-ignored worktree, declared inputs and observed platform"}


class StopRequested(KeyboardInterrupt):
    def __init__(self, signum: int):
        self.signum = signum


def _stop(signum, _frame):
    raise StopRequested(signum)


@contextmanager
def signal_handlers(ignore: bool = False):
    previous = {}
    try:
        for signum in ((signal.SIGINT, signal.SIGTERM) if ignore else (signal.SIGTERM,)):
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, signal.SIG_IGN if ignore else _stop)
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # Container PID 1 can reap orphan zombies slowly; zombies cannot execute.
    if sys.platform.startswith("linux"):
        try:
            for entry in Path("/proc").iterdir():
                if not entry.name.isdigit():
                    continue
                try:
                    fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
                except (FileNotFoundError, ProcessLookupError):
                    continue
                if int(fields[2]) == pgid and fields[0] not in {"Z", "X"}:
                    return True
            return False
        except (OSError, ValueError, IndexError):
            return True  # Cannot establish cleanup; never start another trial.
    return True


def cleanup_group(proc: subprocess.Popen) -> dict:
    started, error = time.perf_counter(), None
    with signal_handlers(ignore=True):
        for signum, grace in ((signal.SIGTERM, CLEANUP_GRACE_S), (signal.SIGKILL, CLEANUP_KILL_S)):
            try:
                os.killpg(proc.pid, signum)
            except ProcessLookupError:
                pass
            except OSError as exc:
                error = str(exc)
            deadline = time.monotonic() + grace
            while time.monotonic() < deadline:
                proc.poll()
                if not group_alive(proc.pid):
                    break
                time.sleep(0.01)
            if not group_alive(proc.pid):
                break
        complete = proc.poll() is not None and not group_alive(proc.pid)
    return {"complete": complete, "seconds": round(time.perf_counter() - started, 6),
            "method": "POSIX process group TERM then KILL", **({"error": error} if error else {})}


def run_trial(cmd: list[str], cwd: Path, timeout: float | None, quiet: bool, *, env: dict | None = None) -> dict:
    if os.name != "posix":
        raise ValueError("benchmark execution requires POSIX process-group containment; Windows is unsupported")
    started, wall, proc = time.perf_counter(), stamp(), None
    status, code, interrupted_signal = "failed", 127, None
    cleanup = {"complete": True, "seconds": 0.0, "method": "no remaining process group"}
    extra = {}
    try:
        sink = subprocess.DEVNULL if quiet else None
        proc = subprocess.Popen(cmd, cwd=cwd, stdout=sink, stderr=sink, start_new_session=True, env=env)
        code = proc.wait(timeout=timeout)
        status = "success" if code == 0 else "failed"
        if group_alive(proc.pid):
            extra["command_exit"] = code
            status, code = "background descendants", 125
            cleanup = cleanup_group(proc)
    except subprocess.TimeoutExpired:
        status, code = "timeout", 124
        cleanup = cleanup_group(proc)
    except KeyboardInterrupt as exc:
        interrupted_signal = getattr(exc, "signum", signal.SIGINT)
        status, code = "interrupted", 128 + interrupted_signal
        if proc is not None:
            cleanup = cleanup_group(proc)
    except OSError as exc:
        status, code = "spawn error", 127
        extra["error"] = str(exc)
        if proc is not None:
            cleanup = cleanup_group(proc)
    if not cleanup["complete"]:
        status, code = "cleanup incomplete", code or 125
    return {"seconds": round(time.perf_counter() - started, 6), "exit": code, "status": status,
            "started_at": wall, "finished_at": stamp(), "cleanup": cleanup,
            **({"interrupted_signal": interrupted_signal} if interrupted_signal else {}), **extra}


def run_once(cmd: list[str], cwd: Path, timeout: float | None, quiet: bool) -> tuple[float, int]:
    """Compatibility interface; main retains the richer per-sample records."""
    result = run_trial(cmd, cwd, timeout, quiet)
    return result["seconds"], result["exit"]


def prepare_trial(protocol: dict, cwd: Path, metadata_path: Path | None, quiet: bool) -> tuple[dict, dict]:
    """Run only the reviewed argv; the protocol cannot authorize implicit cleanup."""
    started = time.perf_counter()
    before = snapshot(cwd, metadata_path)
    record = {"protocol_sha256": protocol["sha256"], "nonce": secrets.token_hex(16),
              "states": unknown_states(), "state_evidence": "unknown", "evidence_sha256": None, "issues": []}
    try:
        with tempfile.TemporaryDirectory(prefix="ci-optimiser-preparation-") as directory:
            path = Path(directory) / "state.json"
            env = {**os.environ, "CI_OPTIMISER_TRIAL_EVIDENCE": str(path), "CI_OPTIMISER_TRIAL_NONCE": record["nonce"]}
            record["execution"] = run_trial(protocol["command"], cwd, protocol["timeout_s"], quiet, env=env)
            if record["execution"]["exit"] != 0 or record["execution"]["cleanup"]["complete"] is not True:
                record["issues"].append("preparation command failed or cleanup is incomplete")
            else:
                value = preparation.read_json(path)
                record["states"] = preparation.response(value, record["nonce"])
                record["state_evidence"] = "recorded_by_reviewed_protocol"
                record["evidence_sha256"] = preparation.digest(value)
                mismatches = preparation.mismatches(protocol["expected_states"], record["states"])
                if mismatches:
                    record["issues"].append("prepared state differs from the reviewed protocol: " + ", ".join(mismatches))
    except (OSError, ValueError, UnicodeError) as exc:
        record["issues"].append("preparation evidence unavailable: " + str(exc))
    with signal_handlers(ignore=True):
        after = snapshot(cwd, metadata_path)
        record["provenance"] = provenance(before, after)
        if record["provenance"]["changes"]:
            record["issues"].append("preparation changed source, platform or declared workload inputs")
    if "execution" not in record:
        record["execution"] = {"seconds": 0.0, "exit": 127, "status": "preparation infrastructure error",
                               "cleanup": {"complete": True}}
    record["wall_seconds"] = round(time.perf_counter() - started, 6)
    return record, after


def summarize(times: list[float]) -> dict:
    if not times:
        return {"n": 0}
    return {"n": len(times), "min": round(min(times), 3), "p50": round(pct(times, 50), 3),
            "p95": round(pct(times, 95), 3), "max": round(max(times), 3),
            "mean": round(sum(times) / len(times), 3)}


def preparation_summary(records: list[dict]) -> dict:
    return {**summarize([row["wall_seconds"] for row in records]),
            "total_s": round(sum(row["wall_seconds"] for row in records), 6),
            "command_s": round(sum(row["execution"]["seconds"] for row in records), 6), "includes_failed": True}


def _samples(result: dict) -> list[float]:
    return [run["seconds"] for run in result.get("runs", []) if isinstance(run, dict) and run.get("exit") == 0
            and isinstance(run.get("seconds"), (int, float)) and not isinstance(run["seconds"], bool)
            and math.isfinite(run["seconds"]) and run["seconds"] >= 0]


def _validate_environment(value: object) -> None:
    if not isinstance(value, dict):
        raise ValueError("benchmark environment must be an object")
    for key in ENV_KEYS:
        if key not in value or value[key] == UNKNOWN:
            continue
        observed = value[key]
        if key == "cpu_count":
            good = type(observed) is int and observed > 0
        elif key == "cpu_affinity":
            good = (isinstance(observed, list) and bool(observed)
                    and all(type(cpu) is int and cpu >= 0 for cpu in observed)
                    and len(set(observed)) == len(observed))
        else:
            good = valid_text(observed)
        if not good:
            raise ValueError(f"benchmark environment.{key} is malformed")
    if "ci" in value and type(value["ci"]) is not bool:
        raise ValueError("benchmark environment.ci must be boolean")


def _validate_git(value: object) -> None:
    if not isinstance(value, dict):
        raise ValueError("benchmark snapshot must contain a Git object")
    for key, pattern in (("sha", REVISION), ("fingerprint", FINGERPRINT)):
        if key in value and value[key] != UNKNOWN and (not isinstance(value[key], str) or not pattern.fullmatch(value[key])):
            raise ValueError(f"benchmark Git {key} is malformed")
    if "dirty" in value and value["dirty"] != UNKNOWN and type(value["dirty"]) is not bool:
        raise ValueError("benchmark Git dirty must be boolean or unknown")


def _validate_proof(proof: object) -> None:
    if not isinstance(proof, dict):
        raise ValueError("benchmark provenance must be an object")
    if "stable" in proof and type(proof["stable"]) is not bool and proof["stable"] != UNKNOWN:
        raise ValueError("benchmark provenance.stable must be boolean or unknown")
    if "changes" in proof and (not isinstance(proof["changes"], list)
                               or any(key not in ("git", "env", "metadata") for key in proof["changes"])):
        raise ValueError("benchmark provenance.changes must name changed snapshot sections")
    for key in ("before", "after"):
        if key in proof and (not isinstance(proof[key], dict) or not isinstance(proof[key].get("git"), dict)):
            raise ValueError(f"benchmark.provenance.{key} must contain a Git object")
        if key in proof:
            _validate_git(proof[key]["git"])
            if "env" in proof[key]:
                _validate_environment(proof[key]["env"])
            if "metadata" in proof[key]:
                validate_metadata(proof[key]["metadata"])


def _validate_sample(sample: object, label: str) -> None:
    if (not isinstance(sample, dict) or type(sample.get("exit")) is not int
            or type(sample.get("seconds")) not in (int, float)
            or not 0 <= sample["seconds"] <= sys.float_info.max):
        raise ValueError(f"benchmark.{label} has an invalid exit or duration")
    if "status" in sample and (not valid_text(sample["status"])
                                or (sample["exit"] == 0) != (sample["status"] == "success")):
        raise ValueError(f"benchmark.{label} status contradicts exit")
    if "cleanup" in sample and (not isinstance(sample["cleanup"], dict)
                                 or type(sample["cleanup"].get("complete")) is not bool):
        raise ValueError(f"benchmark.{label} cleanup must declare boolean completeness")
    for key in ("pair", "sequence", "position"):
        if key in sample and (type(sample[key]) is not int or sample[key] < 1
                              or (key == "position" and sample[key] > 2)):
            raise ValueError(f"benchmark.{label}.{key} must be a valid positive index")
    if "provenance" in sample:
        _validate_proof(sample["provenance"])


def validate_preparations(result: dict) -> int:
    protocol = preparation.protocol(result["preparation_protocol"], recorded=True)
    records = result.get("preparation_runs")
    if not isinstance(records, list):
        raise ValueError("benchmark preparation_runs must be an array")
    required = {"protocol_sha256", "nonce", "states", "state_evidence", "evidence_sha256", "issues", "execution",
                "wall_seconds", "provenance", "sequence", "pair", "position", "phase"}
    seen, nonces, failures, last_sequence = {}, set(), 0, 0
    for row in records:
        if (not isinstance(row, dict) or set(row) != required or row["protocol_sha256"] != protocol["sha256"]
                or not isinstance(row["nonce"], str) or re.fullmatch(r"[0-9a-f]{32}", row["nonce"]) is None
                or row["nonce"] in nonces or type(row["wall_seconds"]) not in (int, float)
                or not 0 <= row["wall_seconds"] <= sys.float_info.max
                or not isinstance(row["issues"], list) or any(not valid_text(issue) for issue in row["issues"])
                or row["phase"] not in ("runs", "warmup_runs")):
            raise ValueError("benchmark preparation record has invalid identity, state or time evidence")
        if not isinstance(row["execution"], dict) or not {"seconds", "exit", "status", "cleanup"} <= set(row["execution"]):
            raise ValueError("benchmark preparation lacks complete command evidence")
        _validate_sample({**row["execution"], **{key: row[key] for key in ("pair", "sequence", "position")}}, "preparation")
        _validate_proof(row["provenance"])
        if row["sequence"] <= last_sequence or row["wall_seconds"] < row["execution"]["seconds"]:
            raise ValueError("benchmark preparation order or elapsed time contradicts execution")
        last_sequence = row["sequence"]
        seen[row["sequence"]] = row
        nonces.add(row["nonce"])
        states = validate_states(row["states"])
        if row["state_evidence"] == "recorded_by_reviewed_protocol":
            if row["evidence_sha256"] != preparation.digest({"schema": "ci-benchmark-state/1", "nonce": row["nonce"], "states": states}):
                raise ValueError("benchmark preparation state fingerprint contradicts the response")
            if preparation.mismatches(protocol["expected_states"], states) and not row["issues"]:
                raise ValueError("benchmark preparation hides a state mismatch")
        elif row["state_evidence"] != "unknown" or row["evidence_sha256"] is not None or states != unknown_states() or not row["issues"]:
            raise ValueError("benchmark preparation claims state evidence without a valid response")
        failures += bool(row["issues"]) or row["execution"]["exit"] != 0
    used = set()
    for phase in ("runs", "warmup_runs"):
        for sample in result.get(phase, []):
            sequence = sample.get("preparation_sequence")
            if type(sequence) is not int or sequence not in seen or sequence in used:
                raise ValueError("benchmark work lacks unique per-trial preparation")
            row = seen[sequence]
            if (row["phase"] != phase or any(row[key] != sample.get(key) for key in ("pair", "sequence", "position"))
                    or row["issues"] or row["execution"]["exit"] != 0 or row["execution"].get("cleanup", {}).get("complete") is not True
                    or "provenance" not in sample):
                raise ValueError("benchmark work contradicts its preparation evidence")
            used.add(sequence)
    if (result.get("completed") is True and set(seen) != used
            or result.get("preparation_summary") != preparation_summary(records)
            or type(result.get("preparation_failures")) is not int or result["preparation_failures"] != failures):
        raise ValueError("benchmark preparation summary or completion contradicts raw records")
    return failures


def validate_benchmark(result: object) -> None:
    """Accept old records as incomplete evidence, but reject malformed nested data."""
    if not isinstance(result, dict) or not isinstance(result.get("summary"), dict) or not isinstance(result.get("runs"), list):
        raise ValueError("benchmark must be an object with summary and runs")
    for key in ("metadata", "env", "provenance"):
        if key in result and not isinstance(result[key], dict):
            raise ValueError(f"benchmark.{key} must be an object")
    if "metadata" in result:
        validate_metadata(result["metadata"])
    if "env" in result:
        _validate_environment(result["env"])
    _validate_proof(result.get("provenance", {}))
    observed_failures = {}
    for phase in ("runs", "warmup_runs"):
        samples = result.get(phase, [])
        if not isinstance(samples, list):
            raise ValueError(f"benchmark.{phase} must be an array")
        for sample in samples:
            _validate_sample(sample, phase)
        observed_failures["timed_failures" if phase == "runs" else "warmup_failures"] = sum(sample["exit"] != 0 for sample in samples)
    observed_failures["failures"] = sum(observed_failures.values())
    if "preparation_protocol" in result:
        observed_failures["failures"] += validate_preparations(result)
    elif any(key in result for key in ("preparation_runs", "preparation_summary", "preparation_failures")):
        raise ValueError("benchmark preparation records require their reviewed protocol")
    for key in ("failures", "warmup_failures", "timed_failures"):
        if key in result and (type(result[key]) is not int or result[key] < 0):
            raise ValueError(f"benchmark.{key} must be a non-negative integer")
        if key in result and result[key] != observed_failures[key]:
            raise ValueError(f"benchmark.{key} contradicts raw samples")
    for key in ("aborted", "completed"):
        if key in result and type(result[key]) is not bool:
            raise ValueError(f"benchmark.{key} must be boolean")
    for key, phase, minimum in (("runs_requested", "runs", 1), ("warmup", "warmup_runs", 0)):
        if key in result:
            if type(result[key]) is not int or result[key] < minimum:
                raise ValueError(f"benchmark.{key} must be an integer >= {minimum}")
            if len(result.get(phase, [])) > result[key] or (result.get("completed") is True and len(result.get(phase, [])) != result[key]):
                raise ValueError(f"benchmark.{key} contradicts raw sample count")
    if result.get("completed") is True and result.get("aborted") is True:
        raise ValueError("benchmark cannot be both completed and aborted")
    derived = summarize(_samples(result))
    for key, value in result["summary"].items():
        if key not in derived or type(value) not in (int, float) or value != derived[key] or (key == "n" and type(value) is not int):
            raise ValueError(f"benchmark.summary.{key} contradicts raw samples")


def stable_proof(proof: dict) -> bool:
    before, after = proof.get("before", {}), proof.get("after", {})
    sections = ("git", "env", "metadata")
    complete_snapshots = all(isinstance(snapshot.get(key), dict) for snapshot in (before, after) for key in sections)
    verifiable_git = all(known(snapshot.get("git", {}).get("fingerprint"))
                         and known(snapshot.get("git", {}).get("sha"))
                         and type(snapshot.get("git", {}).get("dirty")) is bool
                         for snapshot in (before, after))
    return (complete_snapshots and proof.get("stable") is True and not proof.get("changes")
            and all(before.get(key) == after.get(key) for key in sections) and verifiable_git)


def evidence_issues(arm: dict) -> list[str]:
    """Derive suitability from records; imported claims alone are never evidence."""
    issues = []
    proof = arm.get("provenance", {})
    before, after = proof.get("before", {}), proof.get("after", {})
    if not stable_proof(proof):
        issues.append("source/input provenance missing, contradictory or changed during the experiment")
    if any(arm.get(key) != before.get(key) or arm.get(key) != after.get(key) for key in ("metadata", "env")):
        issues.append("top-level declarations do not match before/after provenance")
    if arm.get("metadata", {}).get("runner_class") != arm.get("env", {}).get("runner_class"):
        issues.append("runner-class declarations contradict the observed environment")
    if "git_sha" in arm:
        git = before.get("git", {})
        expected = str(git.get("sha", UNKNOWN)) + ("-dirty" if git.get("dirty") is True else "")
        if arm["git_sha"] != expected:
            issues.append("git_sha contradicts captured source provenance")
    samples = arm.get("warmup_runs", []) + arm["runs"]
    if any(sample["exit"] != 0 or sample.get("status") != "success"
           or sample.get("cleanup", {}).get("complete") is not True for sample in samples):
        issues.append("raw samples contain failed work or lack successful cleanup evidence")
    if (arm.get("aborted") is not False or arm.get("completed") is not True
            or arm.get("runs_requested") != len(arm["runs"])
            or arm.get("warmup") != len(arm.get("warmup_runs", []))):
        issues.append("requested work is incomplete or completion cannot be established")
    if arm.get("summary") != summarize(_samples(arm)):
        issues.append("summary is incomplete for the observed successful trials")
    if "preparation_protocol" in arm:
        records = arm["preparation_runs"]
        if any(row["issues"] or row["execution"]["exit"] != 0 or row["execution"].get("cleanup", {}).get("complete") is not True for row in records):
            issues.append("preparation failed or lacks complete cleanup evidence")
        if any(row["state_evidence"] != "recorded_by_reviewed_protocol" or unresolved_states(row["states"]) for row in records):
            issues.append("per-trial cache-layer state is unresolved")
        proofs = [row["provenance"] for row in records] + [sample.get("provenance", {}) for sample in samples]
        if any(not stable_proof(value) for value in proofs):
            issues.append("per-trial source/input provenance is missing or changed")
        if any(value.get(boundary, {}).get(key) != before.get(key) for value in proofs
               for boundary in ("before", "after") for key in ("git", "env", "metadata")):
            issues.append("per-trial inputs differ from experiment provenance")
    return issues


def confidence(a: list[float], b: list[float], paired: bool, seed: int = 0) -> dict:
    rng = random.Random(seed)
    draws = {"p50_delta_s": [], "p95_delta_s": [], "mean_delta_s": []}
    for _ in range(BOOTSTRAP_SAMPLES):
        indices = rng.choices(range(len(a)), k=len(a))
        aa = [a[i] for i in indices]
        bb = [b[i] for i in indices] if paired else rng.choices(b, k=len(b))
        draws["p50_delta_s"].append(pct(bb, 50) - pct(aa, 50))
        draws["p95_delta_s"].append(pct(bb, 95) - pct(aa, 95))
        draws["mean_delta_s"].append(sum(bb) / len(bb) - sum(aa) / len(aa))
    return {"method": "paired percentile bootstrap" if paired else "independent-arm percentile bootstrap",
            "level": 0.95, "resamples": BOOTSTRAP_SAMPLES, "seed": seed,
            "intervals": {key: [round(pct(vals, 2.5), 6), round(pct(vals, 97.5), 6)] for key, vals in draws.items()},
            "limitations": "Conditional on representative independent pairs/trials and correct workload/cache declarations; not a correctness test or guarantee of a population tail."}


def compare(current: dict, baseline: dict, *, paired: bool = False, seed: int = 0) -> dict:
    out = {"baseline_label": baseline.get("label", UNKNOWN) if isinstance(baseline, dict) else UNKNOWN, "decision": "INSUFFICIENT EVIDENCE",
           "adoption": "not evaluated: correctness, security and representative CI gates remain required"}
    try:
        validate_benchmark(current)
        validate_benchmark(baseline)
    except ValueError as exc:
        return {**out, "comparable": False, "warnings": ["malformed benchmark evidence: " + str(exc)]}
    incompatible, warnings = [], []
    for label, arm in (("baseline", baseline), ("candidate", current)):
        incompatible.extend(f"{label} {issue}" for issue in evidence_issues(arm))
        for key in sorted(METADATA_KEYS):
            if not known(arm.get("metadata", {}).get(key)):
                incompatible.append(f"{label} metadata.{key} is unknown")
        for key in ENV_KEYS:
            if not known(arm.get("env", {}).get(key)):
                incompatible.append(f"{label} env.{key} is unknown")
    for key in sorted(METADATA_KEYS):
        if baseline.get("metadata", {}).get(key) != current.get("metadata", {}).get(key):
            incompatible.append(f"metadata.{key} differs")
    for key in ENV_KEYS:
        if baseline.get("env", {}).get(key) != current.get("env", {}).get(key):
            incompatible.append(f"env.{key} differs")
    protocols = [arm.get("preparation_protocol") for arm in (baseline, current)]
    out["cache_state_evidence"] = "unverified_declarations" if not any(protocols) else "per_trial_reviewed_protocol_not_authenticated"
    out["decision_scope"] = "timed command; preparation costs are reported separately"
    if any(protocols):
        if not all(protocols):
            incompatible.append("per-trial preparation evidence is absent for one arm")
        elif preparation.mismatches(protocols[0]["expected_states"], protocols[1]["expected_states"]):
            incompatible.append("controlled starting cache-layer states differ between arms")
        if all(protocols):
            nonces = [row["nonce"] for arm in (baseline, current) for row in arm.get("preparation_runs", [])]
            if len(nonces) != len(set(nonces)):
                incompatible.append("preparation reuses a trial nonce across compared arms")
    source = [arm.get("provenance", {}).get("before", {}).get("git", {}) for arm in (baseline, current)]
    fingerprints = [item.get("fingerprint") for item in source]
    if any(not known(value) for value in fingerprints):
        incompatible.append("Git source fingerprints are unverifiable")
    elif fingerprints[0] != fingerprints[1]:
        reviews = [arm.get("metadata", {}).get("reviewed_comparison") for arm in (baseline, current)]
        review = reviews[0]
        reviewed = isinstance(review, dict) and set(review) == REVIEW_KEYS and all(valid_text(v) and known(v) for v in review.values())
        if (paired or not reviewed or review != reviews[1] or review["baseline_fingerprint"] != fingerprints[0]
                or review["candidate_fingerprint"] != fingerprints[1]):
            incompatible.append("Git sources differ without matching reviewed immutable source/workload evidence")
        else:
            out["reviewed_source_difference"] = review
    a, b = _samples(baseline), _samples(current)
    if min(len(a), len(b)) < MIN_RUNS_FOR_DECISION:
        warnings.append(f"fewer than {MIN_RUNS_FOR_DECISION} successful runs per arm: preliminary only")
    if min(len(a), len(b)) < MIN_TAIL_RUNS:
        warnings.append(f"fewer than {MIN_TAIL_RUNS} successful runs per arm: p95 uncertainty is preliminary")
    if paired:
        pairs = []
        try:
            aa = {run["pair"]: run["seconds"] for run in baseline["runs"] if run["exit"] == 0}
            bb = {run["pair"]: run["seconds"] for run in current["runs"] if run["exit"] == 0}
            if len(aa) != len(a) or len(bb) != len(b) or aa.keys() != bb.keys():
                raise ValueError("missing or duplicate pair")
            pairs = sorted(aa)
            a, b = [aa[i] for i in pairs], [bb[i] for i in pairs]
        except (KeyError, TypeError, ValueError):
            incompatible.append("paired evidence is incomplete or malformed")
        out["pairs"] = len(pairs)
    out["comparable"], out["warnings"] = not incompatible, incompatible + warnings
    if incompatible or not a or not b:
        return out
    for key, q in (("p50", 50), ("p95", 95)):
        base, candidate = pct(a, q), pct(b, q)
        out[f"{key}_delta_s"] = round(candidate - base, 6)
        out[f"{key}_delta_pct"] = round((candidate - base) / base * 100, 2) if base > 0 else UNKNOWN
    if all(protocols):
        totals = []
        for arm in (baseline, current):
            by_sequence = {row["sequence"]: row for row in arm["preparation_runs"]}
            totals.append([sample["seconds"] + by_sequence[sample["preparation_sequence"]]["wall_seconds"] for sample in arm["runs"]])
        deltas = {f"{key}_delta_s": round(pct(totals[1], q) - pct(totals[0], q), 6) for key, q in (("p50", 50), ("p95", 95))}
        out["preparation_cost"] = {"baseline": baseline["preparation_summary"], "candidate": current["preparation_summary"],
                                   "preparation_plus_command": deltas, "independent_verification": False}
        if all(pct(b, q) < pct(a, q) for q in (50, 95)) and any(pct(totals[1], q) >= pct(totals[0], q) for q in (50, 95)):
            out["warnings"].append("timed work improved but preparation plus command did not improve both observed percentiles")
    out["design"] = "balanced interleaved pairs" if paired else "separate arms; temporal drift is not controlled"
    if len(a) >= 2 and len(b) >= 2:
        out["confidence"] = confidence(a, b, paired, seed)
        intervals = out["confidence"]["intervals"]
        improved = all(intervals[key][1] < 0 for key in ("p50_delta_s", "p95_delta_s"))
        regressed = all(intervals[key][0] > 0 for key in ("p50_delta_s", "p95_delta_s"))
        if not improved and not regressed:
            out["warnings"].append("noise/uncertainty does not separate both p50 and p95 from zero")
        if not out["warnings"]:
            out["decision"] = "PERFORMANCE IMPROVEMENT SIGNAL" if improved else "PERFORMANCE REGRESSION SIGNAL"
    return out


def paired_schedule(runs: int, seed: int) -> list[list[int]]:
    rng = random.Random(seed)
    orders = [[0, 1]] * (runs // 2) + [[1, 0]] * (runs // 2)
    if runs % 2:
        orders.append([0, 1] if rng.randrange(2) else [1, 0])
    rng.shuffle(orders)
    return orders


def write_result(path: Path, result: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(result, stream, indent=2, allow_nan=False)
            stream.write("\n")
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--" not in argv:
        if "--help" in argv or "-h" in argv:
            argv = [*argv, "--"]  # Let argparse render help before enforcing a command.
        else:
            print("error: put the command after `--`", file=sys.stderr)
            return 2
    split, cmd = argv.index("--"), argv[argv.index("--") + 1:]
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--label", required=True)
    ap.add_argument("--runs", type=int, default=10)
    ap.add_argument("--warmup", type=int, default=0, help="recorded warmups before measured trials")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--cwd", type=Path, default=Path.cwd())
    ap.add_argument("--timeout", type=float, help="positive finite seconds per command; timeout is exit 124")
    ap.add_argument("--baseline", type=Path, help="earlier benchmark JSON; absent provenance means insufficient evidence")
    ap.add_argument("--metadata", type=Path, help="non-secret workload, repository, toolchain, cache and runner declarations")
    ap.add_argument("--candidate-command", type=Path, help="reviewed JSON argv array for paired comparison")
    ap.add_argument("--candidate-label", default="candidate")
    ap.add_argument("--candidate-metadata", type=Path)
    ap.add_argument("--preparation", type=Path, help="reviewed ci-benchmark-preparation/1 JSON before every warmup/trial")
    ap.add_argument("--candidate-preparation", type=Path, help="candidate preparation protocol; defaults to --preparation")
    ap.add_argument("--seed", type=int, default=0, help="reproducible order/bootstrap seed")
    ap.add_argument("--quiet", action="store_true", help="discard command output")
    ap.add_argument("--allow-failures", action="store_true", help="continue ordinary failed samples; comparison stays ineligible")
    args = ap.parse_args(argv[:split])
    if not cmd or not cmd[0] or any("\0" in item for item in cmd):
        ap.error("empty or invalid command after `--`")
    if args.runs < 1 or args.warmup < 0:
        ap.error("--runs must be >= 1 and --warmup >= 0")
    if args.timeout is not None and (not math.isfinite(args.timeout) or args.timeout <= 0):
        ap.error("--timeout must be positive and finite")
    if os.name != "posix":
        ap.error("safe process-tree execution is supported only on POSIX; Windows is unsupported")
    if not args.cwd.is_dir():
        ap.error("--cwd must be an existing directory")
    if args.baseline and args.candidate_command:
        ap.error("--baseline and --candidate-command are mutually exclusive")
    if args.candidate_metadata and not args.candidate_command:
        ap.error("--candidate-metadata requires --candidate-command")
    if args.candidate_preparation and (not args.candidate_command or not args.preparation):
        ap.error("--candidate-preparation requires --candidate-command and --preparation")
    if not args.label.strip() or (args.candidate_command and (not args.candidate_label.strip() or args.candidate_label == args.label)):
        ap.error("arm labels must be non-empty and distinct")
    for path in (args.baseline, args.metadata, args.candidate_command, args.candidate_metadata, args.preparation, args.candidate_preparation):
        if path and path.resolve() == args.out.resolve():
            ap.error("--out must not overwrite an experiment input")
    try:
        baseline = json.loads(args.baseline.read_text(encoding="utf-8")) if args.baseline else None
        if baseline is not None:
            validate_benchmark(baseline)
        read_metadata(args.metadata)
        candidate = read_command(args.candidate_command) if args.candidate_command else None
        if args.candidate_metadata:
            read_metadata(args.candidate_metadata)
        protocols = [preparation.load_protocol(args.preparation) if args.preparation else None]
        if candidate:
            protocols.append(preparation.load_protocol(args.candidate_preparation) if args.candidate_preparation else protocols[0])
    except (OSError, ValueError, UnicodeError) as exc:
        ap.error(str(exc))

    start = stamp()  # Before provenance and every warmup, not after measured work.
    experiment_started = time.perf_counter()
    commands = [cmd] + ([candidate] if candidate else [])
    labels = [args.label] + ([args.candidate_label] if candidate else [])
    metadata_paths = [args.metadata] + ([args.candidate_metadata or args.metadata] if candidate else [])
    arms = [{"schema": "ci-benchmark/2", "label": label, "command": command, "cwd": str(args.cwd.resolve()),
             "started_at": start, "warmup": args.warmup, "runs_requested": args.runs, "warmup_runs": [], "runs": [],
             "cache_state_evidence": "unverified" if protocol is None else "per_trial_reviewed_protocol",
             "aborted": False, "limitations": ["Workload/toolchain/cache declarations need independent verification.",
               "Process-group containment excludes commands that daemonize or escape the session.",
               "Ignored files and external dependencies are outside the Git fingerprint.",
               "Per-trial state evidence is caller supplied, not authenticated disk/cache contents."],
             **({"preparation_protocol": protocol, "preparation_runs": []} if protocol is not None else {})}
            for label, command, protocol in zip(labels, commands, protocols)]
    before, interrupted, aborted, sequence = [], None, False, 0
    schedule = paired_schedule(args.runs, args.seed) if candidate else [[0] for _ in range(args.runs)]
    try:
        with signal_handlers():
            before = [snapshot(args.cwd, path) for path in metadata_paths]
            for phase, orders in (("warmup_runs", [[*range(len(arms))] for _ in range(args.warmup)]), ("runs", schedule)):
                for pair, order in enumerate(orders, 1):
                    for position, index in enumerate(order, 1):
                        sequence += 1
                        if protocols[index] is not None:
                            prepared, work_before = prepare_trial(protocols[index], args.cwd, metadata_paths[index], args.quiet)
                            prepared.update(sequence=sequence, pair=pair, position=position, phase=phase)
                            arms[index]["preparation_runs"].append(prepared)
                            interrupted = prepared["execution"].get("interrupted_signal")
                            if prepared["issues"] or interrupted:
                                aborted = True
                                break
                        sample = run_trial(commands[index], args.cwd, args.timeout, args.quiet)
                        sample.update(sequence=sequence, pair=pair, position=position)
                        if protocols[index] is not None:
                            sample["preparation_sequence"] = sequence
                            with signal_handlers(ignore=True):
                                sample["provenance"] = provenance(work_before, snapshot(args.cwd, metadata_paths[index]))
                        arms[index][phase].append(sample)
                        print(f"[{labels[index]}] {phase} {pair}: {sample['seconds']:.3f}s exit={sample['exit']}", file=sys.stderr)
                        interrupted = sample.get("interrupted_signal")
                        if (interrupted or not sample["cleanup"]["complete"]
                                or sample.get("provenance", {}).get("changes")
                                or (sample["exit"] != 0 and not args.allow_failures)):
                            aborted = True
                            break
                    if aborted:
                        break
                if aborted:
                    break
    except KeyboardInterrupt as exc:
        interrupted, aborted = getattr(exc, "signum", signal.SIGINT), True
    with signal_handlers(ignore=True):
        after = [snapshot(args.cwd, path) for path in metadata_paths]
        finish = stamp()
        for index, arm in enumerate(arms):
            initial = before[index] if len(before) > index else {**after[index], "git": {"sha": UNKNOWN, "dirty": UNKNOWN, "fingerprint": UNKNOWN}}
            arm.update(finished_at=finish, provenance=provenance(initial, after[index]), metadata=initial["metadata"], env=initial["env"],
                       git_sha=initial["git"]["sha"] + ("-dirty" if initial["git"]["dirty"] is True else ""), aborted=aborted)
            arm["warmup_failures"] = sum(sample["exit"] != 0 for sample in arm["warmup_runs"])
            arm["timed_failures"] = sum(sample["exit"] != 0 for sample in arm["runs"])
            arm["failures"] = arm["warmup_failures"] + arm["timed_failures"]
            if "preparation_runs" in arm:
                records = arm["preparation_runs"]
                arm["preparation_failures"] = sum(bool(row["issues"]) or row["execution"]["exit"] != 0 for row in records)
                arm["failures"] += arm["preparation_failures"]
                arm["preparation_summary"] = preparation_summary(records)
            arm["completed"] = not aborted and len(arm["runs"]) == args.runs and len(arm["warmup_runs"]) == args.warmup
            arm["summary"] = summarize(_samples(arm))
            if interrupted:
                arm["interrupted_signal"] = interrupted
        result = arms[0]
        result["experiment_elapsed_s"] = round(time.perf_counter() - experiment_started, 6)
        if candidate:
            result["candidate"] = arms[1]
            result["paired_design"] = {"method": "balanced randomized AB/BA", "seed": args.seed,
                                       "orders": [[labels[i] for i in order] for order in schedule]}
            result["comparison"] = compare(arms[1], arms[0], paired=True, seed=args.seed)
        elif baseline is not None:
            result["comparison"] = compare(result, baseline, seed=args.seed)
        try:
            write_result(args.out, result)
        except OSError as exc:
            print(f"error: cannot save benchmark evidence: {exc}", file=sys.stderr)
            return 2
    print(json.dumps({"summary": result["summary"], "failures": sum(arm["failures"] for arm in arms),
                      **({"comparison": result["comparison"]} if "comparison" in result else {})}, indent=1))
    if interrupted:
        return 128 + interrupted
    return 1 if aborted or (not args.allow_failures and any(arm["failures"] for arm in arms)) else 0


if __name__ == "__main__":
    sys.exit(main())
