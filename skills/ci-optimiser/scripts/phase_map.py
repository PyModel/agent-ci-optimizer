"""Internal reviewed phase-map validation; never infer semantics from display names."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re

PHASES = ("setup", "restore", "work", "save", "teardown")
MAX_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class PhaseMap:
    entries: tuple[dict, ...]
    review_reference: str
    sha256: str


def text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 1024 and "\0" not in value


def positive(value: object) -> bool:
    return type(value) is int and 0 < value <= 2 ** 53


def validate(value: object) -> PhaseMap | None:
    if value is None or isinstance(value, PhaseMap):
        return value
    if (not isinstance(value, dict) or set(value) != {"schema", "review_reference", "entries"}
            or value["schema"] != "ci-phase-map/1" or not text(value["review_reference"])
            or not isinstance(value["entries"], list) or not 1 <= len(value["entries"]) <= 1000):
        raise ValueError("invalid reviewed phase-map schema")
    entries, seen = [], set()
    fields = {"workflow_id", "config", "job_identity", "setup_end_step", "steps"}
    for entry in value["entries"]:
        if not isinstance(entry, dict) or set(entry) != fields:
            raise ValueError("phase-map entries require exact workflow/config/job/step bindings")
        identity = entry["job_identity"]
        if (not positive(entry["workflow_id"]) or not isinstance(entry["config"], str)
                or re.fullmatch(r"sha256:[0-9a-f]{64}", entry["config"]) is None
                or not isinstance(identity, dict) or set(identity) != {"field", "value"}
                or identity["field"] not in ("logical_job_id", "name") or not text(identity["value"])
                or not positive(entry["setup_end_step"])):
            raise ValueError("invalid phase-map identity or setup marker")
        key = (entry["workflow_id"], entry["config"], identity["field"], identity["value"])
        if key in seen:
            raise ValueError("duplicate phase-map identity")
        seen.add(key)
        steps = entry["steps"]
        if not isinstance(steps, list) or not 1 <= len(steps) <= 1000:
            raise ValueError("phase-map requires a bounded complete step inventory")
        by_number = {}
        for step in steps:
            if (not isinstance(step, dict) or set(step) != {"number", "phase"}
                    or not positive(step["number"]) or step["number"] in by_number
                    or step["phase"] not in PHASES):
                raise ValueError("phase-map step numbers must be unique and phases explicit")
            by_number[step["number"]] = step["phase"]
        if entry["setup_end_step"] not in by_number or by_number[entry["setup_end_step"]] not in ("setup", "restore"):
            raise ValueError("phase-map setup marker must identify a setup or restore step")
        if "work" not in by_number.values():
            raise ValueError("phase-map requires an explicit work step")
        first_work, last_work = min(n for n, p in by_number.items() if p == "work"), max(n for n, p in by_number.items() if p == "work")
        setup_numbers = [n for n, p in by_number.items() if p in ("setup", "restore")]
        if (max(setup_numbers) != entry["setup_end_step"] or entry["setup_end_step"] >= first_work
                or any(n <= last_work for n, p in by_number.items() if p in ("save", "teardown"))):
            raise ValueError("phase-map requires setup before work and save/teardown after work")
        entries.append({**entry, "steps": by_number})
    digest = hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
    return PhaseMap(tuple(entries), value["review_reference"], digest)


def select(mapping: PhaseMap, run: dict, job: dict) -> tuple[dict | None, str | None]:
    cohort = run.get("cohort") if isinstance(run.get("cohort"), dict) else {}
    workflow, config = run.get("workflow_id"), cohort.get("config")
    if not positive(workflow) or not isinstance(config, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", config) is None:
        return None, "reviewed phase-map workflow/config binding is absent or malformed"
    candidates = [entry for entry in mapping.entries if entry["workflow_id"] == workflow and entry["config"] == config
                  and job.get(entry["job_identity"]["field"]) == entry["job_identity"]["value"]]
    if len(candidates) != 1:
        return None, "reviewed phase-map job binding is missing or ambiguous"
    return candidates[0], None


def load(path: Path) -> PhaseMap:
    if path.stat().st_size > MAX_BYTES:
        raise ValueError("phase-map input exceeds size limit")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate phase-map JSON key")
            result[key] = value
        return result

    result = validate(json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique))
    if result is None:
        raise ValueError("phase-map input cannot be null")
    return result
