# review-run: design

Status: designed 2026-11-10 (architecture v1.16, interface 0.1.0-design). The rules are in [interface.md](interface.md), which is the single normative source; this document records the decisions, the live verification behind them and the internal design. It links to interface headings and restates none of its tables.

Section references (architecture N) point to `../awkit-docs/architecture.md`; "design guide N" points to `../awkit-docs/design-guide.md`.

## Role and layer

Run a review skill on a pull request and store the resulting review draft. Layer 3 (workflow). Works fully standalone at a terminal (it needs forgectl, pr-comment and agent-spawn installed, and wtctl only for `run_pr_code`). It is a skill-wrapper (design guide section 11): default slot `review-pr`, bundled as `review-run:review-pr` in `plugin/`. It never posts a review; posting belongs to pr-comment and the decision to post to the caller (review-watch).

## Uses

| Tool | Binding | What for |
|---|---|---|
| forgectl | A | pre-fetch: `show`, `files`, `diff`, `snapshot` (`--json`); one more `show` at the end ([Pre-fetch](interface.md#pre-fetch)) |
| pr-comment | A | `draft new` (draft id and anchor), `draft check` (validation) ([Finalize](interface.md#finalize)); its review-draft schema is pinned in tests. This replaces the earlier "schema only" edge |
| agent-spawn | A | `launch`, `resume`, `teardown` ([Launch](interface.md#launch)); never `--ephemeral`, never promotion |
| wtctl | A | `create --throwaway`, `remove`, only when a repo configures `run_pr_code` ([Code run](interface.md#code-run)) |
| `git` | none directly | review-run runs no git itself; wtctl creates the throwaway |
| `bwrap`, `socat` | third party | the sandbox for `run_pr_code` (Linux and WSL2) |
| claude-session | indirect | through agent-spawn; review-run reads `status --json` for the frozen artifact path and the result file's skill list, and relies on its new per-call `--no-implicit-done` |

No tool is aware of review-run. review-watch (its caller) is not a dependency.

## Decisions

| # | Decision | Rationale | Interface |
|---|---|---|---|
| D-1 | `run` blocks until the run is `done`, `escalated` or `failed`; `--no-wait` returns at session start | review-watch runs it inside a jobsctl job, so the queue cap counts running reviews and SIGTERM cancels cleanly | [run](interface.md#run), [Run order](interface.md#run-order) |
| D-2 | Always `--kind review --no-implicit-done`; `report.implicit: true` is a failed run | spike A: with implicit done a session that cannot report ends `done` with a prose file; with it off the same case exits 14 and agent-spawn's single report-resume recovered the review in 5 s. A per-call flag keeps the kind name and needs no user config | [Launch](interface.md#launch), [Finalize](interface.md#finalize) |
| D-3 | Lossless normalization of a copy, then `pr-comment draft check`; no repair resume | spike A: 11 of 37 artifacts failed the schema, nearly all by invented properties or null optionals; the fixes lose nothing and the model keeps authority over content | [Normalization](interface.md#normalization) |
| D-4 | Per-file input files, a manifest and a coverage statement; caps give a partial review that says so; `too_large` fails | spike A: the Read tool refuses files over 256 KB, haiku silently skimmed head and tail of a 2.3 MB diff; forgectl's `diff` is all or nothing | [Manifest and inputs](interface.md#manifest-and-inputs), [Meta](interface.md#meta) |
| D-5 | `run_pr_code` runs in bubblewrap by default, with no network, and is skipped (not fatal) where the sandbox is unusable; no macOS sandbox in v1 | spike B: an allowlisted plain run left every attack open (files, sockets, `/proc`, daemons that block wtctl `remove`); bwrap blocked all of them at 0 to 11 ms | [Code run](interface.md#code-run) |
| D-6 | Network is always off in v1; dependencies via `ro_binds`; an `install_command` phase is 1.x work | network-on re-exposes localhost, the host IP and abstract sockets; install scripts are attacker code | [`run_pr_code`](interface.md#run_pr_code) |
| D-7 | The bundled profile denies `Agent` and `Task`; `subagents: true` drops the denies and sets the depth variable | spike C: omitting the tools from `allow` removes nothing, only `deny` does; subagents cost 2 to 8 times more with no visible gain | [Launch](interface.md#launch), [Config](interface.md#config) |
| D-8 | pr-comment is called through its CLI (`draft new`, `draft check`) | one source of truth for what will be accepted at post time; `draft new` already does the head-consistent double read | [Pre-fetch](interface.md#pre-fetch) |
| D-9 | `run --previous <run>` passes an earlier review as a fenced input; otherwise runs are independent | explicit arguments only; a re-review can note what is fixed | [Prompt files](interface.md#prompt-files) |
| D-10 | `run` refuses only a closed or merged PR and an unknown host; drafts, bots, forks and stacked PRs are reviewed and recorded in `meta.review_run.pr` | skip policy belongs to review-watch | [run](interface.md#run), [Meta](interface.md#meta) |
| D-11 | The anchor comes from `draft new`; other reads are compared with it and the pre-fetch retried once; the head is read again at the end and a move is recorded, not fatal | no atomic head-pinned read exists; the anchor names what was reviewed and `pr-comment review --reanchor-to` handles a moved head | [Pre-fetch](interface.md#pre-fetch), [Finalize](interface.md#finalize) |
| D-12 | `resume <run>` finishes an `escalated` run; `show`, `wait` and `reconcile` finalize runs resumed by other means | the architecture keeps the escalated agent; one finalizing code path | [Resume](interface.md#resume) |
| D-13 | Retention 30 days by `prune`; non-terminal runs are never pruned | matches other tools' retention; review-watch copies the artifact into its own draft | [prune](interface.md#prune) |
| D-14 | `--mode interactive` is a pass-through to agent-spawn's window placement; always `--no-promote` | the design guide requires both modes; predictable escalation | [run](interface.md#run) |
| D-15 | The default skill is a single-agent, high-precision reviewer (nits off), with `guidelines_file`; no confidence number | spike A: confidence was 0.90 to 0.97 on every run; a reviewer that cries wolf gets ignored | [Slot contract](interface.md#slot-contract) |
| D-16 | Three hooks: `pre-run`, `post-run`, `post-escalate`; payloads hold trusted or derived values only | nothing from the PR or the model reaches a hook command | [Hooks](interface.md#hooks) |
| D-17 | `run_pr_code` is defined only in the user's own config as an argv list; a failing command is not a failing run; a step that cannot run is recorded as skipped | the output is a bonus input; the skip is never silent | [Code run](interface.md#code-run) |
| D-18 | No TUI, no existing PR comments fed to the reviewer, no label editing commands in v1 | smaller untrusted surface and surface area | [TUI](interface.md#tui), [Labels](interface.md#labels) |
| D-19 | Everything review-run adds to the draft sits under `meta.review_run`; the model's `meta` is replaced by it | no collision with other writers of `meta`; derivable values are recomputed, never trusted | [Meta](interface.md#meta) |

## Resolved open topics

The seed's four open topics: (1) review artifact metadata: `meta.review_run` ([Meta](interface.md#meta)); (2) default reviewer skill design: D-15 and the [Slot contract](interface.md#slot-contract); (3) subagent depth limit: none by default (D-7), depth 1 when enabled; (4) sandboxing the configured command: D-5 and D-6. Architecture section 20 lists them as closed with v1.16.

## Live verification

All spikes ran on 2026-11-10; the private files are not part of the repository. Claude Code was the version recorded in the spike's results (claude-session's `min_claude_version` applies).

| Spike | What was verified | Result that shaped the design |
|---|---|---|
| A | A real headless review round trip (about 58 haiku runs on a synthetic PR) through claude-session with `--kind review`, sandbox required and no setting sources | works with `--env-allowlist-only PATH`, the plugin dir and the skill; median 41 s, $0.009, 5 turns; 10 of 10 filed an explicit report; 6 of 10 passed the real schema (invented properties, null suggestions); no `<`, `@`, backticks or fences in 253 findings; a missing skill exits 0 `done`; implicit done masks failures; a report command with `$CSESSION_DIR` is denied; confidence is noise; the Read tool refuses files over 256 KB and haiku skims big inputs silently; a too-small artifact cap makes the model shrink its review silently; 0 of 5 injections complied |
| B | Running a configured command in a throwaway: plain, with an env allowlist, and under bubblewrap (WSL2, bwrap 0.9.0) | plain execution leaves files, sockets, `/proc` of other processes, signals and the docker socket open and leaves setsid daemons that block wtctl `remove`; bwrap blocks all of it at 0 to 11 ms; the pid namespace kills everything; output must be read only through a descriptor opened before the child; the output cap needs `RLIMIT_FSIZE`; the sanitizer must neutralize `</untrusted` last; containers are a poor fit |
| C | A permission profile without the `Agent` and `Task` tools | omitting them from `allow` does nothing; `deny` removes them; the Skill tool, Write and `report` keep working; the depth variable is moot with Agent denied; a single-agent run costs a quarter of a delegating one |

Every relied-on behavior becomes a `live` or process regression test in the plan. Still to verify: that `Workflow`, `ScheduleWakeup`, `Cron*` and `RemoteTrigger` cannot launch agents when `Agent` is denied (optional hardening denies are untested); the Ubuntu 24.04 AppArmor effect on unprivileged bwrap; everything macOS; a real `--no-implicit-done` once claude-session ships it; the interactive mode round trip.

## Modules

| Module | Responsibility |
|---|---|
| `cli` | argparse subcommands, global flags, exit-code mapping |
| `config` | YAML, drop-ins, env, per-repo overrides, validation (Pydantic) |
| `gateway` | every subprocess: forgectl, pr-comment, agent-spawn, wtctl, bwrap, the `/proc` scan; the single seam for fakes |
| `prefetch` | the reads, head comparison, manifest and per-file patch building, size caps |
| `prompt` | fenced prompt and input files, nonce, neutralization |
| `coderun` | throwaway, sandboxed runner, output descriptor, sanitizer, removal |
| `launch` | builds agent-spawn arguments, signal forwarding, result handling |
| `finalize` | the finalize steps, normalization, meta derivation, teardown |
| `store` | SQLite (WAL, additive migrations), run directory, leases |
| `hooks` | hook execution and payload building |
| `audit` | `audit.jsonl`, `log.jsonl`, `export` |
| `reconcile` | `reconcile`, `prune`, `cron` |
| `plugin/` | the bundled skill and permission profile, located with `importlib.resources.files('review_run') / 'plugin'` |

## Data model

One `runs` table (id, PR, repo, state, phase, reason, exit code, mode, skill, labels, previous run, agent id, session id, draft id, anchor, timestamps, artifact paths and hashes, teardown state, runner PID and start time, config hash) and a `schema_version` table; labels are a child table (a multiset). Artifact files live in the run directory ([Files](interface.md#files)). Migrations are additive only. A per-run lease (holder PID plus start time plus expiry) guards finalize and resume; finalize is a compare-and-set on `(state, phase)`, so exactly one finalizer wins and the rest observe a terminal row.

## Concurrency

`run` takes no global lock: two runs of the same PR may coexist (a rerun, a different skill). jobsctl's `--unique-key` is the caller's de-duplication. `reconcile` and `prune` take a lease and exit 0 "already running" on contention (design guide section 4.3). Pre-fetch and code run happen before launch, inside `run`'s process, so the jobsctl slot covers them.

## Error handling

Sibling failures map to closed codes (`prefetch_failed`, `launch_failed`, `tool_missing`) carrying the sibling's own closed code and reason in `details`, never its text. Every phase ends in a `finally` path that removes the throwaway, kills the sandbox's process tree and deletes prompt and input files once terminal. A crash leaves a `running` row that `reconcile` or `show` repairs. Sibling error messages (`lower_error`) are untrusted and never copied.

## Test strategy

See [plan.md](plan.md). In brief: one gateway module with fakes in `tests/fakebin/` and pinned sibling schemas; golden files for `--json`; process tests with real bubblewrap (skipped when absent) holding the spike B attack set as regressions; `live` tests (haiku, deselected, never in CI) for the round trip, missing-skill detection, denied tools, the report-resume recovery and large-PR coverage; a small skill eval for the default skill's output discipline.
