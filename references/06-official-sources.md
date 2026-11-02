# Official sources

Accessed 2026-11-01. Versions move: re-open the page before quoting a version, flag, or price in a report, and update the access date here when you do.

| Topic | Fact relied on | URL |
|---|---|---|
| Bun type checker | `bun check` added in 1.4.3; `bun check --no-script` always runs the checker and accepts its flags; needs `@types/bun`; follows TS 7.1 since 1.4.4 | https://bun.com/docs/runtime/check · https://bun.com/blog/bun-v1.4.4 · https://bun.com/blog/bun-v1.4.3 |
| TypeScript 7 | 7.0 stable 2026-07-08 as `tsc` in `typescript`; no compiler API until 7.1; `@typescript/typescript6` gives `tsc6`; deprecated 6.0 options are errors; `--build` works | https://devblogs.microsoft.com/typescript/announcing-typescript-7-0/ |
| Oxlint | ~870 rules; plugin coverage; type-aware linting; JS plugins alpha; `@oxlint/migrate`; `eslint-plugin-oxlint` | https://oxc.rs/docs/guide/usage/linter |
| Vitest 5 | `isolate` default true; `pool` default `forks`; `maxWorkers`; `--shard` by file with blob reporter; `sequence.shuffle` | https://vitest.dev/config/isolate · https://vitest.dev/config/pool · https://vitest.dev/config/maxworkers · https://vitest.dev/guide/cli |
| Vitest 4 migration | `poolOptions` removed; `maxThreads`/`maxForks` → `maxWorkers` | https://v4.vitest.dev/guide/migration |
| actions/checkout v7 | `fetch-depth` default 1; `filter` input (partial clone, overrides sparse-checkout); `allow-unsafe-pr-checkout` | https://github.com/actions/checkout |
| Required checks | skipped workflow → Pending, blocks merge; skipped job → Success | https://docs.github.com/en/pull-requests/how-tos/merge-and-close-pull-requests/troubleshooting-required-status-checks · https://docs.github.com/en/pull-requests/reference/status-checks |
| `pull_request_target` | base-repo token and secrets; pwn-request risk | https://docs.github.com/en/actions/reference/security/securely-using-pull_request_target |
| Dependency caching | branch/default-branch restore scope; PR caches scoped to merge ref | https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching |
| setup-node / setup-python caching | npm auto-cache only with `packageManager`; python opt-in | https://github.com/actions/setup-node · https://github.com/actions/setup-python |
| Nx affected | default base main; `NX_BASE`/`NX_HEAD`; `nx-set-shas` | https://nx.dev/docs/features/ci-features/affected |
| Turborepo affected | `--affected` = `--filter=...[main...HEAD]`; `TURBO_SCM_BASE`; shallow checkout ⇒ everything changed | https://turborepo.dev/docs/reference/run |
| Blacksmith | label format; free tier minutes; marketing speed claim | https://docs.blacksmith.sh/blacksmith-runners/overview |
| Depot | label; ephemeral single-tenant EC2; per-second billing | https://depot.dev/docs/github-actions/overview |
| Larger runners | Team / Enterprise Cloud only; per-minute billing | https://docs.github.com/en/actions/concepts/runners/larger-runners |
| Runner pricing | per-minute rates by label | https://docs.github.com/en/billing/reference/actions-runner-pricing |
| Merge queue | workflows need `merge_group` trigger | https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/managing-a-merge-queue |

## Not confirmed on 2026-11-01

Do not assert these without a fresh source: Blacksmith isolation model and per-minute price; whether TS 7 `tsc` has declaration-emit gaps; Nx merge-base semantics in detail.
