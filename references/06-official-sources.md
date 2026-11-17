# Official sources

Accessed 2026-11-01. Versions move: re-open the page before quoting a version, flag, or price in a report, and update the access date here when you do.

| Topic | Fact relied on | URL |
|---|---|---|
| Bun type checker | `bun check` added in 1.4.3; `bun check --no-script` always runs the checker and accepts its flags; needs `@types/bun`; follows TS 7.1 since 1.4.4 | https://bun.com/docs/runtime/check · https://bun.com/blog/bun-v1.4.4 · https://bun.com/blog/bun-v1.4.3 |
| TypeScript 7 | 7.0 stable 2026-07-08 as `tsc` in `typescript`; no compiler API until 7.1; `@typescript/typescript6` gives `tsc6`; deprecated 6.0 options are errors; `--build` works | https://devblogs.microsoft.com/typescript/announcing-typescript-7-0/ |
| Oxlint | ~870 rules; plugin coverage; type-aware linting; JS plugins alpha; `@oxlint/migrate`; `eslint-plugin-oxlint` | https://oxc.rs/docs/guide/usage/linter |
| Vitest 5 | `isolate` default true; `pool` default `forks`; `maxWorkers`; `--shard` by file with blob reporter; `sequence.shuffle` | https://vitest.dev/config/isolate · https://vitest.dev/config/pool · https://vitest.dev/config/maxworkers · https://vitest.dev/guide/cli |
| Vitest 4 migration | `poolOptions` removed; `maxThreads`/`maxForks` → `maxWorkers` | https://v4.vitest.dev/guide/migration |
| actions/checkout v7 | `fetch-depth` default 1; `filter` input (partial clone, overrides sparse-checkout); `allow-unsafe-pr-checkout`; `submodules` (default false) and `lfs` inputs (re-checked 2026-11-02) | https://github.com/actions/checkout |
| Required checks | skipped workflow → Pending, blocks merge; skipped job → Success | https://docs.github.com/en/pull-requests/how-tos/merge-and-close-pull-requests/troubleshooting-required-status-checks · https://docs.github.com/en/pull-requests/reference/status-checks |
| `pull_request_target` | base-repo token and secrets; pwn-request risk | https://docs.github.com/en/actions/reference/security/securely-using-pull_request_target |
| Dependency caching | branch/default-branch restore scope; PR caches scoped to merge ref | https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching |
| setup-node / setup-python caching | npm auto-cache only with `packageManager`; python opt-in | https://github.com/actions/setup-node · https://github.com/actions/setup-python |
| Nx affected | default base main; `NX_BASE`/`NX_HEAD`; `nx-set-shas` | https://nx.dev/docs/features/ci-features/affected |
| Turborepo affected | `--affected` = `--filter=...[main...HEAD]`; `TURBO_SCM_BASE`; shallow checkout ⇒ everything changed | https://turborepo.dev/docs/reference/run |
| Blacksmith | label format; free tier minutes; marketing speed claim; per-job Firecracker microVM, destroyed after the job | https://docs.blacksmith.sh/blacksmith-runners/overview · https://www.blacksmith.sh/security |
| Depot | label; ephemeral single-tenant EC2; per-second billing | https://depot.dev/docs/github-actions/overview |
| Larger runners | Team / Enterprise Cloud only; per-minute billing | https://docs.github.com/en/actions/concepts/runners/larger-runners |
| Runner pricing | per-minute rates by label | https://docs.github.com/en/billing/reference/actions-runner-pricing |
| Merge queue | workflows need `merge_group` trigger | https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/managing-a-merge-queue |
| PR activity types (accessed 2026-11-02) | `pull_request` runs by default on `opened`, `synchronize`, `reopened`; `edited`, `labeled`, `assigned`, `review_requested` etc. are opt-in types | https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows |
| Matrix strategy (accessed 2026-11-02) | `strategy.fail-fast` defaults to true and cancels in-progress and queued matrix jobs when one fails; without `max-parallel`, GitHub runs as many matrix jobs in parallel as runners allow | https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax |
| OIDC permission (accessed 2026-11-02) | `id-token: write` is required to request the JWT and can be set on a single job | https://docs.github.com/en/actions/reference/security/oidc |

## Validation architecture and reviewed updates (2026-11-17)

The four-category placement, 300-second PR objective, rolling-main controls and
release evidence contract are proposed design rules, not measured improvements or
provider guarantees. Keep exact repository policy and tool/platform support explicit.

| Topic | Fact relied on | URL |
|---|---|---|
| Merge candidates | Required queue checks apply to the combined candidate; GitHub Actions needs `merge_group`; temporary candidate SHA differs from the PR | https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/managing-a-merge-queue |
| Artifact attestations | Build source/workflow provenance must be verified; provenance alone does not establish safety or passing tests | https://docs.github.com/en/actions/concepts/security/artifact-attestations |
| Deployment gates | Jobs must satisfy configured environment protection before execution/access to environment secrets; availability depends on repository/plan | https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments |
| Progressive delivery | Optional Argo example: analysis results can continue, abort or pause a rollout; use the existing platform's equivalent | https://argo-rollouts.readthedocs.io/en/stable/features/analysis/ |
| Draft lifecycle | Default PR types exclude `ready_for_review`; add it when draft skipping relies on a later ready transition | https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows |
| Matrix cancellation | `fail-fast` controls sibling cancellation; `continue-on-error` tolerates a failure | https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax |
| Vitest 5 reports | Default blob reports moved to `.vitest/blob/blob-*.json`; explicit output paths remain supported | https://vitest.dev/guide/migration/ · https://vitest.dev/guide/reporters |
| Missing artifacts | upload-artifact defaults to warning for no matching files; `if-no-files-found: error` makes missing required reports fail | https://github.com/actions/upload-artifact |
| Workflow lockfiles | `.github/workflows/actions.lock` resolves action dependencies; reusable workflow calls require separate pinning; discovery markers alone do not verify resolution | https://docs.github.com/en/actions/concepts/security/workflow-dependency-lockfiles |
| Parallel steps | Native `background`, `parallel`, wait and cancel syntax; measurements must account for overlapping step intervals | https://github.blog/changelog/2026-06-25-actions-steps-can-now-be-run-in-parallel/ |
| Cache access | Workflow/job `cache-mode` is service-enforced; inspect effective scope rather than inferring permission from action names | https://github.blog/changelog/2026-09-10-control-github-actions-cache-access-with-cache-mode/ |

## Not confirmed on 2026-11-01

Do not assert these without a fresh source: Blacksmith per-minute price; whether TS 7 `tsc` has declaration-emit gaps; Nx merge-base semantics in detail; the exact `github.event.pull_request.draft` payload description (field name used in recipes, page not re-read on 2026-11-02).
