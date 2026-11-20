#!/usr/bin/env python3
"""Run one argv under a strict elapsed-time budget; never retry a failed check.

Usage: run_budget.py --seconds 5 --label lint [--json evidence.json] -- COMMAND ARG...
Only exit 0 when the command succeeds strictly before the budget. Time includes
process launch and supervision, measured with a monotonic clock. This does not
measure runner queue/setup time, certify p50/p95, or replace the full CI suite.

On POSIX, start an owned session and terminate its process group with TERM then
KILL at the deadline. A successful child that leaves live descendants also fails.
Commands must not daemonize or escape the process group. On Windows the fallback
terminates/kills only the direct child: descendant cleanup is NOT guaranteed;
use a POSIX runner/WSL when process-tree containment is required. That limitation
is reported on stderr and in evidence, never presented as verified tree cleanup.

Inherit stdout/stderr unchanged. The wrapper records neither argv nor environment;
the caller must avoid secrets in labels and the command's own output. Cleanup is
bounded and any cleanup/evidence-write failure stays nonzero. No shell is used.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

POSIX = os.name == "posix"
TERM_GRACE_SECONDS = 0.15
KILL_GRACE_SECONDS = 0.5
POLL_SECONDS = 0.01


def positive_seconds(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("seconds must be positive and finite") from exc
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("seconds must be positive and finite")
    return number


def command_exit_code(returncode: int) -> int:
    """Represent signal exits conventionally; never truncate a failure to zero."""
    if returncode < 0:
        return min(255, 128 - returncode)
    return returncode if returncode <= 255 else 1


class Interrupted(BaseException):
    def __init__(self, signum: int):
        self.signum = signum


def interrupt(signum, _frame):
    raise Interrupted(signum)


@contextmanager
def signal_handlers(*, ignore: bool = False):
    previous = {}
    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, signal.SIG_IGN if ignore else interrupt)
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
    # Orphan zombies can persist under container PID 1 but cannot execute.
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
            return True  # Unknown cleanup cannot turn a check green.
    return True


def stop_process(proc: subprocess.Popen) -> dict:
    started = time.monotonic()
    error = False
    with signal_handlers(ignore=True):
        for force, grace in ((False, TERM_GRACE_SECONDS), (True, KILL_GRACE_SECONDS)):
            try:
                if POSIX:
                    os.killpg(proc.pid, signal.SIGKILL if force else signal.SIGTERM)
                elif force:
                    proc.kill()
                else:
                    proc.terminate()
            except ProcessLookupError:
                pass
            except OSError:
                error = True
            deadline = time.monotonic() + grace
            while time.monotonic() < deadline:
                proc.poll()  # Reap our child before inspecting its group.
                if proc.returncode is not None and (not POSIX or not group_alive(proc.pid)):
                    break
                time.sleep(POLL_SECONDS)
            proc.poll()
            if proc.returncode is not None and (not POSIX or not group_alive(proc.pid)):
                break
    complete = proc.poll() is not None and (not POSIX or not group_alive(proc.pid))
    return {
        "complete": complete and not error,
        "seconds": time.monotonic() - started,
        "scope": "process_group" if POSIX else "direct_child_only",
        "tree_verified": complete and not error if POSIX else False,
    }


def run_command(command: list[str], seconds: float, label: str) -> dict:
    if isinstance(seconds, bool) or not isinstance(seconds, (float, int)) or not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("seconds must be positive and finite")
    if not command or not command[0] or any(not isinstance(arg, str) or "\0" in arg for arg in command):
        raise ValueError("command must be a non-empty argv without NUL bytes")
    started = time.monotonic()
    proc = None
    command_exit = None
    status, exit_code = "spawn_error", 127
    cleanup = {"complete": True, "seconds": 0.0,
               "scope": "process_group" if POSIX else "direct_child_only",
               "tree_verified": POSIX}
    with signal_handlers():
        try:
            proc = subprocess.Popen(command, start_new_session=POSIX)
            remaining = seconds - (time.monotonic() - started)
            if remaining <= 0:
                raise subprocess.TimeoutExpired("command", seconds)
            command_exit = proc.wait(timeout=remaining)
            exit_code = command_exit_code(command_exit)
            status = "passed" if exit_code == 0 else "command_failed"
            if POSIX and group_alive(proc.pid):
                cleanup = stop_process(proc)
                status, exit_code = "background_descendants", exit_code or 125
        except subprocess.TimeoutExpired:
            status, exit_code = "budget_exceeded", 124
            cleanup = stop_process(proc)
            command_exit = proc.returncode
        except (Interrupted, KeyboardInterrupt) as exc:
            signum = getattr(exc, "signum", signal.SIGINT)
            status, exit_code = "interrupted", 128 + signum
            if proc is not None:
                cleanup = stop_process(proc)
                command_exit = proc.returncode
        except OSError:
            # Exception messages may contain a sensitive executable/argv path.
            status, exit_code = "spawn_error", 127
            if proc is not None:
                cleanup = stop_process(proc)
                command_exit = proc.returncode
    elapsed = time.monotonic() - started
    over_budget = elapsed >= seconds
    if over_budget and status in {"passed", "command_failed"}:
        status, exit_code = "budget_exceeded", 124
    if not cleanup["complete"]:
        status, exit_code = "cleanup_incomplete", exit_code or 125
    return {
        "schema_version": 1,
        "label": label,
        "scope": "single_command_elapsed_time",
        "budget_seconds": seconds,
        "elapsed_seconds": elapsed,
        "over_budget": over_budget,
        "status": status,
        "command_exit": command_exit,
        "exit_code": exit_code,
        "cleanup": cleanup,
    }


def write_evidence(path: Path, result: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".budget-", suffix=".json", delete=False) as stream:
            temp_path = Path(stream.name)
            json.dump(result, stream, indent=2, allow_nan=False)
            stream.write("\n")
        os.replace(temp_path, path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seconds", type=positive_seconds, required=True)
    parser.add_argument("--label", required=True, help="non-secret check name")
    parser.add_argument("--json", type=Path, dest="evidence", help="optional JSON evidence file")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if len(args.command) < 2 or args.command[0] != "--":
        parser.error("put a non-empty command argv after --")
    if not args.label.strip() or "\0" in args.label:
        parser.error("label must be non-empty and contain no NUL bytes")
    if not POSIX:
        print("run_budget: Windows fallback controls only the direct child; process-tree cleanup is unverified.", file=sys.stderr)
    try:
        result = run_command(args.command[1:], args.seconds, args.label)
    except ValueError as exc:
        parser.error(str(exc))
    if args.evidence is not None:
        try:
            write_evidence(args.evidence, result)
        except (OSError, ValueError):
            print("run_budget: could not write evidence; this check cannot pass.", file=sys.stderr)
            return result["exit_code"] or 125
    print(f"run_budget: {json.dumps(args.label)} {result['status']}; "
          f"elapsed={result['elapsed_seconds']:.6f}s budget={args.seconds:g}s", file=sys.stderr)
    return result["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
