"""Internal bounded protocol parsing for reviewed per-trial preparation.

This module never executes commands or cleans caches. Evidence references remain
caller supplied; a nonce binds a response to a trial, not to truthful disk state.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

from cache_evidence import LAYERS, reference, validate_states

MAX_BYTES = 64 * 1024
FIELDS = {"schema", "review_reference", "command", "timeout_s", "expected_states"}


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def read_json(path: Path) -> object:
    """Reject links, devices, stale partial JSON and unbounded output."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("preparation evidence must be a regular file")
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("preparation JSON exceeds size limit")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate preparation JSON key")
            result[key] = value
        return result

    def nonfinite(_):
        raise ValueError("nonfinite preparation JSON value")

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=unique, parse_constant=nonfinite)
    except RecursionError as exc:
        raise ValueError("preparation JSON nesting exceeds parser limits") from exc


def protocol(value: object, *, recorded: bool = False) -> dict:
    keys = FIELDS | ({"sha256"} if recorded else set())
    if not isinstance(value, dict) or set(value) != keys or value["schema"] != "ci-benchmark-preparation/1":
        raise ValueError("invalid reviewed preparation protocol")
    command, timeout = value["command"], value["timeout_s"]
    if (not isinstance(command, list) or not 1 <= len(command) <= 128
            or any(not isinstance(arg, str) or "\0" in arg or len(arg) > 8192 for arg in command) or not command[0]):
        raise ValueError("preparation command must be bounded exact reviewed argv")
    if type(timeout) not in (int, float) or not 0 < timeout <= sys.float_info.max:
        raise ValueError("preparation timeout_s must be positive and finite")
    review = reference(value["review_reference"])
    if review is None:
        raise ValueError("preparation requires a review reference")
    normalized = {"schema": value["schema"], "review_reference": review, "command": command,
                  "timeout_s": timeout, "expected_states": validate_states(value["expected_states"])}
    fingerprint = digest(normalized)
    if recorded and value["sha256"] != fingerprint:
        raise ValueError("preparation protocol fingerprint does not match its contents")
    return {**normalized, "sha256": fingerprint}


def load_protocol(path: Path) -> dict:
    return protocol(read_json(path))


def response(value: object, nonce: str) -> dict:
    if (not isinstance(value, dict) or set(value) != {"schema", "nonce", "states"}
            or value["schema"] != "ci-benchmark-state/1" or value["nonce"] != nonce
            or re.fullmatch(r"[0-9a-f]{32}", nonce) is None):
        raise ValueError("preparation response is malformed or belongs to another trial")
    return validate_states(value["states"])


def mismatches(expected: dict, observed: dict) -> list[str]:
    return [f"{layer}.{key}" for layer in LAYERS for key in ("state", "identity")
            if expected[layer][key] != observed[layer][key]]
