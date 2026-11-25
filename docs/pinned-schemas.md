# Pinned sibling schemas

review-run reads the JSON output of its sibling tools. The schemas for those
outputs are copied under `tests/pinned_schemas/<sibling>/schemas/`, pinned to a
released sibling version. They exist for tests only: nothing under `src/`
imports a sibling or reads these files (`tests/test_pinned_schemas.py`
enforces it).

## Header

Each file carries a top-level `x-pinned-from` object:

```json
{"x-pinned-from": {"sibling": "forgectl", "version": "0.56.0", "source": "schemas/cli/show.json"}}
```

`version` is the sibling's release (tag without the `v`), `source` the path in
the sibling repo and under the sibling's pinned directory. A test fails for a
file without a valid header, and for a sibling whose files disagree on version.
Directory layout mirrors the sibling's, so relative `$ref`s keep resolving;
files that a pinned schema references (for example `defs.json`) are copied too.

## What is pinned

| Sibling | Version | Files |
|---|---|---|
| forgectl | 0.56.0 | `cli/show`, `cli/files`, `cli/diff`, `cli/ci`, `cli/error`, `defs` |
| agent-spawn | 0.38.1 | `cli/launch` (launch result), `cli/wait` and `cli/show` (outcome object), `cli/resume` (resume result), `cli/list` (`list --json`), `cli/error` (error envelope), `defs`; also `cli/launch-dry-run`, `cli/stop`, `cli/teardown`, `cli/teardown-dry-run` (read only by the fake harness) |
| claude-session | 0.45.2 | `cli/status`, `cli/outcome` (referenced by `status`), `cli/error`, `cli/forget` (read only by the fake harness) |
| wtctl | 1.2.0 | `cli/create`, `cli/remove`, `cli/list` (`wtctl list --json`, which `list --throwaways` filters), `cli/error`, `defs` |
| pr-comment | 0.39.0 | `review-draft`, `cli/draft-new`, `cli/draft-check`, `cli/error`, `defs` |

Only the outputs review-run parses are pinned. For agent-spawn that is
`launch`, `wait`, `show`, `resume` and `list`. The fake harness also validates
the fakes' `launch --dry-run`, `stop`, `teardown` and claude-session `forget`
outputs, so those schemas are pinned too; review-run does not read them.

## Refreshing after a sibling release

`<sibling checkout>` stands for any clone of the sibling repository. Its
working tree and local tags may be stale, so never copy files from the working
tree or trust local tags without fetching.

1. Find the latest release: `git -C <sibling checkout> fetch --tags`, then
   `gh release list --repo agent-workflow-kit/<sibling> --limit 1` (or
   `git -C <sibling checkout> tag --sort=-v:refname | head -1` after the fetch).
2. Re-copy each pinned file (and any file it newly `$ref`s) from that tag:
   `git -C <sibling checkout> show v<version>:schemas/<path>`.
3. Put the `x-pinned-from` header first in each file (`sibling`, new `version`,
   `source`) and keep the rest of the document unchanged. Remove files the
   sibling deleted.
4. Update the table above.
5. Run `make verify`. A schema change that breaks a fake sibling output fails
   the fake-validation test (issue #10); fix the fake or the code that reads
   the output in the same change.

## For the fake harness (issue #10)

Every fake sibling output under `tests/fakebin/` must validate against the
matching file here (acceptance criterion of issue #3, enforced by #10). Schemas
use JSON Schema 2020-12 with relative `$ref`s across files, so a validator needs
a registry over this directory tree. Each file's `$id` is `<sibling>/<path>.json`
without the `schemas/` segment (for example `wtctl/cli/list.json`,
`agent-spawn/defs.json`, `pr-comment/review-draft.json`), so key the registry by
`$id`, or map `<sibling>/<path>` to
`tests/pinned_schemas/<sibling>/schemas/<path>`. This issue adds no validator dependency.

## The fake siblings (issue #10)

`tests/fakebin/` holds fake `forgectl`, `pr-comment`, `agent-spawn`,
`claude-session` and `wtctl` programs, and `tests/fakes.py` the `fakebin`
fixture that runs them. Each fake follows the sibling's `docs/interface.md` at
the pinned version (argv grammar, exit codes, stdout and stderr split, error
envelope, signals). The harness validates every recorded stdout that names a
pinned schema when the test ends, so a fake that emits an invalid document fails
the test. Named scenarios live in `tests/fake_scenarios.py`; the format of a
scenario step is in `tests/fakebin/_common.py`. When a sibling release changes a
pinned schema, update the fake in the same change.
