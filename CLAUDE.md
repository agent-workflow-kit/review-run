# review-run: instructions for Claude sessions

**Naming.** Tools have no shared prefix; see design guide §1 naming rules.

## What this is

`review-run` is a Layer 3 (workflow) tool of the awkit tool family. Run a review skill on a pull request and store the resulting review draft.
It must work fully standalone at a terminal: it never depends on the harness (`awkit`).

## Read first

- `../awkit-docs/architecture.md`: this tool's §5 entry (§5.3, `review-run`) and §20 entry, plus the sections listed in `docs/design.md`.
- `../awkit-docs/design-guide.md`: the conventions every tool follows.
- `docs/design.md`: this tool's design state, decisions and open questions.

## Rules

- This repo is public and open source. Never add confidential information: no company names, hostnames, ticket project keys, people's names, or private skill/plugin names. Organization-specific values belong in the user's config.
- Never import sibling repos. Read their docs and schemas by path only. Copying a sibling's schema files into `tests/` pinned to a version is allowed.
- No upward awareness: this tool never knows about, or behaves differently for, tools that call it. Behavior may differ only by explicit arguments.
- Interface first: write `docs/interface.md` and `schemas/` before implementation.
- Semver. A breaking interface change is, before 1.0, a `feat` commit whose title names the break (never `!` before 1.0), and from 1.0 on a `!` commit; the title becomes the changelog line (details go in the PR body). Either way, update `awkit-docs/interface-registry.md`. Never hand-edit CHANGELOG.md or the version.
- Run `make verify` before every commit; it must pass.
- All subprocess and HTTP calls go through `src/review_run/gateway.py` so tests can substitute fakes.
- Never run destructive git commands (reset, checkout of files, clean, restore, forced push). Never kill processes by name or pattern; only exact PIDs the tool started, or process groups it created for hooks.
- Untrusted or agent-generated text travels only in files or on stdin, never in argv or env.
- Minimize dependencies; justify each one in `docs/design.md`.

## Workflow

1. Start with a design session that resolves the open topics in `docs/design.md` and records decisions there.
2. Write `docs/interface.md` and `schemas/`.
3. Implement test-first.

## Repository workflow

- Work in a git worktree on a branch named `<type>/<short-slug>`, never in the main checkout (design guide section 17).
- PR titles are Conventional Commit headers; they become the squash commit, the version bump and the changelog line (architecture section 18.3). Do not use `!` before 1.0 (a breaking change before then is a `feat` whose title names the break).
- Never use `--no-verify`. Check PR titles, bodies and comments with `guard text` where the private-content guard is installed (architecture section 18.7).
- Never merge a PR without the maintainer's explicit instruction for that PR; merges go through `awkit-ci`'s `scripts/pr-merge` (architecture section 18.6).
- Never hand-edit `CHANGELOG.md` or the version in `pyproject.toml`; the release job owns them.
