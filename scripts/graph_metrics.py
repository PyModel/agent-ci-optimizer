"""Validated, precedence-only CI graph models. No inferred YAML or merge eligibility.

Input schema ci-job-graph/1 requires complete=true, matrix_resolved=true,
required_nodes, and nodes [{id, needs, duration_s}]. Optional counterfactuals are
[{name, durations_s: {node_id: replacement_seconds}}]. Durations include no
inferred queue or resource-contention model; outputs are conditional estimates.
"""
from __future__ import annotations

import math

UNKNOWN = "unknown"


def number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def names(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(x, str) and x.strip() for x in value) and len(set(value)) == len(value)


def evaluate_graph(graph: object) -> dict:
    """Return unknown on an incomplete/ambiguous graph, including malformed scenarios."""
    out = {"status": UNKNOWN, "required_completion_s": UNKNOWN, "critical_path": UNKNOWN,
           "model": "precedence-only, unlimited capacity; excludes queue, contention and unmodeled conditions",
           "gaps": [], "counterfactuals": []}
    if not isinstance(graph, dict) or graph.get("schema") != "ci-job-graph/1":
        out["gaps"].append("unsupported or missing graph schema")
        return out
    for field in ("complete", "matrix_resolved"):
        if graph.get(field) is not True:
            out["gaps"].append(f"{field} is not verified true")
    raw = graph.get("nodes")
    if not isinstance(raw, list) or not raw:
        out["gaps"].append("nodes must be a non-empty array")
        return out
    nodes = {}
    for node in raw:
        if not isinstance(node, dict) or not isinstance(node.get("id"), str) or not node["id"].strip():
            out["gaps"].append("invalid node identity")
            continue
        key = node["id"]
        if key in nodes:
            out["gaps"].append(f"duplicate node {key}")
        if not names(node.get("needs")) or not number(node.get("duration_s")):
            out["gaps"].append(f"invalid dependencies or unknown duration for {key}")
        if node.get("matrix_resolved", True) is not True or node.get("resolved", True) is not True:
            out["gaps"].append(f"unresolved node {key}")
        nodes[key] = node
    required = graph.get("required_nodes")
    if not names(required) or not required or any(key not in nodes for key in required):
        out["gaps"].append("required_nodes are missing, duplicate or unresolved")
    if out["gaps"]:
        return out
    for key, node in nodes.items():
        if any(dep not in nodes for dep in node["needs"]):
            out["gaps"].append(f"missing dependency for {key}")
    if out["gaps"]:
        return out
    remaining, order = set(nodes), []
    while remaining:
        ready = sorted(key for key in remaining if all(dep not in remaining for dep in nodes[key]["needs"]))
        if not ready:
            out["gaps"].append("cycle in dependency graph")
            return out
        order.extend(ready)
        remaining.difference_update(ready)

    def model(overrides: dict) -> dict:
        finishes, paths = {}, {}
        for key in order:
            parent = max(nodes[key]["needs"], key=lambda dep: (finishes[dep], dep), default=None)
            finishes[key] = (finishes[parent] if parent else 0) + overrides.get(key, nodes[key]["duration_s"])
            paths[key] = (paths[parent] if parent else []) + [key]
        gate = max(required, key=lambda key: (finishes[key], key))
        return {"required_completion_s": round(finishes[gate], 6), "critical_path": paths[gate],
                "gating_node": gate, "all_nodes_runner_s": round(sum(overrides.get(k, v["duration_s"]) for k, v in nodes.items()), 6),
                "earliest_finish_s": {key: round(finishes[key], 6) for key in sorted(finishes)}}

    baseline = model({})
    out.update(baseline, status="modeled")
    scenarios = graph.get("counterfactuals", [])
    if not isinstance(scenarios, list):
        out["counterfactuals"] = [{"status": UNKNOWN, "gaps": ["counterfactuals must be an array"]}]
        return out
    seen = set()
    for scenario in scenarios:
        name = scenario.get("name") if isinstance(scenario, dict) else None
        changes = scenario.get("durations_s") if isinstance(scenario, dict) else None
        valid = (isinstance(name, str) and bool(name.strip()) and name not in seen and isinstance(changes, dict)
                 and all(key in nodes and number(value) for key, value in changes.items()))
        if not valid:
            out["counterfactuals"].append({"name": name, "status": UNKNOWN,
                                            "gaps": ["invalid scenario identity, node reference or duration"]})
            continue
        seen.add(name)
        candidate = model(changes)
        out["counterfactuals"].append({"name": name, "status": "modeled", **candidate,
                                        "required_path_savings_s": round(baseline["required_completion_s"] - candidate["required_completion_s"], 6),
                                        "runner_savings_s": round(baseline["all_nodes_runner_s"] - candidate["all_nodes_runner_s"], 6)})
    return out
