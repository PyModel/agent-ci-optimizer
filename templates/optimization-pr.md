## What

<One intervention, one module (#N). Files touched.>

## Why

<Bottleneck it targets, with the metric and source: e.g. "test job is 62% of the critical path, p50 14m12s over 120 runs, .ci-audit/metrics.json 2026-10-01..2026-10-31".>

## Evidence

| | Baseline | Candidate | Delta | n |
|---|---|---|---|---|
| p50 | | | | |
| p95 | | | | |
| runner-minutes / run | | | | |

Experiment record: `experiments/<id>.json`. Cold and warm cache measured separately: yes / no.

## Correctness gates

- [ ] Required check names unchanged (or branch rules updated with owner approval: <link>)
- [ ] Forced failure in each affected job turns the required check red (probe commits: <shas>)
- [ ] Same test count and coverage as baseline (<numbers>)
- [ ] No new `continue-on-error`, `|| true`, or always-skip `if:`
- [ ] Cold-cache run green
- [ ] Selector changes: adversarial diffs return FULL
- [ ] Isolation changes: ≥3 shuffled seeds green

## Security

- Permissions changed: none / <diff> (security-owner review: <name>)
- Cache trust boundary changed: none / <description>
- New dependency or vendor: none / <name, maintainer, license, install scripts, cost> (project-owner approval: <link>)

## Rollback

Revert this PR. Rollback triggers: p95 push→green +20% over 20 runs, flake +2 pts, any false green.
