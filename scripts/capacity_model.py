"""Optional deterministic capacity scheduling and explicit-rate cost estimates.

Consumes ci-capacity-model/1, never infers runner pools or current provider prices.
The precedence-only graph_metrics API is deliberately unchanged.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from graph_metrics import evaluate_graph, names, number

UNKNOWN = "unknown"


def positive_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _evaluate_capacity(spec: object) -> dict:
    out = {"schema": "ci-capacity-analysis/1", "status": UNKNOWN, "gaps": [],
           "required_completion_s": UNKNOWN, "elapsed_s": UNKNOWN, "runner_s": UNKNOWN,
           "schedule": [], "modeled_cost": {"status": UNKNOWN, "amount": UNKNOWN},
           "observed_cost": {"status": UNKNOWN, "amount": UNKNOWN},
           "assumptions": ["Non-preemptive, lexical ready-job priority; not an optimal schedule",
                           "All graph nodes run; pools and exclusive locks are isolated to this model",
                           "No external queue, retries, startup overhead or cancellation unless included in durations",
                           "Required completion is not merge eligibility; runner cost is not elapsed time"]}
    if not isinstance(spec, dict) or spec.get("schema") != "ci-capacity-model/1":
        out["gaps"].append("unsupported or missing model schema")
        return out
    graph = spec.get("graph")
    baseline = evaluate_graph(graph)
    if baseline["status"] != "modeled":
        out["gaps"].extend(baseline["gaps"])
        return out
    pools, bindings = spec.get("pools"), spec.get("node_resources")
    if not isinstance(pools, dict) or not pools or any(
        not isinstance(k, str) or not k.strip() or not isinstance(v, dict)
        or not positive_int(v.get("capacity")) for k, v in pools.items()
    ):
        out["gaps"].append("every pool requires a positive integer capacity")
        return out
    nodes = {n["id"]: n for n in graph["nodes"]}
    if not isinstance(bindings, dict) or set(bindings) != set(nodes):
        out["gaps"].append("node_resources must bind exactly every graph node")
        return out
    for key, binding in bindings.items():
        if (not isinstance(binding, dict) or not isinstance(binding.get("pool"), str)
                or binding["pool"] not in pools or not names(binding.get("exclusive", []))):
            out["gaps"].append(f"invalid pool or exclusive resources for {key}")
    if out["gaps"]:
        return out
    pending, running, completed = set(nodes), {}, {}
    occupied = {pool: 0 for pool in pools}
    locks, now = set(), 0
    while pending or running:
        # Release every finish at this timestamp before choosing the next batch.
        for key in sorted(list(running)):
            if running[key] <= now:
                completed[key] = running.pop(key)
                occupied[bindings[key]["pool"]] -= 1
                locks.difference_update(bindings[key].get("exclusive", []))
        launched = False
        for key in sorted(pending):
            node, binding = nodes[key], bindings[key]
            pool, needed = binding["pool"], set(binding.get("exclusive", []))
            if (all(dep in completed for dep in node["needs"])
                    and occupied[pool] < pools[pool]["capacity"] and not locks.intersection(needed)):
                finish = now + node["duration_s"]
                if not math.isfinite(finish):
                    out["gaps"].append("schedule exceeds finite numeric range")
                    return out
                running[key] = finish
                occupied[pool] += 1
                locks.update(needed)
                pending.remove(key)
                out["schedule"].append({"id": key, "pool": pool, "start_s": now, "finish_s": finish})
                launched = True
        if running:
            now = min(running.values())
        elif pending and not launched:
            out["gaps"].append("unschedulable graph")
            return out
    runner_s = sum(node["duration_s"] for node in nodes.values())
    if not math.isfinite(runner_s):
        out["gaps"].append("runner time exceeds finite numeric range")
        return out
    out.update(status="modeled", elapsed_s=now, runner_s=runner_s,
               required_completion_s=max(completed[key] for key in graph["required_nodes"]),
               precedence_only_required_s=baseline["required_completion_s"])
    costs, gaps, currencies = [], [], set()
    for key in sorted(nodes):
        pool = bindings[key]["pool"]
        price = pools[pool].get("pricing")
        if (not isinstance(price, dict) or not number(price.get("per_minute"))
                or not number(price.get("billing_quantum_s")) or price["billing_quantum_s"] <= 0
                or not isinstance(price.get("currency"), str) or not price["currency"].strip()
                or not isinstance(price.get("source"), str) or not price["source"].strip()):
            gaps.append(f"explicit rate, positive billing quantum, currency and source missing for {pool}")
            costs.append({"id": key, "amount": UNKNOWN})
            continue
        duration, quantum = nodes[key]["duration_s"], price["billing_quantum_s"]
        ratio = duration / quantum
        if not math.isfinite(ratio):
            gaps.append(f"billing arithmetic exceeds finite numeric range for {key}")
            costs.append({"id": key, "amount": UNKNOWN})
            continue
        billed = math.ceil(ratio) * quantum
        amount = billed / 60 * price["per_minute"]
        if not math.isfinite(amount):
            gaps.append(f"cost exceeds finite numeric range for {key}")
            costs.append({"id": key, "amount": UNKNOWN})
            continue
        currencies.add(price["currency"])
        costs.append({"id": key, "billed_runner_s": billed, "amount": amount,
                      "currency": price["currency"], "rate_source": price["source"]})
    if len(currencies) > 1:
        gaps.append("mixed currencies cannot be totaled without explicit conversion")
    total = sum(row["amount"] for row in costs if number(row["amount"]))
    if not math.isfinite(total):
        gaps.append("total cost exceeds finite numeric range")
    out["modeled_cost"] = {"status": UNKNOWN if gaps else "modeled", "amount": UNKNOWN if gaps else total,
                           "currency": next(iter(currencies)) if len(currencies) == 1 else UNKNOWN,
                           "per_job": costs, "gaps": sorted(set(gaps)),
                           "scope": "All nodes; per-job billing rounding; excludes discounts, credits, taxes and fixed infrastructure costs"}
    actual = spec.get("observed_cost")
    if actual is not None:
        if (isinstance(actual, dict) and number(actual.get("amount"))
                and all(isinstance(actual.get(k), str) and actual[k].strip()
                        for k in ("currency", "source", "scope"))):
            out["observed_cost"] = {"status": "observed", **{k: actual[k] for k in ("amount", "currency", "source", "scope")},
                                    "verification": "user-supplied observation, not independently verified"}
        else:
            out["observed_cost"]["gaps"] = ["observed amount requires finite nonnegative amount, currency, source and scope"]
    return out


def evaluate_capacity(spec: object) -> dict:
    """Evaluate explicit input, failing closed on unrepresentable arithmetic."""
    try:
        return _evaluate_capacity(spec)
    except (OverflowError, ValueError):
        return {"schema": "ci-capacity-analysis/1", "status": UNKNOWN,
                "gaps": ["numeric input or arithmetic exceeds supported finite range"],
                "required_completion_s": UNKNOWN, "elapsed_s": UNKNOWN,
                "runner_s": UNKNOWN, "schedule": [],
                "modeled_cost": {"status": UNKNOWN, "amount": UNKNOWN},
                "observed_cost": {"status": UNKNOWN, "amount": UNKNOWN}}


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Explicit ci-capacity-model/1 JSON")
    args = parser.parse_args()
    try:
        spec = json.loads(args.input.read_text(encoding="utf-8"), object_pairs_hook=unique_object)
        result = evaluate_capacity(spec)
        print(json.dumps(result, indent=2, allow_nan=False))
    except (OSError, ValueError, OverflowError) as error:
        parser.exit(2, f"capacity model: {error}\n")
    return 0 if result["status"] == "modeled" else 1


if __name__ == "__main__":
    raise SystemExit(main())
