# review-run

Run a review skill on a pull request and store the resulting review draft.

## Status

Designed (interface 0.1.0-design); not yet implemented. The rules are in [`docs/interface.md`](docs/interface.md), the decisions in [`docs/design.md`](docs/design.md) and the staged plan in [`docs/plan.md`](docs/plan.md).

## Layer

L3 (workflow)

## What it does

- Skill-wrapper: runs a review skill (default `review-run:review-pr`) on a PR, headless by default, and blocks until the review is `done`, `escalated` or `failed`.
- Pre-fetches the PR through forgectl and pr-comment into a fenced prompt, a manifest and one patch file per changed file, so the session needs no git-host credentials and never has a checkout (kind `review`, sandbox required, no setting sources, cwd in the session directory).
- Optionally (per repo, in your own config) runs a command on the PR's code first, in a no-network bubblewrap sandbox in a throwaway checkout, and passes its sanitized output to the session as untrusted text.
- Validates the review the session reports: lossless normalization, then `pr-comment draft check`. Stores its own immutable copy as a pr-comment review draft whose `meta.review_run` carries the verdict, severity counts, model-reported risk, coverage (full or partial), PR facts and the code-run summary.
- Tears its review agent down when the run is `done` or `failed`; an `escalated` run keeps its agent until `review-run resume` or another resume finishes it.
- Never posts anything: posting is pr-comment's job and the decision belongs to the caller.

## Commands

```text
review-run run <pr> [--skill S] [--mode headless|interactive] [--label k=v] [--previous <run>] [--no-wait] [--dry-run]
review-run resume <run> [--prompt-file F]
review-run wait <run>
review-run list [--state S] [--pr P] [--label k=v] --json
review-run show <run> [--draft] --json
review-run reconcile | prune | cron install|uninstall | export
```

Exit codes: 0 done, 11 vetoed, 12 failed outside the session, 13 escalated, 14 to 18 the session's outcome (passed through from agent-spawn), 20 refused, 21 ambiguous, 22 the session finished but its artifact is missing or invalid. Everything else is in `docs/interface.md`.

## Dependencies

- `forgectl` (pre-fetch of PR facts, files, diff and CI); binding A.
- `pr-comment` (`draft new` and `draft check`; review-draft schema pinned in tests); binding A.
- `agent-spawn` (launches the headless review session through claude-session); binding A.
- `wtctl` (throwaway checkout, only when a repo configures `run_pr_code`); binding A.
- `bwrap` and `socat` on Linux and WSL2 (the code-run sandbox and Claude Code's session sandbox); install with your distribution's package manager (for example `apt install bubblewrap socat`). macOS has no code-run sandbox in v1.
- `claude` (Claude Code), used by claude-session.

A missing sibling tool exits 12 (`tool_missing`) and names its install command.

## Install

```sh
uv tool install git+https://github.com/agent-workflow-kit/review-run
```

## Documentation

- System architecture: [`../awkit-docs/architecture.md`](../awkit-docs/architecture.md) ([published](https://github.com/agent-workflow-kit/awkit-docs/blob/main/architecture.md))
- Tool design guide: [`../awkit-docs/design-guide.md`](../awkit-docs/design-guide.md) ([published](https://github.com/agent-workflow-kit/awkit-docs/blob/main/design-guide.md))
- This tool's design: [`docs/design.md`](docs/design.md)
- This tool's interface: [`docs/interface.md`](docs/interface.md)
- This tool's plan: [`docs/plan.md`](docs/plan.md)

## License

MIT — see LICENSE.
