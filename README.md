# review-run

Run a review skill on a pull request and store the resulting review draft.

## Status

Pre-design: interface not yet specified; see docs/design.md.

## Layer

L3 (workflow)

## What it does

- Skill-wrapper: runs a review skill (default `review-run:review-pr`) on a PR, headless by default.
- Pre-fetches the diff and metadata into a fenced prompt file, so the review session needs no git-host credentials and never has a checkout (kind `review`, `--worktree none`, cwd in the session dir).
- Optionally (per-repo `run_pr_code: true`) runs a configured command in a throwaway checkout first and passes its fenced output to the session.
- Stores runs (`running`, `done`, `escalated`, `failed`) and its own immutable copy of each review artifact (pr-comment's review-draft format plus metadata).
- Tears down its review agents when a run is `done` or `failed`.

## Indicative commands

Indicative, finalized in `docs/interface.md`.

```text
review-run run <pr> [--skill ...] [--mode ...] [--label k=v]
review-run list [--label k=v] --json
review-run show <run> --json
```

## Dependencies

- `forgectl` (pre-fetch of diff and metadata); binding: TBD (A/B/C), decided in this tool's design session
- `wtctl` (throwaway checkouts, only when `run_pr_code: true`); binding: A (stated in architecture §5.3)
- `git` (throwaway checkouts, only when `run_pr_code: true`); binding: A (stated in architecture §5.3)
- `agent-spawn` (launches the headless review session); binding: TBD (A/B/C), decided in this tool's design session
- `pr-comment` (review-draft schema only); binding: A (stated in architecture §5.3)

## Install

```sh
uv tool install git+https://github.com/agent-workflow-kit/review-run
```

## Documentation

- System architecture: [`../awkit-docs/architecture.md`](../awkit-docs/architecture.md) ([published](https://github.com/agent-workflow-kit/awkit-docs/blob/main/architecture.md))
- Tool design guide: [`../awkit-docs/design-guide.md`](../awkit-docs/design-guide.md) ([published](https://github.com/agent-workflow-kit/awkit-docs/blob/main/design-guide.md))
- This tool's design: [`docs/design.md`](docs/design.md)
- This tool's interface: [`docs/interface.md`](docs/interface.md)

## License

MIT — see LICENSE.
