# CI audit: <repo> (<date>)

Mode: OBSERVE · Window: <from> → <to> · Sample: <n> runs (<m> with jobs) · Source: `.ci-audit/metrics.json`

## Answer

<One paragraph: the top bottleneck on the required path, the first PR to ship, expected wall-clock and runner-minute saving with confidence, and what is NOT worth changing. If push→green p50 is already a few minutes and nothing gates on a glaring cause, say CI is in good shape first.>

## Detected stack

| Item | Value | Evidence |
|---|---|---|
| CI provider(s) | | `discovery.json` |
| Languages / package manager | | |
| Monorepo graph | none / Nx / Turbo / … | |
| Test runners | | |
| Required checks | | `raw/required_checks.json` |
| Trust boundaries | fork PRs? `pull_request_target`? deploy jobs? | |

## Baseline

| Metric | p50 | p95 | n | Definition / limitation |
|---|---|---|---|---|
| push→green | | | | approximation, not required-check aware |
| slowest workflow wall | | | | |
| job queue | | | | |
| setup share | | | | |
| shard skew | | | | |
| flake rate | | | | |
| first-pass failure rate (× time-to-green) | | | | `first_pass_failure_rate` |
| gap to runner-up job (floor) | | | | `gap_to_runner_up_s`: max saving from fixing the last job alone |
| cache hit rate | unknown | | | needs logs |
| cost per merge | unknown | | | needs pricing |

Critical path: `<job A> → <job B> → <job C>` (see `critical-path.md`). Gating job: `<name>`, last in <k>/<n> runs, required: yes / no / unknown. Config changed in window: no / yes → `--since <date>`.

## Seven modules

| # | Module | Applicable | Observed bottleneck | Δ wall-clock p50/p95 (on path / below floor) | Δ runner-min/mo | Evidence tier | Correctness/security risk | Effort | Cost delta | Next proof |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | Fast runners | | | | | | | | | |
| 2 | Native lint/typecheck | | | | | | | | | |
| 3 | Machines / Vitest reuse | | | | | | | | | |
| 4 | Caching / blobless clone | | | | | | | | | |
| 5 | Parallel jobs | | | | | | | | | |
| 6 | Affected tasks | | | | | | | | | |
| 7 | Pre-push hooks | | | | | | | | | |

Combined wall-clock saving (stacked model, not the sum of rows): <value>; method: <how the path was recomputed>. Runner-minute rows add.

## Risks found (independent of speed)

- <false-green pattern, privileged trigger, unpinned action, missing timeout…> — file:line — severity

## PR plan (one intervention each)

| Order | PR | Module | Acceptance criteria | Owner review | Rollback |
|---|---|---|---|---|---|
| 1 | | | | | revert PR |

## No-change findings

- <module>: NOT APPLICABLE because <evidence>.

## Limits of this audit

- <missing data, low samples, approximations>
