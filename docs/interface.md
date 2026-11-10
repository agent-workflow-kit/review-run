# review-run: interface

Status: 0.1.0-design, 2026-11-10. This file is the single normative source for review-run's rules; `design.md`, the README and the architecture link to its headings and never restate its tables. Written before implementation (architecture section 18.1). Section references (architecture N) point to `../awkit-docs/architecture.md`; "design guide N" points to `../awkit-docs/design-guide.md`.

review-run runs one review skill on one pull request and stores the resulting review draft. It is a skill-wrapper (design guide section 11): it gathers the pull request's content deterministically, launches a headless Claude session through agent-spawn, validates and stores the artifact the session reports, and tears the agent down. It never posts anything; posting is pr-comment's job, and the decision to post belongs to the caller.

## Concepts

### Identifiers

A run id is `rr-` followed by 8 lowercase base32 characters (`a-z2-7`), allocated by `run`. Commands accept any unique prefix of at least 4 characters after `rr-` (the `rr-` is optional in input); a shorter prefix exits 2, an ambiguous one 21. Ids are never reused. A PR is accepted as a URL or as `host/<full project path>#N` and printed as `host/<full project path>#N`; a repo is `host/<full project path>`.

### Runs, states and phases

A run is one execution of a review skill on one PR at one head. Its public state is one of:

| State | Meaning | Terminal |
|---|---|---|
| `running` | the run is in one of its phases below, or its session is live | no |
| `done` | the artifact was validated and stored | yes |
| `escalated` | the session reported `needs-input` or `blocked`; its agent is kept for a human | no |
| `failed` | the run ended without a stored artifact; `reason` says why ([Reasons](#reasons)) | yes |

While `running`, a run is in exactly one phase: `prefetch` (reads and prompt building), `code_run` (the optional sandboxed command), `session` (agent-spawn runs the review session) or `finalize` (artifact checks and storing). `escalated` leaves to `running` (phase `session`) when `resume` starts, then ends `done`, `failed` or `escalated` again. Every transition is a compare-and-set on the run's record under its lease; finalizing is idempotent: a run reaches a terminal state once, fires its hook once and tears its agent down once, however many commands race to finalize it.

### The artifact

The artifact is a pr-comment review draft (`schema_version` 1; pr-comment's `docs/interface.md`, section Review draft) whose `meta.review_run` object is review-run's own data ([Meta](#meta)). review-run supplies the draft's `draft_id` and anchor (head, base ref, base sha, merge-base sha) from `pr-comment draft new`; the session copies them and never invents them. The stored `review.json` is immutable once the run is `done`; `original.json` is the byte-exact frozen copy the session reported, kept as evidence.

### Untrusted text

Everything that comes from the forge (titles, bodies, labels, branch names, paths, diffs, check names, comments) and everything the session produces (the artifact, its summary, its reason, any `lower_error` text) is untrusted. review-run never puts untrusted text in argv or env, in a hook payload, in an audit record or in its own error messages (they carry pointers, counts and closed codes). Untrusted text reaches the session only inside nonce fences ([Prompt files](#prompt-files)).

## Run order

`run` performs these steps in this order. A failure ends the run at that step with the reason shown; nothing after it runs.

1. **Usage checks** (exit 2): flags, PR syntax, config, `--previous` run exists and belongs to the same PR (`previous_mismatch`, exit 2). Sibling tools resolved: a missing `forgectl`, `pr-comment` or `agent-spawn` exits 12 (`tool_missing`) with its install command. Nothing is allocated.
2. **Resolve:** `forgectl show <pr> --json` (PR facts, state, repo). A closed or merged PR exits 20 (`not_open`); an unknown host exits 20 (`unknown_host`, from forgectl). The run id is reserved.
3. **`pre-run` hooks** ([Hooks](#hooks)): veto exits 11, escalation exits 13 (no run record exists in either case; review-run has no pending store, so the caller records the confirmation).
4. **Create the run record** (`running`, phase `prefetch`).
5. **Pre-fetch** ([Pre-fetch](#pre-fetch)): anchor, facts, files, diff, CI, manifest, prompt, inputs. Failure: `failed` (`prefetch_failed`, `stale_head`, `too_large`).
6. **Code run** ([Code run](#code-run)), only when the repo configures `run_pr_code`. Never fails the run.
7. **Launch** ([Launch](#launch)): `agent-spawn launch ... --wait`. Without `--no-wait`, `run` waits for the session; with it, `run` returns the run id as soon as the session has started.
8. **Finalize** ([Finalize](#finalize)): artifact checks, normalization, validation, storing, end-of-run head, teardown, hooks.

`run` exits with the outcome of the last step reached ([Exit codes](#exit-codes)). SIGINT and SIGTERM during `run` stop the run cleanly: review-run forwards SIGTERM to the exact `agent-spawn` PID it started (agent-spawn's own signal chain then ends the session), kills the code-run sandbox's process tree if one is live, removes the throwaway, ends the run `failed` (`canceled`) and exits 130 or 143. A run whose process died without cleanup (SIGKILL, crash) stays `running` until `reconcile` or the next `show` finds it ([reconcile](#reconcile)).

## Pre-fetch

review-run reads the PR only through forgectl and `pr-comment draft new`, with `--json`, in this order:

1. `pr-comment draft new <pr> --json`: the skeleton with `draft_id` and `anchor`. pr-comment performs the head-consistent double read and exits 20 (`stale_head`) when the head will not hold still; review-run passes that through as a failed run with reason `stale_head`. When forgectl's `diff` answered `too_large`, `draft new` warns and gives `merge_base_sha: null`; review-run then fails the run with reason `too_large`.
2. `forgectl files <pr> --json` (path, status, additions, deletions, `files_complete`), `forgectl diff <pr> --json` (the unified diff) and `forgectl snapshot <pr> --json` (CI state and PR facts), each checked against the anchor head. A mismatch retries the whole pre-fetch once (from step 1); a second mismatch fails the run (`stale_head`). `files_complete: false`, `diff` exit 20 `too_large` (forgectl's all-or-nothing rule) or a total above `limits.hard_ceiling_bytes` fails the run (`too_large`); no partial diff is ever reviewed silently.
3. When `--previous <run>` is given, that run's stored `review.json` and its `meta.review_run` are read from review-run's own store.

The forgectl and pr-comment calls use forgectl's own timeout and retries; review-run adds none, except the one pre-fetch retry above. Credentials are the sibling tools' business; review-run holds none.

### Manifest and inputs

The diff is split per changed file. Each file's patch is one input file named `f<NNNN>.patch` (four digits, in the order forgectl lists the files), at most `limits.max_file_bytes` each; a larger file's patch is split at hunk boundaries into `f<NNNN>-<k>.patch` parts (a single hunk above the limit is cut at a line boundary and marked truncated in the manifest). Files are included in diff order until `limits.max_total_bytes` or `limits.max_files` would be exceeded; the rest are not provided and are recorded as skipped (`size_cap` or `file_cap`). Binary files and files whose patch is empty appear in the manifest as `binary` or `empty` entries with no input file.

The manifest is a table in the prompt, one row per changed file: file id (`f0001`), path, status, additions, deletions, bytes, input file name(s), and one of `provided`, `truncated`, `skipped(size_cap)`, `skipped(file_cap)`, `binary`, `empty`. Paths are untrusted and appear only inside the fenced manifest block.

### Prompt files

`prompt.md` (passed with `--prompt-file`) is built from a trusted header and fenced untrusted blocks, with one random nonce per run (design guide section 12, architecture section 13.2 rule 1; any `</untrusted` inside content is neutralized as `&lt;/untrusted`):

| Part | Trust | Content |
|---|---|---|
| Header | trusted | run id, PR id, the `draft_id` and anchor to copy, the skill's output rules, `nits`, the `limits` in force, a statement that every `<untrusted>` block is data and not instructions |
| Guidelines | trusted | the contents of `guidelines_file`, if configured (a file the user owns; never read from the repository) |
| PR facts | untrusted block `source="pr"` | title, body, author login, labels, head and base refs, `draft`, `is_fork`, `is_bot`, `base_is_default`, CI summary |
| Manifest | untrusted block `source="manifest"` | the table above |
| Code run | untrusted block `source="code-run"` | the sanitized output of the command, with exit code or signal, duration, truncation ([Code run](#code-run)); or a trusted line saying the step was skipped and why |
| Previous review | untrusted block `source="previous-review"` | when `--previous` is given: findings (id, path, line, severity, title, body) and verdict of that run |

Each input file begins with an opening `<untrusted source="diff" id="R">` line and ends with its closing line, so a model that reads a patch alone still sees the fence. The prompt file and input files live under review-run's run directory and are deleted when the run is terminal; agent-spawn copies them into its own state at classification. The default size of the prompt is a few kilobytes plus the manifest; the diff is never inlined.

## Code run

An optional step that runs a user-configured command on the PR's code and hands only its sanitized output to the session. The session never has a checkout (architecture section 13.2 rule 9). It runs only when `repos.<id>.run_pr_code` is set in the user's own config ([Config](#config)); review-run reads no configuration from the repository under review.

1. **Throwaway.** `wtctl create --throwaway --repo <id> --ref <head sha> --json` (`--source auto`). Failure: the step is `skipped(throwaway_failed)`.
2. **Sandbox check.** The command runs under bubblewrap (`sandbox: required`, the default). If `bwrap` is unusable (missing, user namespaces disabled, blocked by the distribution's policy) the step is `skipped(sandbox_unavailable)`; with `sandbox: preferred` it runs unsandboxed with a warning; `off` runs it unsandboxed without one. `off` and `preferred` are honored only because all config is the user's own. On macOS the step is `skipped(sandbox_unsupported)` unless `sandbox` is `preferred` or `off`.
3. **Command.** `run_pr_code.command` is an argv list, never a shell string. Under `required`, it runs in a sandbox with: no network (`--unshare-net`, always in v1), a new pid, ipc and uts namespace, `--die-with-parent`, `--new-session`, all capabilities dropped, read-only binds of the system directories and of every runtime path in `ro_binds`, the throwaway bound read-write at its own path and used as cwd, an empty tmpfs `HOME` and `/tmp`, and nothing else of the host (no home, no `~/.config`, no state directories, no tmux or SSH agent sockets). The environment is built from scratch: `PATH`, `LANG`, `TERM=dumb`, `HOME` (the empty one), the names in `env_allow`, `GH_CONFIG_DIR` and `GLAB_CONFIG_DIR` set to empty directories; no variable from review-run's own environment otherwise, no `GIT_*`.
4. **Limits.** stdin is `/dev/null`; stdout and stderr go to one file that review-run opened before the child started, in its run directory outside the throwaway; `RLIMIT_FSIZE` bounds that file and a monitor stops the command at `max_output_bytes`; a wall-clock `timeout` ends it; `RLIMIT_CORE` is 0 and `RLIMIT_NOFILE` is bounded; inside the sandbox `RLIMIT_NPROC` is bounded (set from inside the sandbox's user namespace; never set on the host process). On timeout, review-run sends SIGTERM and then SIGKILL to the sandbox's outer process, which ends every process in its pid namespace. Without a sandbox it uses a subreaper and a process sweep by process group, session and parent, followed by a `/proc` scan of working directories and open files, and removal proceeds only when nothing holds the throwaway.
5. **Output.** review-run reads the output only through the file descriptor it opened (never by path). It decodes with replacement, marks every control character other than tab and LF visibly as `<U+XXXX>`, converts CRLF to LF and a lone CR to its marker, truncates to the head and tail with a marker, and neutralizes `</untrusted` last. The result (exit code or signal, `timed_out`, `truncated`, byte count, duration, sandbox mode, sha256 of the sanitized output) is stored in the run directory and summarized in `meta.review_run.code_run`.
6. **Removal.** `wtctl remove <throwaway id>` after the sandbox process tree is gone, in a `finally` path. A removal that exits 20 (`in_use`) or 1 (`partial`) is retried once after a short wait and then logged as a warning; the leaked throwaway is wtctl's `prune --throwaways` to reclaim. Removal failure never fails the run.

A non-zero exit, a signal, a timeout or truncation is not a run failure: the output reaches the session as fenced text and is summarized in `meta.review_run.code_run.status` (`ok`, `failed`, `timed_out`, `truncated`). A step that cannot run is `skipped` with a closed reason; the session is told plainly in the prompt that no test output exists. The skip is always recorded in `meta.review_run.code_run`.

## Launch

`agent-spawn launch` is called with exactly these arguments (flags in this order do not matter):

| Flag | Value |
|---|---|
| `--mode` | `headless` or `interactive` ([`run`](#run)) |
| `--kind` | `review` |
| `--no-implicit-done` | always (a new per-call claude-session override passed through agent-spawn; an unreported clean exit becomes exit 14 and agent-spawn's single automatic report-resume recovers it) |
| `--cwd` | `session-dir` |
| `--repo` | the repo id |
| `--skill` | the configured skill |
| `--plugin-dir` | the bundled plugin directory |
| `--prompt-file`, `--input-file` (repeatable) | the prompt and the per-file patches, previous-review input |
| `--permission-profile` | the bundled profile file, or the configured replacement |
| `--sandbox` | `required` (or the configured value) |
| `--setting-sources` | `""` (or the configured value) |
| `--env-allowlist-only` | each name in `env_allowlist`; when `subagents` is on, also `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH`, whose value `max_subagent_depth` review-run sets in the launch's own environment |
| `--model`, `--timeout` | only when configured or given |
| `--no-promote` | always |
| `--label` | `owner=review-run`, `run=<run id>`, `pr=<pr>`, `repo=<repo>`, plus the caller's labels |
| `--result-file` | a path in the run directory (outside the session directory and cwd, as agent-spawn requires) |
| `--wait` | passed unless `run --no-wait` |
| `--json`, `--actor`, `--decision-id` | as given |

`--ephemeral` is never used: it deletes the session directory, and with it the frozen artifact, before review-run has copied it. `--worktree` is `none` (the default). With `--no-wait`, review-run launches without `--wait`, stores the `agent_id`, returns the run id when agent-spawn reports the run started, and leaves finalization to `wait`, `show`, `resume` or `reconcile`.

The bundled permission profile (`plugin`-adjacent file `permission_profile.json`) is a settings fragment with `permissions.allow: ["Skill"]` and `permissions.deny: ["Agent", "Task", "WebFetch", "WebSearch", "mcp__*", "Bash(gh:*)", "Bash(glab:*)", "Bash(agent-spawn:*)", "Bash(memoctl add:*)"]` and `sandbox.network.allowedDomains: []`; claude-session generates the remaining Bash, Read and Write rules. With `subagents: true`, review-run passes a variant of the profile without the `Agent` and `Task` denies. Leaving the tools out of `allow` does not remove them; only the deny entries do.

A launch failure (agent-spawn's error envelope or a result with `phase: launch`, exits 11, 12, 13 or 20) ends the run `failed` (`launch_failed`) and tears the parked agent down if one was created. agent-spawn's `lower_error` text is untrusted and never copied into review-run's output beyond its closed `code` and `reason`.

## Finalize

Run when the session ended (agent-spawn's outcome object) or when `show`, `wait`, `resume` or `reconcile` finds the agent no longer live. Steps, in order:

1. **Classify the outcome** (agent-spawn exit code and outcome object): `0` `done` continues; `13` becomes `escalated` (reason `needs_input` or `blocked`, from the outcome's report status) and finalizing stops, keeping the agent; `14` to `18` and `12` end the run `failed` with the matching reason ([Reasons](#reasons)).
2. **Reject implicit done:** `report.implicit: true` fails the run (`implicit_done`).
3. **Locate the artifact:** `report.artifact` must be present and named `review.json`; otherwise `artifact_missing`.
4. **Copy and verify:** copy the frozen artifact path from the outcome (read from the outcome object or `claude-session status <csession_id> --json`) into the run directory as `original.json`, opened with no-follow, compared with the reported size and sha256 (`artifact_mismatch` on any difference). All later steps use this copy.
5. **Skill check:** the configured skill must appear in the init message's skill list of the run's result file (`status --json`, `run.result_path`; claude-session exits 0 `done` for a missing skill). Otherwise `skill_missing`.
6. **Parse:** strict JSON, one object, at most `limits.max_artifact_bytes` (default 8 MiB) and a bounded nesting depth; otherwise `artifact_invalid`.
7. **Identity:** `draft_id`, `pr`, `schema_version` and `anchor` must equal the values review-run supplied; otherwise `artifact_mismatch` (never rewritten silently).
8. **Normalize** ([Normalization](#normalization)).
9. **Validate:** `pr-comment draft check - --json` on the normalized draft; exit 0 required, otherwise `artifact_invalid` with the first 20 `errors[]` pointers and kinds (not their messages) in the run record.
10. **Positions:** each finding's path must be in the manifest and its line within a hunk of the provided patch on the stated side; a violation is not an error (pr-comment moves such a finding to the body at post time) and is recorded in `meta.review_run.warnings` (`path_unknown`, `line_not_in_diff`).
11. **Derive meta** ([Meta](#meta)) and write `review.json`.
12. **End head:** `forgectl show <pr> --json` once more; store `head_at_end` and `head_moved` (a failed read stores `head_moved: null`; the run stays `done`).
13. **Teardown:** `agent-spawn teardown <agent_id>` (never `--force`). A refusal or failure is a warning on the run and is retried by `reconcile`; the run is `done` regardless.
14. **Terminal state and hooks:** the run becomes `done`, the audit and log records are written and `post-run` fires.

A run that ends `failed` keeps `original.json` when one was copied and tears its agent down (step 13) unless the failure is a live agent; an `escalated` run keeps its agent until it is resolved by `resume`.

### Normalization

Normalization applies a closed list of lossless fixes to the copy, in this order; each applied fix is listed once in `meta.review_run.normalized` as `{rule, pointer, count}`. Anything not in the list is never changed.

| Rule | Fix |
|---|---|
| `drop_unknown` | remove properties the review-draft schema does not define, at the top level and inside findings (but never inside `meta`) |
| `drop_null` | remove optional finding fields (`title`, `severity`, `start_line`, `side`, `suggestion`) whose value is `null`; `line: null` (a file-level finding) is kept |
| `trim_title` | trim surrounding whitespace of `title`; an empty result removes the field |
| `drop_unanchored` | remove a session-written `unanchored` array (only pr-comment writes it) |
| `replace_meta` | replace the whole `meta` object by the canonical `meta.review_run` ([Meta](#meta)); other `meta` keys the session wrote are dropped and counted |
| `event_downgrade` | an `event` of `request_changes` with no `blocker` or `major` finding becomes `comment` (the reverse is never applied) |

Findings are never added, removed, reordered or re-worded.

### Meta

The draft's `meta` object holds exactly one key, `review_run`, written by review-run:

| Field | Source |
|---|---|
| `version` | review-run (1) |
| `run_id`, `skill`, `skill_version`, `mode` | review-run (`skill_version` is the bundled plugin's version for the default skill, else `null`) |
| `verdict` | the session's `approve`, `comment` or `request_changes`; stored as given, never used by review-run to act |
| `risk`, `risk_reasons` | the session's `low`, `medium` or `high` and up to 10 short strings (each at most 200 characters, one line); untrusted text |
| `summary` | the session's summary, one paragraph, at most 2000 characters; untrusted text |
| `severity_counts` | review-run, counted from the findings |
| `coverage` | review-run: `{status: full|partial, reviewed: [file ids], skipped: [{file, reason}]}`; the session reports the file ids it reviewed, review-run intersects them with the provided ones and marks every other manifest entry `skipped` with the reason `size_cap`, `file_cap`, `truncated` or `not_reviewed` (`binary` and `empty` entries are neither); `partial` when any entry is skipped |
| `pr` | review-run: `{draft, is_bot, is_fork, base_is_default}` from forgectl, each possibly `null` |
| `code_run` | review-run ([Code run](#code-run)): `{status: ok|failed|timed_out|truncated|skipped|not_configured, reason, exit_code, signal, timed_out, truncated, bytes, duration_ms, sandbox, output_sha256}` |
| `head_at_end`, `head_moved` | review-run (step 12) |
| `normalized` | review-run ([Normalization](#normalization)) |
| `warnings` | review-run ([Finalize](#finalize), step 10) |
| `previous_run` | review-run: the `--previous` run id or `null` |

A consumer treats `coverage.status: partial`, `code_run.status: skipped` (when it expected output) and `head_moved: true` as reasons not to act automatically. `meta` is untrusted text downstream. The model's confidence is not collected.

## Resume

`resume <run>` finishes an `escalated` run. It verifies the run's `agent_id` and `csession_id` against agent-spawn's own record for that agent (never labels), checks the agent is not live (exit 20, `live`), and then calls `agent-spawn resume <agent_id> --kind review --no-implicit-done --skill <skill> --plugin-dir <bundled> --permission-profile ... --sandbox ... --setting-sources "" --env-allowlist-only ... --no-promote [--prompt-file <answer>] --wait --result-file <path> --json` with the same safety arguments as `run`. The answer file is untrusted text (the human's reply) and is delivered as a file. On exit it runs [Finalize](#finalize) exactly as `run` does and exits with the same outcome codes. A run that is not `escalated` exits 20 (`not_escalated`); a torn-down or unknown agent exits 10. Because a resumed session keeps its working directory and carries its conversation, the artifact must be attached again; the skill's contract says so ([Slot contract](#slot-contract)).

`show`, `wait` and `reconcile` finalize an `escalated` or `running` run whose agent was resumed or finished by other means, using the same code path. The finalizing step is the same compare-and-set, so concurrent finalizers cannot duplicate any effect.

## Reasons

A failed run carries one `reason` from this closed list, and `exit_code` per the table in [Exit codes](#exit-codes):

| Reason | Meaning |
|---|---|
| `prefetch_failed` | a forgectl or pr-comment read failed (their code is in `details`) |
| `stale_head` | the head moved during pre-fetch and a retry did not settle it |
| `too_large` | forgectl could not produce the whole diff, `files_complete` was false, or the PR exceeds `limits.hard_ceiling_bytes` |
| `launch_failed` | agent-spawn did not start the session |
| `canceled` | SIGINT or SIGTERM during `run` |
| `session_failed` | the session reported `failed` (exit 18) |
| `unreported` | the session ended without a report even after agent-spawn's automatic report-resume (exit 14) |
| `killed`, `timeout`, `lost` | the session was killed, timed out or was lost (exit 15, 16, 17) |
| `implicit_done` | the outcome was an implicit done |
| `artifact_missing` | no `review.json` was attached |
| `artifact_mismatch` | the frozen artifact differed from its reported size or hash, or its `draft_id`, `pr`, `schema_version` or `anchor` differ from what review-run supplied |
| `skill_missing` | the configured skill never loaded |
| `artifact_invalid` | not valid JSON, too large, or rejected by `pr-comment draft check` after normalization |
| `internal` | an unexpected error |

An `escalated` run carries `reason` `needs_input` or `blocked`. A refusal before a run exists (exit 20) has its own `reason` in the error envelope: `not_open`, `unknown_host` (forgectl's), `live`, `not_escalated`, `retention` (prune refusing a non-terminal run).

## Commands

### `run`

`review-run run <pr> [--skill <name>] [--mode headless|interactive] [--label k=v]... [--previous <run>] [--no-wait] [--model <m>] [--timeout <dur>] [--repo <id>] [--hook NAME=CMD]... [--actor A] [--decision-id ID]... [--dry-run] [--json]`

Runs the order of [Run order](#run-order). `--mode` defaults to the config `mode` (`headless`); `interactive` passes `--mode interactive --placement window` to agent-spawn, needs tmux (exit 12, `no_tmux`, otherwise) and still waits and validates afterwards. `--skill` is any name Claude Code accepts as a slash command. `--previous` names a run of the same PR whose stored review is passed as an extra fenced input. `--repo` overrides the repo id derived from the PR (config and per-repo overrides are keyed by it). `--no-wait` returns after the session has started with the run id on stdout (or the run object with `--json`) and exit 0. `--dry-run` performs the reads of the pre-fetch (read-only forgectl and pr-comment calls and agent-spawn's and claude-session's own dry runs), allocates nothing, runs no hook and no code run, launches nothing and prints the plan ([`schemas/cli/run-dry-run.json`](../schemas/cli/run-dry-run.json)): resolved settings with their origin, the launch arguments (no values of secrets; there are none), manifest and coverage preview, sizes and the reasons it would refuse or fail. The exit code is the run's outcome as listed in [Exit codes](#exit-codes).

### `resume`

`review-run resume <run> [--prompt-file <path>|-] [--no-wait] [--hook ...] [--actor A] [--decision-id ID]... [--dry-run] [--json]`. See [Resume](#resume). `--no-wait` is accepted and returns once the resumed session has started.

### `wait`

`review-run wait <run> [--timeout <dur>] [--json]` blocks until the run is terminal or `escalated` (finalizing it if its agent has ended) and exits with the run's exit code; a wait timeout exits 12 with `reason: wait_timeout` and the run unchanged (exit 22 keeps its single meaning).

### `list`

`review-run list [--state S]... [--pr <pr>] [--repo <id>] [--label k=v]... [--since <time>] [--limit N] [--json]`. Filters AND; `--label` matches any value of the key. Newest first. Does not refresh runs.

### `show`

`review-run show <run> [--no-refresh] [--draft] [--json]` prints the run object ([Run object](#run-object)). Unless `--no-refresh`, a `running` run with an agent, or an `escalated` run, is refreshed from agent-spawn first and finalized if its agent has ended (a read with a side effect; it is idempotent). `--draft` prints the stored `review.json` on stdout instead (untrusted text; exit 10 if the run has none). `--json` always includes `artifact.path`, the absolute path of the stored `review.json`.

### `reconcile`

`review-run reconcile [--dry-run] [--json]` repairs state against reality and reports what it changed: it finalizes `running` or `escalated` runs whose agent ended; ends a `running` run whose runner process (PID plus start time) is gone and whose agent was never launched as `failed` (`lost`); retries pending agent teardowns; removes run-directory leftovers of terminal runs older than a day (prompt and input files); and logs leaked throwaways it recorded. It takes a lease; a second concurrent invocation exits 0 reporting "already running". It never signals a process.

### `prune`

`review-run prune [--older-than <duration>] [--dry-run] [--json]` deletes terminal runs (`done` and `failed`) older than the duration (default config `retention`, 30 days) together with their directories and artifact copies. It never touches a `running` or `escalated` run, never signals anything and is audited. The audit and log records of pruned runs stay.

### `cron`

`review-run cron install|uninstall [--dry-run]` writes or removes the tagged crontab block `# review-run managed` with an explicit `PATH` including `~/.local/bin`: `reconcile` every 15 minutes and `prune` daily (design guide section 4.3).

### `export`

`review-run export [--log] [--since <time>]` prints review-run's audit records (or log records with `--log`) as JSONL.

## Global flags

`--json`, `--config PATH`, `--verbose`, `--quiet`, `--dry-run` (where meaningful), `--actor`, `--decision-id` (repeatable), `--hook NAME=CMD` (repeatable, on `run` and `resume`), `--version`. There is no `--yes`: review-run has no confirm-by-default action. Untrusted text arrives only as `--prompt-file <path>|-`; `--label` values follow the label grammar of architecture section 8.1.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | `done` (for `run --no-wait` and `resume --no-wait`: the session started) |
| 1 | internal error |
| 2 | usage error |
| 10 | not found (a run, an agent, a draft) |
| 11 | a `pre-run` hook vetoed (no run exists) |
| 12 | the run failed outside the session (`prefetch_failed`, `stale_head`, `too_large`, `launch_failed`, `internal`), a sibling tool is missing (`tool_missing`), tmux is missing for `--mode interactive` (`no_tmux`) or a `wait` timed out (`wait_timeout`) |
| 13 | the run is `escalated`, or a `pre-run` hook escalated (no run exists; the caller records the confirmation) |
| 14 to 18 | the session's outcome, passed through from agent-spawn: 14 exited-unreported, 15 killed, 16 timeout, 17 lost, 18 reported failed |
| 20 | refused (`reason`: `not_open`, `unknown_host`, `live`, `not_escalated`); all are found before any run record exists or on a run that is not in the right state |
| 21 | ambiguous run prefix |
| 22 | the session finished but its artifact is missing or invalid (`artifact_missing`, `artifact_mismatch`, `artifact_invalid`, `skill_missing`, `implicit_done`) |
| 130, 143 | interrupted (SIGINT, SIGTERM) |

Exit 22 is review-run's only tool-specific code (agent-spawn's wait timeout code 22 is not passed through: a session never times out as a wait). A `failed` run's `show` always reports its `reason` and the `exit_code` it ended with. `stale_head` and `too_large` are found during pre-fetch, after the run record exists, so they end the run `failed` and exit 12 with that reason.

## Output by exit code

| Exit | `--json` stdout |
|---|---|
| 0 | the run object ([`schemas/cli/run.json`](../schemas/cli/run.json)); for `run --no-wait` the same with `state: running` |
| 11, 13 (hook) | the error envelope with `hook` and `decision_ids` |
| 12, 14 to 18, 22 | the run object (`state: failed`, `reason`) |
| 13 (run) | the run object (`state: escalated`) |
| 2, 10, 20, 21, 1 | the error envelope ([`schemas/cli/error.json`](../schemas/cli/error.json)): `{error: {code, message, reason?, details?, lower_code?}}`; messages and details hold only closed codes, ids and counts |

### Run object

`run_id`, `pr`, `repo`, `state`, `phase`, `reason`, `exit_code`, `mode`, `skill`, `labels` (multiset of `k=v`), `previous_run`, `agent_id`, `csession_id`, `draft_id`, `anchor` (head_sha, base_ref, base_sha, merge_base_sha), `created_at`, `finished_at`, `artifact` (`path`, `sha256`, `size`, `original_sha256`; null until stored), `summary` (the `meta.review_run` fields `verdict`, `severity_counts`, `risk`, `coverage.status`, `code_run.status`, `head_moved`; null until stored), `warnings`, `teardown` (`done`, `pending`, `failed`), `schema_version`. The artifact and `summary` text fields are untrusted; the schema marks them.

## Hooks

Hooks are configured as `hooks.<name>` lists in config (design guide section 8; `sh -c`, own process group, JSON on stdin, 30 s default timeout, `async` for post hooks). Environment: `REVIEW_RUN_HOOK`, `REVIEW_RUN_ID` (the run id; reserved for `pre-run`), `REVIEW_RUN_OBJECT_TYPE=run`. Hook processes inherit review-run's environment minus configured token variables. Payloads ([`schemas/hooks/`](../schemas/hooks/)) carry top-level `tool`, `hook`, `object_type`, `object_id`, `repo`, `labels` and hold only trusted or derived values: never PR text, the session's report summary or reason, `meta.summary` or `risk_reasons`.

| Hook | Fires | Veto or escalate | Payload (beyond the common keys) |
|---|---|---|---|
| `pre-run` | after resolve (step 3), before the run record exists and before any read of the diff or launch | veto exit 11; escalate exit 13 | `pr`, `skill`, `mode`, `previous_run`, `head_sha`, `actor`, `decision_ids` |
| `post-run` | on the transition to `done` or `failed` (once) | no | `state`, `reason`, `exit_code`, `pr`, `agent_id`, and for `done`: `verdict`, `severity_counts`, `risk`, `coverage_status`, `code_run_status`, `head_moved` |
| `post-escalate` | on the transition to `escalated` (each time) | no | `reason` (`needs_input` or `blocked`), `pr`, `agent_id` |

A `decision_id` printed by a `pre-run` hook is recorded on the run and in the audit record. Gate hooks should use a longer `timeout` than `gatectl check --timeout` (architecture section 12.3). review-run has no hook for posting; review-watch's own `post-draft` hook is review-watch's.

## Config

Config is YAML at `~/.config/review-run/config.yaml` plus `config.d/*.yaml` drop-ins (design guide section 5); schema in [`schemas/config.json`](../schemas/config.json); every key is documented in `config.example.yaml`. Precedence: flags, then `repos.<id>`, then environment (`REVIEW_RUN_<KEY>` for top-level scalars only; nested keys joined with `__` are accepted for scalars), then global config, then defaults. The repo is `--repo`, else derived from the PR argument; review-run never derives it from the working directory and reads no configuration from the repository under review. Invalid config fails fast naming the key.

| Key | Default | Meaning |
|---|---|---|
| `skill` | `review-run:review-pr` | the skill; bundled default or any other |
| `mode` | `headless` | default mode |
| `permission_profile` | bundled file | permission profile file passed on every call |
| `sandbox` | `required` | session sandbox level passed on every call |
| `setting_sources` | `[]` | setting sources passed on every call |
| `env_allowlist` | `[PATH]` | names passed with `--env-allowlist-only` |
| `model` | `null` | `--model` (null: Claude Code's default) |
| `timeout` | `null` | `--timeout` (null: the kind's default, 120 minutes) |
| `subagents` | `false` | `true` drops the `Agent` and `Task` denies from the profile |
| `max_subagent_depth` | `1` | depth variable when `subagents` is on |
| `nits` | `false` | ask the skill for `nit`-severity findings |
| `guidelines_file` | `null` | a user-owned file placed in the prompt as trusted instructions |
| `limits.max_file_bytes` | `200000` | per input file (below the Read tool's 256 KB refusal) |
| `limits.max_total_bytes` | `1048576` | total of provided patches before files are skipped |
| `limits.max_files` | `300` | number of input files |
| `limits.hard_ceiling_bytes` | `8388608` | the PR's whole diff above this fails the run (`too_large`) |
| `limits.max_artifact_bytes` | `8388608` | artifact size above this is `artifact_invalid` |
| `retention` | `30d` | age at which `prune` deletes terminal runs |
| `run_pr_code` | absent | per repo only ([below](#run_pr_code)) |
| `me` | OS user | `me.name` for actors |
| `hooks` | `{}` | hook lists |

`repos.<id>` may override `skill`, `model`, `timeout`, `nits`, `guidelines_file`, `subagents`, `max_subagent_depth`, `limits`, `permission_profile`, `sandbox`, `setting_sources`, `env_allowlist` and `run_pr_code`.

### `run_pr_code`

Only under `repos.<id>.run_pr_code`; a repo without it runs no command.

| Key | Default | Meaning |
|---|---|---|
| `command` | required | argv list (never a shell string) run in the throwaway |
| `sandbox` | `required` | `required`, `preferred` or `off` |
| `timeout` | `300s` | wall-clock limit |
| `max_output_bytes` | `1048576` | output captured before the command is stopped |
| `env_allow` | `[]` | extra names for the command's environment (non-secret) |
| `ro_binds` | `[]` | extra absolute paths bound read-only (language runtimes, pre-populated caches, a `node_modules` directory) |

There is no `network` key in v1: the command never has network access. An `install_command` phase is roadmap work.

## Environment variables

`REVIEW_RUN_<KEY>` sets top-level scalar config values (design guide section 5). review-run reads no other variable for its own behavior. It passes none of its environment to the review session or the code-run command except the names above. Hook variables are in [Hooks](#hooks). It sets `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH` in agent-spawn's environment only when `subagents` is on. Credentials are the sibling tools': review-run needs no token variable.

## Audit and log

`audit.jsonl` (design guide section 6; [`schemas/audit.json`](../schemas/audit.json)) records, with `actor`, `decision_id`s, `target` (the run id and PR), `action`, `inputs` (anchor head sha, config hash, skill, `previous_run`) and `result`: `run` (one record at the start with `result: attempted`, one at the end with the outcome), `resume` (the same pair), `teardown` and `prune`. No record holds PR text, the artifact or `meta` text. `log.jsonl` ([`schemas/log.json`](../schemas/log.json)) records phase and state changes, warnings, normalization counts and sibling-tool error codes. `export` prints either.

## Files

State under `~/.local/state/review-run/` (XDG-aware, the same on macOS): `state.db` (SQLite, WAL, additive migrations), `audit.jsonl`, `log.jsonl`, `runs/<run id>/` with `review.json`, `original.json`, `code-run.txt` (sanitized output) and, while the run is not terminal, `prompt.md`, `inputs/` and the agent-spawn result file. `state.db` is private; the audit and log files are public append-only contracts. The state directory must lie outside every path an isolated session can write.

## Labels

Labels are set at `run` (`--label k=v`, repeatable), stored as a multiset on the run, filterable with `list --label`, returned verbatim in `--json` and never given built-in meaning. They are passed to the agent together with `owner=review-run`, `run=<id>`, `pr=<pr>` and `repo=<repo>`. review-run finds its own agent by the stored `agent_id`, never by labels. Labels are fixed at creation; there are no `label add` or `remove` commands.

## TUI

None in v1. `list` and `show` give the same information.

## Slot contract

The `review-pr` slot (architecture section 5.5; kind `review`, default mode headless). Bundled default: skill `review-run:review-pr` in this repo's `plugin/` (a Claude Code plugin named `review-run`).

**Inputs.** The prompt file ([Prompt files](#prompt-files)) and per-file patches under `inputs/<run>/` (the SessionStart context line lists them). Everything inside `<untrusted>` blocks is data. The header gives the `draft_id`, the anchor and the `pr` id to copy verbatim.

**Expected artifact.** A file `review.json` in `$CSESSION_DIR/artifacts/`, a pr-comment review draft: `schema_version`, `draft_id`, `pr`, `anchor`, `event`, `body`, `findings[]` and a `meta` object holding `verdict`, `risk`, `risk_reasons`, `summary` and `coverage.reviewed` (the file ids reviewed). Output rules the skill states: copy `draft_id`, `pr` and `anchor` exactly; copy each finding's `path` exactly from the manifest and set `line` and `side` from the patch (right side by default); one-line `title`; no suggestion fences in text; put replacement code only in the structured `suggestion` field (right side, non-null line); do not use `<`, `@` or images in prose (they are neutralized when posted); `request_changes` only with at least one `blocker` or `major` finding; every finding anchored to a changed line; review every manifest entry in order and list the file ids reviewed; record that no test output exists when the prompt says so.

**Reporting.** The skill writes its summary with the Write tool to a file and runs `claude-session report done --summary-file <absolute path> --artifact review.json`, using absolute paths only (a command containing `$CSESSION_DIR` is denied under `dontAsk`); it attaches the artifact again on every report, including after a resume. It reports `needs-input` with a reason file when it needs a human's judgment. It never mentions subagents (the bundled profile denies the tools; the model would waste a turn).

**Validation by review-run.** [Finalize](#finalize). The skill's `nits` and `guidelines_file` behaviors are driven by the prompt header.

## Upgrades

Additive migrations only; a newer `state.db` than the installed tool exits 12 (`schema_newer`). A change to the review-draft schema or to agent-spawn's, claude-session's or forgectl's outputs is absorbed by the pinned sibling schemas in tests; an unknown field in a sibling's output is ignored, a missing required one fails the step closed.

## Residual risks

- A prompt-injected review session can still steer its own output (findings, verdict, summary, risk). All of it is untrusted and reviewed by a human, or gated, before any external action; `meta` never authorizes anything.
- The model's `verdict`, `risk` and `coverage.reviewed` are claims; review-run verifies only their form and recomputes what it can derive.
- A silently partial read inside one provided file (the model skimming a long patch) is not detectable; the per-file size limit and the coverage statement reduce it.
- Without the sandbox (`sandbox: preferred` or `off` for the code run, or a lowered session sandbox) attacker code or a steered session runs with the user's rights; both are loud (warnings and recorded modes).
- On macOS there is no code-run sandbox in v1; the step is skipped.
- The code-run sandbox does not limit memory or CPU; a hostile command can exhaust them until the timeout.
- A same-user process can alter review-run's state directory and forge a run's artifact; the audit trail and the frozen original are not tamper-proof (architecture section 19.2).
- Skill detection relies on the init message's skill list in claude-session's result file; a change in Claude Code's init format would make the check fail closed (`skill_missing`).
- `meta.review_run.coverage.reviewed` can claim files the model skimmed; coverage is accounting, not proof.

## Platform notes

Linux and WSL: everything. macOS: everything except the code run (skipped in v1); the session sandbox is Claude Code's own (`sandbox-exec`), checked by claude-session. Prerequisites: `forgectl`, `pr-comment` and `agent-spawn` on `PATH` (each missing one exits 12 `tool_missing` naming its install command); `wtctl` when any repo configures `run_pr_code`; `bwrap` and `socat` for a sandboxed code run and for the sandboxed session on Linux and WSL2. Claude Code's skill-loading, permission and sandbox behaviors were verified against the version recorded in `design.md`; a `live` suite re-checks them.
