# Changelog

Release Please maintains new entries from Conventional Commits after the existing
0.1.0 release. Older descriptive commits are not rewritten or inferred as release notes.

## [0.2.0](https://github.com/PyModel/ci-optimiser/compare/v0.1.0...v0.2.0) (2026-11-20)


### Features

* automate verified skill releases with Release Please ([#29](https://github.com/PyModel/ci-optimiser/issues/29)) ([4728c74](https://github.com/PyModel/ci-optimiser/commit/4728c747810ce7ee3daa3382020a5f97afbd44ea))


### Bug Fixes

* distinguish approval holds from executed CI retries ([#32](https://github.com/PyModel/ci-optimiser/issues/32)) ([4a0abef](https://github.com/PyModel/ci-optimiser/commit/4a0abef2808820ac94b0b9904bef49942a5aa926))

### Reviewed enhancements

- **Cache evidence:** Keep slow cache events visible in mixed or incomplete cohorts, distinguish runner-disk and action-archive state, preserve restore/save outcomes, and measure exact hits over an explicit eligible PR population. ([#19](https://github.com/PyModel/ci-optimiser/pull/19), [#21](https://github.com/PyModel/ci-optimiser/pull/21), [#24](https://github.com/PyModel/ci-optimiser/pull/24))
- **Timing evidence:** Report observed/missing sample counts and attempt outcomes; use reviewed workflow/step identities for setup, restore, work, save and teardown measurements, retaining overlap and unattributed time. ([#20](https://github.com/PyModel/ci-optimiser/pull/20), [#22](https://github.com/PyModel/ci-optimiser/pull/22))
- **Controlled benchmarks:** Add per-trial preparation and starting-state records, include preparation cost in comparisons, clarify equivalent-state requirements, and make cache-inventory transport retries explicitly opt-in and observable. ([#25](https://github.com/PyModel/ci-optimiser/pull/25), [#26](https://github.com/PyModel/ci-optimiser/pull/26), [#27](https://github.com/PyModel/ci-optimiser/pull/27))
- **Agent contracts:** Add evidence/instruction boundaries, public behavior cases, comparison-ledger validation, and a complete registry linking public/internal helpers to documentation and tests. ([#23](https://github.com/PyModel/ci-optimiser/pull/23), [#28](https://github.com/PyModel/ci-optimiser/pull/28))
- **Lint diagnostics:** Capture a bounded process snapshot near a timeout while preserving the existing five-second limit and command result. ([#31](https://github.com/PyModel/ci-optimiser/pull/31))

### Evidence and limits

No live Helm/Zode benchmark was run for this release. The supplied cold/warm observations and synthetic fixtures do not establish a causal cache speedup, stable p95 or invoice saving. Ledger checks and smoke exercises are not a completed two-arm model benchmark.

The historical workflow-lint timeout remains unresolved; the new snapshot is diagnostic only. Full-suite merge-queue validation and existing budgets remain required. Historical failed executions stay failed; approval-only holds qualify only when complete evidence proves that no earlier job ran. Upstream transport retries and GitHub publication concurrency limits remain documented in the [release guide](https://github.com/PyModel/ci-optimiser/blob/main/docs/release-automation.md).

## 0.1.0 (2026-11-20)

Initial published release. See the existing
[release notes](https://github.com/PyModel/ci-optimiser/releases/tag/v0.1.0).

Source: `d849a540787e37a004a699da0b300b796efd184c`. The tag and release are preserved.
