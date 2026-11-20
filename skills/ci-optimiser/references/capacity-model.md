# Optional capacity and cost model

This is a local, conditional simulation, not provider scheduling or billing
telemetry. It does not change `graph_metrics.py` or its unlimited-capacity model.
Run `python3 scripts/capacity_model.py capacity.json`. Exit 0 means scheduling
was modeled, **not** that cost is known; inspect each cost status. Exit 1 means
an unresolved model; exit 2 means a file/JSON error. No network or writes occur.

## Input

```json
{
  "schema": "ci-capacity-model/1",
  "graph": {
    "schema": "ci-job-graph/1",
    "complete": true,
    "matrix_resolved": true,
    "required_nodes": ["unit", "lint"],
    "nodes": [
      {"id": "unit", "needs": [], "duration_s": 90},
      {"id": "lint", "needs": [], "duration_s": 30}
    ]
  },
  "pools": {
    "linux": {
      "capacity": 1,
      "pricing": {
        "per_minute": 0.10,
        "billing_quantum_s": 60,
        "currency": "USD",
        "source": "illustrative assumption, NOT a provider price"
      }
    }
  },
  "node_resources": {
    "unit": {"pool": "linux", "exclusive": ["test-database"]},
    "lint": {"pool": "linux", "exclusive": []}
  }
}
```

Every node must have one binding and consumes one pool slot for its full
`duration_s`. Capacity is a positive integer. `exclusive` is an optional list
of unique, nonblank lock names; these locks exclude overlap **across pools**.
Locks are acquired together at job start and released at job completion.
Only include locks that actually serialize the target workload. A named pool
is assumed homogeneous; distinct runner classes require distinct pools.

The graph retains its existing completeness and matrix-resolution requirements.
Unresolved dependencies, cycles, missing duration/capacity/bindings yield
`unknown`. Existing graph counterfactuals are not simulated here: supply a
separate explicit input for each candidate and compare conditional outputs.

## Scheduling and cost interpretation

Ready jobs are considered in lexical node-ID order at each finish event. This
is deterministic list scheduling, **not an optimal schedule or a prediction of
provider queue ordering**. All nodes run, including non-required jobs that may
compete with required jobs. The model reports required-job completion,
all-job elapsed time, and sum of runner time separately. Zero-duration nodes
release their slots and locks at the same timestamp.

Pricing is optional. For each job the model rounds its duration upward to the
explicit billing quantum and multiplies billed minutes by the supplied rate.
Rates, currency, and source are user-supplied assumptions, not verified pricing.
No prices are bundled. Missing pricing leaves total cost `unknown`, not zero;
known per-job values remain visible. Explicit zero is permitted only as an
explicit supplied rate. Mixed currencies are never silently summed.

Optional `observed_cost` must contain a finite nonnegative `amount`, nonblank
`currency`, `source`, and `scope`. This field is copied separately as a
user-supplied observation, not independently verified. An invoice for a month
is not comparable to one workflow unless their scopes are reconciled. Modeled
cost never becomes observed merely because its durations came from telemetry.

Limits: no external contention, dynamic runner availability, priority policy,
fail-fast/cancellation, retries, conditional paths, or automatic matrix
expansion. Include setup and teardown occupancy in supplied durations. This
per-job variable-cost calculation excludes fixed self-hosted infrastructure
costs, idle capacity, credits, discounts, and taxes. It is not a spending
forecast or a bill. Reducing elapsed time through parallelism need not reduce
runner time or cost. Validate changes against actual runs before adoption.
