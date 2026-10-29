# review-run: design

Status: Seeded from awkit architecture v1.4; not yet designed.

Section references (§N) point to `../awkit-docs/architecture.md` unless marked "design guide" (`../awkit-docs/design-guide.md`).

## Role and layer

Run a review skill on a pull request and store the resulting review draft.

Layer 3 (workflow). Must work fully standalone; no upward awareness.

Skill-wrapper tool (§5.5, design guide §11). Default slot: `review-pr` (bundled as `review-run:review-pr` in `plugin/`).

## Responsibilities

From §5.3 (`review-run`), verbatim:

Skill-wrapper: run a review skill on a PR.

- Commands: `run <pr> [--skill ...] [--mode ...] [--label k=v]`, `list [--label k=v] --json`, `show <run> --json`.
- Uses: forgectl (pre-fetch), wtctl and `git` (throwaway checkouts, only when `run_pr_code: true`; binding A), agent-spawn, pr-comment (review-draft schema only; binding A).
- Notes:
  - Default mode headless. The session is always launched with `--kind review`, `--worktree none` and its cwd in `$CSESSION_DIR`; it never has a checkout. review-run pre-fetches the diff and metadata so the session needs no git-host credentials, and passes the review safety settings per call (section 13.1). `run --label` values pass through to the agent.
  - Stores runs and its own copy of their artifacts; run states are `running`, `done`, `escalated` and `failed`, and `show` refreshes from agent-spawn for escalated or promoted runs. The artifact is pr-comment's review-draft format plus metadata (verdict, severities, model-reported risk); it is immutable once stored. A run whose artifact is missing or fails the review-draft schema is `failed` (an implicit-done `result.md` is not a review draft).
  - Tears down its review agents (`owner=review-run`) only when the run is `done` or `failed`; an `escalated` run keeps its agent until it is resolved.
  - With per-repo `run_pr_code: true`, runs the throwaway-checkout sequence before the session starts (section 13.2 rule 9).
  - Ships a generic default review skill; config can point at any other review skill. Never requests env-file copying.

## Uses

- `forgectl` (pre-fetch of diff and metadata); binding: TBD (A/B/C), decided in this tool's design session
- `wtctl` (throwaway checkouts, only when `run_pr_code: true`); binding: A (stated in architecture §5.3)
- `git` (throwaway checkouts, only when `run_pr_code: true`); binding: A (stated in architecture §5.3)
- `agent-spawn` (launches the headless review session); binding: TBD (A/B/C), decided in this tool's design session
- `pr-comment` (review-draft schema only); binding: A (stated in architecture §5.3)

## Key flow: §10.1 Review request (flow A, priority slice)

Verbatim from the architecture:

0. When jobsctl is used, `jobsctl run` runs from cron (or as a loop).
1. Cron runs `review-watch poll`.
2. forgectl lists PRs that request your review directly; draft PRs, bot-authored PRs and PRs you already reviewed yourself are skipped.
3. For each new queue item (PR, head sha, review-request instance), review-watch enqueues a jobsctl job (queue `reviews`, `--unique-key review:<pr>@<sha>`) or, without jobsctl, spawns a detached process under its internal cap, running `review-run run <pr> --mode headless --label rw-item=<id>`.
4. review-run pre-fetches diff and metadata into a fenced prompt file. If `run_pr_code: true`, it first runs the throwaway-checkout sequence of section 13.2 rule 9 and appends the fenced command output to that file. It then calls `agent-spawn launch --mode headless --kind review --cwd session-dir --skill review-run:review-pr --plugin-dir <bundled> --worktree none --prompt-file <file> --permission-profile <bundled file> --env-allowlist-only <vars> --label owner=review-run --wait` (section 13.1). The session never has a checkout.
5. The session reports done with the review artifact. review-run validates it against the review-draft schema, stores the run with its own copy of the artifact, and tears down the agent; review-watch copies the artifact into its review draft; its `post-draft` hook calls notice-route (Slack/Home Assistant).
6. You accept (CLI or TUI). If the head moved, the review draft is stale (`rerun`, or `accept --reanchor` to re-anchor onto the new head via pr-comment). Otherwise pr-comment posts one review (COMMENT, or REQUEST_CHANGES per the draft) with inline comments and an attribution footer, and records an audit entry.
7. Optional: gatectl decides auto-post / auto-approve, in shadow mode first, within the hard rules (section 5.3).
8. A new review starts only on a fresh review request or an explicit `rerun`, up to the configured per-PR round limit.
9. If the review session escalates, the run is `escalated`, its job is held and its agent is kept. Once the agent is resumed and the run is done or failed (or the item is rejected), `poll` resolves the held job.

## Relevant cross-cutting sections

- §3.2, §3.3: layering rules and edge bindings
- §5.5: capability slots: `review-pr`, kind `review`
- §8: session model: kinds, outcomes, artifacts, taint (claude-session)
- §9: agent lifecycle, post-exit policies and teardown (agent-spawn)
- §10.1: review request flow (flow A)
- §12: autonomy and gates (public defaults, escalation)
- §13: security: review profile (§13.1), fencing and transport, third-party code (§13.2 rule 9)
- §14: audit and attribution marker
- design guide §11, §12: skill-wrapper pattern; safety

## Constraints (decided)

From §20 (`review-run`):

with `run_pr_code: true`, the per-repo configured command runs deterministically in a throwaway checkout with an env allowlist and a timeout, and only its fenced output reaches the session; the session never has a checkout (section 13.2 rule 9).

Packaging: The bundled plugin is shipped inside the package and located via `importlib.resources.files('review_run') / 'plugin'`.

## Open topics

From §20 (`review-run`):

review artifact metadata, default reviewer skill design, subagent depth limit, sandboxing the configured command (e.g. a container).

## Decisions

| Decision | Rationale | Date |
|---|---|---|

## Questions for the design session

1. What metadata does the review artifact carry beyond the review-draft format (verdict, severities, model-reported risk), and how is it schematized?
2. How is the default reviewer skill designed (structure, coverage, output discipline)?
3. What subagent depth limit applies to review sessions?
4. Should the `run_pr_code` command be sandboxed (e.g. in a container), and how?

## Prior art and references

- **Model review as one input among deterministic checks.** Chris Adams et al., "Automating Low-Risk Code Review at Meta: RADAR, Risk Calibration, and Review Efficiency", arXiv:2605.30208 [cs.SE], 2026. <https://arxiv.org/abs/2605.30208>. In that system an LLM review stage sits between deterministic gates and never decides alone. Likewise, review-run only produces an artifact (including model-reported risk); posting and any automatic action are decided elsewhere (review-watch, gatectl), where model output can only make the outcome stricter.
