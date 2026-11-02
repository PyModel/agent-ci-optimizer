# Config recipes

Starting points to adapt, not paste. Action majors are the latest releases as of 2026-11-01 (checkout v7, setup-node v7, setup-python v7, cache v6, upload-artifact v7, download-artifact v8, setup-go v7, setup-uv v10, setup-bun v2, nx-set-shas v5, pnpm/action-setup v6). Pin each to a full commit SHA per repo policy: `gh api repos/OWNER/ACTION/commits/vX.Y.Z --jq .sha`. Run `actionlint` on every workflow you write.

## Parallel jobs + one aggregate required check

Keep the old required check name on the aggregate job (`ci` here) so branch rules keep working.

```yaml
name: ci
on:
  pull_request:
  push:
    branches: [main]
  merge_group:
permissions:
  contents: read
concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: ${{ github.event_name == 'pull_request' }}
jobs:
  lint:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v7
        with: {persist-credentials: false}
      - uses: pnpm/action-setup@v6
      - uses: actions/setup-node@v7
        with: {node-version-file: .nvmrc, cache: pnpm}
      - run: pnpm install --frozen-lockfile
      - run: pnpm lint
  test:
    runs-on: ubuntu-latest
    timeout-minutes: 20
    strategy:
      fail-fast: false
      matrix:
        shard: [1, 2, 3, 4]
    steps:
      - uses: actions/checkout@v7
        with: {persist-credentials: false}
      - uses: pnpm/action-setup@v6
      - uses: actions/setup-node@v7
        with: {node-version-file: .nvmrc, cache: pnpm}
      - run: pnpm install --frozen-lockfile
      - run: pnpm vitest run --shard=${{ matrix.shard }}/4 --reporter=blob
      - uses: actions/upload-artifact@v7
        with:
          name: blob-${{ matrix.shard }}
          path: .vitest-reports/*
          include-hidden-files: true
  ci:
    if: always()
    needs: [lint, test]
    runs-on: ubuntu-latest
    timeout-minutes: 2
    steps:
      - name: Fail unless every need succeeded
        env:
          NEEDS: ${{ toJSON(needs) }}
        run: |
          echo "$NEEDS" | jq -e 'to_entries | all(.value.result == "success")' > /dev/null \
            || { echo "$NEEDS" | jq -r 'to_entries[] | "\(.key): \(.value.result)"'; exit 1; }
```

If some needs may be skipped on purpose, list them explicitly in the jq filter (`.key == "e2e" and .value.result == "skipped"`); never accept `skipped` for all. Prove the aggregate goes red by failing each child once ([failure testing](03-security-correctness.md#failure-testing-do-it-dont-argue-it)).

## Pnpm store / uv / Go caches

```yaml
- uses: pnpm/action-setup@v6          # before setup-node; reads packageManager
- uses: actions/setup-node@v7
  with: {node-version-file: .nvmrc, cache: pnpm}
```
```yaml
- uses: astral-sh/setup-uv@v10
  with: {enable-cache: true}           # keyed on uv.lock
- run: uv sync --locked
```
```yaml
- uses: actions/setup-go@v7
  with: {go-version-file: go.mod}      # module + build cache on by default
```

Hand-rolled cache: key = OS + arch + toolchain + lockfile hash. Restore-only on untrusted events:

```yaml
- uses: actions/cache/restore@v6
  with:
    path: ~/.cache/tool
    key: tool-${{ runner.os }}-${{ runner.arch }}-${{ hashFiles('**/lockfile') }}
```

## Blobless clone with history for affected

```yaml
- uses: actions/checkout@v7
  with:
    fetch-depth: 0
    filter: blob:none
    persist-credentials: false
- uses: nrwl/nx-set-shas@v5            # sets NX_BASE / NX_HEAD
- run: pnpm nx affected -t lint test typecheck
```

Turborepo: `turbo run test --affected` (base override `TURBO_SCM_BASE=${{ github.event.pull_request.base.sha }}`). Shallow checkout makes every package "changed": correct but slow.

## Reference selector in CI

```yaml
- run: |
    python3 scripts/select_affected.py --map ci-map.json \
      --base "origin/${{ github.base_ref }}" > selection.json
    jq -e '.mode == "FULL" or (.targets | length > 0)' selection.json
```

`FULL` must run everything. Treat any parse error as FULL.

## Oxlint + ESLint fallback

```jsonc
// package.json scripts
"lint": "oxlint && eslint ."
```
```js
// eslint.config.js: last entry turns off rules Oxlint already covers
import oxlint from "eslint-plugin-oxlint";
export default [/* existing config */, ...oxlint.configs["flat/recommended"]];
```

Generate a full replacement config with `npx @oxlint/migrate` and diff diagnostics before dropping ESLint.

## Typecheck

```sh
npx tsc -b                     # TypeScript 7 (native) — same CLI name
npx tsc6 -b                    # @typescript/typescript6, when tooling needs the 6.0 API
bun check --no-script          # Bun ≥1.4.3 type checker, ignores a package.json "check" script
```

## Vitest isolation per project

```ts
// vitest.config.ts
import { defineConfig } from "vitest/config";
export default defineConfig({
  test: {
    projects: [
      { test: { name: "unit", include: ["src/**/*.unit.test.ts"], isolate: false } },
      { test: { name: "integration", include: ["src/**/*.int.test.ts"] } }, // isolate: true (default)
    ],
  },
});
```

Leak check: `vitest run --project unit --sequence.shuffle --sequence.seed=$RANDOM`, three seeds.

## Pre-push hook (lefthook)

```yaml
# lefthook.yml
pre-push:
  parallel: true
  commands:
    lint:
      glob: "*.{js,ts,tsx}"
      run: npx oxlint {push_files}
    types:
      run: npx tsc --noEmit -p tsconfig.json
```

Use the same commands and versions CI uses. CI remains authoritative.
