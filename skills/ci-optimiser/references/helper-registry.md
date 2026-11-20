# Helper inventory and capability mapping

The [feature matrix](feature-matrix.json) contains a `ci-helper-inventory/1` registry
covering every Python module under `scripts/`. Each entry declares `public` or
`internal`, its `cli` or `library` interface, and its exact repository feature IDs.
A public library is still a supported helper; lack of a command entrypoint does not
make it internal. Internal entries also name their importing consumers and purpose.

Every public helper maps to implementation, documentation and executable test
references. The matrix tests discover actual Python files, inspect entrypoints and
imports, resolve documentation links and compare the recorded mapping with feature
paths. A new, removed or renamed module requires a registry update. Internal modules
cannot hide a CLI, lack an importing consumer, or escape capability traceability.
The tests detect stale paths/mappings; static references do not prove meaningful
semantic coverage or downstream runtime compatibility.

Feature review dates remain attached to their individual scopes. Updating the
inventory date does not reverify older external versions or prices. Existing
recommendation IDs retain their exact resolved evidence unions. Local fixture
results remain separate from live provider, model and application experiments.

## Public library and repository commands

Most public command contracts are in [measured adapters](13-measured-adapters.md)
and [evidence formats](11-evidence-formats.md). Use each command's `--help` and exact
input schema; no helper's diagnostic success authorizes a gate or release change.

| Helper | Scope and boundary |
|---|---|
| [graph_metrics.py](../scripts/graph_metrics.py) | Public Python API consumed by analysis and capacity helpers. Evaluates complete supplied DAGs and duration counterfactuals with unlimited capacity; no CLI or provider scheduling. See the [capacity model](capacity-model.md). |
| [pre_push.py](../scripts/pre_push.py) | Repository-specific hook command consuming Git's ref-update stdin. Checks committed whitespace, changed Python syntax, package contracts and changed workflows with installed actionlint. Invoke through the preserved pre-push hook under the nine-second wrapper. Compilation is not static typechecking; CI still runs the full suite. |
| [report_gh_timings.py](../scripts/report_gh_timings.py) | Reads `--metrics FILE` from `ci-metrics/2` and writes bounded local report files under `--out DIRECTORY`; required `--collection-status` records collection status. Displays support, attempts and attribution limits. It does not authenticate data, assess protection/merge readiness or change provider configuration. |

## Other public commands

| Helper | Detailed contract |
|---|---|
| [analyze_cache.py](../scripts/analyze_cache.py) | [Layered cache economics and retained negative observations](13-measured-adapters.md) |
| [analyze_gh.py](../scripts/analyze_gh.py) | [Consistent cohorts, attempts, usage and conditional price estimates](04-measurement-benchmarking.md) |
| [benchmark.py](../scripts/benchmark.py) | [Process cleanup, paired trials, provenance and statistical evidence](04-measurement-benchmarking.md) |
| [capacity_model.py](../scripts/capacity_model.py) | [Finite-capacity scheduling and explicit-rate estimates](capacity-model.md) |
| [check_behavior_results.py](../scripts/check_behavior_results.py) | [Offline agent-comparison evidence ledger](../evals/README.md) |
| [check_job_budgets.py](../scripts/check_job_budgets.py) | [Exact job and step budget evidence](11-evidence-formats.md) |
| [check_reports.py](../scripts/check_reports.py) | [Validate and stage the expected shard report transport](07-config-recipes.md) |
| [collect_gh.py](../scripts/collect_gh.py) | [Immutable GitHub snapshots and bounded read-only collection](04-measurement-benchmarking.md) |
| [collect_gh_cache.py](../scripts/collect_gh_cache.py) | [Bounded GitHub cache inventory](13-measured-adapters.md) |
| [collect_gh_evidence.py](../scripts/collect_gh_evidence.py) | [Exact-revision GitHub check evidence](13-measured-adapters.md) |
| [collect_gitlab.py](../scripts/collect_gitlab.py) | [Read-only GitLab pipeline and job snapshots](13-measured-adapters.md) |
| [discover_repo.py](../scripts/discover_repo.py) | [Bounded evidence inventory and conditional adapter routing](01-discovery-matrix.md) |
| [evaluate_shadow.py](../scripts/evaluate_shadow.py) | [Selected-versus-FULL shadow evidence](13-measured-adapters.md) |
| [evaluate_validation.py](../scripts/evaluate_validation.py) | [Four-category policy, feedback, full-suite and release/rollout evidence](08-validation-architecture.md) |
| [inspect_workflows.py](../scripts/inspect_workflows.py) | [Advisory normalized workflow structure inspection](09-platform-updates.md) |
| [plan_shards.py](../scripts/plan_shards.py) | [Inventory-preserving duration-based shard plans](13-measured-adapters.md) |
| [run_budget.py](../scripts/run_budget.py) | [Monotonic command budgets with preserved failures](13-measured-adapters.md) |
| [select_affected.py](../scripts/select_affected.py) | [Conservative affected-target selection](03-security-correctness.md) |
| [verify_gh_attestation.py](../scripts/verify_gh_attestation.py) | [Pinned GitHub CLI artifact provenance verification](13-measured-adapters.md) |

## Maintaining the registry

When adding or changing a helper, update its feature's input/version scope,
constraints, implementation, documentation and tests. Add the helper entry and map
all repository features that include its implementation path. Internal `consumed_by`
paths must equal actual importing modules. Keep public documentation linked to the
helper file and tests referencing its import or command path. Run the full package
unittest suite and actionlint before committing; do not replace behavior tests with
this inventory check.
