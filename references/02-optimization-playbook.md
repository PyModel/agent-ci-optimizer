# Seven-module playbook

Each module: when it applies, the experiment, the counterexamples that make it a loss, and the gate. Facts dated 2026-11-01 come from [official sources](06-official-sources.md); re-check versions before quoting them. Config snippets: [recipes](07-config-recipes.md).

## 1. Fast runners

**Applies when** CPU- or I/O-bound jobs dominate the critical path, or queue p95 is high on the current pool.

**Experiment.** Same commit, same workflow, interleave baseline and candidate labels (`runs-on: ubuntu-latest` vs larger GitHub runner vs third-party label). Record queue and execution separately, warm and cold cache separately; third-party runners often have their own cache backend.

**Options** (verify current terms; vendor speed claims are marketing, not evidence for this repo):
- GitHub larger runners: Team or Enterprise Cloud plans only; billed per minute, including on public repos.
- Blacksmith: labels like `blacksmith-4vcpu-ubuntu-2404`; isolation model and per-minute price were not confirmed from docs on 2026-11-01. Ask the vendor before approving.
- Depot: `depot-ubuntu-24.04`; ephemeral single-tenant EC2 instances, never reused; per-second billing.
- Self-hosted: only ephemeral (one job per VM/container). Persistent runners on public repos let fork PRs execute on your hardware.

**Counterexamples.** Job is network/registry-bound (more CPU does nothing); tests serialize on one DB; runner cold cache erases the CPU win; vendor concurrency cap creates new queueing.

**Gate.** p50 and p95 total (queue + run) better with ≥10 paired runs; cost per successful merge known; isolation, data residency, cancellation and log retention reviewed; **explicit spend approval**.

## 2. Native lint and typecheck (JS/TS only)

**Oxlint.** Rust linter with ~870 built-in rules covering ESLint core, TypeScript, React, Jest, Vitest, Import, Unicorn, jsx-a11y. Type-aware linting exists; JS plugins are alpha. Migration paths: `@oxlint/migrate` to replace ESLint, or run Oxlint first and keep ESLint for the rest with `eslint-plugin-oxlint` turning off overlapping rules.

**Typecheck.** TypeScript 7.0 (stable 2026-07-08) is the native port and ships as `tsc` in the `typescript` package; `tsgo` / `@typescript/native-preview` are the old preview names. It has no compiler API yet (typescript-eslint needs `@typescript/typescript6`, which provides `tsc6`), errors on 6.0-deprecated options (`target: es5`, `baseUrl`, `moduleResolution: node10`, AMD/UMD/System), and defaults `types` to `[]`. Bun ≥1.4.3 has `bun check`, a type checker; `bun check --no-script` always runs the checker even when `package.json` defines a `check` script, and accepts checker flags (`-b`). It needs `@types/bun`.

**Experiment.** Run old and new side by side on the same commit; diff the diagnostic sets (rule id + file + line), not only exit codes. For typecheck, compare error counts per project and confirm declaration output (`--emitDeclarationOnly` diff of `.d.ts`) is byte-identical if packages publish types.

**Counterexamples.** Custom ESLint rules or plugins with no Oxlint equivalent; framework files (Vue, Svelte, Astro, MDX, Angular) that still need TS 6 tooling; repo relies on TS compiler API (codegen, typed lint rules).

**Gate.** Every dropped rule listed with a reason or kept in a fallback ESLint pass; zero new false negatives on a seeded-violation fixture; build/declaration gates unchanged.

## 3. Bigger machines and Vitest reuse

**Shards and workers.** Matrix of runner CPU × `--shard=i/n` × `maxWorkers`. Vitest 5 shards by **file**, not test case; pair with `--reporter=blob` and `vitest --merge-reports`. Sizing rule: pick n so the slowest shard ≈ median shard; `shard_skew` > 1.5 in `metrics.json` means rebalance before adding machines.

**Isolation.** Vitest defaults: `pool: 'forks'`, `isolate: true`. `isolate: false` reuses module state across files in a worker: faster, but leaks globals, mocks, singletons and module caches between files. `poolOptions` was removed in Vitest 4; `maxThreads`/`maxForks` are now `maxWorkers`.

**Experiment.** Turn isolation off **per project** (Vitest `projects`) for pure unit suites only. Run with `--sequence.shuffle` and several seeds, and run each file alone, to catch order dependence.

**Counterexamples.** Suites touching DB, filesystem, env vars, timers, `vi.mock` hoisting, global polyfills; integration/E2E; anything with `beforeAll` side effects.

**Gate.** Full suite green under ≥3 shuffled seeds; same test count and coverage; flake rate not up over 20+ runs.

## 4. Caching and blobless clone

**Cache scope (GitHub).** A run can restore caches from its own branch or the default branch, never from sibling or child branches; a PR cache is scoped to the PR merge ref and only re-runs of that PR can restore it. So: warm the default-branch cache from `push` to main, and let PRs read it.

**Keys.** Include lockfile hash, OS, arch, toolchain version, and any build-affecting env. `restore-keys` fallbacks are fine for package stores (content-addressed) and dangerous for build outputs (stale artifacts). Prefer `actions/setup-node` / `setup-python` built-in caches for package stores: setup-node caches npm automatically only when `packageManager` names npm; yarn/pnpm need `cache:`; setup-python caching is opt-in (`pip`, `pipenv`, `poetry`).

**Clone.** `actions/checkout` defaults to `fetch-depth: 1`. `filter: blob:none` gives a partial clone (blobs fetched on demand) and overrides sparse-checkout; it does **not** change depth by itself. Affected tools need the base commit: Turborepo treats every package as changed when the checkout is too shallow; Nx needs `NX_BASE` reachable. Typical fix: `fetch-depth: 0` + `filter: blob:none`.

**Counterexamples.** Cache restore slower than install (small deps, slow cache backend); cache size near the repo limit causing eviction churn; tools that read every blob (`git log -p`, some changelog generators) negate blobless clones.

**Gate.** Cold path still correct (delete cache, rerun); restore time + install < install-from-scratch on p50; untrusted events cannot save caches trusted jobs restore.

## 5. Parallel jobs

**Experiment.** Draw the `needs` DAG; split lint, typecheck, unit, build into sibling jobs; keep one aggregate job (`needs: [all]`, `if: always()`, fail when any need is not `success`) as the single required check.

**Counterexamples.** Each job repeats a 2-minute install, so 4 parallel jobs cost 8 runner-minutes for a 1-minute gain; concurrency limit on the plan turns parallel jobs into queued jobs; hidden ordering (build artifacts consumed by tests).

**Gate.** Critical path shorter at p50 and p95; aggregate check fails when any child fails, is cancelled, or is skipped unexpectedly (test it by forcing a failure in each child); runner-minutes delta reported.

## 6. Affected tasks

**Only with a real project graph.** Nx: `nx affected -t test`, base defaults to main, override with `--base/--head` or `NX_BASE/NX_HEAD`; `nrwl/nx-set-shas` sets base to the last successful main commit. Turborepo: `turbo run test --affected` = `--filter=...[main...HEAD]`; override with `TURBO_SCM_BASE`/`TURBO_SCM_HEAD`.

**Experiment.** Replay the last 50–100 merged PRs: compute the selection, then check whether any test outside it failed on main afterwards (escaped defect). Run `select_affected.py` or the tool against adversarial diffs: lockfile, root config, CI file, codegen input, schema, migration, rename across packages, deleted file.

**Counterexamples.** Implicit dependencies (env files, shared fixtures, Docker images, generated code) missing from the graph; merge-base wrong in merge queue or shallow clone.

**Gate.** Adversarial diffs select FULL; zero escaped defects in replay; scheduled FULL run on main (at least daily) and on `merge_group` if a merge queue exists.

## 7. Pre-push hooks

**Experiment.** Changed-file format + lint (seconds), and a project-level typecheck only when it is fast. Tools: lefthook, husky + lint-staged, pre-commit. Agents benefit most: a red hook beats a 10-minute CI round-trip.

**Counterexamples.** Hook slower than 30 s (people and agents bypass it); hook differs from CI config (different results); hook installs via `postinstall` from untrusted packages.

**Gate.** Same tool versions and config as CI; non-zero exit propagates; documented escape hatch (`--no-verify`) for emergencies; **CI stays authoritative**, since hooks are bypassable.
