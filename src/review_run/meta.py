"""Meta derivation: finalize steps 11 and 12, and the bytes of ``review.json`` for step 13
(issue #21; interface.md, Finalize and Meta).

``derive(ctx, validated)`` is the production ``Seams.derive``. It runs after steps 6 to 10
(``artifact``) and returns ``Processed``: the canonical ASCII bytes of the final draft (the
validated draft plus ``meta.review_run``), the run's ``summary`` and the closed warnings. It
writes nothing: finalize writes ``review.json`` (step 13) and runs the terminal transaction.

* Step 11, end head: ``forgectl show <pr> --json`` once more through the gateway (the closed
  readers of ``prefetch``). A head different from the anchor's sets ``head_moved: true`` and the
  run stays ``done``; a failed or malformed read sets ``head_at_end`` and ``head_moved`` to null and
  the warning ``head_unreadable``. Nothing of the sibling's output is kept but the sha.
* Step 12, meta (``schemas/meta.json``): trusted facts come from the run row, ``settings.json``,
  ``pr.json``, ``manifest.json`` and ``code-run.json`` of the run directory; the session's own
  claims (verdict, risk, reasons, summary, reviewed ids) are copied after their bounds are
  asserted again. Coverage is computed from the manifest, never from the session's word: it can
  only name files the manifest provided.

Everything the session wrote is untrusted text; a value that breaks a bound here is a bug of this
code or a hostile row, and ends the run ``internal`` (``ReviewRunError``, no text). No timestamps
and no sizes go into ``meta`` (the schema has neither), so the same inputs give the same bytes.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from . import artifact
from .errors import ReviewRunError
from .finalize import ArtifactContext, FinalizeFailure, Processed, Seams
from .manifest import (
    BINARY,
    EMPTY,
    MAX_FILES_IN_PR,
    PROVIDED,
    SKIPPED_FILE_CAP,
    SKIPPED_SIZE_CAP,
    TRUNCATED,
    Manifest,
)
from .prefetch import LowerFailure, show_facts, strict_json
from .settings import RunSettings, bundled_plugin_dir

META_VERSION: Final = 1
MANIFEST_NAME: Final = "manifest.json"
PR_NAME: Final = "pr.json"
CODE_RUN_NAME: Final = "code-run.json"
MAX_FILE_BYTES: Final = 64 << 20
MAX_PLUGIN_JSON: Final = 64 << 10
# manifest.json worst case: a path appears at least twice in its diff header, so a path's
# JSON-escaped bytes (6 per BMP control or non-ASCII character, 12 per astral one) cost at most 3
# per byte of the diff, which ``limits.hard_ceiling_bytes`` bounds; every entry adds its keys,
# indentation and numbers (the input names of all entries total at most 2000 short names).
MANIFEST_ENTRY_OVERHEAD: Final = 1024
BUNDLED_PLUGIN: Final = "review-run"
DEFAULT_SKILL: Final = "review-run:review-pr"  # schemas/config.json skill default
SKILL_VERSION_MAX: Final = 64
SEVERITIES: Final = ("blocker", "major", "minor", "nit", "info")
SANDBOXES: Final = ("required", "preferred", "off")
PROFILES: Final = ("bundled", "variant", "custom")
CODE_RUN_STATUSES: Final = ("ok", "failed", "skipped", "not_configured")
CODE_RUN_REASONS: Final = (
    "wtctl_missing",
    "throwaway_refused",
    "ref_unavailable",
    "throwaway_failed",
    "sandbox_unavailable",
    "sandbox_unsupported",
    "sandbox_failed",
    "ro_bind_refused",
    "throwaway_leaks",
)
CODE_RUN_KEYS: Final = (
    "status",
    "reason",
    "exit_code",
    "signal",
    "timed_out",
    "truncated",
    "bytes",
    "duration_ms",
    "sandbox",
    "output_sha256",
)
MAX_NORMALIZED_ENTRIES: Final = artifact.MAX_NORMALIZED + 1
MAX_POINTER_CHARS: Final = 1024
MAX_REVIEWED: Final = 9999

_SIGNAL = re.compile(r"SIG[A-Z0-9]+", re.ASCII)
_SHA256 = re.compile(r"[0-9a-f]{64}", re.ASCII)
_FILE_ID = re.compile(r"f[0-9]{4}", re.ASCII)
_REASON_OF = {
    TRUNCATED: "truncated",
    SKIPPED_SIZE_CAP: "size_cap",
    SKIPPED_FILE_CAP: "file_cap",
}


def _internal(why: str) -> ReviewRunError:
    """A failure of the run's own files or of an invariant; never carries untrusted text."""
    return ReviewRunError("internal", why)


# -- the run directory's files -------------------------------------------------------------------


def read_run_file(run_dir: Path, name: str, limit: int) -> bytes | None:
    """The bytes of ``run_dir/name`` (at most ``limit``): opened without following links, a
    regular file of this user that no group or other can access. ``None`` when it does not
    exist; anything else wrong with it is ``internal``."""
    try:
        fd = os.open(run_dir / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError:
        raise _internal(f"{name} of the run cannot be opened") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise _internal(f"{name} of the run is not a private regular file")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            data = handle.read(limit + 1)
    except OSError:
        raise _internal(f"{name} of the run cannot be read") from None
    finally:
        os.close(fd)
    if len(data) > limit:
        raise _internal(f"{name} of the run is too large")
    return data


def manifest_limit(hard_ceiling_bytes: int) -> int:
    """The most bytes ``manifest.json`` can hold for a run whose diff was capped at
    ``hard_ceiling_bytes``, never below ``MAX_FILE_BYTES``."""
    worst = 3 * hard_ceiling_bytes + MAX_FILES_IN_PR * MANIFEST_ENTRY_OVERHEAD
    return max(MAX_FILE_BYTES, worst)


def _json_file(run_dir: Path, name: str, limit: int) -> Any:
    data = read_run_file(run_dir, name, limit)
    if data is None:
        raise _internal(f"{name} of the run is missing")
    try:
        return strict_json(data.decode("utf-8"))
    except ValueError:  # UnicodeDecodeError is one
        raise _internal(f"{name} of the run is not strict JSON") from None


# -- step 11: the end head -----------------------------------------------------------------------


def end_head(pr: str, anchor_head: str) -> tuple[str | None, bool | None]:
    """``(head_at_end, head_moved)`` from one more ``forgectl show``; ``(None, None)`` when the
    read fails, is malformed, or answers for another pull request. A closed or merged PR still has
    a head, so its state is not looked at."""
    try:
        facts = show_facts(pr)
    except LowerFailure:
        return None, None
    if facts.pr != pr:
        return None, None
    return facts.head_sha, facts.head_sha != anchor_head


# -- the session's claims, asserted again ----------------------------------------------------------


def assert_claims(claims: artifact.Claims, max_files: int) -> None:
    """The bounds of Session claims, once more (``artifact`` checked them; a value that fails here
    did not come through it). ``internal``, never the value."""
    reasons, summary, reviewed = claims.risk_reasons, claims.summary, claims.reviewed
    ok = (
        claims.verdict in artifact.VERDICTS
        and claims.risk in artifact.RISKS
        and isinstance(reasons, tuple)
        and len(reasons) <= artifact.REASONS_MAX
        and all(
            isinstance(r, str)
            and 1 <= len(r) <= artifact.REASON_MAX
            and not any(artifact.forbidden_in_claim(c, allow_lf=False) for c in r)
            for r in reasons
        )
        and isinstance(summary, str)
        and 1 <= len(summary) <= artifact.SUMMARY_MAX
        and not any(artifact.forbidden_in_claim(c, allow_lf=True) for c in summary)
        and isinstance(reviewed, tuple)
        and len(reviewed) <= min(max_files, MAX_REVIEWED)
        and len(set(reviewed)) == len(reviewed)
        and all(isinstance(i, str) and _FILE_ID.fullmatch(i) for i in reviewed)
    )
    if not ok:
        raise _internal("the session claims are out of their bounds")


# -- coverage, severity counts, normalized -------------------------------------------------------


def coverage_of(manifest: Manifest, reviewed_claim: Sequence[str]) -> dict[str, Any]:
    """The honest ``coverage`` object (interface, Meta).

    ``reviewed``: the claimed ids that are ``provided`` entries, in manifest order. ``skipped``, in
    manifest order: a ``provided`` entry the session did not list (``not_reviewed``), every
    ``truncated`` entry (always, even when listed), and the ``skipped_size_cap`` and
    ``skipped_file_cap`` entries. ``binary`` and ``empty`` entries are in neither list. Claimed ids
    that are not ``provided`` entries (unknown ids, truncated, skipped, binary or empty files) are
    ignored and counted once each in ``ignored_ids``. ``partial`` exactly when something is
    skipped."""
    claimed = set(reviewed_claim)
    provided = {e.file for e in manifest.entries if e.disposition == PROVIDED}
    reviewed: list[str] = []
    skipped: list[dict[str, str]] = []
    for entry in manifest.entries:
        if entry.disposition == PROVIDED:
            if entry.file in claimed:
                reviewed.append(entry.file)
            else:
                skipped.append({"file": entry.file, "reason": "not_reviewed"})
        elif entry.disposition in _REASON_OF:
            skipped.append({"file": entry.file, "reason": _REASON_OF[entry.disposition]})
        elif entry.disposition not in (BINARY, EMPTY):
            raise _internal("a manifest entry has an unknown disposition")
    return {
        "status": "partial" if skipped else "full",
        "reviewed": reviewed,
        "skipped": skipped,
        "ignored_ids": len(claimed - provided),
    }


def severity_counts(findings: object) -> dict[str, int]:
    """Counted from the findings; one without a (known) severity is ``unspecified``."""
    counts = {name: 0 for name in (*SEVERITIES, "unspecified")}
    if not isinstance(findings, list):
        raise _internal("the validated draft has no findings list")
    for finding in findings:
        severity = finding.get("severity") if isinstance(finding, dict) else None
        counts[
            severity if isinstance(severity, str) and severity in SEVERITIES else "unspecified"
        ] += 1
    return counts


def _normalized(entries: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for entry in entries:
        rule, pointer, count = entry.get("rule"), entry.get("pointer"), entry.get("count")
        if (
            set(entry) != {"rule", "pointer", "count"}
            or rule
            not in ("drop_unknown", "drop_null", "drop_unanchored", "event_downgrade", "truncated")
            or not isinstance(pointer, str)
            or len(pointer) > MAX_POINTER_CHARS
            or type(count) is not int  # noqa: E721 (a bool is not a count)
            or count < 1
        ):
            raise _internal("a normalization entry is out of its bounds")
        out.append({"rule": rule, "pointer": pointer, "count": count})
    if len(out) > MAX_NORMALIZED_ENTRIES:
        raise _internal("too many normalization entries")
    return out


# -- the files of the run ------------------------------------------------------------------------


def load_manifest(run_dir: Path, hard_ceiling_bytes: int = 0) -> Manifest:
    limit = manifest_limit(hard_ceiling_bytes)
    try:
        return Manifest.from_json(_json_file(run_dir, MANIFEST_NAME, limit))
    except ValueError:
        raise _internal("manifest.json of the run is malformed") from None


def load_pr_facts(run_dir: Path) -> dict[str, bool | None]:
    doc = _json_file(run_dir, PR_NAME, 4096)
    keys = ("draft", "is_bot", "is_fork", "base_is_default")
    if (
        not isinstance(doc, dict)
        or set(doc) != set(keys)
        or not all(doc[k] is None or type(doc[k]) is bool for k in keys)  # noqa: E721
    ):
        raise _internal("pr.json of the run is malformed")
    return {k: doc[k] for k in keys}


def _not_configured() -> dict[str, Any]:
    return {
        "status": "not_configured",
        "reason": None,
        "exit_code": None,
        "signal": None,
        "timed_out": False,
        "truncated": False,
        "bytes": None,
        "duration_ms": None,
        "sandbox": None,
        "output_sha256": None,
    }


def _count(value: object, *, ran: bool) -> bool:
    """A count of a run that ran is a non-negative int; of one that did not, null."""
    if not ran:
        return value is None
    return type(value) is int and value >= 0  # noqa: E721


def code_run_of(doc: object) -> dict[str, Any]:
    """The ``code_run`` meta object from ``code-run.json`` (``schemas/defs.json`` ``code_run``):
    only its ten keys are kept, and the rules of the schema's status cases are applied. ``internal``
    when it breaks one."""
    if not isinstance(doc, dict) or not all(k in doc for k in CODE_RUN_KEYS):
        raise _internal("code-run.json of the run is malformed")
    c = {k: doc[k] for k in CODE_RUN_KEYS}
    status, reason, signal, sandbox = c["status"], c["reason"], c["signal"], c["sandbox"]
    sha, exit_code = c["output_sha256"], c["exit_code"]
    ran = status in ("ok", "failed")
    ok = (
        status in CODE_RUN_STATUSES
        and (reason in CODE_RUN_REASONS if status == "skipped" else reason is None)
        and (exit_code is None or type(exit_code) is int)  # noqa: E721
        and (signal is None or (isinstance(signal, str) and _SIGNAL.fullmatch(signal) is not None))
        and type(c["timed_out"]) is bool  # noqa: E721
        and type(c["truncated"]) is bool  # noqa: E721
        and _count(c["bytes"], ran=ran)
        and _count(c["duration_ms"], ran=ran)
        and (sandbox in SANDBOXES if ran else sandbox is None)
        and ((isinstance(sha, str) and _SHA256.fullmatch(sha) is not None) if ran else sha is None)
        and (ran or (exit_code is None and signal is None))
        and (ran or (c["timed_out"] is False and c["truncated"] is False))
    )
    if ok and status == "ok":
        ok = exit_code == 0 and signal is None and c["timed_out"] is False
    if ok and status == "failed":
        ok = (
            (exit_code is not None and exit_code != 0)
            or signal is not None
            or c["timed_out"] is True
        )
    if not ok:
        raise _internal("code-run.json of the run is out of its bounds")
    return c


def load_code_run(run_dir: Path, *, configured: bool = False) -> dict[str, Any]:
    """``code-run.json``, which the code run writes in every case (interface.md). Absent: a run
    whose settings configure ``run_pr_code`` is ``internal``; one that does not made no code run
    (the prompt said so), which is ``not_configured``. TODO(#35): until the code run writes the
    file, a run with ``run_pr_code`` configured ends ``internal`` here."""
    data = read_run_file(run_dir, CODE_RUN_NAME, MAX_FILE_BYTES)
    if data is None:
        if configured:
            raise _internal("code-run.json of the run is missing")
        return _not_configured()
    try:
        return code_run_of(strict_json(data.decode("utf-8")))
    except ValueError:
        raise _internal("code-run.json of the run is not strict JSON") from None


def run_pr_code_configured(settings: RunSettings) -> bool:
    """Whether the run's settings configure ``run_pr_code`` (a repo's key, null when unset)."""
    entry = settings.entries.get("run_pr_code")
    return entry is not None and entry[0] is not None


def skill_version(skill: str) -> str | None:
    """The bundled plugin's ``plugin.json`` version for the default skill, else null.
    TODO(#77): the file says ``0.0.0`` until the release keeps it in step; it is also read at
    finalize time, not at launch time, so a plugin updated during the run is reported as it is
    then."""
    if skill != DEFAULT_SKILL:
        return None
    data = read_plugin_json()
    version = data.get("version") if isinstance(data, dict) else None
    if (
        isinstance(version, str)
        and 1 <= len(version) <= SKILL_VERSION_MAX
        and version.isascii()
        and version.isprintable()
    ):
        return version
    return None


def read_plugin_json() -> Any:
    path = bundled_plugin_dir() / ".claude-plugin" / "plugin.json"
    try:
        with open(path, "rb") as handle:
            return strict_json(handle.read(MAX_PLUGIN_JSON).decode("utf-8"))
    except (OSError, ValueError):
        return None


def session_of(settings: RunSettings, ctx: ArtifactContext) -> dict[str, Any]:
    sandbox, profile = settings.value("sandbox"), settings.profile_kind
    if sandbox not in SANDBOXES or profile not in PROFILES:
        raise _internal("the run's settings are out of their bounds")
    checked = ctx.session.tools_checked is True
    unexpected = ctx.session.tools_unexpected if checked else 0
    if type(unexpected) is not int or unexpected < 0:  # noqa: E721 (a bool is not a count)
        raise _internal("the session's unexpected tool count is out of its bounds")
    return {
        "sandbox": sandbox,
        "profile": profile,
        "profile_sha256": settings.profile_sha256,
        "tools_checked": checked,
        "tools_unexpected": unexpected,
    }


# -- the whole of steps 11 and 12 ------------------------------------------------------------------


def build_meta(
    ctx: ArtifactContext,
    validated: artifact.Validated,
    *,
    manifest: Manifest,
    pr_facts: Mapping[str, bool | None],
    code_run: Mapping[str, Any],
    head_at_end: str | None,
    head_moved: bool | None,
) -> dict[str, Any]:
    """``meta.review_run`` (``schemas/meta.json``) from its sources."""
    run, claims = ctx.run, validated.claims
    return {
        "version": META_VERSION,
        "run_id": run.run_id,
        "skill": run.skill,
        "skill_version": skill_version(run.skill),
        "skill_checked": ctx.session.skill_checked is True,
        "mode": run.mode,
        "session": session_of(ctx.settings, ctx),
        "verdict": claims.verdict,
        "risk": claims.risk,
        "risk_reasons": list(claims.risk_reasons),
        "summary": claims.summary,
        "severity_counts": severity_counts(validated.draft.get("findings")),
        "coverage": coverage_of(manifest, claims.reviewed),
        "pr": dict(pr_facts),
        "code_run": dict(code_run),
        "head_at_end": head_at_end,
        "head_moved": head_moved,
        "normalized": _normalized(validated.normalized),
        "previous_run": run.previous_run,
    }


def summary_of(meta: Mapping[str, Any]) -> dict[str, Any]:
    """The run's ``summary`` (``schemas/defs.json`` ``run_summary``) from the derived meta."""
    coverage, code_run = meta["coverage"], meta["code_run"]
    return {
        "verdict": meta["verdict"],
        "severity_counts": dict(meta["severity_counts"]),
        "risk": meta["risk"],
        "coverage_status": coverage["status"],
        "code_run_status": code_run["status"],
        "code_run_reason": code_run["reason"],
        "skipped_files": len(coverage["skipped"]),
        "normalized_count": sum(e["count"] for e in meta["normalized"]),
        "head_moved": meta["head_moved"],
    }


def derive(ctx: ArtifactContext, validated: artifact.Validated) -> Processed:
    """Steps 11 and 12 and the bytes of step 13: see the module docstring.

    The draft check of step 10 ran on the draft without ``meta`` (pr-comment ignores it), and the
    final draft differs from that one only by ``meta``, so there is no second check; the final
    bytes must still fit ``limits.max_artifact_bytes`` (a stored ``review.json`` above it could
    not be read back by ``--previous``): ASCII escapes can inflate a valid draft past it, which
    ends the run ``artifact_invalid`` (kind ``length``) before anything is written."""
    run = ctx.run
    max_files = int(ctx.settings.value("limits.max_files"))
    assert_claims(validated.claims, max_files)
    if run.anchor is None or not isinstance(run.anchor.get("head_sha"), str):
        raise _internal("the run has no stored anchor")
    manifest = load_manifest(ctx.run_dir, int(ctx.settings.value("limits.hard_ceiling_bytes")))
    pr_facts = load_pr_facts(ctx.run_dir)
    code_run = load_code_run(ctx.run_dir, configured=run_pr_code_configured(ctx.settings))
    head_at_end, head_moved = end_head(run.pr, run.anchor["head_sha"])
    meta = build_meta(
        ctx,
        validated,
        manifest=manifest,
        pr_facts=pr_facts,
        code_run=code_run,
        head_at_end=head_at_end,
        head_moved=head_moved,
    )
    final = {**validated.draft, "meta": {"review_run": meta}}
    review = (artifact.canonical_json(final) + "\n").encode("ascii")
    if len(review) > int(ctx.settings.value("limits.max_artifact_bytes")):
        raise FinalizeFailure("artifact_invalid", errors=[{"pointer": "", "kind": "length"}])
    return Processed(
        review=review,
        summary=summary_of(meta),
        warnings=("head_unreadable",) if head_moved is None else (),
    )


def production_seams() -> Seams:
    """The seams of a production run: ``derive`` is this module's. TODO(#22): ``check_session``
    (the skill and tool checks); until it lands the seams stay incomplete and finalize leaves a
    done session ``running`` for a later finalizer."""
    return Seams(derive=derive)


__all__ = [
    "assert_claims",
    "build_meta",
    "code_run_of",
    "coverage_of",
    "derive",
    "end_head",
    "load_code_run",
    "load_manifest",
    "load_pr_facts",
    "production_seams",
    "severity_counts",
    "skill_version",
    "summary_of",
]
