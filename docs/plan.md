# review-run: plan

Implements [interface.md](interface.md) and [design.md](design.md). Issues are expanded into bite-sized TDD steps when picked up; this plan fixes the stages, the module map, the conventions and the version roadmap.

## Conventions for every stage

- **Gateway boundary.** Every subprocess (forgectl, pr-comment, agent-spawn, wtctl, `bwrap`, `socat`, the `/proc` scan) goes through `src/review_run/gateway.py`; tests substitute fakes. No other module spawns a process.
- **Fakes and pinned schemas.** Fake binaries for forgectl, pr-comment, agent-spawn and wtctl live in `tests/fakebin/` (copied per repo, never shared). Sibling schemas the tool reads (forgectl `show`, `files`, `diff`, `snapshot`; pr-comment's review-draft schema and `draft new`/`draft check` output; agent-spawn's launch result and error envelope; claude-session's `status` outcome object) are copied into `tests/` pinned to a version and every fake's output is validated against them.
- **Process and live split.** Unit and process tests run in `make verify` (process tests that need real bubblewrap skip when it is unusable). Tests that need a real model or real host carry the pytest marker `live`, are deselected by default and never run in CI.
- **Golden files** for every `--json` output, validated against `schemas/`.
- **Spike regressions.** Every behavior relied on from the live verification is a test: the spike B attack set (canary reads, sockets, a surviving setsid daemon, a symlink swap on the output file, an output flood, a SIGTERM-ignoring timeout), the spike C deny profile (the `Agent` tool is absent from the init tool list), and spike A's findings (missing-skill detection, `--no-implicit-done`, large-input behavior).
- **`make verify`** passes before every commit. Untrusted text never appears in argv, env, hook payloads, audit records or error messages; a test greps every output for planted markers.

## Module map

| Module | Stage | Responsibility |
|---|---|---|
| `cli` | 0 | argparse subcommands, global flags, exit-code mapping |
| `config` | 0 | YAML, drop-ins, env, per-repo overrides, validation |
| `gateway` | 0 | the only place that spawns processes |
| `store` | 0 | SQLite (WAL, additive migrations), run directory, leases, ids |
| `audit` | 0 | `audit.jsonl`, `log.jsonl`, `export` |
| `prefetch` | 1 | forgectl and `pr-comment draft new` reads, head comparison, manifest, per-file patches, size caps |
| `prompt` | 1 | fenced prompt and input files, nonce, neutralization |
| `launch` | 2 | agent-spawn arguments, signal forwarding, result handling |
| `finalize` | 2 | artifact checks, normalization, meta derivation, teardown |
| `hooks` | 2 | hook execution and payloads |
| `plugin/` | 2 | bundled `review-pr` skill and permission profile |
| `reconcile` | 3 | `reconcile`, resume, cancellation repair |
| `coderun` | 4 | throwaway, sandboxed runner, output descriptor, sanitizer, removal |

## Stage 0: Scaffold, config, store, gateway

**Goal**: A CLI skeleton that loads and validates config, keeps runs in `state.db`, writes audit and log records and spawns nothing except through the gateway.
**Success Criteria**: `review-run --version`, `list --json` and `show` on an empty store work; config errors name the key; migrations apply; the gateway is the single subprocess seam with a recording fake.
**Tests**: config precedence and validation (flags, repo override, env, global, defaults); schema validation of `config.example.yaml`; store CAS and lease behavior under concurrent processes; id prefix resolution (unique, ambiguous exit 21, too short exit 2); golden `list` and `show` outputs; a grep test that no module imports `subprocess` outside `gateway`.
**Status**: Not Started

## Stage 1: Pre-fetch and prompt

**Goal**: From a PR reference, produce the anchor, manifest, per-file patches and fenced prompt, with head consistency and size policy, and print them with `run --dry-run`.
**Success Criteria**: Anchor from `pr-comment draft new`; mismatching heads retry once then `stale_head`; `too_large` and `files_complete: false` fail; files over the per-file limit split at hunk boundaries; caps yield skipped entries; closed PR and unknown host refused (exit 20).
**Tests**: fakes for each forgectl read incl. head flips; manifest and coverage preview goldens; fence neutralization (`</untrusted` in a title, a path, a diff line); nonce uniqueness; prompt contains no raw diff; a property test that provided plus skipped files equal the changed files.
**Status**: Not Started

## Stage 2: Launch, finalize, hooks, bundled skill

**Goal**: A blocking `run` that launches the session through agent-spawn, finalizes the artifact and exits with the interface's codes.
**Success Criteria**: Exact launch arguments (table in the interface); outcome mapping to states and exits; implicit done, missing artifact, mismatch, missing skill, invalid artifact each fail with the right reason and exit 22; normalization closed list; `draft check` gate; `meta.review_run` derived; end head recorded; teardown (never `--force`, never `--ephemeral`); `pre-run`, `post-run` hooks; the bundled skill and profile ship.
**Tests**: golden `run` outputs per outcome (fake agent-spawn results); normalization fixtures from the spike A failures (`failure_scenario`, `"suggestion": null`); derived-meta recomputation ignoring a lying model; frozen-artifact copy with a size/hash mismatch; init skill-list check; `live`: the round trip with haiku, the missing skill, denied `Agent`/`Task`. Depends on claude-session's `--no-implicit-done` and agent-spawn's pass-through (cross-tool issues); until they ship, tests use fakes and the `live` recovery test is skipped.
**Status**: Not Started

## Stage 3: Escalation, resume, cancellation, reconcile

**Goal**: Runs survive escalation, a dead runner, cancellation and a second finalizer.
**Success Criteria**: `escalated` keeps the agent; `resume` verifies the stored agent id and finalizes like `run`; `show`/`wait`/`reconcile` finalize runs resumed elsewhere exactly once; SIGINT/SIGTERM forward to the exact agent-spawn PID, end the run `canceled` and exit 130/143; `--no-wait`; `wait`; `reconcile` repairs lost runs and pending teardowns.
**Tests**: concurrent finalizers (process test, one winner); kill the runner at each phase and reconcile; resume of a non-escalated run (exit 20); torn-down agent (exit 10); signal tests with a fake agent-spawn that records its signals; `live`: needs-input then resume with a new artifact (spike A9).
**Status**: Not Started

## Stage 4: Code run

**Goal**: The optional `run_pr_code` step in a bubblewrap sandbox with safe output capture.
**Success Criteria**: The interface's Code run steps exactly: throwaway, sandbox check, argv-only command, from-scratch environment, no network, pid namespace, limits, output file opened before the child and read through its descriptor, sanitizer, removal after the process tree is gone; skip reasons recorded; macOS skips.
**Tests**: process tests (need bubblewrap) holding the spike B attack set; sanitizer fixtures (NUL, invalid UTF-8, ANSI, bidi, CR tricks, `</untrusted`); output flood and `RLIMIT_FSIZE` bound; timeout with a SIGTERM-ignoring setsid grandchild; removal after `in_use`; skip when `bwrap` is faked unusable; the command never sees any variable from review-run's environment (a planted canary).
**Status**: Not Started

## Stage 5: Retention, cron, export, interactive mode, previous run

**Goal**: The remaining commands and options.
**Success Criteria**: `prune` (never touches non-terminal runs), `cron install|uninstall` (tagged block), `export`, `--mode interactive` pass-through (needs tmux), `--previous` input, doctor-relevant facts printed by `run --dry-run`.
**Tests**: prune age and state filters; crontab golden with a fake `crontab`; `--previous` fenced input and same-PR check; interactive launch arguments golden.
**Status**: Not Started

## Stage 6: Live suite, skill evaluation, macOS checklist

**Goal**: Re-verify every relied-on third-party behavior against the current Claude Code and document what only a human can check.
**Success Criteria**: A `live` suite (haiku; deselected) covering the round trip, missing skill, denied tools, the implicit-done recovery, large-PR coverage and the code run under real bubblewrap; a small eval of the default skill's output discipline (no suggestion fences, one-line titles, exact paths, lines inside hunks) over a fixed set of synthetic PRs; a macOS checklist (everything except the code run, which stays skipped).
**Tests**: the suite itself, run by hand before each minor release.
**Status**: Not Started

## Version roadmap

- **0.x:** stages 0 to 6, released as the stages land (`feat` commits; no `!`).
- **1.0 qualifies when:** the live suite passes against the current Claude Code; claude-session's `--no-implicit-done` and agent-spawn's pass-through have shipped and review-run's recovery test passes live; review-watch drives review-run on the author's machine end to end (headless, an escalated run resumed, a partial-coverage run); the macOS checklist is done for the non-code-run parts; the sandboxed code run has run against real repositories on Linux or WSL.
- **1.x (additive):** an `install_command` phase with a filtered network proxy; a macOS (Seatbelt) code-run sandbox; a TUI; label add and remove commands; feeding existing PR comments to the reviewer; additional bundled skills; optional multi-agent reviewing as a bundled alternative skill.
- **2.0:** only for a breaking change to the review-draft contract, the `meta.review_run` shape, the exit codes or the run states.
