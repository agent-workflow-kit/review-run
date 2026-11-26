"""The run object, the reasons table and the read paths ``list`` and ``show`` (interface.md,
Reasons, Run object, ``list``, ``show``).

The read paths never change state, never touch the network and tolerate a corrupt or hostile
row. What ``run_object`` guarantees, for any stored row:

- It either returns an object that validates against ``schemas/defs.json`` ``run``, or raises
  ``CorruptRunError`` (an ``internal`` error with a fixed message, never stored text).
- Every field is checked against the pattern or enum of its schema definition. An optional field
  that fails is null (a bad label is dropped).
- A row is corrupt when an identity field (``run_id``, ``pr``, ``repo``, ``mode``, ``skill``,
  ``head_sha``, ``created_at``) has no valid value. ``list`` skips corrupt rows (and reports a
  count on stderr); ``show`` exits 1.
- A row whose state needs fields it lacks (``done`` without artifact, summary, draft id, anchor
  or finish time; ``escalated`` without agent id or escalation time) is reported ``failed`` with
  reason ``internal`` (exit 12). A ``failed`` row without ``finished_at`` uses ``created_at``; a
  ``canceled`` one without ``cancel_requested_at`` uses ``finished_at``; a ``done`` row drops a
  stray ``cancel_requested_at``.

``--json`` keeps text raw (JSON escapes it); human output passes every stored string through
the sanitizer.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import stat
from collections.abc import Callable
from dataclasses import dataclass
from itertools import islice
from typing import Any

from .errors import ReviewRunError
from .sanitizer import sanitize_inline
from .store import Run, Store, format_time, is_run_id

SCHEMA_VERSION = 1
MAX_LABELS = 60
MAX_STRING = 1024  # any stored string, in a run object, is cut to this many characters
MAX_LABEL = 330  # grammar: 64 + "=" + 256, with slack
MAX_ERRORS = 20
MAX_POINTER = 200
MAX_DRAFT_BYTES = 16 * 1024 * 1024  # 2x the default artifact cap
MAX_LIMIT = 1_000_000
MAX_LABEL_FILTERS = 50  # distinct --label filters; each adds an EXISTS clause to the query
MODES = ("headless", "interactive")

# The patterns of schemas/defs.json, by definition name (a test pins them to the schema). Matched
# with fullmatch after a length bound, so a hostile value costs little.
SCHEMA_PATTERNS: dict[str, re.Pattern[str]] = {
    name: re.compile(pattern)
    for name, pattern in {
        "run_id": "^rr-[a-z2-7]{8}$",
        "repo_id": "^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*(?::[0-9]{1,5})?(?:/(?!\\.\\.?(?:/|$))[A-Za-z0-9._-]+)+$",  # noqa: E501
        "pr": "^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*(?::[0-9]{1,5})?(?:/(?!\\.\\.?(?:/|#))[A-Za-z0-9._-]+)+#[1-9][0-9]{0,9}$",  # noqa: E501
        "agent_id": "^ag-[a-z2-7]{8}$",
        "csession_id": "^cs-[a-z2-7]{8}$",
        "draft_id": "^[0-9A-HJKMNP-TV-Z]{26}$",
        "sha": "^[0-9a-f]{40}$",
        "sha256": "^[0-9a-f]{64}$",
        "ts": "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\\.[0-9]+)?Z$",
        "label": "^[a-z0-9][a-z0-9_.-]{0,63}=[A-Za-z0-9._:/#@+=,-]{0,256}$",
        "skill": "^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$",
    }.items()
}

STATES = ("running", "done", "escalated", "failed")
PHASES = ("prefetch", "code_run", "session", "finalize")
TEARDOWNS = ("done", "pending", "failed")
TOOLS = ("forgectl", "pr-comment", "agent-spawn", "claude-session", "wtctl")
WARNINGS = (
    "teardown_failed",
    "teardown_refused",
    "throwaway_leaked",
    "sandbox_lowered",
    "head_unreadable",
    "hook_failed",
    "skill_unchecked",
    "agent_unknown",
    "agent_mismatch",
    "tools_unexpected",
    "tools_unchecked",
)
_VERDICTS = ("approve", "comment", "request_changes")
_RISKS = ("low", "medium", "high")
_COVERAGE = ("full", "partial")
_CODE_RUN = ("ok", "failed", "skipped", "not_configured")
_CODE_RUN_REASONS = (
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
_SEVERITIES = ("blocker", "major", "minor", "nit", "info", "unspecified")
_CODE_RE = re.compile(r"[a-z][a-z0-9_]*\Z")
_RETRYABLE_LOWER = ("rate_limited", "network_error")


# ---------------------------------------------------------------- the reasons table


@dataclass(frozen=True)
class Reason:
    """One row of the Reasons table for a reason that ends a run."""

    state: str  # ``failed`` or ``escalated``
    exits: tuple[int, ...]  # the exit codes the run may carry; the first is the default
    retryable: str  # "never", "always", "lower_code" (rate_limited, network_error) or "lower"


REASONS: dict[str, Reason] = {
    "prefetch_failed": Reason("failed", (12,), "lower_code"),
    "stale_head": Reason("failed", (12,), "always"),
    "too_large": Reason("failed", (12,), "never"),
    "launch_failed": Reason("failed", (12,), "lower"),
    "canceled": Reason("failed", (143, 130), "never"),
    "session_failed": Reason("failed", (18,), "never"),
    "unreported": Reason("failed", (14,), "never"),
    "killed": Reason("failed", (15,), "never"),
    "timeout": Reason("failed", (16,), "never"),
    "lost": Reason("failed", (17,), "never"),
    "runner_lost": Reason("failed", (12,), "always"),
    "agent_gone": Reason("failed", (12,), "never"),
    "expired": Reason("failed", (12,), "never"),
    "implicit_done": Reason("failed", (22,), "never"),
    "artifact_missing": Reason("failed", (22,), "never"),
    "artifact_mismatch": Reason("failed", (22,), "never"),
    "skill_missing": Reason("failed", (22,), "never"),
    "artifact_invalid": Reason("failed", (22,), "never"),
    "internal": Reason("failed", (12,), "never"),
    "needs_input": Reason("escalated", (13,), "never"),
    "blocked": Reason("escalated", (13,), "never"),
}

# Reasons with no run: envelope code and exit code (the rows of the table whose state is none).
NO_RUN_REASONS: dict[str, tuple[str, int]] = {
    "not_open": ("refused", 20),
    "unknown_host": ("refused", 20),
    "live": ("refused", 20),
    "not_escalated": ("refused", 20),
    "lower_refused": ("refused", 20),
    "tool_missing": ("failed", 12),
    "tool_too_old": ("failed", 12),
    "no_tmux": ("failed", 12),
    "schema_newer": ("failed", 12),
    "state_unsafe": ("failed", 12),
    "plugin_changed": ("failed", 12),
    "lower_failed": ("failed", 12),
    "wait_timeout": ("failed", 12),
    "previous_mismatch": ("usage", 2),
    "previous_not_done": ("usage", 2),
    "skill_not_found": ("usage", 2),
    "label_reserved": ("usage", 2),
    "too_many_labels": ("usage", 2),
    "pr_too_long": ("usage", 2),
}
# Rows with no reason: envelope code and exit code.
NO_REASON_CODES: dict[str, int] = {
    "usage": 2,
    "config_invalid": 2,
    "not_found": 10,
    "ambiguous": 21,
    "vetoed": 11,
    "escalated": 13,
    "internal": 1,
}


def is_retryable(reason: str, lower_code: str | None, lower_retryable: bool) -> bool:
    """Whether a retry can help, per the Reasons table (escalations: n/a, reported false)."""
    info = REASONS.get(reason)
    if info is None:
        return False
    if info.retryable == "always":
        return True
    if info.retryable == "lower_code":
        return lower_code in _RETRYABLE_LOWER
    if info.retryable == "lower":
        return lower_retryable
    return False


# ---------------------------------------------------------------- the run object


class CorruptRunError(ReviewRunError):
    """A stored row has no valid identity: it cannot be shown as a run (exit 1, ``internal``).

    The message is fixed; no stored text ever reaches it.
    """

    def __init__(self) -> None:
        super().__init__("internal", "a stored run is corrupt")


def _text(value: object, limit: int = MAX_STRING) -> str | None:
    return value[:limit] if isinstance(value, str) else None


def _pat(name: str, value: object, limit: int) -> str | None:
    """``value`` when it is a string of at most ``limit`` characters matching the schema pattern."""
    if isinstance(value, str) and len(value) <= limit and SCHEMA_PATTERNS[name].fullmatch(value):
        return value
    return None


def _need(value: str | None) -> str:
    if value is None:
        raise CorruptRunError
    return value


def _ts(value: object) -> str | None:
    return _pat("ts", value, 40)


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _count(value: object) -> int:
    n = _int(value)
    return n if n is not None and n >= 0 else 0


def _enum(value: object, allowed: tuple[str, ...]) -> str | None:
    return value if isinstance(value, str) and value in allowed else None


def _anchor(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    head = _pat("sha", value.get("head_sha"), 40)
    if head is None:
        return None
    return {
        "head_sha": head,
        "base_ref": _text(value.get("base_ref"), 255) or "",
        "base_sha": _pat("sha", value.get("base_sha"), 40),
        "merge_base_sha": _pat("sha", value.get("merge_base_sha"), 40),
    }


def _summary(value: object) -> dict[str, Any] | None:
    """The run summary, or None when it is not the closed shape (a hostile row)."""
    if not isinstance(value, dict):
        return None
    verdict = _enum(value.get("verdict"), _VERDICTS)
    risk = _enum(value.get("risk"), _RISKS)
    coverage = _enum(value.get("coverage_status"), _COVERAGE)
    code_run = _enum(value.get("code_run_status"), _CODE_RUN)
    counts = value.get("severity_counts")
    if None in (verdict, risk, coverage, code_run) or not isinstance(counts, dict):
        return None
    head_moved = value.get("head_moved")
    return {
        "verdict": verdict,
        "severity_counts": {k: _count(counts.get(k)) for k in _SEVERITIES},
        "risk": risk,
        "coverage_status": coverage,
        "code_run_status": code_run,
        "code_run_reason": _enum(value.get("code_run_reason"), _CODE_RUN_REASONS),
        "skipped_files": _count(value.get("skipped_files")),
        "normalized_count": _count(value.get("normalized_count")),
        "head_moved": head_moved if isinstance(head_moved, bool) else None,
    }


summary_of = _summary  # the closed shape check, for the finalizer


def _failure(value: object, reason: str) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    tool = _enum(raw.get("tool"), TOOLS)
    lower_code = raw.get("lower_code")
    lower_code = lower_code if isinstance(lower_code, str) and _CODE_RE.match(lower_code) else None
    lower_reason = raw.get("lower_reason")
    lower_reason = (
        lower_reason if isinstance(lower_reason, str) and _CODE_RE.match(lower_reason) else None
    )
    retry_after = _int(raw.get("retry_after_s"))
    errors: list[dict[str, str]] = []
    items = raw.get("errors")
    for item in items[:MAX_ERRORS] if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        pointer, kind = item.get("pointer"), item.get("kind")
        if isinstance(pointer, str) and isinstance(kind, str) and _CODE_RE.match(kind):
            errors.append({"pointer": pointer[:MAX_POINTER], "kind": kind})
    return {
        "tool": tool,
        "lower_code": lower_code,
        "lower_reason": lower_reason,
        "retryable": is_retryable(reason, lower_code, raw.get("retryable") is True),
        "retry_after_s": retry_after if retry_after is not None and retry_after >= 0 else None,
        "errors": errors,
    }


def _outcome(run: Run) -> tuple[str, str | None, int | None]:
    """State, reason and exit code as the Reasons table allows them.

    A stored reason outside the closed set, or an exit code the table does not allow for it, is
    a corrupt row: a ``failed`` run reports ``internal`` (12), an ``escalated`` one ``blocked``
    (13). ``running`` has neither; ``done`` is 0.
    """
    state = run.state if run.state in STATES else "failed"
    if state == "running":
        return state, None, None
    if state == "done":
        return state, None, 0
    info = REASONS.get(run.reason) if isinstance(run.reason, str) else None
    if info is None or info.state != state:
        reason = "blocked" if state == "escalated" else "internal"
        return state, reason, REASONS[reason].exits[0]
    assert isinstance(run.reason, str)
    exit_code = _int(run.exit_code)
    if run.reason == "canceled" and exit_code not in info.exits:
        exit_code = _int(run.cancel_exit_code)
    if exit_code not in info.exits:
        exit_code = info.exits[0]
    return state, run.reason, exit_code


def _artifact(run: Run) -> dict[str, Any] | None:
    path = run.artifact_path
    sha = _pat("sha256", run.artifact_sha256, 64)
    original = _pat("sha256", run.original_sha256, 64)
    size = _int(run.artifact_size)
    if (
        isinstance(path, str)
        and path.startswith("/")
        and len(path) <= 4096
        and sha is not None
        and original is not None
        and size is not None
        and size >= 0
    ):
        return {"path": path, "sha256": sha, "size": size, "original_sha256": original}
    return None


def _labels(raw: object) -> list[str]:
    if not isinstance(raw, tuple | list):
        return []
    valid = (x for x in raw if _pat("label", x, MAX_LABEL) is not None)
    return list(islice(valid, MAX_LABELS))


def run_object(run: Run) -> dict[str, Any]:
    """The run object of ``schemas/defs.json`` ``run`` built from a stored row. Text is raw.

    Raises ``CorruptRunError`` when an identity field has no valid value; see the module
    docstring for what else is repaired.
    """
    run_id = _need(_pat("run_id", run.run_id, 16))
    pr = _need(_pat("pr", run.pr, 512))
    repo = _need(_pat("repo_id", run.repo, 255))
    skill = _need(_pat("skill", run.skill, 128))
    mode = _need(_enum(run.mode, MODES))
    head_sha = _need(_pat("sha", run.head_sha, 40))
    created_at = _need(_ts(run.created_at))

    state, reason, exit_code = _outcome(run)
    agent_id = _pat("agent_id", run.agent_id, 11)
    draft_id = _pat("draft_id", run.draft_id, 26)
    anchor = _anchor(run.anchor)
    artifact = _artifact(run)
    summary = _summary(run.summary)
    escalated_at = _ts(run.escalated_at)
    cancel_requested_at = _ts(run.cancel_requested_at)
    finished_at = _ts(run.finished_at)

    incomplete_done = state == "done" and None in (
        artifact,
        summary,
        draft_id,
        anchor,
        finished_at,
    )
    incomplete_escalated = state == "escalated" and (agent_id is None or escalated_at is None)
    if incomplete_done or incomplete_escalated:
        state, reason, exit_code = "failed", "internal", REASONS["internal"].exits[0]
        finished_at = finished_at or created_at

    phase = None
    if state == "running":
        phase = _enum(run.phase, PHASES) or "prefetch"
    if state != "escalated":
        escalated_at = None
    if state in ("running", "escalated"):
        finished_at = None
    if state == "failed":
        finished_at = finished_at or created_at
        if reason == "canceled" and cancel_requested_at is None:
            cancel_requested_at = finished_at
    if state != "done":
        artifact = summary = None
    else:
        cancel_requested_at = None
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "pr": pr,
        "repo": repo,
        "state": state,
        "phase": phase,
        "reason": reason,
        "exit_code": exit_code,
        "mode": mode,
        "skill": skill,
        "labels": _labels(run.labels),
        "previous_run": _pat("run_id", run.previous_run, 11),
        "agent_id": agent_id,
        "csession_id": _pat("csession_id", run.csession_id, 11),
        "draft_id": draft_id,
        "anchor": anchor,
        "head_sha": head_sha,
        "created_at": created_at,
        "escalated_at": escalated_at,
        "cancel_requested_at": cancel_requested_at,
        "finished_at": finished_at,
        "artifact": artifact,
        "summary": summary,
        "failure": _failure(run.failure, reason or "internal") if state == "failed" else None,
        "warnings": _warnings(run.warnings),
        "teardown": _enum(run.teardown, TEARDOWNS),
    }


def _warnings(raw: object) -> list[str]:
    out: list[str] = []
    if isinstance(raw, list):
        for w in raw:
            if isinstance(w, str) and w in WARNINGS and w not in out:
                out.append(w)
    return out


# ---------------------------------------------------------------- human output


def _line(label: str, value: object) -> str:
    return f"{label}: {sanitize_inline('' if value is None else str(value))}"


def human_run(obj: dict[str, Any]) -> str:
    """The run object for a terminal: one ``key: value`` per line, every value sanitized."""
    state = obj["state"]
    head = state + (f" ({obj['reason']})" if obj["reason"] else "")
    lines = [f"run {sanitize_inline(str(obj['run_id']))}: {sanitize_inline(head)}"]
    if obj["phase"]:
        lines.append(_line("phase", obj["phase"]))
    if obj["exit_code"] is not None:
        lines.append(_line("exit code", obj["exit_code"]))
    for key in ("pr", "repo", "mode", "skill", "head_sha", "agent_id", "created_at"):
        lines.append(_line(key.replace("_", " "), obj[key]))
    for key in ("escalated_at", "cancel_requested_at", "finished_at", "previous_run"):
        if obj[key]:
            lines.append(_line(key.replace("_", " "), obj[key]))
    for label in obj["labels"]:
        lines.append(_line("label", label))
    if obj["anchor"]:
        lines.append(_line("anchor head", obj["anchor"]["head_sha"]))
        lines.append(_line("base ref", obj["anchor"]["base_ref"]))
    if obj["artifact"]:
        lines.append(_line("artifact", obj["artifact"]["path"]))
    summary = obj["summary"]
    if summary:
        lines.append(_line("verdict", summary["verdict"]))
        lines.append(_line("risk", summary["risk"]))
        lines.append(_line("coverage", summary["coverage_status"]))
        lines.append(_line("code run", summary["code_run_status"]))
        counts = ", ".join(f"{k} {v}" for k, v in summary["severity_counts"].items() if v)
        lines.append(_line("findings", counts or "none"))
    failure = obj["failure"]
    if failure:
        if failure["tool"]:
            lines.append(_line("failed tool", failure["tool"]))
        if failure["lower_code"]:
            lines.append(_line("lower code", failure["lower_code"]))
        lines.append(_line("retryable", "yes" if failure["retryable"] else "no"))
        for err in failure["errors"]:
            lines.append(_line("error", f"{err['pointer']} {err['kind']}"))
    if obj["warnings"]:
        lines.append(_line("warnings", " ".join(obj["warnings"])))
    return "\n".join(lines) + "\n"


def human_list(objects: list[dict[str, Any]], truncated: bool) -> str:
    """One line per run: id, state, reason, exit code, pr, head, created."""
    rows = []
    for o in objects:
        state = o["state"] + (f"/{o['reason']}" if o["reason"] else "")
        rows.append(
            "  ".join(
                sanitize_inline(str(part), 120)
                for part in (
                    o["run_id"],
                    state,
                    "-" if o["exit_code"] is None else o["exit_code"],
                    o["pr"] or "-",
                    (o["head_sha"] or "")[:12],
                    o["created_at"],
                )
            )
        )
    if truncated:
        rows.append("[more runs match; raise --limit]")
    return "\n".join(rows) + ("\n" if rows else "")


# ---------------------------------------------------------------- list


def _label_filter(text: str) -> str:
    if not text or len(text) > MAX_LABEL or "\x00" in text:
        raise ReviewRunError("usage", "not a valid label filter")
    return text


def _ceil_ms(moment: dt.datetime) -> dt.datetime:
    """``moment`` rounded up to the next millisecond (``format_time`` truncates)."""
    extra = moment.microsecond % 1000
    return moment + dt.timedelta(microseconds=1000 - extra) if extra else moment


def since_cutoff(since: dt.timedelta | dt.datetime | None, now: dt.datetime) -> str | None:
    """``--since`` as a ``format_time`` cutoff (an age counts back from ``now``).

    A cutoff with sub-millisecond precision is rounded up, so it never moves earlier.
    """
    if since is None:
        return None
    if isinstance(since, dt.timedelta):
        try:
            return format_time(_ceil_ms(now - since))
        except OverflowError:  # an age older than year 1: everything matches
            return "0000-00-00T00:00:00.000Z"
    try:
        return format_time(_ceil_ms(since))
    except (OverflowError, ValueError):
        raise ReviewRunError("usage", "not a valid --since time") from None


def list_runs(
    store: Store,
    *,
    states: list[str] | tuple[str, ...] = (),
    pr: str | None = None,
    repo: str | None = None,
    labels: list[str] | tuple[str, ...] = (),
    head: str | None = None,
    since: dt.timedelta | dt.datetime | None = None,
    limit: int = 100,
    now: dt.datetime | None = None,
    on_corrupt: Callable[[int], None] | None = None,
) -> dict[str, Any]:
    """``{schema_version, runs, truncated}``; read-only, no refresh.

    A corrupt row (``CorruptRunError``) is left out of ``runs``; ``on_corrupt`` is called once
    with how many were left out, when any were.
    """
    for label in labels:
        _label_filter(label)
    distinct_labels = tuple(dict.fromkeys(labels))
    if len(distinct_labels) > MAX_LABEL_FILTERS:
        raise ReviewRunError(
            "usage",
            "too many --label filters",
            details={"max": MAX_LABEL_FILTERS},
        )
    if limit < 1:
        raise ReviewRunError("usage", "the limit is out of range")
    limit = min(limit, MAX_LIMIT)
    cutoff = since_cutoff(since, now or dt.datetime.now(dt.UTC))
    ids, truncated = store.list_run_ids(
        states=tuple(dict.fromkeys(states)),
        pr=pr,
        repo=repo,
        labels=distinct_labels,
        head=head,
        since=cutoff,
        limit=limit,
    )
    objects = []
    corrupt = 0
    for run_id in ids:
        run = store.get_run(run_id, tolerant=True) if is_run_id(run_id) else None
        if run is None:
            corrupt += 1
            continue
        try:
            objects.append(run_object(run))
        except CorruptRunError:
            corrupt += 1
    if corrupt and on_corrupt is not None:
        on_corrupt(corrupt)
    return {"schema_version": SCHEMA_VERSION, "runs": objects, "truncated": truncated}


# ---------------------------------------------------------------- show


def show_run(store: Store, ref: str) -> dict[str, Any]:
    """The run object of a run id or prefix (``show --no-refresh``). Read-only.

    A corrupt row raises ``CorruptRunError`` (exit 1).

    TODO(#24): the refresh of ``show`` (finalize, runner check) is added with finalize; until
    then ``show`` always behaves as ``--no-refresh``.
    """
    run_id = store.resolve_run_id(ref)
    run = store.get_run(run_id, tolerant=True)
    if run is None:
        raise ReviewRunError("not_found", "no such run")
    return run_object(run)


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _open_dir(path: str, no_draft: ReviewRunError, dir_fd: int | None = None) -> int:
    """Open a directory without following a link: missing is no draft, a link is unsafe."""
    try:
        return os.open(path, _DIR_FLAGS, dir_fd=dir_fd)
    except FileNotFoundError:
        raise no_draft from None
    except OSError:  # ELOOP (a symlink), ENOTDIR (not a directory), no permission
        raise ReviewRunError("failed", "state path is unsafe", reason="state_unsafe") from None


def read_draft(store: Store, ref: str) -> bytes:
    """The stored ``review.json`` of a ``done`` run, as stored (``show --draft``).

    Not re-hashed: the bytes are printed as stored. Read from ``runs/<run id>/review.json``
    through directory descriptors opened without following links (a link in ``runs/<run id>`` is
    ``state_unsafe``), at most ``MAX_DRAFT_BYTES``. A run id that is not of the schema's form is
    never used as a path. A run with no stored artifact, or a file that is missing, not a
    regular file or too large, is ``not_found`` (exit 10).
    """
    run_id = store.resolve_run_id(ref)
    if not is_run_id(run_id):
        raise ReviewRunError("not_found", "no such run")
    run = store.get_run(run_id, tolerant=True)
    if run is None:
        raise ReviewRunError("not_found", "no such run")
    no_draft = ReviewRunError(
        "not_found", "the run has no stored draft", details={"run_id": run_id}
    )
    try:
        shown = run_object(run)
    except CorruptRunError:
        raise no_draft from None
    if shown["state"] != "done" or shown["artifact"] is None:
        raise no_draft
    runs_fd = _open_dir(os.fspath(store.root / "runs"), no_draft)
    try:
        run_fd = _open_dir(run_id, no_draft, runs_fd)
        try:
            try:
                fd = os.open(
                    "review.json",
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                    dir_fd=run_fd,
                )
            except OSError:
                raise no_draft from None
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_DRAFT_BYTES:
                    raise no_draft
                with os.fdopen(fd, "rb", closefd=False) as handle:
                    data = handle.read(MAX_DRAFT_BYTES + 1)
            finally:
                os.close(fd)
        finally:
            os.close(run_fd)
    finally:
        os.close(runs_fd)
    if len(data) > MAX_DRAFT_BYTES:
        raise no_draft
    return data


def dump(document: object) -> str:
    """The one ``--json`` document, ASCII-escaped."""
    return json.dumps(document, ensure_ascii=True) + "\n"
