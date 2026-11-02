# CI audit: <repo> (<date>)

Mode: OBSERVE · Window: <from> → <to> · Sample: <n> runs (<m> with jobs) · Source: `.ci-audit/metrics.json`

## Answer

<One paragraph: the top bottleneck on the required path, the first PR to ship, expected saving with confidence, and what is NOT worth changing.>

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
| cache hit rate | unknown | | | needs logs |
| cost per merge | unknown | | | needs pricing |

Critical path: `<job A> → <job B> → <job C>` (see `critical-path.md`).

## Seven modules

| # | Module | Applicable | Observed bottleneck | Expected critical-path saving | Confidence | Correctness/security risk | Effort | Cost delta | Next proof |
|---|---|---|---|---|---|---|---|---|---|
| 1 | Fast runners | | | | | | | | |
| 2 | Native lint/typecheck | | | | | | | | |
| 3 | Machines / Vitest reuse | | | | | | | | |
| 4 | Caching / blobless clone | | | | | | | | |
| 5 | Parallel jobs | | | | | | | | |
| 6 | Affected tasks | | | | | | | | |
| 7 | Pre-push hooks | | | | | | | | |

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
