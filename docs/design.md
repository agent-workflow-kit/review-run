# review-run: design

Status: designed 2026-11-10 (architecture v1.16, interface 0.1.0-design). The rules are in [interface.md](interface.md), which is the single normative source; this document records the decisions, the live verification behind them and the internal design. It links to interface headings and restates none of its tables.

Section references (architecture N) point to `../awkit-docs/architecture.md`; "design guide N" points to `../awkit-docs/design-guide.md`.

## Role and layer

Run a review skill on a pull request and store the resulting review draft. Layer 3 (workflow). Works fully standalone at a terminal (it needs forgectl, pr-comment, agent-spawn and claude-session installed, and wtctl only for `run_pr_code`). It is a skill-wrapper (design guide section 11): default slot `review-pr`, bundled as `review-run:review-pr` in `plugin/`. It never posts a review; posting belongs to pr-comment and the decision to post to the caller (review-watch).

## Uses

| Tool | Binding | What for |
|---|---|---|
| forgectl | A | pre-fetch: `show`, `files`, `diff`, `ci` (`--json`); one more `show` at the end ([Pre-fetch](interface.md#pre-fetch)) |
| pr-comment | A | `draft new` (draft id and anchor), `draft check` (validation) ([Finalize](interface.md#finalize)); its review-draft schema is pinned in tests. This replaces the earlier "schema only" edge |
| agent-spawn | A | `launch` (without `--wait`), `wait`, `show`, `stop`, `resume`, `teardown`, `list` for the one label recovery ([Launch](interface.md#launch), [Cancel](interface.md#cancel)); never `--ephemeral`, never promotion |
| wtctl | A | `create --throwaway`, `remove`, only when a repo configures `run_pr_code` ([Code run](interface.md#code-run)) |
| `git` | none directly | review-run runs no git itself; wtctl creates the throwaway |
| `bwrap`, `socat` | third party | the sandbox for `run_pr_code` (Linux and WSL2) |
| claude-session | A | `status --run <n> --json` for the skill check (headless only); the frozen artifact path comes from agent-spawn's outcome object. Its per-call `--no-implicit-done` is passed through agent-spawn ([Finalize](interface.md#finalize)); its `status` schema is pinned in tests |

No tool is aware of review-run. review-watch (its caller) is not a dependency.

## Decisions

| # | Decision | Rationale | Interface |
|---|---|---|---|
| D-1 | `run` blocks until the run is `done`, `escalated` or `failed`; `--no-wait` returns at session start | review-watch runs it inside a jobsctl job, so the queue cap counts running reviews and SIGTERM cancels cleanly (per phase, see [Signals](interface.md#signals)) | [run](interface.md#run), [Run order](interface.md#run-order) |
| D-2 | Always `--kind review --no-implicit-done`; `report.implicit: true` is a failed run | spike A: with implicit done a session that cannot report ends `done` with a prose file; with it off the same case exits 14 and agent-spawn's single report-resume recovered the review in 5 s. A per-call flag keeps the kind name and needs no user config | [Launch](interface.md#launch), [Finalize](interface.md#finalize) |
| D-3 | Lossless normalization of a copy, then `pr-comment draft check`; no repair resume | spike A: 11 of 37 artifacts failed the schema, nearly all by invented properties or null optionals; the fixes lose nothing and the model keeps authority over content. Failures with no lossless fix are accepted losses and end `artifact_invalid` (a title over 256 characters, a `suggestion` on the left side, `start_line >= line`, duplicate finding ids, an empty finding body, more than 500 findings); positions are pr-comment's check at post time | [Normalization](interface.md#normalization) |
| D-4 | Per-file input files, a manifest and a coverage statement; caps give a partial review that says so; `too_large` fails | spike A: the Read tool refuses files over 256 KB, haiku silently skimmed head and tail of a 2.3 MB diff; forgectl's `diff` is all or nothing | [Manifest and inputs](interface.md#manifest-and-inputs), [Meta](interface.md#meta) |
| D-5 | `run_pr_code` runs in bubblewrap by default, with no network, and is skipped (not fatal) where the sandbox is unusable; no macOS sandbox in v1 | spike B: an allowlisted plain run left every attack open (files, sockets, `/proc`, daemons that block wtctl `remove`); bwrap blocked all of them at 0 to 11 ms. Nested user namespaces are disabled and the tmpfs and workspace sizes are bounded, so the sandbox cannot be widened from inside or fill the disk | [Code run](interface.md#code-run) |
| D-6 | Network is always off in v1; dependencies via `ro_binds`; an `install_command` phase is 1.x work | network-on re-exposes localhost, the host IP and abstract sockets; install scripts are attacker code | [`run_pr_code`](interface.md#run_pr_code) |
| D-7 | The bundled profile denies `Agent`, `Task` and every other tool the review does not use (never a bare `Edit` or `Write` deny: claude-session generates the path-scoped rules for them, and only `Edit(...)` rules govern the Write tool); `subagents: true` drops only the `Agent` and `Task` denies and sets the depth variable | spike C: omitting the tools from `allow` removes nothing, only `deny` does, and the init tool list shows account-level tools beyond `Agent` and `Task`; subagents cost 2 to 8 times more with no visible gain. A live test pins the init tool inventory, so a Claude Code upgrade cannot add a tool silently | [Launch](interface.md#launch), [Config](interface.md#config) |
| D-8 | pr-comment is called through its CLI (`draft new`, `draft check`) | one source of truth for what will be accepted at post time; `draft new` already does the head-consistent double read | [Pre-fetch](interface.md#pre-fetch) |
| D-9 | `run --previous <run>` passes an earlier review as a fenced input; otherwise runs are independent | explicit arguments only; a re-review can note what is fixed | [Prompt files](interface.md#prompt-files) |
| D-10 | `run` refuses only a closed or merged PR and an unknown host; drafts, bots, forks and stacked PRs are reviewed and recorded in `meta.review_run.pr` | skip policy belongs to review-watch | [run](interface.md#run), [Meta](interface.md#meta) |
| D-11 | The anchor comes from `draft new`; other reads are compared with it and the pre-fetch retried once; the head is read again at the end and a move is recorded, not fatal | no atomic head-pinned read exists; the anchor names what was reviewed and `pr-comment review --reanchor-to` handles a moved head | [Pre-fetch](interface.md#pre-fetch), [Finalize](interface.md#finalize) |
| D-12 | `resume <run>` finishes an `escalated` run; `cancel <run>` ends one without an answer; `show`, `wait` and `reconcile` finalize runs resumed by other means | the architecture keeps the escalated agent; one finalizing code path ([Finalize recovery](interface.md#finalize-recovery)) | [Resume](interface.md#resume), [cancel](interface.md#cancel) |
| D-13 | Retention 30 days by `prune`; non-terminal runs are never pruned, and an `escalated` run ends by `resume`, `cancel` or `escalation_ttl` expiry | matches other tools' retention; review-watch copies the artifact into its own draft; the expiry bounds what an attacker-driven escalation can accumulate | [prune](interface.md#prune), [cancel](interface.md#cancel) |
| D-14 | `--mode interactive` is a pass-through to agent-spawn's window placement; always `--no-promote` | the design guide requires both modes; predictable escalation | [run](interface.md#run) |
| D-15 | The default skill is a single-agent, high-precision reviewer (nits off), with `guidelines_file`; no confidence number; another review skill from a custom plugin loads through `plugin_dirs` as `<plugin>:<skill>` and must satisfy the slot contract | spike A: confidence was 0.90 to 0.97 on every run; a reviewer that cries wolf gets ignored; the header's output rules come from a versioned template, so any skill gets the same rules | [Slot contract](interface.md#slot-contract), [Config](interface.md#config) |
| D-16 | Three hooks: `pre-run`, `post-run`, `post-escalate`; payloads hold trusted or derived values only; a `pre-run` escalation is overridden only by a human with `run --override-escalation` (a veto still vetoes) | nothing from the PR or the model reaches a hook command; the override follows design guide 4.4 and the siblings' human-only flags, and the gate sees what it needs (PR facts, whether code will run) | [Hooks](interface.md#hooks), [Global flags](interface.md#global-flags) |
| D-17 | `run_pr_code` is defined only in the user's own config as an argv list; a failing command is not a failing run; a step that cannot run is recorded as skipped (including a fork PR whose head is not fetchable from the base repository) | the output is a bonus input; the skip is never silent | [Code run](interface.md#code-run) |
| D-18 | No TUI, no existing PR comments fed to the reviewer, no label editing commands in v1 | smaller untrusted surface and surface area | [TUI](interface.md#tui), [Labels](interface.md#labels) |
| D-19 | Everything review-run adds to the draft sits under `meta.review_run`; the model's `meta` is replaced by it and review-run sets the identity fields (`schema_version`, `draft_id`, `pr`, `anchor`) itself | no collision with other writers of `meta`; derivable values are recomputed, never trusted; the trusted header carries no forge text and no copy mistake can fail a review | [Meta](interface.md#meta), [Finalize](interface.md#finalize) |
| D-20 | Launch without `--wait`, store the agent id from the launch result, then `agent-spawn wait <agent_id>`; one recovery lookup by label (`reconcile`, `show`, `wait`) for a run that died before the id was stored | the agent id is known from the session's start, so a dead runner never orphans a live session; the label lookup is verified against agent-spawn's own record and is never used to authorize anything else | [Launch](interface.md#launch), [Finalize recovery](interface.md#finalize-recovery), [Labels](interface.md#labels) |
| D-21 | Identity fields are set by review-run after parsing; the session's values are replaced | simplification: no copy mistake fails a review and the header needs no `anchor.base_ref` | [Finalize](interface.md#finalize) |
| D-22 | No review-run position check, no `meta.review_run.warnings` | pr-comment checks paths and lines at post time and moves invalid findings to the body; a second check duplicated it and needed hunk parsing | [Finalize](interface.md#finalize), [Meta](interface.md#meta) |
| D-23 | The session's `meta` claims are validated strictly: closed enums, counts and lengths; a violation is `artifact_invalid` with pointers and kinds only | consistent with D-3 (lossless fixes only, anything else fails); lets `verdict` and `risk` reach `post-run` while `summary` and `risk_reasons` stay untrusted text | [Session claims](interface.md#session-claims) |
| D-24 | Exception to design guide 11.3: the bundled skill carries one report instruction (use absolute paths in the report command; `$CSESSION_DIR` is denied) | spike A: the standard report instructions alone fail under `dontAsk` | [Slot contract](interface.md#slot-contract) |
| D-25 | `cancel <run>` ends a `running` or `escalated` run (`canceled`); config `escalation_ttl` (default `7d`, `null` disables) expires an `escalated` run (`expired`) | no other way existed to stop a run or abandon an escalated one; the TTL bounds parked agents and non-terminal rows; "one escalated run per PR" was declined because it makes `run` behave differently by caller pattern (de-duplicate with jobsctl `--unique-key`) | [cancel](interface.md#cancel), [Config](interface.md#config), [Reasons](interface.md#reasons) |
| D-26 | One sanitizer for code-run output and every untrusted string placed in a prompt or printed in human mode; `--json` keeps raw text | one rule set to test; `</untrusted` is neutralized last | [Sanitizer](interface.md#sanitizer) |
| D-27 | The process runs with umask 077; state and run directories are 0700, files 0600 and created with `O_EXCL` and `O_NOFOLLOW`; a symlinked or foreign-owned state root is refused; the prompt and input files are deleted as soon as the launch has returned | the run directory holds third-party code and review text | [Files](interface.md#files) |

## Resolved open topics

The seed's four open topics: (1) review artifact metadata: `meta.review_run` ([Meta](interface.md#meta)); (2) default reviewer skill design: D-15 and the [Slot contract](interface.md#slot-contract); (3) subagent depth limit: none by default (D-7), depth 1 when enabled; (4) sandboxing the configured command: D-5 and D-6. Architecture section 20 lists them as closed with v1.16.

## Live verification

All spikes ran on 2026-11-10; the private files are not part of the repository. Claude Code was the version recorded in the spike's results (claude-session's `min_claude_version` applies).

| Spike | What was verified | Result that shaped the design |
|---|---|---|
| A | A real headless review round trip (about 58 haiku runs on a synthetic PR) through claude-session with `--kind review`, sandbox required and no setting sources | works with `--env-allowlist-only PATH`, the plugin dir and the skill; median 41 s, $0.009, 5 turns; 10 of 10 filed an explicit report; 6 of 10 passed the real schema (invented properties, null suggestions); no `<`, `@`, backticks or fences in 253 findings; a missing skill exits 0 `done`; implicit done masks failures; a report command with `$CSESSION_DIR` is denied; confidence is noise; the Read tool refuses files over 256 KB and haiku skims big inputs silently; a too-small artifact cap makes the model shrink its review silently; 0 of 5 injections complied |
| B | Running a configured command in a throwaway: plain, with an env allowlist, and under bubblewrap (WSL2, bwrap 0.9.0) | plain execution leaves files, sockets, `/proc` of other processes, signals and the docker socket open and leaves setsid daemons that block wtctl `remove`; bwrap blocks all of it at 0 to 11 ms; the pid namespace kills everything; output must be read only through a descriptor opened before the child; the output cap needs `RLIMIT_FSIZE`; the sanitizer must neutralize `</untrusted` last; containers are a poor fit |
| C | A permission profile without the `Agent` and `Task` tools | omitting them from `allow` does nothing; `deny` removes them; the Skill tool, Write and `report` keep working; the depth variable is moot with Agent denied; a single-agent run costs a quarter of a delegating one |

Every relied-on behavior becomes a `live` or process regression test in the plan. Still to verify: that denying a tool name absent from the session is accepted and that the init `tools` list under the bundled profile equals the pinned set (`Skill`, `Read`, `Write`, `Edit`, `Glob`, `Grep`, `Bash`, `ToolSearch`, `TodoWrite`); that a `NotebookEdit` deny does not affect Write (left out of the profile until confirmed); that `Workflow`, `ScheduleWakeup`, `Cron*` and `RemoteTrigger` cannot launch agents when `Agent` is denied; the Ubuntu 24.04 AppArmor effect on unprivileged bwrap; everything macOS; a real `--no-implicit-done` once claude-session ships it; the interactive mode round trip (the skill check is not performed there, `skill_unchecked`); whether a 200 KB input is read whole or in pages; whether a fork PR's head is fetchable by wtctl.

Minimum sibling versions: review-run needs claude-session and agent-spawn releases that support `--no-implicit-done` and a wtctl release that accepts `create --throwaway --source auto --label`; the exact versions are filled when those releases ship, and until then the step-1b preflight exits 12 `tool_too_old` before anything is allocated ([Run order](interface.md#run-order), [Upgrades](interface.md#upgrades)).

## Modules

| Module | Responsibility |
|---|---|
| `cli` | argparse subcommands, global flags, exit-code mapping |
| `config` | YAML, drop-ins, env, per-repo overrides, validation (PyYAML, jsonschema against `schemas/config.json`) |
| `gateway` | every subprocess: forgectl, pr-comment, agent-spawn, claude-session, wtctl, bwrap, the `/proc` scan; the single seam for fakes |
| `prefetch` | the reads, head comparison, manifest and per-file patch building, size caps |
| `prompt` | fenced prompt and input files, nonce, the sanitizer |
| `coderun` | throwaway, sandboxed runner, output descriptor, removal (output goes through the sanitizer) |
| `launch` | builds agent-spawn arguments, the per-phase signal handling ([Signals](interface.md#signals)), result handling |
| `finalize` | the finalize steps, session-claim validation, normalization, meta derivation, teardown |
| `store` | SQLite (WAL, additive migrations), run directory, leases |
| `hooks` | hook execution and payload building |
| `audit` | `audit.jsonl`, `log.jsonl`, `export` |
| `reconcile` | `reconcile` (including resume of finalization, expiry and the label recovery), `cancel`, `prune`, `cron` |
| `plugin/` | the bundled skill and permission profile, located with `importlib.resources.files('review_run') / 'plugin'` |

## Data model

One `runs` table (id, PR, repo, head sha, state, phase, reason, exit code, mode, skill, labels, previous run, agent id, session id, `launch_result_path`, `awaited_run`, `escalated_run`, `finalized_run`, the per-invocation hooks, actor and decision ids, draft id, anchor, timestamps including `escalated_at`, artifact paths and hashes, teardown state, runner PID and start time, config hash) and a `schema_version` table; labels are a child table (a multiset); an `events` table is the outbox for hook delivery (each event has an `event_id`; delivery is at least once and deduplicated by `event_id`). Artifact files live in the run directory ([Files](interface.md#files)). Migrations are additive only. A per-run lease (holder PID plus start time plus expiry, 5 minutes, renewed every 60 s by its holder) guards finalize and resume; finalize is a compare-and-set on `(state, phase, awaited_run)`, so exactly one finalizer wins and the rest observe a terminal row. The three run-number columns decide which agent-spawn run a finalizer may take as its outcome ([Finalize recovery](interface.md#finalize-recovery)).

## Concurrency

`run` takes no global lock: two runs of the same PR may coexist (a rerun, a different skill). jobsctl's `--unique-key` is the caller's de-duplication. `reconcile` and `prune` take a lease and exit 0 "already running" on contention (design guide section 4.3). Pre-fetch and code run happen before launch, inside `run`'s process, so the jobsctl slot covers them.

## Error handling

Every reason, its state, exit code and output shape is one table ([Reasons](interface.md#reasons)). Sibling failures map to closed codes (`prefetch_failed`, `launch_failed`, `tool_missing`) carrying the sibling's own closed code and reason in the run's `failure`, never its text. Signals are handled per phase ([Signals](interface.md#signals)). Every phase ends in a `finally` path that removes the throwaway, kills the sandbox's process tree and deletes prompt and input files once terminal. A crash leaves a `running` row that `reconcile`, `show` or `wait` repairs ([Finalize recovery](interface.md#finalize-recovery)). Sibling error messages (`lower_error`) are untrusted and never copied.

## Dependencies

Run time: PyYAML (config) and `jsonschema` (config validation against `schemas/config.json`, the single source, so no second model duplicates it); nothing else. Test only: pytest. Third-party programs are listed under Uses.

## Test strategy

See [plan.md](plan.md). In brief: one gateway module with fakes in `tests/fakebin/` and pinned sibling schemas; golden files for `--json`; process tests with real bubblewrap (skipped when absent) holding the spike B attack set as regressions; `live` tests (haiku, deselected, never in CI) for the round trip, missing-skill detection, denied tools, the report-resume recovery and large-PR coverage; a small skill eval for the default skill's output discipline.
