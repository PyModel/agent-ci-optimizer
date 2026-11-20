# Workflow lint timeout evidence

The [main CI run 44778984205](https://github.com/PyModel/ci-optimiser/actions/runs/44778984205)
failed its five-second workflow-lint budget after **5.010401 seconds**, with exit
124 and no process diagnostics. The [merge-queue run 44778841323](https://github.com/PyModel/ci-optimiser/actions/runs/44778841323)
had passed the same check in **0.464510 seconds** for the same source SHA,
`2f5495e33df28d7dda739d6f922d21b68f2bdf9c`. These observations do not explain the
difference. The failure remains an observed failure with **unknown cause**.

## Investigated changes

Local paired experiments on 2026-11-20 tested CPU affinity and ShellCheck
packaging against a fixed snapshot of the two existing workflows. Each arm had
20 ordinary trials plus 20 stress trials. No arm reproduced a timeout; deliberate
ShellCheck and actionlint violations failed with identical findings in each
comparison. Percentiles below describe only the 20 ordinary local trials per
arm; the small-sample p95 is preliminary.

| Comparison | Arm | Local p50 | Local p95 | Ordinary / stress timeouts |
| --- | --- | ---: | ---: | ---: |
| CPU affinity | Default, four available cores | 28.17 ms | 33.02 ms | 0 / 0 |
| CPU affinity | One core | 63.63 ms | 82.99 ms | 0 / 0 |
| ShellCheck packaging | Ubuntu package | 36.46 ms | 43.05 ms | 0 / 0 |
| ShellCheck packaging | Upstream static binary | 33.56 ms | 64.20 ms | 0 / 0 |

One-core affinity was slower. The static package had a worse observed tail.
Neither comparison justifies a runner, tool or packaging change. These were
local diagnostics, not measurements on the historical CI VM, and they do not
establish the historical cause. The new release workflow was outside the fixed
experiment corpus; actual repository lint still discovers every workflow.

## One observation before the existing deadline

`Validate workflows` runs `.github/lint_diagnostics.py` **inside** the unchanged
`run_budget.py --seconds 5 --label workflow-lint` command. The supervisor launches
the existing `"$RUNNER_TEMP/actionlint" -color` argv with inherited output and
the budget-owned process group. It preserves the command's success, failure and
signal status. It neither changes ShellCheck coverage nor retries a command.

If actionlint is still running at four seconds, the supervisor emits one JSON
line prefixed `lint_diagnostics:`, with schema `ci-lint-process-snapshot/1`.
The snapshot adds evidence for a future slow run; it does not fix or reclassify
the historical failure. The outer budget continues to include supervision,
fails at five seconds, and terminates the entire owned process group.

Only the owned group's processes are reported: supervisor, actionlint,
ShellCheck where its process name matches, and `other`. The allowlist contains
elapsed time, numeric process/parent/group/session IDs, start ticks, state,
cumulative user/system CPU ticks, RSS pages, thread count, recognized wait
channels and pipe endpoint counts by direction. Clock frequency and page size
state their units. Thread wait channels are aggregated. Pipe identities are
used only to detect descriptor reuse and are never emitted. Counts marked
`complete: false` are observed lower bounds; unobserved entries remain unknown.

No argv, environment, cwd, script, command output, stack dump or raw descriptor
target is recorded. Unknown names, missing/denied reads and detected process or
descriptor races remain `unknown`; they do not become zero. Sampling is not an
atomic view of a changing process tree. In particular a zero wait-channel value
does not prove that a process is running.

Collection is capped at 12 processes, 1,024 process-directory entries, 32 threads
and 64 descriptors per process, 128 KiB of reads, and an 8 KiB output line. It
checks a 100 ms collection deadline; a daemon collection thread also has a
100 ms supervisor wait bound if an OS read stalls. A capped observation names
the applicable limits and sets `truncated: true`. The collector never gets an
extension to the five-second lint budget.

Repository-only tests cover the unchanged exit/stream behavior, signal exits,
one bounded observation, secret sentinels, missing/denied/reused process data,
descriptor races, collection limits, and a hung descendant tree that still
exits 124 under `run_budget.py`. That control injects a shorter observation and
budget through Python test code; production has no interval override.

The field meanings and permission/race limitations follow the
[Linux kernel proc documentation](https://docs.kernel.org/filesystems/proc.html)
and [Linux proc_pid_fd documentation](https://man7.org/linux/man-pages/man5/proc_pid_fd.5.html),
checked 2026-11-20. Investigating a future snapshot must preserve its failed run;
the snapshot alone does not establish a cause or authorize rerunning it green.
