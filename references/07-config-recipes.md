# Config recipes

Starting points to adapt, not paste. The examples use source-reviewed action majors
(checkout v7, setup-node v7, setup-python v7, cache v6, upload-artifact v7,
download-artifact v8, setup-go v7, setup-uv v10, setup-bun v2, nx-set-shas v5,
pnpm/action-setup v6). Verify the exact current release and runtime support, then pin
to an immutable commit or supported reviewed native lockfile under repo policy.
Example resolution: `gh api repos/OWNER/ACTION/commits/vX.Y.Z --jq .sha`.
Run actionlint and the [semantic checks](09-platform-updates.md); record unsupported
new syntax separately. No downstream stack execution is implied by these examples.

First map [the four validation categories](08-validation-architecture.md). The full-test
example below preserves every check on each event; it is not a universal PR schedule.
Moving full verification to a queue or rolling-main lane requires the documented policy,
selector shadow evidence and an aggregate that evaluates the work intended for that event.

## Parallel jobs + one aggregate required check

Keep the old required check name on the aggregate job (`ci` here) so branch rules keep working.
The report job assumes the reviewed `check_reports.py` helper has been made available
at `scripts/check_reports.py`. Keep the project's existing coverage collection,
payload transport, merge options and thresholds when adapting its commands.

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
        if: ${{ !cancelled() }}
        with:
          name: blob-${{ github.run_attempt }}-${{ matrix.shard }}
          path: .vitest/blob/blob-*.json
          if-no-files-found: error
          include-hidden-files: true
  reports:
    if: ${{ !cancelled() }}
    needs: [test]
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v7
        with: {persist-credentials: false}
      - uses: pnpm/action-setup@v6
      - uses: actions/setup-node@v7
        with: {node-version-file: .nvmrc, cache: pnpm}
      - run: pnpm install --frozen-lockfile
      - uses: actions/download-artifact@v8
        with:
          pattern: blob-${{ github.run_attempt }}-*
          path: downloaded-reports
          merge-multiple: false
      - name: Require every current-attempt shard and merge
        env:
          ATTEMPT: ${{ github.run_attempt }}
        run: |
          python3 scripts/check_reports.py --root downloaded-reports \
            --expected-shards 1 2 3 4 --prefix "blob-${ATTEMPT}-" \
            --stage-out "$RUNNER_TEMP/validated-vitest-reports"
          pnpm vitest run --merge-reports="$RUNNER_TEMP/validated-vitest-reports" --reporter=default
  ci:
    if: always()
    needs: [lint, test, reports]
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

If some needs may skip, keep the workflow itself eligible for every protected candidate.
The selector must be a direct need of the aggregate; validate its successful
completion, candidate revision, event, and explicit per-job decision before using
its outputs. Accept `skipped` only for a job that the validated decision marks not
required. A failed/missing selector or unexpectedly skipped/cancelled job fails the
gate. Run FULL on unknown selection inputs. Top-level `paths` filters and
`if: always()` inside a skipped workflow cannot implement this contract. Prove the
aggregate goes red on each unexpected skip and child failure
([failure testing](03-security-correctness.md#failure-testing-do-it-dont-argue-it)).

The blob path above is the Vitest 5 default (verified 2026-11-17); honor an explicit
`outputFile` in the project. The example proves report transport and retains test
exit status. If the project requires coverage, its existing coverage collection and
merge/threshold commands are additional required work, not optional flags to drop.
Verify actual provider/version coverage payloads travel with each shard, including
untested-file obligations and attachments. Seed missing-shard and low-coverage
failures before adoption. Uploading on test failure preserves diagnostics; it never
clears that failure. A partial rerun must either rebuild the complete report set or
explicitly verify retained predecessor artifacts; this example fails closed unless
all current-attempt shards exist.

If heavy jobs skip drafts, include `ready_for_review` alongside `opened`, `synchronize`
and `reopened`, and test draft→ready without a new commit. Decide draft-state handling
and intended skips explicitly. See [draft lifecycle sources](06-official-sources.md).

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
- env:
    BASE_REF: ${{ github.base_ref }}
  run: |
    # This example selects only on PR events; other events deliberately run FULL.
    if [ "$GITHUB_EVENT_NAME" = pull_request ] && [ -n "$BASE_REF" ]; then
      if ! python3 scripts/select_affected.py --map ci-map.json \
        --base "origin/$BASE_REF" > selection.json; then
        printf '%s\n' '{"mode":"FULL","targets":[],"reasons":["selector error"]}' > selection.json
      fi
    else
      printf '%s\n' '{"mode":"FULL","targets":[],"reasons":["full event policy"]}' > selection.json
    fi
    # FULL must work even if the map was missing or malformed.
    if ! jq -e -s 'length == 1 and (.[0] | .mode == "FULL" and .targets == [])' selection.json > /dev/null; then
      # Declare known_targets before consuming SELECTED in CI.
      jq -e -s --slurpfile map ci-map.json '
        length == 1 and ($map | length == 1) and
        (.[0] | .mode == "SELECTED" and (.targets | type == "array" and length > 0) and
         (.targets | all(type == "string" and length > 0)) and
         ($map[0].known_targets | type == "array" and length > 0) and
         ((.targets - $map[0].known_targets) | length == 0))
      ' selection.json
    fi
```

`FULL` must execute the full required suite; SELECTED must dispatch every validated
target. A malformed output fails closed here. A selector error requests FULL, but
never catches a test failure or makes it green. Resolve and fetch the correct event
base/head before selection; do not use this PR-base example for merge groups.

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

For a compatible installed Oxlint/tsgolint version, also benchmark
`oxlint --type-aware --type-check` against separate typed lint and `tsc --noEmit`.
Preserve declaration/build checks and unsupported rules until diagnostic/negative
fixtures prove equivalence. See [typed-lint adapter](10-stack-adapters.md#typed-lint-and-typescript).

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
