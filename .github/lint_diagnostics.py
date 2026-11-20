#!/usr/bin/env python3
"""Observe a slow actionlint once; the enclosing run_budget owns its deadline.

The command inherits streams, environment, cwd and the existing process group.
There is no retry, capture of command output, or replacement timeout. Production
emits at most one bounded, allowlisted /proc record after four seconds. Never
collect argv, environment, cwd, scripts, stack dumps or raw descriptor targets.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import threading
import time

SNAPSHOT_AFTER_SECONDS = 4.0
COLLECTION_SECONDS = 0.1
MAX_PROCESSES = 12
MAX_PROC_ENTRIES = 1024
MAX_THREADS = 32
MAX_FDS = 64
MAX_READ_BYTES = 131072
MAX_OUTPUT_BYTES = 8192
PREFIX = "lint_diagnostics: "
UNKNOWN = "unknown"
WAIT_CHANNELS = frozenset({
    "futex_wait", "futex_wait_queue", "futex_wait_queue_me", "do_futex",
    "do_wait", "kernel_wait", "wait_woken", "pipe_read", "pipe_write",
    "anon_pipe_read", "anon_pipe_write", "hrtimer_nanosleep", "do_nanosleep",
    "poll_schedule_timeout", "ep_poll", "do_epoll_wait", "do_select",
    "unix_stream_read_generic", "unix_stream_sendmsg",
})


class LimitReached(Exception):
    pass


class Probe:
    """Bound every directory walk and read; never preserve OS error messages."""

    def __init__(self):
        self.started = time.monotonic()
        self.bytes_read = 0
        self.unknown_reads = 0
        self.limits = set()

    def check(self):
        if time.monotonic() - self.started >= COLLECTION_SECONDS:
            self.limits.add("collection_time")
            raise LimitReached
        if self.bytes_read >= MAX_READ_BYTES:
            self.limits.add("read_bytes")
            raise LimitReached

    def read(self, path, cap):
        self.check()
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                raw = os.read(descriptor, min(cap + 1, MAX_READ_BYTES - self.bytes_read))
            finally:
                os.close(descriptor)
            self.bytes_read += len(raw)
            if len(raw) > cap:
                self.limits.add("file_bytes")
                return None
            return raw.decode("ascii")
        except (OSError, UnicodeError):
            self.unknown_reads += 1
            return None

    def names(self, path, cap, reason):
        self.check()
        names = []
        try:
            with os.scandir(path) as entries:
                for index, entry in enumerate(entries):
                    self.check()
                    if index >= cap:
                        self.limits.add(reason)
                        return names, False
                    if re.fullmatch(r"[0-9]{1,10}", entry.name):
                        names.append(entry.name)
            return names, True
        except OSError:
            self.unknown_reads += 1
            return None, False

    def pipe_identity(self, path):
        self.check()
        try:
            info = os.stat(path)
            # Numeric identity detects descriptor reuse without reading a raw
            # symlink target or opening the descriptor's underlying content.
            return (info.st_dev, info.st_ino) if stat.S_ISFIFO(info.st_mode) else False
        except OSError:
            self.unknown_reads += 1
            return None


def parse_stat(raw, pid):
    if raw is None:
        return None
    try:
        prefix, tail = raw.rsplit(")", 1)
        identity, comm = prefix.split(" (", 1)
        fields = tail.split()
        if identity != str(pid) or fields[0] not in set("RSDZTWtXxKWPIN"):
            return None
        indices = {"ppid": 1, "pgid": 2, "session_id": 3, "cpu_user_ticks": 11,
                   "cpu_system_ticks": 12, "thread_count": 17, "start_ticks": 19,
                   "rss_pages": 21}
        row = {key: int(fields[index]) for key, index in indices.items()}
        if any(value < 0 or value > 2**63 - 1 for value in row.values()):
            return None
        return {"pid": pid, "role": "shellcheck" if comm == "shellcheck" else "other",
                "state": fields[0], **row}
    except (ValueError, IndexError):
        return None


def wait_channel(probe, path):
    raw = probe.read(path, 128)
    value = raw.strip() if raw is not None else None
    # A zero can mean unavailable/permission-masked; it does not prove running.
    return value if value in WAIT_CHANNELS else UNKNOWN


def thread_waits(probe, directory):
    names, complete = probe.names(directory / "task", MAX_THREADS, "threads")
    if names is None:
        return UNKNOWN
    counts = {}
    for name in names:
        channel = wait_channel(probe, directory / "task" / name / "wchan")
        counts[channel] = counts.get(channel, 0) + 1
    return {"observed": counts, "complete": complete and UNKNOWN not in counts}


def pipe_endpoints(probe, directory):
    names, complete = probe.names(directory / "fd", MAX_FDS, "descriptors")
    if names is None:
        return UNKNOWN
    counts = {"read": 0, "write": 0, "read_write": 0}
    unknown = 0
    for name in names:
        identity = probe.pipe_identity(directory / "fd" / name)
        if identity is None:
            unknown += 1
        elif identity is not False:
            raw = probe.read(directory / "fdinfo" / name, 512)
            flags = re.search(r"(?m)^flags:\s*([0-7]{1,12})$", raw) if raw is not None else None
            mode = int(flags[1], 8) & os.O_ACCMODE if flags else None
            direction = {os.O_RDONLY: "read", os.O_WRONLY: "write", os.O_RDWR: "read_write"}.get(mode)
            if direction is None or probe.pipe_identity(directory / "fd" / name) != identity:
                unknown += 1
            else:
                counts[direction] += 1
    return {"observed": counts, "unknown_entries": unknown, "complete": complete and unknown == 0}


def process_row(probe, root, pid, pgid, role=None, initial=None):
    directory = root / str(pid)
    first = initial or parse_stat(probe.read(directory / "stat", 4096), pid)
    missing = {"pid": pid, "role": role or UNKNOWN, "status": UNKNOWN}
    if first is None or first["pgid"] != pgid:
        return missing
    row = {**first, "role": role or first["role"], "status": "observed",
           "wait_channel": wait_channel(probe, directory / "wchan"),
           "thread_wait_channels": thread_waits(probe, directory),
           "pipe_endpoints": pipe_endpoints(probe, directory)}
    last = parse_stat(probe.read(directory / "stat", 4096), pid)
    if last is None or (last["pgid"], last["start_ticks"]) != (pgid, first["start_ticks"]):
        return missing  # Exit, reuse or group change cannot be reported as a zero.
    return row


def base_snapshot(elapsed, pgid):
    return {"schema": "ci-lint-process-snapshot/1", "elapsed_seconds": round(elapsed, 6),
            "scope": "owned_process_group", "process_group_id": pgid,
            "processes": [], "truncated": False, "limits_reached": []}


def system_unit(name):
    try:
        value = os.sysconf(name)
        return value if isinstance(value, int) and value > 0 else UNKNOWN
    except (OSError, ValueError):
        return UNKNOWN


def collect_snapshot(supervisor_pid, command_pid, pgid, elapsed, *, proc_root=Path("/proc")):
    snapshot = base_snapshot(elapsed, pgid)
    if supervisor_pid != pgid or not sys.platform.startswith("linux"):
        return {**snapshot, "status": UNKNOWN, "reason": "unowned_or_unsupported_group"}
    snapshot.update({"clock_ticks_per_second": system_unit("SC_CLK_TCK"),
                     "page_bytes": system_unit("SC_PAGESIZE")})
    probe = Probe()
    rows = snapshot["processes"]
    seen = {supervisor_pid, command_pid}
    try:
        # Prioritize the two known identities before the bounded group scan.
        for pid, role in ((supervisor_pid, "supervisor"), (command_pid, "actionlint")):
            rows.append(process_row(probe, proc_root, pid, pgid, role))
        names, complete = probe.names(proc_root, MAX_PROC_ENTRIES, "process_scan")
        snapshot["scan_complete"] = complete
        for name in names or ():
            pid = int(name)
            if pid in seen:
                continue
            first = parse_stat(probe.read(proc_root / name / "stat", 4096), pid)
            if first is None:
                snapshot["scan_complete"] = False
            elif first["pgid"] == pgid:
                if len(rows) >= MAX_PROCESSES:
                    probe.limits.add("processes")
                    snapshot["scan_complete"] = False
                    break
                rows.append(process_row(probe, proc_root, pid, pgid, initial=first))
    except LimitReached:
        snapshot["scan_complete"] = False
    snapshot.update({"truncated": bool(probe.limits), "limits_reached": sorted(probe.limits),
                     "unknown_reads": probe.unknown_reads,
                     "collection_seconds": round(time.monotonic() - probe.started, 6)})
    return snapshot


def render_snapshot(snapshot):
    def encode():
        return PREFIX + json.dumps(snapshot, separators=(",", ":"), allow_nan=False) + "\n"
    line = encode()
    if len(line.encode("utf-8")) > MAX_OUTPUT_BYTES:
        snapshot["truncated"] = True
        snapshot["limits_reached"].append("output_bytes")
        while snapshot["processes"] and len(line.encode("utf-8")) > MAX_OUTPUT_BYTES:
            snapshot["processes"].pop()
            line = encode()
    return line


def emit_snapshot(supervisor_pid, command_pid, started):
    """The supervisor waits at most 100ms even if a proc read stalls."""
    pgid = os.getpgrp()
    result = []
    def collect():
        try:
            result.append(collect_snapshot(supervisor_pid, command_pid, pgid, time.monotonic() - started))
        except Exception:
            result.append({**base_snapshot(time.monotonic() - started, pgid),
                           "status": UNKNOWN, "reason": "collector_error"})
    worker = threading.Thread(target=collect, daemon=True)
    worker.start()
    worker.join(COLLECTION_SECONDS)
    snapshot = result[0] if result else {
        **base_snapshot(time.monotonic() - started, pgid), "status": UNKNOWN,
        "truncated": True, "limits_reached": ["collection_time"],
    }
    try:
        sys.stderr.write(render_snapshot(snapshot))
        sys.stderr.flush()
    except OSError:
        pass  # Unavailable diagnostics cannot rewrite the command's result.


def supervise(command, *, snapshot_after=SNAPSHOT_AFTER_SECONDS):
    # Only Python tests inject an earlier observation. CLI policy stays four seconds.
    if not math.isfinite(snapshot_after) or snapshot_after <= 0:
        raise ValueError("snapshot interval must be positive and finite")
    started = time.monotonic()
    try:
        proc = subprocess.Popen(command)  # Inherit streams and the budget-owned group.
    except (OSError, ValueError):
        print("lint_diagnostics: command could not start", file=sys.stderr)
        return 127
    try:
        return proc.wait(timeout=max(0, snapshot_after - (time.monotonic() - started)))
    except subprocess.TimeoutExpired:
        if proc.poll() is None:
            emit_snapshot(os.getpid(), proc.pid, started)
        return proc.wait()


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) < 2 or args[0] != "--":
        print("lint_diagnostics: expected -- followed by the command", file=sys.stderr)
        return 2
    code = supervise(args[1:])
    if code < 0:
        signum = -code
        if signum not in (signal.SIGKILL, signal.SIGSTOP):
            signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)  # Preserve a signal exit for the outer budget.
    return code


if __name__ == "__main__":
    raise SystemExit(main())
