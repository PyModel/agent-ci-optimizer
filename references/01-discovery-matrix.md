# Discovery matrix

Map what `scripts/discover_repo.py` found to the modules that may apply. A detection is a hint: open the file before you rely on it.

## Stack → applicable modules

| Detected | Modules worth evaluating | Usually not applicable |
|---|---|---|
| `package.json` + TS/JS | all seven; module 2 (Oxlint, `bun check`, `tsc` 7) | — |
| `nx.json` / `turbo.json` / `pnpm-workspace.yaml` / Bazel / Pants / Gradle multi-project | 6 (affected) via the tool's own graph | the reference selector, except as a test oracle |
| Python (`pyproject.toml`, `uv.lock`) | 1, 3 (pytest-xdist, `--dist loadfile`), 4 (uv / pip cache), 5, 7 (ruff, mypy/pyright) | 2 (JS-only) |
| Rust (`Cargo.toml`) | 1, 3 (`cargo nextest`), 4 (target dir + registry cache, sccache), 5 | 2 |
| Go (`go.mod`) | 1, 3 (`go test -p`), 4 (module + build cache via setup-go), 5 | 2 |
| JVM (Gradle/Maven) | 1, 3, 4 (Gradle build cache, configuration cache), 5, 6 (Gradle's own up-to-date checks) | 2 |
| Single small package, CI < 3 min | 4 and 5 only if setup dominates; often **no change** | 1, 6 |
| No CI config found | report NOT APPLICABLE; ask where CI runs | everything |

## CI provider adapters

| Provider | Config | Read-only run data | Notes |
|---|---|---|---|
| GitHub Actions | `.github/workflows/*.yml` | `scripts/collect_gh.py` (REST via `gh api`) | job `needs` only in YAML; required checks via branch protection or rulesets |
| GitLab CI | `.gitlab-ci.yml` | `GET /projects/:id/pipelines`, `/pipelines/:id/jobs` (token with `read_api`) | `needs:` DAG and `rules:changes` live in YAML |
| CircleCI | `.circleci/config.yml` | API v2 `/insights/...` and `/pipeline/:id/workflow` | test splitting by timing is built in |
| Buildkite | `.buildkite/*.yml` / UI pipelines | REST `/builds` with `read_builds` | agent queues are the runner pool |
| Azure Pipelines | `azure-pipelines.yml` | `_apis/build/builds`, timeline API | |
| Jenkins | `Jenkinsfile` | `/api/json?tree=...` | agents are long-lived: weigh isolation |

Normalize other providers into the `runs.json` / `jobs/<id>.json` shape in [measurement](04-measurement-benchmarking.md#raw-schema) so `analyze_gh.py` can read them.

## Workflow risk flags from discovery

| Key in `github_workflows` | Why it matters | Action |
|---|---|---|
| `pull_request_target`, `workflow_run` | privileged context; secrets reachable | read [security](03-security-correctness.md#privileged-triggers) before touching |
| `continue_on_error`, `or_true` | may hide failures behind green | prove each is intentional, or flag as a false-green risk |
| `paths_filter` on a required workflow | skipped workflow leaves required check Pending | see [skipped checks](03-security-correctness.md#required-checks-and-skips) |
| `unpinned_action` | supply-chain and reproducibility | pin to SHA per repo policy |
| `fetch_depth`, `blob_filter` | history depth for affected/merge-base | module 4/6 interaction |
| no `concurrency` | superseded pushes keep burning runners | `cancel-in-progress` for PR branches, never for default/release |
| no `timeout` | stuck jobs hold the queue for 6h default | add `timeout-minutes` from p99 × 2 |
| `secret_names` | trust boundary inventory; names only | never print values |
| `push_and_pr_unfiltered` | `push` with no branch/tag filter plus `pull_request`: every PR commit runs twice (`paths` alone does not stop it) | restrict `push` to the default branch; keep tag/release workflows as they are |
| `pr_types` with `edited`/`labeled`/… | title, label and assignee changes rerun CI on unchanged code | confirm the `types:` sits under `pull_request` (issues/release triggers match too); drop metadata events unless a job reads them |
| no `draft_gate` on heavy PR jobs | drafts burn the full suite | job-level `if:` on draft state, routed through the aggregate check (a skipped job reports Success) |
| `cancel_in_progress_false` | superseded runs finish anyway | intentional for default branch/release; otherwise see `concurrency` |
| `cron` | schedules more often than results are read | match frequency to who reads the result |
| `max_parallel` `1`, `fail_fast` `false` | serialized matrix; shards keep running after a sure failure | `fail-fast: false` is right for per-OS diagnosis, wasteful on test shards |
| `submodules_or_lfs` | checkout payload in jobs that may not read it | confirm the job reads those paths before dropping |
| `sleep` | fixed waits in CI steps; readiness should poll with a bounded timeout | replace with a health check, never an unbounded loop |
| `image_latest` | floating service/container tag: non-reproducible, cache-hostile | pin a version or digest |
| `id_token_write` | OIDC token scope | job-level only, never workflow-wide |
| `no_cache_or_clean` | `--no-cache` / `rm -rf` defeating a cache or a warm runner | check whether the clean is load-bearing (self-hosted runners) |
| `action_pins`, `hints.pinned_action_ratio` | share of remote `uses:` pinned to a full SHA | aim for all; list the rest |

## Monorepo graph sources

Prefer, in order: the build tool's graph (`nx graph --file`, `turbo run --dry=json`, `bazel query`, `pants dependents`, Gradle `projects`) → package manager workspaces (`pnpm -r ls --json`) → a hand-written map for `select_affected.py`. A hand map rots: put a test in CI that fails when a new top-level package directory appears without a rule.
