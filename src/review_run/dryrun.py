"""``run --dry-run``: the plan of one run, printed (issue #15; interface.md, ``run``).

Run order steps 1 and 2 already ran (``preflight.plan_run``). This module adds the pre-fetch reads,
the manifest and the prompt, built by the same functions a real run uses (``inputs``), and the
document of ``schemas/cli/run-dry-run.json``. It is read-only by contract: no run id, no run row, no
audit or log record, no hook, no launch and no code run. The prompt and inputs go to a private
directory under ``<state root>/tmp/`` that is removed whatever happens.

A failure of the pre-fetch or the manifest (``prefetch_failed``, ``stale_head``, ``too_large``) is
not an error of the dry run: it is the plan's ``would_fail`` with ``would_exit`` 12, and the dry run
itself exits 0. Its manifest, coverage and sizes are then empty (nothing was built).

Seams: ``tools`` stays empty and ``code_run_plan`` reports no sandbox probe until the readiness
facts land (TODO(#41)); the code run itself is TODO(#35).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import launch as lc
from .errors import ExitCode
from .inputs import compose_prompt, fetch_and_manifest
from .prefetch import PrefetchFailure
from .preflight import PLACEHOLDER_RUN_ID, RunPlan, scratch_dir
from .prompt import PromptError, input_paths, write_prompt_files
from .sanitizer import sanitize_inline
from .store import Store

SCHEMA_VERSION = 1
_EMPTY_COVERAGE: dict[str, Any] = {
    "status": "full",
    "reviewed": [],
    "skipped": [],
    "ignored_ids": 0,
}
_EMPTY_SIZES = {"files": 0, "diff_bytes": 0, "provided_bytes": 0, "provided_files": 0}


def code_run_plan(plan: RunPlan) -> dict[str, Any]:
    """What the code-run step would do. TODO(#41): the bubblewrap probe and ``would_skip`` (the
    step itself is TODO(#35)); until then a configured code run reports the probe as
    ``unavailable`` without a skip reason, and none configured is ``not_applicable``."""
    config = plan.resolved.values.get("run_pr_code")
    if config is None:
        return {
            "configured": False,
            "sandbox": None,
            "sandbox_probe": "not_applicable",
            "would_skip": None,
        }
    return {
        "configured": True,
        "sandbox": config.get("sandbox", "required"),
        "sandbox_probe": "unavailable",
        "would_skip": None,
    }


def build_plan(plan: RunPlan, root: Path) -> dict[str, Any]:
    """The dry-run document for ``plan``. Reads through forgectl and ``pr-comment draft new`` and
    (with ``--previous``) the stored review; writes only into a scratch directory it removes."""
    run_dir = Path(root) / "runs" / PLACEHOLDER_RUN_ID  # the argv's shape; nothing is created
    manifest_json: list[dict[str, Any]] = []
    coverage: dict[str, Any] = {"status": "full", "reviewed": [], "skipped": [], "ignored_ids": 0}
    sizes: dict[str, int] = {"files": 0, "diff_bytes": 0, "provided_bytes": 0, "provided_files": 0}
    argv: list[str] = []
    would_fail: list[str] = []
    try:
        if plan.previous is None:
            fetched, manifest, previous = fetch_and_manifest(plan, None)
        else:
            with Store.open(Path(root)) as store:
                fetched, manifest, previous = fetch_and_manifest(plan, store)
        with scratch_dir(Path(root)) as scratch:
            files = compose_prompt(
                plan,
                run_id=PLACEHOLDER_RUN_ID,
                fetched=fetched,
                manifest=manifest,
                previous=previous,
            )
            write_prompt_files(scratch, files, manifest)  # the same writer a run uses
        manifest_json = manifest.to_json()
        coverage = manifest.coverage_preview()
        sizes = manifest.sizes(len(fetched.diff.encode("utf-8", errors="surrogatepass")))
        argv = lc.launch_argv(
            plan.settings,
            repo=plan.repo,
            pr=plan.pr,
            run_id=PLACEHOLDER_RUN_ID,
            run_dir=run_dir,
            input_paths=input_paths(run_dir, files),
            labels=plan.request.labels,
            actor=plan.request.actor,
            decision_ids=plan.request.decision_ids,
        )
    except PrefetchFailure as exc:
        would_fail = [exc.reason]
    except PromptError:  # a real run ends ``internal`` here (the runner's catch-all)
        would_fail = ["internal"]
    would_exit = ExitCode.FAILED if would_fail else ExitCode.OK
    return {
        "schema_version": SCHEMA_VERSION,
        "dry_run": True,
        "pr": plan.pr,
        "repo": plan.repo,
        "skill": plan.skill,
        "mode": plan.mode,
        "previous_run": plan.previous.run_id if plan.previous else None,
        "head_sha": plan.facts.head_sha,
        "settings": plan.settings.document()["settings"],
        "launch_argv": argv,
        "plugin_dirs": list(plan.settings.plugin_dirs),
        "tools": [],  # TODO(#41): sibling names, paths and versions
        "manifest": manifest_json,
        "coverage_preview": coverage,
        "sizes": sizes,
        "code_run_plan": code_run_plan(plan),
        "would_fail": would_fail,
        "would_exit": int(would_exit),
        "warnings": [],
    }


def _s(value: object, limit: int = 200) -> str:
    return sanitize_inline("" if value is None else str(value), limit)


def human_plan(doc: dict[str, Any]) -> str:
    """The plan for a terminal: every value from the PR or the config goes through
    ``sanitize_inline`` (control, format and look-alike characters are marked)."""
    lines = [f"dry run of {_s(doc['pr'])}: would exit {doc['would_exit']}"]
    for key in ("repo", "skill", "mode", "head_sha", "previous_run"):
        if doc[key] is not None:
            lines.append(f"{key.replace('_', ' ')}: {_s(doc[key])}")
    if doc["would_fail"]:
        lines.append("would fail: " + " ".join(_s(r) for r in doc["would_fail"]))
    lines.append("settings:")
    for item in doc["settings"]:
        value = json.dumps(item["value"], ensure_ascii=True, sort_keys=True)
        lines.append(f"  {_s(item['key'])} = {_s(value, 120)} ({_s(item['origin'])})")
    for directory in doc["plugin_dirs"]:
        lines.append(f"plugin dir: {_s(directory, 400)}")
    if doc["launch_argv"]:
        lines.append("launch: " + " ".join(_s(a, 400) for a in doc["launch_argv"]))
    sizes = doc["sizes"]
    lines.append(
        f"sizes: {sizes['files']} files, diff {sizes['diff_bytes']} bytes, "
        f"provided {sizes['provided_files']} files / {sizes['provided_bytes']} bytes"
    )
    coverage = doc["coverage_preview"]
    lines.append(
        f"coverage preview: {coverage['status']}, {len(coverage['reviewed'])} reviewed, "
        f"{len(coverage['skipped'])} skipped"
    )
    lines.append(f"manifest ({len(doc['manifest'])}):")
    for entry in doc["manifest"]:
        old = f" (from {_s(entry['old_path'], 400)})" if entry["old_path"] else ""
        lines.append(
            f"  {_s(entry['file'])} {_s(entry['status'])} {_s(entry['disposition'])} "
            f"{entry['bytes']} bytes {_s(entry['path'], 400)}{old}"
        )
    code = doc["code_run_plan"]
    lines.append(
        "code run: "
        + (f"configured, sandbox {_s(code['sandbox'])}" if code["configured"] else "not configured")
    )
    return "\n".join(lines) + "\n"
