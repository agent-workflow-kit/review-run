"""What ``run`` and ``run --dry-run`` both compute from a plan: the pre-fetch, the manifest and the
prompt. One code path, so the dry run's preview is what a real run would produce (the run only adds
persistence, cancellation checks and the files in its run directory)."""

from __future__ import annotations

from typing import Any, Final

from .config_semantic import read_guidelines_file
from .manifest import Manifest, build_manifest
from .prefetch import Prefetch, PreviousReview, load_previous, prefetch
from .preflight import RunPlan
from .prompt import CodeRunInput, PromptFiles, build_prompt
from .settings import RunSettings, bundled_header_template
from .store import Store

DEFAULT_MAX_OUTPUT_BYTES: Final = 1024 * 1024  # run_pr_code.max_output_bytes' default
LIMIT_KEYS: Final = (
    "max_file_bytes",
    "max_total_bytes",
    "max_files",
    "hard_ceiling_bytes",
    "max_answer_bytes",
)


def run_limits(settings: RunSettings) -> dict[str, int]:
    return {k: int(settings.value(f"limits.{k}")) for k in LIMIT_KEYS}


def fetch_and_manifest(
    plan: RunPlan, store: Store | None, settings: RunSettings | None = None
) -> tuple[Prefetch, Manifest, PreviousReview | None]:
    """Run order steps 5 and 6: ``--previous`` (needs ``store``), the pre-fetch and the manifest.
    Raises ``PrefetchFailure`` (``prefetch_failed``, ``stale_head``, ``too_large``).
    ``settings`` default to the plan's; a run passes the ones it stored."""
    s = plan.settings if settings is None else settings
    limits = run_limits(s)
    previous = None
    if plan.previous is not None:
        if store is None:
            raise ValueError("--previous needs the store")
        previous = load_previous(
            store, plan.previous.run_id, max_bytes=int(s.value("limits.max_artifact_bytes"))
        )
    fetched = prefetch(plan.facts.pr, hard_ceiling_bytes=limits["hard_ceiling_bytes"])
    manifest = build_manifest(
        fetched.files,
        fetched.diff,
        max_file_bytes=limits["max_file_bytes"],
        max_total_bytes=limits["max_total_bytes"],
        max_files=limits["max_files"],
        hard_ceiling_bytes=limits["hard_ceiling_bytes"],
    )
    return fetched, manifest, previous


def compose_prompt(
    plan: RunPlan,
    *,
    run_id: str,
    fetched: Prefetch,
    manifest: Manifest,
    previous: PreviousReview | None,
    code_run: CodeRunInput | None = None,
    settings: RunSettings | None = None,
) -> PromptFiles:
    """The prompt and its input files for ``plan`` (the guidelines file is read here)."""
    s = plan.settings if settings is None else settings
    values: Any = plan.resolved.values
    guidelines_path = s.value("guidelines_file")
    guidelines = None if guidelines_path is None else read_guidelines_file(guidelines_path)
    code_cfg = values.get("run_pr_code") or {}
    return build_prompt(
        template=bundled_header_template(),
        run_id=run_id,
        pr_facts=plan.facts,
        ci=fetched.ci,
        manifest=manifest,
        code_run=CodeRunInput.not_configured() if code_run is None else code_run,
        previous_review=None if previous is None else previous.review,
        guidelines=guidelines,
        nits=bool(s.value("nits")),
        limits=run_limits(s),
        max_output_bytes=int(code_cfg.get("max_output_bytes", DEFAULT_MAX_OUTPUT_BYTES)),
    )
