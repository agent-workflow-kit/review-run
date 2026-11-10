# review-run: interface

Status: 0.1.0-design, 2026-11-10. This file is the single normative source for review-run's rules; `design.md`, the README and the architecture link to its headings and never restate its tables. Written before implementation (architecture section 18.1). Section references (architecture N) point to `../awkit-docs/architecture.md`; "design guide N" points to `../awkit-docs/design-guide.md` (both [published](https://github.com/agent-workflow-kit/awkit-docs)).

review-run runs one review skill on one pull request and stores the resulting review draft. It is a skill-wrapper (design guide section 11): it gathers the pull request's content deterministically, launches a headless Claude session through agent-spawn, validates and stores the artifact the session reports, and tears the agent down. It never posts anything; posting is pr-comment's job, and the decision to post belongs to the caller.

## Concepts

### Identifiers

A run id is `rr-` followed by 8 lowercase base32 characters (`a-z2-7`), allocated by `run`. Commands accept any unique prefix of at least 4 characters after `rr-` (the `rr-` is optional in input); a shorter prefix exits 2, an ambiguous one 21. Ids are never reused, including the id reserved by a `pre-run` attempt that was vetoed or escalated (every reserved id is kept in `state.db`, also after `prune`). A PR argument is accepted as a URL or as `host/<full project path>#N`; review-run checks only that shape and passes the argument to `forgectl show`, and the canonical `pr` (`host/<full project path>#N`) and `repo` (`host/<full project path>`) that forgectl returns are the ones used everywhere afterwards, including the `repos.<id>` lookup (exact match). `list --pr` takes the canonical form and `list --repo` the canonical repo id.

### Runs, states and phases

A run is one execution of a review skill on one PR at one head. Its public state is one of:

| State | Meaning | Terminal |
|---|---|---|
| `running` | the run is in one of its phases below, or its session is live | no |
| `done` | the artifact was validated and stored | yes |
| `escalated` | the session reported `needs-input` or `blocked`; its agent is kept for a human | no |
| `failed` | the run ended without a stored artifact; `reason` says why ([Reasons](#reasons)) | yes |

While `running`, a run is in exactly one phase: `prefetch` (reads and prompt building), `code_run` (the optional sandboxed command), `session` (agent-spawn runs the review session) or `finalize` (artifact checks and storing). `escalated` leaves to `running` (phase `session`) when `resume` starts, then ends `done`, `failed` or `escalated` again. Every transition is a compare-and-set on the run's record under its lease; ending a run is one such compare-and-set, so whichever process commits the terminal (or `escalated`) transition first wins and every other process observes the stored row. A cancellation is recorded first, as the run's `cancel_requested_at` with the actor and the exit code (143, or 130 for SIGINT and SIGHUP), by a compare-and-set that succeeds only while the run is `escalated`, or `running` in a phase other than `finalize`; the move into phase `finalize` is a compare-and-set that succeeds only while `cancel_requested_at` is null, so exactly one of the two wins. Any process that finds `cancel_requested_at` set ends the run `failed` (`canceled`) with the stored exit code and never copies its artifact, after `agent-spawn stop` when its agent is not parked. Finalizing is idempotent: a run reaches a terminal state once, enqueues its hook once (delivered at least once, [Hooks](#hooks)) and tears its agent down once, however many commands race to finalize it.

### The artifact

The artifact is a pr-comment review draft (`schema_version` 1; pr-comment's `docs/interface.md`, section Review draft) whose `meta.review_run` object is review-run's own data ([Meta](#meta)). review-run supplies `schema_version`, `draft_id`, `pr` and `anchor` (head, base ref, base sha, merge-base sha) from `pr-comment draft new` and writes them into its copy after parsing; the session does not need to write them, and any value it wrote there is replaced. The stored `review.json` is immutable once the run is `done`; `original.json` is the byte-exact frozen copy the session reported, kept as evidence.

### Untrusted text

Everything that comes from the forge (titles, bodies, labels, branch names, paths, diffs, check names, comments) and everything the session produces (the artifact, its summary, its reason, any `lower_error` text) is untrusted. review-run never puts untrusted text in argv or env, in a hook payload, in an audit record or in its own error messages (they carry pointers, counts and closed codes). Untrusted text reaches the session only inside nonce fences ([Prompt files](#prompt-files)). `verdict` and `risk` are closed enum values after validation ([Session claims](#session-claims)), so they may appear in a hook payload; `summary` and `risk_reasons` never leave `review.json`.

### Sanitizer

One sanitizer is applied to code-run output ([Code run](#code-run)), to every untrusted string placed in the prompt or an input file (PR facts, manifest paths, per-file patches, `previous-review.txt`, the `resume` answer) and to every untrusted string review-run prints in human mode (`show`, `list`, `run`, `wait`). In order: invalid UTF-8 decodes with replacement; characters of Unicode categories Cc (except TAB and LF), Cf, Co, Cs, Cn, Zl and Zp, the variation selectors U+FE00 to U+FE0F and U+E0100 to U+E01EF, and U+034F, U+115F, U+1160, U+3164 and U+FFA0 become `<U+XXXX>`; CRLF becomes LF and a lone CR becomes its marker; only for a field with a budget ([Prompt files](#prompt-files)), truncation keeps the first and the last half of the budget with the line `[... N bytes omitted ...]` between them; last, every case-insensitive match of `<\s*/?\s*untrusted` has its `<` replaced by `&lt;`. Per-file patches, `previous-review.txt` and the answer are never truncated by the sanitizer; their caps apply to the sanitized bytes. `--json` output keeps the raw text, escaped by JSON. `show --draft` prints the stored JSON as it is (it is JSON, so its escapes are inert) and says so on stderr.

## Run order

`run` performs these steps in this order. A failure ends the run at that step with the reason shown; nothing after it runs.

1. **Checks before anything is read or allocated.** (a) Usage (exit 2): flags, the PR argument's shape, config (including [config validation](#config) of every `repos.<id>` entry), `--override-escalation` without a human actor, caller labels with a reserved key (`label_reserved`) or more than 60 of them (`too_many_labels`), an answer or prompt above its cap, and `--previous`: an unknown or pruned run exits 10, a run that is not `done` exits 2 (`previous_not_done`, `details.state`). (b) Tools and state (exit 12): `forgectl`, `pr-comment`, `agent-spawn` and `claude-session` resolved on `PATH` (`tool_missing`, with the install command), `agent-spawn --version` and `claude-session --version` at or above the minimums in `design.md` (`tool_too_old`), a state root that is a symlink or not owned by the user (`state_unsafe`). Nothing is allocated.
2. **Resolve, repo checks and preflight:** `forgectl show <pr> --json` gives the PR facts, state, canonical `pr` and `repo` and head sha (failures: [Resolve step failures](#resolve-step-failures)). With the canonical `pr` and `repo` known, still exit 2 and nothing allocated: a `pr=` label value above 256 characters (`pr_too_long`), `--previous` naming a run of another PR (`previous_mismatch`), the per-repo settings, and skill resolution (`<plugin>:<skill>`; an unknown plugin, skill or a bare name is `skill_not_found`; each plugin directory passed needs the placeholders `.claude/`, `.mcp.json`, `CLAUDE.md` and `CLAUDE.local.md`). Then `wtctl` on `PATH` when the repo configures `run_pr_code` (`tool_missing`), and `agent-spawn launch --dry-run --json` with the planned flags (no prompt or input files), which performs agent-spawn's tool resolution, claude-session's classification dry run and, for `--mode interactive`, the termctl window preflight, mapped as in [Resolve step failures](#resolve-step-failures). Only then is the run id reserved.
3. **`pre-run` hooks** ([Hooks](#hooks)): a veto exits 11, an escalation exits 13 unless `--override-escalation` was given; no run record exists in either case, so each writes an audit `run` record with result `vetoed` or `escalated` (target: the reserved run id and the PR) and the caller records any confirmation. A reserved id of such an attempt is never reused.
4. **Create the run record** (`running`, phase `prefetch`), with `head_sha`, the run's hooks, actor and decision ids, and write `settings.json` and `permission-profile.json` into the run directory ([Run settings](#run-settings)) before anything is launched.
5. **Pre-fetch** ([Pre-fetch](#pre-fetch)): anchor, facts, files, diff, CI, manifest and the per-file input files. Failure: `failed` (`prefetch_failed`, `stale_head`, `too_large`).
6. **Code run** ([Code run](#code-run)), only when the repo configures `run_pr_code`. Never fails the run. It writes `code-run.json` in every case (`status: not_configured` when the repo has no `run_pr_code`).
7. **Launch** ([Launch](#launch)): `prompt.md` is assembled (header, guidelines, PR facts, manifest, code-run block, previous-review line), then `run` launches without agent-spawn's `--wait`, stores `agent_id`, `csession_id` and `awaited_run` in one transaction, then blocks in `agent-spawn wait <agent_id>`; with `--no-wait` it returns the run id after the store.
8. **Finalize** ([Finalize](#finalize)): artifact checks, normalization, validation, storing, end-of-run head, teardown, hooks.

`run` exits with the outcome of the last step reached ([Exit codes](#exit-codes)). A run whose process died without cleanup (SIGKILL, crash) stays `running` until `reconcile`, `show` or `wait` finds it ([reconcile](#reconcile)).

### Resolve step failures

`forgectl show`: `not_found` exits 10 (envelope, `lower_code: not_found`); `unknown_host` exits 20; a PR whose `state` is not `open` exits 20 (`not_open`); forgectl's exit 2 exits 2 (`code: usage`, `lower_code`, for example `invalid_pr`); any other failure exits 12 (`code: failed`, `reason: lower_failed`, `tool: forgectl`, `lower_code`, and `details.retry_after` when forgectl gave one). `agent-spawn launch --dry-run`: exit 0 continues; an envelope whose `tool` is `termctl` (exit 2 or 12) exits 12 `no_tmux` with termctl's code in `lower_code`; agent-spawn's `tool_missing` passes through (exit 12 `tool_missing`); exit 20 (the plan's `would_refuse`) exits 20 (`code: refused`, `reason: lower_refused`, `lower_code` the refusal reason, `tool` from the plan); every other exit, or an output that does not parse, exits 12 (`code: failed`, `reason: lower_failed`, `tool`, `lower_code`).

### Signals

SIGINT, SIGTERM and SIGHUP are handled per phase:

| Phase | SIGTERM, SIGINT | SIGHUP | Result |
|---|---|---|---|
| before the run record (usage, resolve, `pre-run`) | stop the in-flight child (exact PID) or hook process group | as SIGTERM | no record; exit 143 (SIGTERM) or 130, nothing on stdout |
| `prefetch` | record the cancellation, then stop the in-flight child | as SIGTERM | `failed` (`canceled`) |
| `code_run` | record the cancellation, then SIGTERM to the sandbox's outer process, SIGKILL after 5 s, then throwaway removal | as SIGTERM | `failed` (`canceled`) |
| `session` | record the cancellation; with the agent-spawn launcher in flight, forward SIGTERM once to its exact PID and wait for it to exit (at most 60 s); then, when an agent id is known (stored, or in the launcher's result), `agent-spawn stop <agent_id> --grace 10s --actor <actor> --json` (a no-op on a parked agent), the `agent-spawn wait` waiter (exact PID) is stopped, and `agent-spawn teardown` | stop waiting; the session keeps running and the run stays `running` until `show`, `wait` or `reconcile` finalizes it | `failed` (`canceled`), or unchanged for SIGHUP |
| `finalize` | deferred until finalize ends | deferred | the run's own outcome and exit code |

Recording the cancellation is the compare-and-set of [Runs, states and phases](#runs-states-and-phases); when it fails because finalize already began, the signal is deferred as in the `finalize` row. A launcher that has not exited after 60 s, or a runner killed before it ends the run, leaves the run `running` with its cancellation recorded; the next `show`, `wait`, `reconcile` or `cancel` ends it, adopting the agent from `launch.json` first. A canceled run is `failed` (`canceled`) even when the session had already reported; its artifact is not copied. A second identical signal is ignored. `wait` and a blocking `resume` interrupted by a signal stop waiting, change nothing, print nothing and exit 130 (SIGINT, SIGHUP) or 143 (SIGTERM). From the signal to the exit, the worst case is the 60 s launcher wait plus `agent-spawn stop`'s bound (its 10 s grace plus 30 s) plus teardown: queues that run review-run should set jobsctl `kill_grace` to at least 120 s, plus 5 s when `run_pr_code` is configured; a SIGKILL earlier leaves the cancellation for the next finalizer.

### Run settings

At run order step 4 review-run writes two files into the run directory. `settings.json` holds every key of the [Config](#config) table resolved for this run (flags included, each with its origin) except `hooks`, `retention`, `escalation_ttl` and `me`, plus the realpaths of the plugin directories passed, `plugin_dirs_sha256` (the sha256 over their file lists and contents) and `config_sha256`. `permission-profile.json` (mode 0600) is the effective permission profile: the bundled file, its variant without the `Agent` and `Task` denies when `subagents` is on, or a copy of the configured `permission_profile`. Every later step, in whichever process runs it (finalize from `show`, `wait` or `reconcile`, `resume`), reads these two files and never the current config or environment, so a config change or a `REVIEW_RUN_*` variable affects only new runs. `hooks`, `escalation_ttl`, `retention` and `me` come from the current config of the process that acts; the stored per-invocation hooks are added to the configured ones ([Hooks](#hooks)).

## Pre-fetch

review-run reads the PR only through forgectl and `pr-comment draft new`, with `--json`, in this order:

1. `pr-comment draft new <pr> --json`: the skeleton with `draft_id` and `anchor`. pr-comment performs the head-consistent double read and exits 20 (`stale_head`) when the head will not hold still; review-run passes that through as a failed run with reason `stale_head`. A null `anchor.merge_base_sha` is accepted. `draft new` reads the diff a second time; that cost is accepted.
2. `forgectl files <pr> --json` (path, previous path, status, additions, deletions, `files_complete`), `forgectl diff <pr> --json` (the unified diff) and `forgectl ci <pr> --json` (`{pr, head_sha, ci}`), each checked against the anchor head; the PR facts come from step 2 of the run order (`forgectl show`). A mismatch retries the whole pre-fetch once (from step 1); a second mismatch fails the run (`stale_head`). `files_complete: false`, `diff` exit 20 `too_large` (forgectl's all-or-nothing rule) or a total above `limits.hard_ceiling_bytes` fails the run (`too_large`); no partial diff is ever reviewed silently.
3. When `--previous <run>` is given, that run's stored `review.json` is read from review-run's own store and re-verified against its `artifact.sha256`.

The forgectl and pr-comment calls use forgectl's own retries and the gateway timeout ([Finalize recovery](#finalize-recovery)); review-run adds no retry except the one pre-fetch retry above. Credentials are the sibling tools' business; review-run holds none. A failure of any pre-fetch read after the run record exists (including `not_open` or `not_found` from a PR closed meanwhile) is `prefetch_failed` with the sibling's code in `failure.lower_code`. Everything finalize needs later is persisted in the run directory: `settings.json` and `permission-profile.json` at step 4, `manifest.json` and `pr.json` (the four PR facts) by pre-fetch, and `code-run.json` by the code run, so any process can finalize without the deleted prompt.

### Manifest and inputs

The diff is split by its `diff --git` header lines, in forgectl's `files` order. The number of sections must equal the length of `files`, and each section's paths (unquoted as git quotes them, the TAB suffix removed) must equal `files[i].path` and `previous_path`; otherwise the run fails `prefetch_failed` (`lower_code: diff_files_mismatch`). Each section is sanitized before it is measured and split; `bytes` and every cap use the sanitized size. Each file's patch is one input file named `f<NNNN>.patch` (four digits), at most `limits.max_file_bytes` each; a larger file's patch is split at hunk boundaries into `f<NNNN>-<k>.patch` parts numbered from 1 (a single hunk above the limit is cut at a line boundary and marked truncated in the manifest). Parts count toward `max_files`; a file is provided whole or as all its parts, never some of them. Inclusion stops at the first file that does not fit `max_total_bytes` or `max_files`; that file and every later one are not provided and are recorded as skipped (`size_cap` or `file_cap`). Later entries that have no input file (`binary`, `empty`) keep their own status. File ids run from `f0001` in `files` order; more than 9999 changed files fails the run (`too_large`). The caps count the sanitized patch bytes, not the two fence lines of each input file. When no entry is provided and at least one is skipped, the run fails (`too_large`): there is nothing to review.

Status is forgectl's (`added`, `modified`, `deleted`, `renamed`); `additions` and `deletions` may be null; `bytes` is the sanitized section's byte length. An entry is `binary` when its section has headers only and forgectl's `additions` and `deletions` are null (GitHub's `Binary files` line, GitLab's notice), and `empty` when it has headers only and is a pure rename, a mode change or an empty added or deleted file; neither has an input file.

The manifest is a table in the prompt, one row per changed file: file id (`f0001`), path (and `old_path` for a rename), status, additions, deletions, bytes, input file name(s), and one of `provided`, `truncated`, `skipped(size_cap)`, `skipped(file_cap)`, `binary`, `empty`. The prompt shows the rows of provided, truncated, binary and empty entries plus at most 50 skipped ones followed by "N more skipped" (the full list is in `manifest.json` and `meta`). Paths are untrusted and appear only inside the fenced manifest block, rendered with the [sanitizer](#sanitizer)'s marking plus `|` as `<U+007C>` in manifest rows only.

### Prompt files

`prompt.md` (passed with `--prompt-file`) is built from a trusted header and fenced untrusted blocks, with one random nonce per run and per resume prompt (design guide section 12, architecture section 13.2 rule 1; the [sanitizer](#sanitizer) neutralizes `<untrusted` and `</untrusted` inside content):

| Part | Trust | Content |
|---|---|---|
| Header | trusted | run id, the input file list, `nits`, the `limits` in force and the output rules, from the template `plugin/header.md` rendered ([Slot contract](#slot-contract)); never forge text, never the skill's name |
| Guidelines | trusted | the contents of `guidelines_file`, if configured (a file the user owns; never read from the repository) |
| PR facts | untrusted block `source="pr"` | title, body, author login, labels, head and base refs, `draft`, `is_fork`, `is_bot`, `base_is_default`, the CI summary as counts per conclusion plus failing check names, each within the budgets below |
| Manifest | untrusted block `source="manifest"` | the table above |
| Code run | untrusted block `source="code-run"` | the sanitized output of the command, with exit code or signal, duration, truncation ([Code run](#code-run)); or a trusted line saying the step was skipped and why |
| Previous review | trusted line | when `--previous` is given: one line naming the input file `previous-review.txt` |

| Field | Budget |
|---|---|
| title | 1 KiB |
| body | 64 KiB |
| author login, each label, each ref, each check name | 256 bytes |
| labels | at most 50 |
| failing check names | at most 20 |
| each manifest path | 4 KiB |
| code-run output | `max_output_bytes` (capped when captured) |

The nonce is 16 random bytes from the operating system's generator as 32 lowercase hex characters, one per run and a new one for each resume prompt; every fence is `<untrusted source="S" id="<nonce>">` with `S` one of `pr`, `manifest`, `code-run`, `diff`, `previous-review`, `answer`.

Each input file begins with an opening `<untrusted source="diff" id="<nonce>">` line and ends with its closing line, so a model that reads a patch alone still sees the fence. The `previous-review.txt` input is fenced as `source="previous-review"` and holds only the verdict and, per finding, id, path, line, side, severity, title and the first 300 characters of the body, at most 64 KiB (findings beyond it are dropped with a count line); `meta.summary` and `risk_reasons` are never included. Every untrusted field goes through the [sanitizer](#sanitizer). The prompt file and input files live under review-run's run directory and are deleted when `agent-spawn launch` (or `resume`) returns, since agent-spawn copies them at classification, or on any terminal transition, whichever comes first; the prompt's size is bounded by the caps above, and the diff is never inlined.

## Code run

An optional step that runs a user-configured command on the PR's code and hands only its sanitized output to the session. The session never has a checkout (architecture section 13.2 rule 9). It runs only when `repos.<id>.run_pr_code` is set in the user's own config ([Config](#config)); review-run reads no configuration from the repository under review.

1. **Throwaway.** `wtctl create --throwaway --repo <id> --ref <anchor head sha> --source auto --label owner=review-run --label run=<run id> --actor <actor> [--decision-id ...] --json`. The throwaway id is stored on the run as soon as `wtctl create` returns. A failure is `skipped` with a closed reason ([below](#code-run)): `ref_unavailable` (`ref_not_found`, expected for many fork PRs whose head is not fetchable from the base repository) and `throwaway_leaks` (5 or more leaked throwaways are recorded; see Removal) are defined here, the others after step 6.
2. **Sandbox check.** The command runs under bubblewrap (`sandbox: required`, the default). If `bwrap` is unusable (missing, user namespaces disabled, `--unshare-user --disable-userns` unsupported, blocked by the distribution's policy) the step is `skipped(sandbox_unavailable)`; a `bwrap` that started but failed before the command ran is `skipped(sandbox_failed)`; with `sandbox: preferred` an unavailable sandbox runs the command unsandboxed with the run warning `sandbox_lowered` and a log record; `off` runs it unsandboxed without one. `off` and `preferred` are honored only because all config is the user's own. On macOS the step is `skipped(sandbox_unsupported)` unless `sandbox` is `preferred` or `off`.
3. **Command.** `run_pr_code.command` is an argv list, never a shell string. Under `required`, it runs in a sandbox with: no network (`--unshare-net`, always in v1), a new pid, ipc, uts and user namespace (`--unshare-user --disable-userns`, so the command cannot create nested user namespaces), `--die-with-parent`, `--new-session`, all capabilities dropped, `--proc /proc` and `--dev /dev`, read-only binds (`--ro-bind-try`) of exactly `/usr/bin`, `/usr/lib`, `/usr/lib64`, `/usr/libexec`, `/usr/share`, `/usr/include`, `/usr/local`, `/bin`, `/sbin`, `/lib`, `/lib64`, `/etc/ld.so.cache`, `/etc/ssl`, `/etc/ca-certificates`, `/etc/alternatives`, `/etc/passwd` and `/etc/group` (never all of `/usr`), then `--tmpfs /usr/lib/wsl` when that directory exists (WSL's Windows driver store), and of every path in `ro_binds`, the throwaway bound read-write at its own path and used as cwd, an empty tmpfs `HOME` and `/tmp` of 64 MiB each (`--size`), and nothing else of the host (no home, no `~/.config`, no state directories, no tmux or SSH agent sockets). The environment is built from scratch: `PATH`, `LANG`, `TERM=dumb`, `HOME` (the empty one), the names in `env_allow`, `GH_CONFIG_DIR` and `GLAB_CONFIG_DIR` set to empty directories; no variable from review-run's own environment otherwise, no `GIT_*`.
4. **Limits.** stdin is `/dev/null`; stdout and stderr go to one file that review-run opened before the child started, in its run directory outside the throwaway; a monitor polls at least once a second and stops the command at `max_output_bytes`, or when the throwaway's file system has lost more than `max_workspace_bytes` of free space or more than 1,000,000 free inodes since the start; a wall-clock `timeout` ends it. Inside the sandbox `RLIMIT_FSIZE` is 256 MiB (a per-file overshoot bound, not the output cap), `RLIMIT_NOFILE` 1024, `RLIMIT_NPROC` 2000 (set from inside the sandbox's user namespace, never on the host process) and `RLIMIT_CORE` 0. On timeout review-run sends SIGTERM and, 5 s later, SIGKILL to the sandbox's outer process, which ends every process in its pid namespace. Without a sandbox, review-run starts the command in a new session and process group as a child of a subreaper it set; on timeout or cleanup it signals only that process group and the descendants it reaped or can verify by PID and start time as its own children. A `/proc` scan of working directories and open files only decides whether removal may proceed; nothing it finds is signaled. A seccomp filter is roadmap work.
5. **Output.** review-run reads the output only through the file descriptor it opened (never by path) and applies the [sanitizer](#sanitizer). Before the output enters the prompt, every value of review-run's own environment that is at least 8 characters long is replaced by `<redacted>` in it; the redaction is a safety net for accidents, and a transformed value passes it. The result (exit code or signal, `timed_out`, `truncated`, byte count, duration, sandbox mode, sha256 of the sanitized output) is stored in the run directory (`code-run.json`, `code-run.txt`) and summarized in `meta.review_run.code_run`.
6. **Removal.** `wtctl remove <throwaway id> --actor <actor> --json` after the sandbox process tree is gone, in a `finally` path. A removal that exits 20 (`in_use`), 1 (`partial`) or 12 (`busy`) is retried once after a short wait; anything else, and a second failure, is logged as a warning and the throwaway is recorded as leaked; `reconcile` retries the removal of recorded leaked throwaways and reports the count ([reconcile](#reconcile)). With 5 or more leaked throwaways recorded, the code run is skipped (`throwaway_leaks`). Removal failure never fails the run.

A non-zero exit, a signal, a timeout or truncation is not a run failure: the output reaches the session as fenced text and is summarized in `meta.review_run.code_run` (`status` is `ok`, `failed` for a non-zero exit, a signal or a timeout, `skipped` or `not_configured`; `timed_out` and `truncated` are booleans; `signal` is `SIG<NAME>`; `sandbox` is null when nothing ran). A step that cannot run is `skipped` with a closed reason: `wtctl_missing` (wtctl not on `PATH`; checked at run order step 2 only when the repo configures `run_pr_code`), `throwaway_refused` (wtctl exit 11 or 13), `ref_unavailable`, `throwaway_failed` (any other wtctl failure), `sandbox_unavailable`, `sandbox_unsupported`, `sandbox_failed` (all three in step 2), `throwaway_leaks`. The session is told plainly in the prompt that no test output exists. The skip is always recorded in `meta.review_run.code_run`, and an attempted code run writes an audit `code_run` record ([Audit and log](#audit-and-log)).

## Launch

`agent-spawn launch` is called with exactly these arguments (flags in this order do not matter):

| Flag | Value |
|---|---|
| `--mode` | `headless` or `interactive` ([`run`](#run)) |
| `--kind` | `review` |
| `--no-implicit-done` | always (a per-call claude-session override passed through agent-spawn; an unreported clean exit becomes exit 14 and agent-spawn's single automatic report-resume recovers it); requires claude-session and agent-spawn versions that support it; older versions are refused at run order step 1 (`tool_too_old`) |
| `--cwd` | `session-dir` |
| `--repo` | the repo id |
| `--skill` | the configured skill |
| `--plugin-dir` | the bundled plugin directory and, when the skill comes from another plugin, that plugin's directory only |
| `--prompt-file`, `--input-file` (repeatable) | the prompt and the per-file patches, the previous-review input |
| `--permission-profile` | `<run dir>/permission-profile.json` ([Run settings](#run-settings)) |
| `--sandbox` | `required` (or the configured value) |
| `--setting-sources` | `""` (or the configured value) |
| `--env-allowlist-only` | each name in `env_allowlist` (an empty list is passed as `--env-allowlist-only ""`); when `subagents` is on, also `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH`, whose value `max_subagent_depth` review-run sets in the launch's own environment |
| `--model`, `--timeout`, `--max-turns` | only when configured or given |
| `--no-promote` | always |
| `--label` | `owner=review-run`, `run=<run id>`, `pr=<pr>`, `repo=<repo>`, plus the caller's labels |
| `--result-file` | `<run dir>/launch.json` (its path follows from the run id; outside the session directory and cwd, as agent-spawn requires) |
| `--wait` | never; review-run stores the agent from the launch result and then runs `agent-spawn wait <agent_id> --result-file <run dir>/wait.json --json` |
| `--json`, `--actor`, `--decision-id` | as given |

Every value comes from the run's `settings.json` ([Run settings](#run-settings)).

`--ephemeral` is never used: it deletes the session directory, and with it the frozen artifact, before review-run has copied it. `--worktree` is `none` (the default). `run` launches without agent-spawn's `--wait`. agent-spawn exits 0 with the result object once the run is started; review-run stores `agent_id`, `csession_id` and `awaited_run` (the result's `agent.run`; its `run` is null for a started run, and a later automatic report-resume run has a higher number) in one transaction, then blocks in `agent-spawn wait <agent_id> --result-file <run dir>/wait.json --json` (or, with `run --no-wait`, returns the run id). If the launch result already carries a final run, finalize starts at once. Just before the call the row stores the launcher's PID and start time and `launch_started_at`. If the runner dies while `launch` is in flight, `reconcile`, `show` and `wait` take the agent id from `launch.json` ([reconcile](#reconcile)). `launch.json`, `wait.json` and `resume.json` are read like any sibling output: opened without following links, at most 1 MiB, strict JSON validated against agent-spawn's pinned schemas ([step 6](#finalize)'s rules). In `launch.json` a result with an agent is adopted; an error envelope, or a result whose run ended in its launch phase, ends the run `failed` (`launch_failed`) after tearing down the parked agent it names (ownership-checked, [Labels](#labels)); a file that does not parse or validate counts as absent. agent-spawn's exit 22 cannot occur (review-run never passes a wait timeout); it maps to `internal`.

One table maps agent-spawn's `launch`, `resume` and `wait` results to the run:

| agent-spawn result | Run |
|---|---|
| 0, phase `session`, `done` | finalize |
| 13 with phase `session` (`needs-input`, `blocked`) | `escalated` (`needs_input`, `blocked`) |
| 14, 15, 16, 17, 18 | `failed` (`unreported`, `killed`, `timeout`, `lost`, `session_failed`) with the same exit |
| an error envelope, or a result with phase `launch` (exits 2, 10, 11, 12, 13, 20, 21 at launch) | `failed` (`launch_failed`, exit 12); `failure.tool`, `failure.lower_code` and `failure.lower_reason` from the envelope or outcome (`vetoed`, `escalated`, `tool_missing`, `no_tmux` from termctl's `tmux_missing`, `tmux_too_old`, `server_unavailable` or `not_supported`, `third_party_cwd`, ...) |
| `resume` without a started run of this resume | the run stays `escalated` ([Resume](#resume)) |
| `wait` or `show` exit 10 | `failed` (`agent_gone`, exit 12) |
| `wait` or `show`, any other envelope or an output that does not parse | the run is unchanged; the runner retries `wait` 3 times, 2 s apart, then exits 12 with the envelope (`code: failed`, `reason: lower_failed`, `tool: agent-spawn`, `lower_code`), and `show`, `wait` or `reconcile` finalizes later |
| `launch` exit 1, or a `launch` output that does not parse | `failed` (`internal`) |
| 130 or 143 from the launcher | `failed` (`canceled`) |

The bundled permission profile (`plugin`-adjacent file `permission_profile.json`) is a settings fragment with `permissions.allow: ["Skill"]` and `permissions.deny` of `Agent`, `Task`, `WebFetch`, `WebSearch`, `mcp__*`, `SendMessage`, `ListAgents`, `TaskStop`, `Workflow`, `ReportFindings`, `CronCreate`, `CronDelete`, `CronList`, `ScheduleWakeup`, `EnterWorktree`, `ExitWorktree`, `RemoteTrigger`, `PushNotification`, `PublishPlugin`, `ShareOnboardingGuide`, `DesignSync`, `Monitor`, `Bash(gh:*)`, `Bash(glab:*)`, `Bash(agent-spawn:*)` and `Bash(memoctl add:*)`, plus `sandbox.network.allowedDomains: []`; claude-session generates the remaining Bash, Read, Write and path-scoped Edit rules, so `Edit`, `Write` and `NotebookEdit` are not denied by bare name (only `Edit(...)` rules govern the Write tool, so a bare deny could stop the session from writing its artifact; whether `NotebookEdit` can be denied without affecting Write is to verify in the live test, and it is added only if so). Leaving a tool out of `allow` does not remove it; only a deny entry does. A `live` test, run before each release, compares the init `tools` list of a run under the bundled profile with the pinned allowed set (`Skill`, `Read`, `Write`, `Edit`, `Glob`, `Grep`, `Bash`, `ToolSearch`, `TodoWrite`, plus `Task` when `subagents` is on) and fails on any other tool; every headless run makes the same comparison after the fact ([Finalize](#finalize) step 5). The same test confirms that denying an absent tool name is accepted and that deferred tools (reached through `ToolSearch`) appear in the init list. With `subagents: true` the effective profile is the bundled one without the `Agent` and `Task` denies; a configured `permission_profile` is copied unchanged, and with `subagents: true` review-run warns that it does not edit a user's profile ([Run settings](#run-settings)).

A launch failure follows the table above and tears the parked agent down if one was created. agent-spawn's `lower_error` text is untrusted and never copied into review-run's output beyond its closed `code` and `reason`.

## Finalize

Finalize runs in the process that waited for the session (`run`, `resume`) or, when that process is gone, in `show`, `wait` or `reconcile`. Those read `agent-spawn show <agent_id> --json` and finalize only when the agent is not live, no supervisor is alive and its last run's number is at least the run's `awaited_run` and greater than its `escalated_run` and `finalized_run`; the outcome is that run's outcome object. Otherwise nothing happens: an escalated agent that was not resumed stays escalated, and `post-escalate` fires once per `escalated_run`. The move into phase `finalize` is the compare-and-set of [Runs, states and phases](#runs-states-and-phases); when it fails because a cancellation is recorded, the finalizer ends the run `canceled` instead of running the steps. The compare-and-set key is `(state, phase, awaited_run)`; the stored hooks, actor and decision ids are used ([Hooks](#hooks)). Steps, in order:

1. **Classify the outcome** (the table in [Launch](#launch)): `0` `done` continues; `13` becomes `escalated` (reason `needs_input` or `blocked`, from the outcome's report status) and finalizing stops, keeping the agent; `14` to `18` and the other rows end the run `failed` with the matching reason ([Reasons](#reasons)).
2. **Reject implicit done:** `report.implicit: true` fails the run (`implicit_done`).
3. **Locate the artifact:** `report.artifact` must be present and named `review.json`; otherwise `artifact_missing`.
4. **Copy and verify:** copy the frozen artifact named in the outcome object's `report.artifact` into the run directory as `original.json`, opened without following links and read to at most the reported size plus one byte, compared with the reported size and sha256 (`artifact_mismatch`, "the frozen artifact differed from its reported size or hash", on any difference). All later steps use this copy.
5. **Skill check:** `claude-session status <csession_id> --run <awaited_run> --json` gives the run's `mode` and `result_path`. Headless: review-run opens `result_path` without following links and reads it line by line, at most 1 MiB in all, parsing each line strictly (step 6's rules) until the first object with `type: "system"` and `subtype: "init"`; nothing after it is read. That object's `skills` list must contain the configured `<plugin>:<skill>` exactly. A missing or unreadable file, no init object within the bound, or a skill not listed is `skill_missing` (claude-session exits 0 `done` for a missing skill; the automatic report-resume run is not consulted). Interactive (`result_path` null): the check is not performed; the run gets the warning `skill_unchecked` and `meta.review_run.skill_checked: false`. The check proves the skill could load, not that it was followed. With the bundled profile or its variant, the same init object's `tools` list is compared with the pinned set (`Skill`, `Read`, `Write`, `Edit`, `Glob`, `Grep`, `Bash`, `ToolSearch`, `TodoWrite`, plus `Task` when `subagents` is on); any other tool gives the run warning `tools_unexpected` and its count in `meta.review_run.session.tools_unexpected` (never the names). With a configured profile or in interactive mode the comparison is not made (warning `tools_unchecked`, `session.tools_checked: false`).
6. **Parse:** strict JSON, one object, decoded from the bytes as UTF-8 without a BOM (any other encoding is `artifact_invalid`), at most `limits.max_artifact_bytes` (default 8 MiB). A pre-scan without recursion refuses nesting deeper than 32; decoding refuses duplicate keys, `NaN`, `Infinity` and other non-finite numbers (including a float literal that overflows), lone surrogates and anything after the one object; a parser error of any kind (including Python's integer-digit limit and recursion) is `artifact_invalid`, never `internal`.
7. **Session claims:** extract and validate the session's `meta` ([Session claims](#session-claims)); a violation is `artifact_invalid`.
8. **Identity:** set `schema_version`, `draft_id`, `pr` and `anchor` on the copy to the values from `pr-comment draft new` (whatever the session wrote there is replaced).
9. **Normalize** the draft without `meta` ([Normalization](#normalization)).
10. **Validate:** `pr-comment draft check - --json` on the normalized draft without `meta` (pr-comment ignores `meta`); exit 0 required; exit 2 (`valid: false`) is `artifact_invalid` with the first 20 `errors[]` items as `{pointer, kind}` (pointer cut to 200 characters, `kind` `schema` when pr-comment's is null, never the message) in `failure.errors`; any other exit, or output that does not parse, is `internal` with `failure.tool: pr-comment` and its `lower_code`. Positions are not checked here: pr-comment checks paths and lines at post time and moves invalid findings to the body (`invalid_position`).
11. **End head:** `forgectl show <pr> --json` once more; store `head_at_end` and `head_moved` (a failed read stores `head_moved: null` and the warning `head_unreadable`; the run stays `done`).
12. **Derive meta** ([Meta](#meta)) and set it.
13. **Write `review.json`:** serialized with ASCII escapes, written to a temporary file (mode 0600, `O_EXCL|O_NOFOLLOW`), fsynced and renamed, before the terminal transaction.
14. **Terminal transaction:** state, the audit records of the transition and the hook outbox entries (`event_id`) in one transaction. Then **teardown:** `agent-spawn teardown <agent_id>` (never `--force`), recorded in `teardown` and retried by `reconcile`; a refusal or failure is a warning on the run, and the run is `done` regardless.

Before the first `stop`, `teardown` or `resume` on an agent id, a process checks the agent's ownership ([Labels](#labels)). A run that ends `failed` keeps `original.json` when one was copied and tears its agent down unless the failure is a live agent; an `escalated` run keeps its agent until it is resolved by `resume`, `cancel` or `escalation_ttl`.

### Finalize recovery

Steps 1 to 13 are idempotent and repeatable from the run directory's files and the frozen copy (teardown follows the terminal transaction, so the agent is still there), so a crashed finalizer is re-run from step 1. The terminal transaction sets the state and enqueues the post hook and the audit records in the outbox ([Hooks](#hooks), Delivery). Teardown runs after the terminal transaction. Every sibling call has a timeout: 300 s for `pr-comment draft check`, `agent-spawn stop` and `agent-spawn teardown`, 60 s for the others; `agent-spawn launch`, `resume` and `wait` are bounded by agent-spawn. A call that times out is ended (its process group) and fails with `lower_code: timeout`. The per-run lease lasts 5 minutes and is renewed every 60 s by its holder; a finalizer that finds the lease held by a live holder does not wait (`show` prints the current row, `wait` keeps polling, `reconcile` skips the run). The blocking `run` that loses the compare-and-set race prints the stored run object and exits with its stored `exit_code`. An unexpected exception in finalize ends the run `failed` (`internal`) once, never a crash loop.

### Session claims

A closed table defines the session-side `meta`; every field is validated before anything is derived, and a violation ends the run `failed` (`artifact_invalid`) with error kinds and pointers only ([`schemas/session-meta.json`](../schemas/session-meta.json)).

| Field | Rule |
|---|---|
| `meta` | an object; missing: `meta_missing` |
| `meta.verdict` | `approve`, `comment` or `request_changes` |
| `meta.risk` | `low`, `medium` or `high` |
| `meta.risk_reasons` | array of 0 to 10 strings, each 1 to 200 characters, no character of categories Cc, Cf, Cs, Zl or Zp and none of the extra characters the sanitizer marks |
| `meta.summary` | string of 1 to 2000 characters, no character of categories Cc (except LF), Cf, Cs, Zl or Zp and none of the extra characters the sanitizer marks (`Co` and `Cn` are allowed: they are marked when printed) |
| `meta.coverage.reviewed` | array of at most `limits.max_files` file ids matching `^f[0-9]{4}$`, no duplicates |

review-run's code applies the category test; [`schemas/session-meta.json`](../schemas/session-meta.json) expresses only the control characters it can.

Any other key under `meta` is ignored, and a missing field is `meta_missing` with that field's pointer. A violation is `artifact_invalid` with `failure.errors[]` entries `{pointer, kind}` (kinds `meta_missing`, `type`, `enum`, `length`, `count`, `pattern`), never the value. `event` decides how the draft is posted; `verdict` is stored as the session gave it, even when it differs from `event` after `event_downgrade`. `summary` and `risk_reasons` stay untrusted text and never reach a hook, audit record or error message.

### Normalization

Normalization applies a closed list of lossless fixes to the copy, in this order; each applied fix is listed once in `meta.review_run.normalized` as `{rule, pointer, count}`. Anything not in the list is never changed.

| Rule | Fix |
|---|---|
| `drop_unknown` | remove properties the review-draft schema does not define, in every object except `meta` (findings, `suggestion` and any other closed object) |
| `drop_null` | remove optional finding fields (`title`, `severity`, `start_line`, `side`, `suggestion`) whose value is `null`; `line: null` (a file-level finding) is kept |
| `drop_unanchored` | remove a session-written `unanchored` array (only pr-comment writes it) |
| `event_downgrade` | an `event` of `request_changes` with no `blocker` or `major` finding, and any `approve`, become `comment` (the reverse is never applied; `approve` is never a draft event in pr-comment) |

Each entry records the parent object's JSON pointer and a count, never a dropped key name; at most 200 entries are kept, then one `{rule: "truncated", pointer: "", count: N}`. Findings are never added, removed, reordered or re-worded. Failures with no lossless fix are accepted losses and end `artifact_invalid`; there is no repair: a title over 256 characters, a `suggestion` on the left side, `start_line >= line`, duplicate finding ids, an empty finding body, more than 500 findings. pr-comment already strips titles and treats an empty one as absent, so no title rule exists here.

### Meta

The draft's `meta` object holds exactly one key, `review_run`, always rebuilt by review-run; whatever `meta` the session wrote is replaced after its claims are validated.

| Field | Source |
|---|---|
| `version` | review-run (1) |
| `run_id`, `skill`, `skill_version`, `mode` | review-run (`skill_version` is the bundled plugin's version for the default skill, else `null`, at most 64 characters) |
| `skill_checked` | review-run: `true` when the skill check ran (headless), `false` when it did not (interactive) |
| `session` | review-run: `{sandbox: required\|preferred\|off, profile: bundled\|variant\|custom, profile_sha256, tools_checked, tools_unexpected}` from `settings.json`, `permission-profile.json` and finalize step 5 (`tools_unexpected` is a count, 0 when the tools were not checked) |
| `verdict` | the session's `approve`, `comment` or `request_changes`; stored as given, never used by review-run to act |
| `risk`, `risk_reasons` | the session's `low`, `medium` or `high` and up to 10 short strings (each 1 to 200 characters, one line); untrusted text |
| `summary` | the session's summary, 1 to 2000 characters; untrusted text |
| `severity_counts` | review-run, counted from the findings: `blocker`, `major`, `minor`, `nit`, `info` and `unspecified` (findings without a severity) |
| `coverage` | review-run: `{status: full\|partial, reviewed: [file ids], skipped: [{file, reason}], ignored_ids}`; `reviewed` is the session's ids intersected with the provided entries; a provided entry not in it is `skipped` with `not_reviewed`; a `truncated` entry is always `skipped` (`truncated`) even if the session lists it; `binary` and `empty` entries are in neither list; ids the session lists that were not provided are ignored and counted in `ignored_ids`; a skipped entry's other reasons are `size_cap` and `file_cap`; `partial` when any entry is skipped; a finding on a file not in `reviewed` changes nothing |
| `pr` | review-run: `{draft, is_bot, is_fork, base_is_default}` from forgectl, each possibly `null` |
| `code_run` | review-run ([Code run](#code-run)): `{status: ok\|failed\|skipped\|not_configured, reason, exit_code, signal, timed_out, truncated, bytes, duration_ms, sandbox, output_sha256}` |
| `head_at_end`, `head_moved` | review-run (step 11) |
| `normalized` | review-run ([Normalization](#normalization)) |
| `previous_run` | review-run: the `--previous` run id or `null` |

A consumer treats `coverage.status: partial`, `code_run.status: skipped` (when it expected output), `head_moved: true`, `session.sandbox` other than `required`, `session.tools_checked: false` and a non-zero `session.tools_unexpected` as reasons not to act automatically. `meta` is untrusted text downstream. The model's confidence is not collected.

## Resume

`resume <run> --prompt-file <path>|-` finishes an `escalated` run; the prompt file (the answer) is required, otherwise exit 2. In order: (1) take the run's lease and refresh it as `show` does; a run that is not `escalated` afterwards (also one whose agent already finished a newer run, which is finalized now) exits 20 (`not_escalated`, `details.state`). (2) Check the agent ([Labels](#labels), ownership check); a torn-down, unknown or foreign agent ends the run `failed` (`agent_gone`), and `resume` prints the run object and exits 12. (3) A live agent exits 20 (`live`). (4) Build the resume prompt: a trusted preface ("This continues your review of run <id>; your earlier inputs are in `inputs/1/` of your session; the reply follows; the output rules are unchanged") and the reply inside `<untrusted source="answer" id="<new nonce>">`, sanitized ([Sanitizer](#sanitizer)); a reply above `limits.max_answer_bytes` (64 KiB) exits 2. (5) Call `agent-spawn resume <agent_id> --kind review --no-implicit-done --plugin-dir <dirs> --permission-profile <run dir>/permission-profile.json --sandbox ... --setting-sources ... --env-allowlist-only ... [--model] [--timeout] [--max-turns] --no-promote --prompt-file <built> --result-file <run dir>/resume.json --json --actor ... --decision-id ...`, every value from `settings.json` ([Run settings](#run-settings)), with the depth variable in its environment when `subagents` is on. `--mode` and `--placement` are not passed (agent-spawn resumes in the mode of the agent's last run, placement `window` when interactive); `--skill` is not passed (the plugin directories are re-passed, so the skill check of the resumed run still applies); `--allow-tainted` is never passed (a `review` session resumed as `review` is not refused `tainted`). (6) When agent-spawn returns a started run, one transaction moves the row to `running` (phase `session`), sets `awaited_run` to the result's `agent.run` and clears `reason`, `exit_code` and `escalated_at` (`escalated_run` is kept; a later escalation sets new values); then `resume` waits and finalizes as `run` does and exits with the same outcome codes (`--no-wait` returns here). Because a resumed session keeps its working directory and carries its conversation, the artifact must be attached again; the skill's contract says so ([Slot contract](#slot-contract)).

Whenever agent-spawn's `resume` returns without a started run of this resume (an error envelope, or a result whose run ended in its launch phase; agent-spawn's Resume failures rows), the run stays `escalated`, nothing fires, the agent is kept, and `resume` exits with agent-spawn's exit code and the error envelope: exit 20 is `code: refused`, `reason: lower_refused`, with agent-spawn's reason (`live`, `tainted`, `tearing_down`, `third_party_cwd`, `third_party_run`, ...) in `lower_code`; 11 and 13 are `code: vetoed` and `code: escalated` with `hook: null` and `tool` naming the lower tool; 10 is handled as step 2; 12, 1 and anything else are `code: failed`, `reason: lower_failed`, with `tool`, `lower_code` and `details.retryable` copied. A run that ended in its launch phase is recorded as the run's `escalated_run`, so no finalizer takes it as an outcome. For `resume`, a run object means the run changed and an envelope means it did not; an exit 13 envelope is a lower tool's escalation, not a session escalation. There is no `pre-resume` hook in v1: claude-session's `pre-launch` hooks gate every resume. `resume --hook` accepts `post-run` and `post-escalate`. The session's question is untrusted and is not printed by review-run; a human reads it with `agent-spawn show <agent_id>` or `claude-session status <csession_id>`, or answers interactively with `agent-spawn resume <agent_id> --mode interactive`, after which `show`, `wait` or `reconcile` finalizes the run ([Finalize](#finalize)).

## Reasons

One table gives, for every reason, the run's state, the exit code, what `--json` prints and whether a retry can help. A failure while a run exists is a run object (`state: failed`; exit 12 for `internal`); the error envelope (`{error: {code, message, reason?, details?, tool?, lower_code?, hook?, decision_ids?}}`, [`schemas/cli/error.json`](../schemas/cli/error.json)) is for failures with no run and for read commands. `code` is one of `usage`, `config_invalid`, `not_found`, `ambiguous`, `vetoed`, `escalated`, `refused`, `failed`, `internal`. The run object's `failure` ([Run object](#run-object)) carries the sibling's closed codes and the `retryable` flag.

| Reason | State | Exit | `--json` stdout | Retryable |
|---|---|---|---|---|
| `prefetch_failed` | `failed` | 12 | run object; forgectl's or pr-comment's code is in `failure.lower_code` | when `lower_code` is `rate_limited` or `network_error` |
| `stale_head` | `failed` | 12 | run object | yes |
| `too_large` | `failed` | 12 | run object | no |
| `launch_failed` | `failed` | 12 | run object | when the lower `details.retryable` is true |
| `canceled` | `failed` | 143 (SIGTERM, `cancel`) or 130 (SIGINT, SIGHUP) | run object | no |
| `session_failed` | `failed` | 18 | run object | no |
| `unreported` | `failed` | 14 | run object | no |
| `killed` | `failed` | 15 | run object | no |
| `timeout` | `failed` | 16 | run object | no |
| `lost` | `failed` | 17 | run object | no |
| `runner_lost` | `failed` | 12 | run object | yes |
| `agent_gone` | `failed` | 12 | run object | no |
| `expired` | `failed` | 12 | run object | no |
| `implicit_done` | `failed` | 22 | run object | no |
| `artifact_missing` | `failed` | 22 | run object | no |
| `artifact_mismatch` | `failed` | 22 | run object | no |
| `skill_missing` | `failed` | 22 | run object | no |
| `artifact_invalid` | `failed` | 22 | run object (`failure.errors`) | no |
| `internal` | `failed` | 12 | run object | no |
| `needs_input` | `escalated` | 13 | run object | n/a |
| `blocked` | `escalated` | 13 | run object | n/a |
| `not_open`, `unknown_host` | none | 20 | envelope (`code: refused`) | no |
| `live`, `not_escalated` | none | 20 | envelope (`code: refused`) | no |
| `lower_refused` | none | 20 | envelope (`code: refused`, `lower_code`, `tool`) | no |
| `tool_missing`, `tool_too_old`, `no_tmux`, `schema_newer`, `state_unsafe` | none | 12 | envelope (`code: failed`) | no |
| `lower_failed` | none | 12 | envelope (`code: failed`, `tool`, `lower_code`); the run, if any, is unchanged | when the lower `details.retryable` is true |
| `wait_timeout` | none | 12 | envelope (`code: failed`); the run is unchanged | yes |
| `previous_mismatch`, `previous_not_done`, `skill_not_found`, `label_reserved`, `too_many_labels`, `pr_too_long` | none | 2 | envelope (`code: usage`) | no |
| (no reason) `usage` | none | 2 | envelope (`code: usage`): bad flags, a short prefix, an answer over the cap, `--override-escalation` without a human actor | no |
| (no reason) `config_invalid` | none | 2 | envelope (`code: config_invalid`, `details.key`) | no |
| (no reason) `not_found` | none | 10 | envelope (`code: not_found`): a run, a draft, a resolve-step `not_found` | no |
| (no reason) `ambiguous` | none | 21 | envelope (`code: ambiguous`, the matching ids) | no |
| (no reason) `vetoed` | none | 11 | envelope (`code: vetoed`, `hook` or `tool`, `decision_ids`) | no |
| (no reason) `escalated` | none | 13 | envelope (`code: escalated`, `hook` or `tool`, `decision_ids`) | n/a |
| (no reason) `internal` | none | 1 | envelope (`code: internal`) | no |

Meanings of the run reasons: `prefetch_failed` a forgectl or pr-comment read failed; `stale_head` the head moved during pre-fetch and a retry did not settle it; `too_large` forgectl could not produce the whole diff, `files_complete` was false, the PR exceeds `limits.hard_ceiling_bytes`, or no file fits the caps; `launch_failed` agent-spawn did not start the session; `canceled` a signal ([Signals](#signals)) or [`cancel`](#cancel); `session_failed` the session reported `failed`; `unreported` the session ended without a report even after agent-spawn's automatic report-resume; `killed`, `timeout`, `lost` the session was killed, timed out or was lost (agent-spawn's lost session); `runner_lost` the runner and the launcher (PID plus start time) are both gone and no agent exists or can be recovered; `agent_gone` the stored agent was torn down or is unknown to agent-spawn; `expired` an escalated run older than `escalation_ttl`; `implicit_done` the outcome was an implicit done; `artifact_missing` no `review.json` was attached; `artifact_mismatch` the frozen artifact differed from its reported size or hash; `skill_missing` the configured skill never loaded; `artifact_invalid` not valid JSON, too large, a session claim out of its table, or rejected by `pr-comment draft check` after normalization; `internal` an unexpected error. Rules the table encodes: exit 12 without a run (`tool_missing`, `tool_too_old`, `no_tmux`, `wait_timeout`, `schema_newer`, `state_unsafe`, `lower_failed`) prints the envelope; `stale_head` and `too_large` deviate from the design guide's exit-20 list on purpose, since the run record exists and the run therefore ends `failed`; `previous_mismatch` and the other exit-2 reasons are usage errors found before any run exists.

## Commands

### `run`

`review-run run <pr> [--skill <plugin>:<skill>] [--mode headless|interactive] [--label k=v]... [--previous <run>] [--no-wait] [--model <m>] [--timeout <dur>] [--hook NAME=CMD]... [--override-escalation] [--actor A] [--decision-id ID]... [--dry-run] [--json]`

Runs the order of [Run order](#run-order) and blocks until the run is `done`, `escalated` or `failed`. `--mode` defaults to the config `mode` (`headless`); `interactive` passes `--mode interactive --placement window` to agent-spawn, needs tmux (checked by the step-2 preflight: exit 12, `no_tmux`) and still waits and validates afterwards; an interactive window closed without a report ends the run `failed` (`unreported`). `--skill` is `<plugin>:<skill>`, from the bundled plugin or a `plugin_dirs` entry. `--previous` names a `done` run of the same PR whose stored review is passed as one extra fenced input (a different head is allowed). `--override-escalation` (human-only: a TTY on stdin or `--actor human:<user>`, else exit 2; audited) treats exit 13 of the `pre-run` hooks as allow; a veto still vetoes. `--no-wait` returns after the store of the agent (run order step 7) with the run id on stdout (or the run object with `--json`) and exit 0. A pre-run escalation held in a jobsctl job is continued by a human running `run ... --override-escalation` with the original labels (no run record exists for the escalated attempt; only its audit record names the reserved id); the job is resolved separately.

`--dry-run` performs the pre-fetch reads and agent-spawn's and claude-session's dry runs, writes the prompt and inputs into a private directory under the state root (`tmp/`, 0700) that it removes in a `finally` path (`reconcile` removes leftovers), allocates no run id, writes no audit record, runs no hook and no code run, launches nothing, and prints the plan ([`schemas/cli/run-dry-run.json`](../schemas/cli/run-dry-run.json)). Steps 1 and 2 of the run order run as for `run`, so a failure there prints the error envelope with that step's exit code; past them the dry run exits 0 and prints `would_exit`. The plan is `{dry_run, settings: [{key, value, origin}], launch_argv, plugin_dirs, tools: [{name, path, version}], manifest, coverage_preview, sizes, code_run_plan (including the sandbox probe result), would_fail: [reason], would_exit, warnings}`. `settings` lists every key `settings.json` would hold ([Run settings](#run-settings)), with `origin` one of `flag`, `repo`, `env`, `config`, `default`; `would_fail` lists every reason it would refuse or fail with (`tool_too_old` conditions included) and never includes a hook outcome (hooks do not run). This gives one PR's readiness facts: sibling versions, the bwrap probe result, the resolved skill and plugin directories. There is no `doctor` command in v1 (readiness belongs to `awkit doctor`); without a PR, `awkit doctor` learns config validity from any command (exit 2, `config_invalid` naming the key) and the cron block from `cron install --dry-run --json`. Without `--dry-run` the exit code is the run's outcome as listed in [Reasons](#reasons).

### `resume`

`review-run resume <run> --prompt-file <path>|- [--no-wait] [--hook NAME=CMD]... [--actor A] [--decision-id ID]... [--json]`. See [Resume](#resume). `--no-wait` is accepted and returns once the resumed session has started (agent-spawn's wait is omitted).

### `wait`

`review-run wait <run> [--timeout <dur>] [--json]` polls every 2 s until the run is terminal or `escalated` (also waiting on a run in `prefetch` or `code_run`; finalizing it if its agent has ended, and applying the runner check of [reconcile](#reconcile) to this run) and exits with the run's exit code; a wait timeout exits 12 with `reason: wait_timeout` and the run unchanged (exit 22 keeps its single meaning). An `escalated` run is reported at once, also while a human answers it through agent-spawn; it is finalized once that session ends. A run that ended `canceled` exits with its stored code (143 or 130) and prints the run object; interrupted by a signal, `wait` prints nothing, exits 130 or 143 and changes nothing.

### `list`

`review-run list [--state S]... [--pr <pr>] [--repo <id>] [--label k[=v]]... [--head <sha>] [--since <time>] [--limit N] [--json]`. Output `{runs, truncated}`; default `--limit 100`; newest by `created_at`, then run id. `--since` takes RFC 3339 or a duration and matches `created_at`; `--label k=v` matches that exact pair, `--label k` any value of key `k`; `--head` matches `head_sha` or `anchor.head_sha`; filters AND. Does not refresh runs.

### `show`

`review-run show <run> [--no-refresh] [--draft] [--json]` prints the run object ([Run object](#run-object)), with exit 0 whenever it printed (the run's outcome is its `exit_code`); an unknown run exits 10. Unless `--no-refresh`, a `running` run with an agent, or an `escalated` run, is refreshed from agent-spawn first and finalized if its agent has ended, and the runner check of [reconcile](#reconcile) applies to the one run (a read with a side effect; it is idempotent). Adapters and dashboards poll with `--no-refresh`; finalization is then left to `wait` and `reconcile`. `--draft` prints the stored `review.json` on stdout instead (untrusted text; the JSON as stored, no envelope, also with `--json`); when the run has none it exits 10 with the error envelope. `--json` includes `artifact.path` whenever the artifact is stored.

### `reconcile`

`review-run reconcile [--dry-run] [--json]` repairs state against reality and reports what it changed (`{changes: [{run_id, kind, details}], warnings, stuck_teardowns, already_running, dry_run}`, [`schemas/cli/reconcile.json`](../schemas/cli/reconcile.json)). It takes a lease; a second concurrent invocation exits 0 reporting `already_running`. It exits 0 whatever the per-run results (2 on usage errors). It does the following:

- finalizes `running` or `escalated` runs per [Finalize](#finalize) whose agent ended, and retries pending agent teardowns;
- reads `launch.json` for a run whose `agent_id` is null and whose file exists;
- for a run in phase `session` whose `agent_id` is null and whose `launch.json` is absent, and only when its runner and its agent-spawn launcher (each by PID plus start time, stored before the launch) are both gone, runs `agent-spawn list --label owner=review-run --label run=<run id> --json` and adopts a candidate only when exactly one agent matches, its kind is `review`, it passes the ownership check ([Labels](#labels)) and it was created at or after the run's `launch_started_at`; it stores the id and continues as for any run (finalize or teardown); zero or several candidates end the run `failed` (`runner_lost`) with the run warning `agent_unknown`. While the runner or the launcher is alive the run is left alone. `show` and `wait` apply the same lookup to the one run they look at;
- ends a `running` run whose runner and launcher (PID plus start time) are both gone and that has no agent (after the two checks above) as `failed` (`runner_lost`); a run whose runner is gone but whose agent is still live stays `running` until the agent ends;
- ends a run whose stored agent is torn down or unknown to agent-spawn as `failed` (`agent_gone`);
- ends an `escalated` run whose `escalated_at` is older than `escalation_ttl` as `failed` (`expired`) after the finalize check: a run whose agent is live (a human answering it through agent-spawn) is skipped until a later pass, and one whose agent has a newer finished run is finalized instead; the terminal transaction comes first, then teardown as in [Finalize](#finalize) step 14, and `post-run` fires;
- retries the removal of throwaways recorded as leaked and, for a run that ended `runner_lost` in phase `prefetch` or `code_run`, removes the stored throwaway and any other that `wtctl list --throwaways --label owner=review-run --label run=<run id> --json` finds, counting failures as leaks ([Code run](#code-run) step 6), and reports the count;
- delivers outbox events left undelivered ([Hooks](#hooks), Delivery);
- removes the prompt and input files left by runs that are terminal or whose runner and launcher are both gone.

Its output has `stuck_teardowns`, the count of runs whose teardown failed 5 times or more, and then `warnings` includes `teardown_stuck`. It never signals a process.

### `prune`

`review-run prune [--older-than <duration>] [--dry-run] [--json]` deletes terminal runs (`done` and `failed`) whose `finished_at` is older than the duration (default config `retention`, 30 days) together with their directories and artifact copies, and prints `{older_than, results: [{run_id, result, reason}], warnings, dry_run}` ([`schemas/cli/prune.json`](../schemas/cli/prune.json)). It never touches a `running` or `escalated` run (it skips them; there is no refusal) and skips a terminal run whose `teardown` is not `done` (`result: skipped`, `reason: teardown_pending`) so its agent id is not lost; it derives each directory from `runs/<run id>` after the id grammar check and deletes through directory file descriptors, never from a stored path. It never signals anything and is audited. It exits 0 whatever the per-run results. The audit and log records of pruned runs stay.

### `cancel`

`review-run cancel <run> [--actor A] [--decision-id ID]... [--dry-run] [--json]` ends a run without an answer; prints `{run, changed, action, dry_run, warnings}` ([`schemas/cli/cancel.json`](../schemas/cli/cancel.json)). In order: (1) a terminal run: exit 0, `changed: false`, `action: none`. (2) The run is refreshed as `show` refreshes it (finalize check, runner check, recovery); a run that ends this way, for example one whose agent already finished a newer run, is reported with `changed: false`, so a finished review is never discarded by a later cancel. (3) A run in phase `finalize` is waited for as in `wait` and reported with `changed: false`. (4) The cancellation is recorded ([Runs, states and phases](#runs-states-and-phases)); a lost compare-and-set returns to step 1 or 3. (5) One action: an agent that is not parked gets `agent-spawn stop <agent_id> --grace 10s --actor <actor> --json` (`action: stop`); with no agent and a live runner (PID plus start time), SIGTERM to the runner (`signal_runner`); with no agent, no runner and a live agent-spawn launcher (PID plus start time), SIGTERM to the launcher (`signal_launcher`); otherwise `none`. (6) Once no live runner remains (a signaled runner is waited for at most 120 s), `cancel` ends the run itself in the terminal transaction (`failed`, `canceled`, `exit_code` 143), then tears the agent down as in [Finalize](#finalize) step 14; a runner still alive after 120 s ends the run itself, and `cancel` prints the row as it is (`state: running`, `cancel_requested_at` set). `changed` is true from step 4 on. `agent-spawn stop` exit 10 continues with step 6; its exit 12 or 20 exits 12 with the envelope (`code: failed`, `reason: lower_failed`, `tool: agent-spawn`, `lower_code`), the cancellation staying recorded so the next finalizer ends the run `canceled` once the agent has ended; a teardown refusal or failure is the warning of step 14 and never undoes the cancel. Audited (`cancel`); `post-run` fires. `--dry-run` performs steps 1 to 3 without changing anything and reports the action it would take. Exit codes: 0, 2, 10, 12, 21.

### `cron`

`review-run cron install|uninstall [--dry-run] [--json]` writes or removes the tagged crontab block `# review-run managed` with an explicit `PATH` including `~/.local/bin`: `*/15 * * * * review-run reconcile --quiet` and `17 3 * * * review-run prune --quiet` (design guide section 4.3). With `--json` it prints `{action, dry_run, changed, block}` ([`schemas/cli/cron.json`](../schemas/cli/cron.json)); `block` holds the lines of the block that were (or would be) installed, empty after `uninstall`.

### `export`

`review-run export [--log] [--since <time>]` prints review-run's audit records (or log records with `--log`) as JSONL lines of [`schemas/audit.json`](../schemas/audit.json) or [`schemas/log.json`](../schemas/log.json), with no envelope.

## Global flags

`--json`, `--config PATH`, `--verbose`, `--quiet`, `--dry-run` (where meaningful), `--actor`, `--decision-id` (repeatable), `--hook NAME=CMD` (repeatable, on `run` and `resume`), `--override-escalation` (`run` only; human-only), `--version`. There is no `--yes`: review-run has no confirm-by-default action. Durations (`--timeout`, `--older-than`, `--since`, config durations) are `0` or an integer with a unit `ms`, `s`, `m`, `h` or `d` ([`schemas/defs.json`](../schemas/defs.json) `duration`); `--timeout` must be greater than 0 and is passed to agent-spawn as given; `--since` also takes RFC 3339. Untrusted text arrives only as `--prompt-file <path>|-`; `--label` values follow the label grammar of architecture section 8.1.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | `done` (for `run --no-wait` and `resume --no-wait`: the session started); also every command that printed its result (`show`, `list`, `export`, `reconcile`, `prune`, `cancel`, a dry run) |
| 1 | internal error (error envelope; no run) |
| 2 | usage error (also `--override-escalation` without a human actor) |
| 10 | not found (a run, an agent, a draft; a resolve-step `not_found`) |
| 11 | a `pre-run` hook vetoed (no run exists), or a lower tool vetoed a `resume` (the run is unchanged) |
| 12 | the run failed outside the session (`prefetch_failed`, `stale_head`, `too_large`, `launch_failed`, `runner_lost`, `agent_gone`, `expired`, `internal`), or a failure with no run (`tool_missing`, `tool_too_old`, `no_tmux`, `wait_timeout`, `schema_newer`, `state_unsafe`, `lower_failed`) |
| 13 | the run is `escalated`, or a `pre-run` hook escalated (no run exists), or a lower tool escalated a `resume` (the run is unchanged; the discriminator is in [Output by exit code](#output-by-exit-code)) |
| 14 to 18 | the session's outcome, passed through from agent-spawn: 14 exited-unreported, 15 killed, 16 timeout, 17 lost, 18 reported failed |
| 20 | refused (`not_open`, `unknown_host`, `live`, `not_escalated`, `lower_refused` (a sibling's refusal)); found before any run record exists or on a run that is not in the right state |
| 21 | ambiguous run prefix |
| 22 | the session finished but its artifact is missing or invalid (`artifact_missing`, `artifact_mismatch`, `artifact_invalid`, `skill_missing`, `implicit_done`) |
| 130, 143 | interrupted (SIGINT or SIGHUP: 130; SIGTERM: 143); also a run that ended `canceled` (its stored exit code) |

Exit 22 is review-run's only tool-specific code (agent-spawn's wait timeout code 22 is not passed through: a session never times out as a wait). A `failed` run's `show` always reports its `reason` and the `exit_code` it ended with. `run --dry-run` exits 0 and prints `would_exit`.

## Output by exit code

With `--json`, stdout holds at most one document. A document with a top-level `error` key is the error envelope ([`schemas/cli/error.json`](../schemas/cli/error.json)); any other is the command's result, for `run`, `resume`, `wait` and `show` the run object ([`schemas/cli/run.json`](../schemas/cli/run.json); for `run --no-wait` with `state: running`). The run object is printed whenever the outcome is a run's state, also for exits 130 and 143 when this command ended the run `canceled` and for `wait` on a run that ended `canceled`; an interrupted waiter (`wait`, a blocking `resume`, `run` after SIGHUP) and an interrupt before the run record exists print nothing. Every other outcome prints the envelope, also for exits 12, 13 and 20 without a run or with the run unchanged; messages and details hold only closed codes, ids and counts. The two exit-13 shapes are normative: the envelope with `code: escalated` and `hook` (and `decision_ids`; `hook: null` and `tool` when a lower tool escalated, on `resume`) for a `pre-run` hook escalation, the run object with `state: escalated` for a session escalation; a caller that needs the distinction uses `--json`. The hook's stderr is never reported (untrusted). The exact stdout of each reason is the [Reasons](#reasons) table. `show --draft --json` prints the draft (no envelope) and exits 10 with the envelope when the run has none.

### Run object

`run_id`, `pr`, `repo`, `state`, `phase` (non-null exactly while `running`), `reason` (null for `running` and `done`), `exit_code` (null while `running`), `mode`, `skill`, `labels` (multiset of `k=v`, at most 60, architecture 8.1's pattern), `previous_run`, `agent_id` and `csession_id` (agent-spawn's and claude-session's grammars), `draft_id`, `anchor` (head_sha, base_ref, base_sha, merge_base_sha), `head_sha` (from the resolve step, set at creation, so a run is findable by head before the anchor exists), `created_at`, `escalated_at` (set while `escalated`), `cancel_requested_at` (null unless a cancellation is recorded), `finished_at`, `artifact` (`path`, `sha256`, `size`, `original_sha256`; null until stored), `summary` (verdict, severity counts, risk, coverage status, code-run status and reason, `head_moved`, and the counts `skipped_files` and `normalized_count`; [`schemas/defs.json`](../schemas/defs.json) `run_summary`; null until stored), `failure` (null unless `failed`; then `{tool, lower_code, lower_reason, retryable, retry_after_s, errors}`, where `errors` holds at most 20 `{pointer, kind}` items with pointers cut to 200 characters; every value is a closed code, an id or a count, never a message), `warnings` (closed codes: `teardown_failed`, `teardown_refused`, `throwaway_leaked`, `sandbox_lowered`, `head_unreadable`, `hook_failed`, `skill_unchecked`, `agent_unknown`, `agent_mismatch`, `tools_unexpected`, `tools_unchecked`), `teardown` (`done`, `pending`, `failed`), `schema_version`. Timestamps are UTC RFC 3339 with `Z`. The artifact and `summary` text fields are untrusted; the schema marks them.

## Hooks

Hooks are configured as `hooks.<name>` lists in config (design guide section 8; `sh -c`, own process group, JSON on stdin, 30 s default timeout, `async` for post hooks only). Per-invocation `--hook` entries and decision ids of `run` and of each `resume` are stored with the run and accumulate (a `resume` adds its entries; an identical entry is kept once); the actor of the latest `run` or `resume` is the run's stored actor. Whichever process finalizes or escalates the run fires the hooks of its current config plus the stored entries, and payloads carry the stored actor and decision ids. `--hook NAME=CMD` strings are shell commands from the caller, stored in `state.db` and run later by whichever process finalizes; they are never logged, audited or printed. Environment: `REVIEW_RUN_HOOK`, `REVIEW_RUN_ID` (the run id; for `pre-run`, the reserved id, since no run record exists yet), `REVIEW_RUN_OBJECT_TYPE=run`, and for post hooks `REVIEW_RUN_EVENT_ID`. Hooks inherit review-run's environment (review-run configures no token variable to strip), so a hook sees whatever credentials the user exported, including when `reconcile` fires it from cron; absolute paths in hook commands are recommended. Payloads ([`schemas/hooks/`](../schemas/hooks/)) carry top-level `tool`, `hook`, `object_type`, `object_id`, `repo` and `labels` (for `pre-run`, the `--label` values), plus `actor` and `decision_ids` in every payload; the post hooks also carry `event_id`. They hold only trusted or derived values: never PR text, the session's report summary or reason, `meta.summary` or `risk_reasons`.

**Delivery.** The transaction that ends or escalates a run stores one outbox event per post hook with `event_id` `<run id>-<hook>-<n>` (`n` is 1 for `post-run` and the run's `escalated_run` for `post-escalate`), also in the payload and in `REVIEW_RUN_EVENT_ID`, and the transition's audit records. Right after the commit, the committing process appends those audit records to `audit.jsonl` (each carrying the `event_id`), then runs the hook entries (synchronous ones within their timeout, `async` ones detached) and marks the event delivered; `show` and `wait` therefore block for at most the hook timeouts when they finalize (`show --no-refresh` never finalizes). An event left undelivered (the process died, or an entry failed) is redelivered by the next `reconcile`, oldest first, up to 5 attempts in all, then abandoned with the run warning `hook_failed` and a log record. Delivery is at least once: a hook that must not act twice deduplicates on `event_id`.

| Hook | Fires | Veto or escalate | Payload (beyond the common keys) |
|---|---|---|---|
| `pre-run` | after resolve and preflight (run order step 2), before the run record exists and before any read of the diff or launch | veto exit 11; escalate exit 13, overridden by `run --override-escalation` | `pr`, `skill`, `mode`, `previous_run`, `head_sha` (the resolve-time head, which can differ from the reviewed anchor), `pr_facts` (`draft`, `is_bot`, `is_fork`, `base_is_default`), `will_run_code`, `code_run_sandbox` (the configured mode), `override_escalation`, `session_sandbox`, `subagents`, `permission_profile` (`bundled`, `variant` or `custom`), `setting_sources`, `sizes` (`additions`, `deletions`, `changed_files` from forgectl) |
| `post-run` | on the transition to `done` or `failed` (once; also for `cancel` and `expired`) | no | `state`, `reason`, `exit_code` (130 or 143 for `canceled`), `pr`, `agent_id` (null for a run with no agent), `draft_id`, `anchor_head_sha` (both null when the run never got an anchor), and for `done`: `verdict`, `severity_counts`, `risk`, `coverage_status`, `code_run_status`, `head_moved` |
| `post-escalate` | on the transition to `escalated` (once per escalation, per run number) | no | `reason` (`needs_input` or `blocked`), `pr`, `agent_id` |

A `decision_id` printed by a `pre-run` hook is recorded on the run and in the audit record. Gate hooks should use a longer `timeout` than `gatectl check --timeout` (architecture section 12.3). review-run has no hook for posting; review-watch's own `post-draft` hook is review-watch's.

## Config

Config is YAML at `~/.config/review-run/config.yaml` plus `config.d/*.yaml` drop-ins (design guide section 5); schema in [`schemas/config.json`](../schemas/config.json); every key is documented in `config.example.yaml`. Precedence: flags, then `repos.<id>`, then environment (`REVIEW_RUN_<KEY>` for top-level scalars only), then global config, then defaults. The repo is forgectl's canonical `repo` from `show`; review-run never derives it from the working directory and reads no configuration from the repository under review. Invalid config fails fast naming the key.

| Key | Default | Meaning |
|---|---|---|
| `skill` | `review-run:review-pr` | the skill, `<plugin>:<skill>`: the bundled default or one from a `plugin_dirs` entry |
| `plugin_dirs` | `[]` | absolute directories of extra plugins holding other review skills; each needs the placeholders `.claude/`, `.mcp.json`, `CLAUDE.md` and `CLAUDE.local.md`; each is used by its realpath and must be a directory owned by the user and not writable by group or others; two plugins with the same `.claude-plugin/plugin.json` `name`, or one named `review-run`, are a config error |
| `mode` | `headless` | default mode (global only) |
| `permission_profile` | bundled file | permission profile file passed on every call |
| `sandbox` | `required` | session sandbox level (`required`, `preferred` or `off`) passed on every call; a `REVIEW_RUN_SANDBOX` below `required` lowers the session sandbox, which claude-session records (`sandbox_lowered`) |
| `setting_sources` | `[]` | setting sources (`user`, `project`, `local`) passed on every call |
| `env_allowlist` | `[PATH]` | names passed with `--env-allowlist-only`; an empty list is passed as `--env-allowlist-only ""`; the same names are refused as for `env_allow` ([`run_pr_code`](#run_pr_code)) |
| `model` | `null` | `--model` (null: Claude Code's default) |
| `timeout` | `null` | `--timeout` (null: the kind's default, 120 minutes) |
| `max_turns` | `null` | `--max-turns` on launch and resume (null: the kind's default) |
| `subagents` | `false` | `true` drops the `Agent` and `Task` denies from the bundled profile |
| `max_subagent_depth` | `1` | depth variable when `subagents` is on |
| `nits` | `false` | ask the skill for `nit`-severity findings |
| `guidelines_file` | `null` | an absolute path (`~` expanded at load) to a regular file, opened without following links, at most 64 KiB of UTF-8, placed in the prompt as trusted instructions |
| `limits.max_file_bytes` | `200000` | per input file (below the Read tool's 256 KB refusal; the skill reads in pages) |
| `limits.max_total_bytes` | `1048576` | total of provided patches before files are skipped |
| `limits.max_files` | `300` | number of input files, at most 2000 (each is one `--input-file` argument to agent-spawn and claude-session) |
| `limits.hard_ceiling_bytes` | `8388608` | the PR's whole diff above this fails the run (`too_large`) |
| `limits.max_artifact_bytes` | `8388608` | artifact size above this is `artifact_invalid` |
| `limits.max_answer_bytes` | `65536` | the `resume` answer above this exits 2 |
| `retention` | `30d` | age at which `prune` deletes terminal runs |
| `escalation_ttl` | `7d` | age of an `escalated` run at which `reconcile` ends it `failed` (`expired`); `null` disables |
| `run_pr_code` | absent | per repo only ([below](#run_pr_code)) |
| `me` | OS user | `me.name` for actors |
| `hooks` | `{}` | hook lists |

`repos.<id>` may override `skill`, `plugin_dirs`, `model`, `timeout`, `max_turns`, `nits`, `guidelines_file`, `subagents`, `max_subagent_depth`, `limits` (merged key by key), `permission_profile`, `sandbox`, `setting_sources`, `env_allowlist` and `run_pr_code`; `mode`, `retention`, `escalation_ttl`, `me` and `hooks` are global only.

### `run_pr_code`

Only under `repos.<id>.run_pr_code`; a repo without it runs no command.

| Key | Default | Meaning |
|---|---|---|
| `command` | required | argv list (never a shell string) run in the throwaway |
| `sandbox` | `required` | `required`, `preferred` or `off` |
| `timeout` | `300s` | wall-clock limit |
| `max_output_bytes` | `1048576` | output captured before the command is stopped |
| `max_workspace_bytes` | `2147483648` | the monitor stops the command when the throwaway's file system has lost more free space than this, or more than 1,000,000 free inodes, since the start |
| `env_allow` | `[]` | extra names for the command's environment (non-secret); refused when a name matches `*TOKEN*`, `*SECRET*`, `*KEY*`, `*PASS*`, `*CREDENTIAL*`, `*AUTH*`, `*COOKIE*`, `*SESSION*`, `*DSN*`, `*_URL`, `*_PAT`, `*PROXY*` or `NETRC` (case-insensitive) or starts with `ANTHROPIC_` or `CLAUDE_` |
| `ro_binds` | `[]` | extra absolute paths bound read-only (language runtimes, pre-populated caches, a `node_modules` directory). Each entry is resolved with realpath at config load, and the realpath is what is bound. Refused when the realpath is `/`, the home directory or an ancestor of it, `/mnt`, `/media`, `/root` or inside a WSL drive mount (`/mnt/<letter>`), under `/proc`, `/sys`, `/dev`, `/run` or `/var/run`, an ancestor of review-run's or any sibling's state directory, contains or is inside a credential path of claude-session's list (its interface, Path rules: `~/.ssh`, `~/.aws`, `~/.gnupg`, `~/.kube`, `~/.docker`, `~/.config`, `~/.claude`, `~/.netrc`, `~/.git-credentials`, `~/.local/state`, `~/.local/share/keyrings`, `~/.cargo/credentials*`, `~/.gem/credentials`, `~/.gradle/gradle.properties`, `~/.m2/settings*.xml` and the XDG equivalents), or has a component named `.ssh`, `.gnupg`, `.aws`, `.kube`, `.docker`, `.config`, `.claude`, `.netrc`, `.npmrc`, `.pypirc`, `credentials.toml` or `.git-credentials`; a toolchain directory that holds a credential file must be narrowed (for example `~/.cargo/registry` instead of `~/.cargo`) |

There is no `network` key in v1: the command never has network access. An `install_command` phase is roadmap work. The refusals above are semantic checks at config load, not schema checks.

## Environment variables

`REVIEW_RUN_<KEY>` sets top-level scalar config values only (design guide section 5). review-run reads no other variable for its own behavior. It passes none of its environment to the review session or the code-run command except the names above. Hook variables are in [Hooks](#hooks). It sets `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH` in agent-spawn's environment only when `subagents` is on. Credentials are the sibling tools': review-run needs no token variable.

## Audit and log

`audit.jsonl` (design guide section 6; [`schemas/audit.json`](../schemas/audit.json)) records, with `attempt_id` (shared by a pair), `actor`, `actor_source` (`flag`, `tty`, `default`), `stdin_tty`, `decision_id`s, `labels`, `event_id` (set on records written from the outbox), `target` (`run_id`, `pr`; both null for `prune`), `action`, `inputs` (for `run` and `resume`: the head sha, `config_sha256`, `skill`, `previous_run`, `permission_profile_sha256`, `session_sandbox`, `setting_sources`, `plugin_dirs_sha256` (null without extra plugins), and for `run` `override_escalation`; the other actions list their own fields in the schema) and `result`: `run` (one record at the start with `result: attempted`, one at the end with the outcome, including `escalated`), `resume` (its own pair for each resume), `cancel` (the same pair), `finalize`, `code_run` (the throwaway id, the sha256 of the argv, the sandbox mode actually used: `required`, `preferred_fallback` or `off`, and the status), `teardown` (one record per attempt) and `prune`. A `pre-run` veto or escalation writes a `run` record with result `vetoed` or `escalated` (target: the reserved run id and the PR; no run record exists). A pair is closed only by the invocation that opened it (`run`, `resume`, `cancel`). When `show`, `wait` or `reconcile` ends or escalates a run, it writes one `finalize` record (its own `attempt_id`, `actor: auto:review-run`, `inputs: {cause, finalized_by}` with `cause` `runner_lost`, `agent_gone`, `expired`, `canceled`, `external_resume` or `finalized` and `finalized_by` `show`, `wait` or `reconcile`, and `result` the run's state, reason and exit code). No record holds PR text, the artifact or `meta` text. `log.jsonl` ([`schemas/log.json`](../schemas/log.json)) records phase and state changes, warnings, normalization counts and sibling-tool error codes as structured fields only (events `phase_change`, `state_change`, `warning`, `normalization`, `sibling_error`, `hook_failed`, `hook_skipped`, `teardown_retry`, `reconcile`, `throwaway_leaked`). `export` prints either.

## Files

review-run runs with umask 077. State is under `~/.local/state/review-run/` (XDG-aware, the same on macOS): `state.db` (SQLite, WAL, additive migrations), `audit.jsonl`, `log.jsonl` (both 0600), `runs/<run id>/` with `settings.json`, `permission-profile.json`, `manifest.json`, `pr.json`, `code-run.json`, `code-run.txt` (sanitized output), `launch.json`, `wait.json` (and `resume.json`), `original.json` and `review.json`, kept until `prune`, and, until the launch returns or the run ends, `prompt.md` and `inputs/`; `tmp/` holds the private directories of `run --dry-run`. The state root, `runs/` and each run directory are created 0700 and every file 0600 with `O_CREAT|O_EXCL|O_NOFOLLOW`; an existing state root, `runs/` or run directory owned by the user with a looser mode is set to 0700 at start; a state root that is a symlink or not owned by the user is refused by every command at start (exit 12, `state_unsafe`). `original.json` holds the session's bytes as reported and is untrusted; no command prints it. `prune` and `reconcile` derive a run's directory from `runs/<run id>` after the id grammar check and delete through directory file descriptors, never from a stored path. `state.db` is private; the audit and log files are public append-only contracts. The state directory must lie outside every path an isolated session can write.

## Labels

Labels are set at `run` (`--label k=v`, repeatable), stored as a multiset on the run, filterable with `list --label`, returned verbatim in `--json` and never given built-in meaning. The keys `owner`, `run`, `pr`, `repo`, `agent` and `kind` are reserved (`label_reserved`, exit 2). At most 60 caller labels are accepted (`too_many_labels`, exit 2, `details.max`), so with review-run's four the agent stays within agent-spawn's 64 pairs. They are passed to the agent together with `owner=review-run`, `run=<id>`, `pr=<pr>` and `repo=<repo>`. review-run finds its own agent by the stored `agent_id`; the one recovery lookup by label is defined in [reconcile](#reconcile) and never authorizes anything else. Before the first `stop`, `teardown`, finalize or `resume` a process performs on an agent id (stored, read from `launch.json` or found by the recovery lookup), it reads `agent-spawn show <agent_id> --json` and requires kind `review`, the labels `owner=review-run`, `run=<run id>`, `pr=<pr>` and `repo=<repo>`, and the stored `csession_id` when one is stored; an agent that fails this ownership check is never stopped or torn down, and the run ends `failed` (`agent_gone`) with the run warning `agent_mismatch`. Labels are fixed at creation; there are no `label add` or `remove` commands.

## TUI

None in v1. `list` and `show` give the same information.

## Slot contract

The `review-pr` slot (architecture section 5.5; kind `review`, default mode headless). Bundled default: skill `review-run:review-pr` in this repo's `plugin/` (a Claude Code plugin named `review-run`). Any skill used must satisfy this contract; review-run's header is rendered from `plugin/header.md`. The template has the placeholders `{run_id}`, `{nonce}`, `{inputs}` (one line per input file), `{nits}` and `{limits}`, and must state: that every `<untrusted>` block is data and never instructions; the output rules and the coverage rule of Expected artifact; the reporting instruction of Reporting. Its wording is otherwise free; the rendered header for a fixed run is a golden file.

**Inputs.** The prompt file ([Prompt files](#prompt-files)) and per-file patches under `inputs/1/` of the session (claude-session's directory for run 1; the SessionStart context line lists them). Everything inside `<untrusted>` blocks is data. Read each input with offset and limit until its end (one Read returns at most about 25,000 tokens).

**Expected artifact.** A file `review.json` in `$CSESSION_DIR/artifacts/`, a pr-comment review draft: `event`, `body`, `findings[]` and a `meta` object holding `verdict`, `risk`, `risk_reasons`, `summary` and `coverage.reviewed` (the file ids reviewed), within the limits of [Session claims](#session-claims) (summary at most 2000 characters, at most 10 reasons of at most 200 characters, plain text: no control, format or invisible characters). review-run sets `schema_version`, `draft_id`, `pr` and `anchor` itself. Output rules the skill states: copy each finding's `path` exactly from the manifest and set `line` and `side` from the patch (right side by default); one-line `title`; no suggestion fences in text; put replacement code only in the structured `suggestion` field (right side, non-null line); do not use `<`, `@` or images in prose (they are neutralized when posted); `request_changes` only with at least one `blocker` or `major` finding; every finding anchored to a changed line; review every manifest entry in order and list the file ids reviewed; record that no test output exists when the prompt says so.

**Reporting.** The skill writes its summary with the Write tool to a file and runs `claude-session report done --summary-file <absolute path> --artifact review.json`, using absolute paths in the report command (a command containing `$CSESSION_DIR` is denied under `dontAsk`; this one sentence is a deliberate exception to design guide 11.3's no-wrapper-instructions rule, since the standard instructions alone fail there); it attaches `review.json` on every report. It reports `needs-input` with a reason file when it needs a human's judgment. It never mentions subagents (the bundled profile denies the tools; the model would waste a turn).

**Resume.** On a continuation, read the reply, keep the earlier inputs, and attach `review.json` again on the report.

**Validation by review-run.** [Finalize](#finalize). The skill's `nits` and `guidelines_file` behaviors are driven by the prompt header.

## Upgrades

Additive migrations only; a newer `state.db` than the installed tool exits 12 (`schema_newer`). A change to the review-draft schema or to agent-spawn's, claude-session's or forgectl's outputs is absorbed by the pinned sibling schemas in tests; an unknown field in a sibling's output is ignored, a missing required one fails the step closed. Minimum sibling versions are recorded in `design.md`; an older agent-spawn or claude-session than the recorded minimums exits 12 (`tool_too_old`) at run order step 1.

## Residual risks

- A prompt-injected review session can still steer its own output (findings, verdict, summary, risk). All of it is untrusted and reviewed by a human, or gated, before any external action; `meta` never authorizes anything.
- The model's `verdict`, `risk` and `coverage.reviewed` are claims; review-run verifies only their form and recomputes what it can derive.
- A silently partial read inside one provided file (the model skimming a long patch) is not detectable; the per-file size limit, paged reads and the coverage statement reduce it.
- Without the sandbox (`sandbox: preferred` or `off` for the code run, or a lowered session sandbox) attacker code or a steered session runs with the user's rights; both are loud (warnings and recorded modes).
- On macOS there is no code-run sandbox in v1; the step is skipped.
- The code-run sandbox does not limit memory or CPU; a hostile command can exhaust them until the timeout, and its tmpfs counts against RAM up to their sizes.
- A same-user process can alter review-run's state directory and forge a run's artifact; the audit trail and the frozen original are not tamper-proof (architecture section 19.2).
- The skill check proves only that the skill loaded, and is not performed in interactive mode (`skill_unchecked`); a change in Claude Code's init format would make the headless check fail closed (`skill_missing`).
- A tool added by a Claude Code release is reachable by the session; every headless run with the bundled profile compares its init tool list with the pinned set and flags a difference (`tools_unexpected`), after the fact.
- If the runner and the agent-spawn launcher are both killed (for example by a process-group kill) after agent-spawn allocated the agent and before it wrote its result file, `reconcile` recovers the agent by one label lookup ([reconcile](#reconcile)); with zero or several candidates the run ends `failed` (`runner_lost`) with the warning `agent_unknown`, and an agent that may exist is parked by agent-spawn's own rules until a human tears it down.
- `meta.review_run.coverage.reviewed` can claim files the model skimmed; coverage is accounting, not proof.
- The code-run sandbox can read the bound system directories, including `/usr/local` (user-installed tools; a script there with an embedded token is readable by the command), `/etc/passwd` and `/etc/group`.
- The workspace bound is a free-space and free-inode delta of a shared file system: other processes move it, and a burst between two polls overshoots it.
- Expiry of escalated runs, teardown retries, hook redelivery and leaked-throwaway removal run only in `reconcile`; without the cron block (`review-run cron install`) or a manual call they do not happen.

## Platform notes

Linux and WSL: everything. macOS: everything except the code run (skipped in v1); the session sandbox is Claude Code's own (`sandbox-exec`), checked by claude-session. Prerequisites: `forgectl`, `pr-comment`, `agent-spawn` and `claude-session` on `PATH` (each missing one exits 12 `tool_missing` naming its install command; claude-session is a direct dependency because review-run reads its `status` for the skill check); `wtctl` when any repo configures `run_pr_code`; `bwrap` (0.9.0 or later, for `--disable-userns` and `--size`) and `socat` for a sandboxed code run and for the sandboxed session on Linux and WSL2. Siblings resolve on `PATH`: cron and jobsctl runs need `PATH` to contain them (the cron block sets it; jobsctl jobs get it from jobsctl's env file or config `PATH`), and `HOME` or the XDG variables must point at the same state as the interactive shell. Claude Code's skill-loading, permission and sandbox behaviors were verified against the version recorded in `design.md`; a `live` suite re-checks them.
