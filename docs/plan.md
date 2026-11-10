# review-run: plan

Implements [interface.md](interface.md) and [design.md](design.md). Issues are expanded into bite-sized TDD steps when picked up; this plan fixes the stages, the module map, the conventions and the version roadmap.

## Conventions for every stage

- **Gateway boundary.** Every subprocess (forgectl, pr-comment, agent-spawn, claude-session, wtctl, `bwrap`, `socat`, the `/proc` scan) goes through `src/review_run/gateway.py`; tests substitute fakes. No other module spawns a process.
- **Fakes and pinned schemas.** Fake binaries for forgectl, pr-comment, agent-spawn, claude-session and wtctl live in `tests/fakebin/` (copied per repo, never shared). Sibling schemas the tool reads (forgectl `show`, `files`, `diff`, `ci`; pr-comment's review-draft schema and `draft new`/`draft check` output; agent-spawn's launch result and error envelope; claude-session's `status` outcome object) are copied into `tests/` pinned to a version and every fake's output is validated against them.
- **Process and live split.** Unit and process tests run in `make verify` (process tests that need real bubblewrap skip when it is unusable). Tests that need a real model or real host carry the pytest marker `live`, are deselected by default and never run in CI.
- **Golden files** for every `--json` output, validated against `schemas/`.
- **Spike regressions.** Every behavior relied on from the live verification is a test. Spike A's findings: missing-skill detection, `--no-implicit-done`, large-input behavior, and the normalization fixture (a finding property the model invented, such as an extra free-text field, and `"suggestion": null`). Spike C's deny profile: the `Agent` and `Task` tools and every other unused tool are absent from the init tool list, and the list equals the pinned allowed set. Spike B's attack set, each a process test under real bubblewrap with the expected result in the table:

| Attack | Expected result |
|---|---|
| read planted canary files under a fake home | not found or empty; the canary never appears in the output |
| connect to a planted Unix socket and to an abstract socket | connection refused or absent |
| read another process's `/proc/<pid>/environ` | the process is not visible (pid namespace) |
| write outside the throwaway | read-only file system error; nothing outside changes |
| a `setsid` daemon that outlives the command | gone when the command ends (pid namespace); `wtctl remove` succeeds |
| symlink swap of the output file | review-run reads only through the descriptor it opened before the child; the swapped target is never read |
| an output flood | stopped at the output cap; the run records `truncated` |
| a command that ignores SIGTERM | SIGKILL 5 s after SIGTERM; the run records `timed_out` |
| a nested user namespace (`unshare -Urm`) | fails |
| a 2 GB write into the workspace | stopped by the workspace bound; the run records `failed` |

- **`make verify`** passes before every commit. Untrusted text never appears in argv, env, hook payloads, audit records or error messages; a test greps every output for planted markers. Files and directories created by review-run have modes 0600 and 0700.

## Module map

| Module | Stage | Responsibility |
|---|---|---|
| `cli` | 0 | argparse subcommands, global flags, exit-code mapping |
| `config` | 0 | YAML, drop-ins, env, per-repo overrides, validation |
| `gateway` | 0 | the only place that spawns processes |
| `store` | 0 | SQLite (WAL, additive migrations), run directory, leases, ids |
| `audit` | 0 | `audit.jsonl`, `log.jsonl`, `export` |
| `prefetch` | 1 | forgectl and `pr-comment draft new` reads, head comparison, manifest, per-file patches, size caps |
| `prompt` | 1 | fenced prompt and input files, nonce, the sanitizer |
| `launch` | 2 | agent-spawn arguments, result handling, and the per-phase signal handling (`signals`) |
| `finalize` | 2 | artifact checks, normalization, meta derivation, teardown |
| `hooks` | 2 | hook execution and payloads |
| `plugin/` | 2 | bundled `review-pr` skill and permission profile |
| `reconcile` | 3 | `reconcile` (including `resume`, finalization by another process, expiry and the label recovery), `cancel`, cancellation repair |
| `coderun` | 4 | throwaway, sandboxed runner, output descriptor, removal (output through the sanitizer) |

## Stage 0: Scaffold, config, store, gateway

**Goal**: A CLI skeleton that loads and validates config, keeps runs in `state.db`, writes audit and log records and spawns nothing except through the gateway.
**Success Criteria**: `review-run --version`, `list --json` and `show` on an empty store work; config errors name the key; migrations apply; the gateway is the single subprocess seam with a recording fake.
**Tests**: config precedence and validation (flags, repo override, env, global, defaults); schema validation of `config.example.yaml`; store CAS and lease behavior under concurrent processes; id prefix resolution (unique, ambiguous exit 21, too short exit 2); golden `list` and `show` outputs; a grep test that no module imports `subprocess` outside `gateway`; a golden per row of the reasons table (reason, state, exit code, `--json` shape); config tests for `ro_binds`, `env_allow` and `guidelines_file` refusals, `escalation_ttl`, `plugin_dirs`, `max_turns` and the grammar rules; audit goldens (one record pair per action, the `attempt_id`, the veto record); file modes (0700 and 0600, umask 077) and a symlinked state root refused (`state_unsafe`).
**Status**: Not Started

## Stage 1: Pre-fetch and prompt

**Goal**: From a PR reference, produce the anchor, manifest, per-file patches and fenced prompt, with head consistency and size policy, and print them with `run --dry-run` (the manifest part of the dry run; its launch part lands in Stage 2 and its code-run plan in Stage 4).
**Success Criteria**: Anchor from `pr-comment draft new`; mismatching heads retry once then `stale_head`; `too_large` and `files_complete: false` fail; files over the per-file limit split at hunk boundaries; caps yield skipped entries; closed PR and unknown host refused (exit 20).
**Tests**: fakes for each forgectl read incl. head flips; manifest and coverage preview goldens; fence neutralization (`</untrusted` in a title, a path, a diff line); nonce uniqueness; prompt contains no raw diff; a property test that provided, skipped, binary and empty entries together equal the manifest and the changed files; renames, quoted and TAB paths, a diff-header count mismatch (`diff_files_mismatch`), binary and empty classification and the stop rule; the caps goldens (PR body, labels, CI summary, skipped rows); sanitizer fixtures (TAG characters, bidi controls, U+2028, a case-variant `<untrusted` opener and closer).
**Status**: Not Started

## Stage 2: Launch, finalize, hooks, bundled skill

**Goal**: A blocking `run` that launches the session through agent-spawn, finalizes the artifact and exits with the interface's codes.
**Success Criteria**: Exact launch arguments (table in the interface), the step-1b preflight (`tool_too_old`, `no_tmux`, `label_reserved`, `pr_too_long`) and skill resolution (`skill_not_found`) before anything is allocated; the outcome mapping of every agent-spawn result row to states and exits; implicit done, missing artifact, mismatch, missing skill, invalid artifact each fail with the right reason and exit 22; normalization closed list; `draft check` gate; `meta.review_run` derived; end head recorded; teardown (never `--force`, never `--ephemeral`); `pre-run`, `post-run` hooks; the bundled skill and profile ship.
**Tests**: golden `run` outputs per outcome (a fake agent-spawn result per row of the launch mapping table); preflight goldens with a fake agent-spawn that rejects a flag; normalization fixtures from the spike A failures (`failure_scenario`, `"suggestion": null`) plus an invented property inside `suggestion`, `event: approve` and a padded title (passes `draft check`); derived-meta recomputation ignoring a lying model; a golden that an artifact with a wrong `anchor` is stored with the right one; each session-claim rule violated (an 8 MiB summary, 11 reasons, a reason with a newline, an unknown verdict) ends `artifact_invalid` with the right kind and no text in the output; strict parsing (duplicate keys, deep nesting, a lone surrogate, a file that grows after its size was reported); frozen-artifact copy with a size/hash mismatch; skill check per mode (headless reads the init skills of the awaited run, interactive gives `skill_unchecked`); skill resolution and the placeholder check; `run --override-escalation` without a TTY exits 2, the override makes exit 13 pass and a veto still vetoes; the dry run's launch part; `live`: the round trip with haiku, the missing skill, the init tool list equals the pinned set (denying an absent name is accepted). `live`: the round trip with haiku, the missing skill, denied `Agent`/`Task`. Depends on claude-session's `--no-implicit-done` and agent-spawn's pass-through (cross-tool issues); until they ship, tests use fakes (the step-1b preflight exits 12 `tool_too_old` on an older sibling) and the `live` recovery test is skipped.
**Status**: Not Started

## Stage 3: Escalation, resume, cancel, signals, reconcile

**Goal**: Runs survive escalation, a dead runner, cancellation and a second finalizer.
**Success Criteria**: `escalated` keeps the agent; `resume` verifies the stored agent id and finalizes like `run`; `show`/`wait`/`reconcile` finalize runs resumed elsewhere exactly once; signals are handled per phase ([Signals](interface.md#signals)) and end the run `canceled` with exit 130/143; `cancel` ends a running or escalated run and `escalation_ttl` expires an escalated one; a dead runner never orphans a live session (the id is stored at launch, the result file and the one label lookup cover the rest); finalize is re-runnable at any step; `--no-wait`; `wait`; `reconcile` repairs lost runs, expired escalations and pending teardowns.
**Tests**: concurrent finalizers (process test, one winner) and a race of `resume` and `reconcile`; kill the runner at each phase and reconcile (`wait` on a run killed in `prefetch` returns exit 12 `runner_lost`); SIGKILL of the runner during the session and while `launch` is in flight (reconcile adopts the id from `launch.json`); SIGKILL of the runner and the launcher between allocation and the result file (the label recovery adopts exactly one matching agent, otherwise `runner_lost` with `agent_unknown`); crash at each finalize step and rerun, with a duplicate hook delivery carrying the same `event_id`; a `show` of an escalated, not-resumed run does nothing; `post-escalate` once per escalation; `reconcile` finalizes and fires the stored per-invocation hook; external `agent-spawn teardown` of an escalated run's agent, then `show` (`agent_gone`); resume of a non-escalated run (exit 20); torn-down agent (exit 10); resume failure keeps `escalated`; an answer over the cap; a fenced answer in the built prompt; `cancel` of running, escalated and terminal runs; TTL expiry; signal tests per row of the signals table with a fake agent-spawn that records its signals and `stop` calls (a reported-done session canceled ends `canceled`; SIGHUP leaves the session and a later `wait` finalizes it); `live`: needs-input then resume with a new artifact (spike A9).
**Status**: Not Started

## Stage 4: Code run

**Goal**: The optional `run_pr_code` step in a bubblewrap sandbox with safe output capture.
**Success Criteria**: The interface's Code run steps exactly: throwaway, sandbox check, argv-only command, from-scratch environment, no network, pid namespace, limits, output file opened before the child and read through its descriptor, sanitizer, removal after the process tree is gone; skip reasons recorded; nested user namespaces disabled and tmpfs and workspace sizes bounded; a skip reason recorded for every case (`wtctl_missing`, `throwaway_refused`, `ref_unavailable`, `throwaway_failed`, `sandbox_unavailable`, `sandbox_unsupported`, `sandbox_failed`, `throwaway_leaks`); macOS skips.
**Tests**: process tests (need bubblewrap) holding the spike B attack table in Conventions (`unshare -Urm` fails, a 2 GB write is stopped, a 300 MB tmpfs write fails, a large legitimate file under 256 MiB passes); sanitizer fixtures (NUL, invalid UTF-8, ANSI, bidi, CR tricks, `</untrusted`); output flood and `RLIMIT_FSIZE` bound; timeout with a SIGTERM-ignoring setsid grandchild; removal after `in_use`; skip when `bwrap` is faked unusable; the command never sees any variable from review-run's environment (a planted canary), and a canary value in the output is redacted before it enters the prompt; a test per skip reason and the leak cap; without bwrap an unrelated process holding the throwaway is never signaled and blocks removal; the `code_run` audit record; the dry run's code-run plan with the sandbox probe.
**Status**: Not Started

## Stage 5: Retention, cron, export, interactive mode, previous run

**Goal**: The remaining commands and options.
**Success Criteria**: `prune` (never touches non-terminal runs), `cron install|uninstall` (tagged block), `export`, `--mode interactive` pass-through (needs tmux), `--previous` input, and the readiness facts printed by `run --dry-run` (sibling tool versions, the bwrap probe result, the resolved skill and plugin directories, `tool_too_old` conditions).
**Tests**: prune age and state filters; crontab golden with a fake `crontab`; `--previous` fenced input, each refusal (`previous_not_done`, `previous_mismatch`, unknown run), the 64 KiB cap and the stored-hash re-check; list filters, limit and `truncated` goldens; cron golden entries with `--json`; the `reconcile` and `prune` output goldens; interactive launch arguments golden including `skill_unchecked`.
**Status**: Not Started

## Stage 6: Live suite, skill evaluation, macOS checklist

**Goal**: Re-verify every relied-on third-party behavior against the current Claude Code and document what only a human can check.
**Success Criteria**: A `live` suite (haiku; deselected) covering the round trip, missing skill, denied tools, the implicit-done recovery, large-PR coverage, a second plugin's skill loaded through `plugin_dirs`, a fork PR's head, the pinned tool inventory and the code run under real bubblewrap; the paging measurement (whether a 200 KB input is read whole, deciding `max_file_bytes`); a small eval of the default skill's output discipline (no suggestion fences, one-line titles, exact paths, lines inside hunks) over a fixed set of synthetic PRs; a macOS checklist (everything except the code run, which stays skipped).
**Tests**: the suite itself, run by hand before each minor release.
**Status**: Not Started

## Version roadmap

- **0.x:** stages 0 to 6, released as the stages land (`feat` commits; no `!`).
- **1.0 qualifies when:** the live suite passes against the current Claude Code; claude-session's `--no-implicit-done` and agent-spawn's pass-through have shipped and review-run's recovery test passes live; review-watch drives review-run on the author's machine end to end (headless, an escalated run resumed, a partial-coverage run); the macOS checklist is done for the non-code-run parts; the sandboxed code run has run against real repositories on Linux or WSL; a runner killed during a session is recovered by `reconcile`; the pinned tool inventory matches.
- **1.x (additive):** a seccomp filter for the code-run sandbox; an `install_command` phase with a filtered network proxy; a macOS (Seatbelt) code-run sandbox; a TUI; label add and remove commands; feeding existing PR comments to the reviewer; additional bundled skills; optional multi-agent reviewing as a bundled alternative skill.
- **2.0:** only for a breaking change to the review-draft contract, the `meta.review_run` shape, the exit codes or the run states.
