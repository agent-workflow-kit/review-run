"""Finalize: classify a session's outcome, copy and verify the artifact, the terminal transaction,
teardown and delivery (issue #19; interface.md, Finalize steps 1 to 14).

Two entry points share one pipeline:

* ``finalize_session`` finishes a run the caller already holds the lease of (``run`` and
  ``resume`` call it with the outcome they waited for);
* ``finalize_agent`` is the entry point of ``show``, ``wait`` and ``reconcile`` for a run left
  ``running`` in phase ``session`` (or ``finalize``, after a crashed finalizer): it takes the
  lease, reads ``agent-spawn show`` and finalizes only when the agent is parked, no supervisor is
  alive and its last run is the one the run awaits.

Exactly one finalizer wins: the move into phase ``finalize`` is a compare-and-set that needs the
lease and no recorded cancellation, the terminal transaction is another, and a finalizer that
finds the run already moved returns the stored row and changes nothing (the process whose
terminal transaction committed owns teardown and delivery). A finalizer that died after the copy
or after the terminal transaction is repeated from the files: ``original.json`` and
``review.json`` are replaced atomically, the terminal transaction records ``teardown: pending``
and the hook event in the outbox, so ``reconcile`` can finish teardown and delivery. In code the
order is: classification (step 1), the ownership verdict, the phase compare-and-set, then steps 2
to 14 in phase ``finalize``; the lease is verified in the database before ``original.json`` and
``review.json`` are replaced. After the terminal transaction the hook is delivered first, then the
agent is torn down.

The artifact is untrusted. It is opened with ``O_NOFOLLOW`` and ``O_NONBLOCK`` and fstat-ed (a
regular file with one link), read at most its reported size plus one byte, compared with the
reported size and sha256 while it is copied to a private temporary file (0600), and the copy is
renamed to ``original.json`` and read and hashed again: everything later uses the copy.

Seams, each with its owner (a ``Seams`` left incomplete makes ``finalize_session`` raise
``internal`` before it changes anything, so a ``done`` session stays ``running`` in phase
``session`` for a later finalizer):

* ``Seams.check_session``: the skill and tool checks (step 5), TODO(#22).
* ``Seams.process``: strict parse, session claims, identity, normalization, ``draft check``,
  end head and meta derivation (steps 6 to 12), TODO(#20) and TODO(#21). It returns the bytes of
  ``review.json`` and the run's ``summary``; this module writes ``review.json`` (step 13) and
  runs the terminal transaction (step 14).
"""

from __future__ import annotations

import hashlib
import os
import secrets
import stat
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from . import gateway, hooks
from . import launch as lc
from .audit import FINALIZER_ACTOR, AuditWriter, Caller, LogWriter
from .ending import Ender
from .errors import ReviewRunError
from .fsmodes import FILE_MODE, write_private_file
from .keeper import LeaseKeeper
from .runs import REASONS, WARNINGS
from .runs import summary_of as _summary_of
from .settings import RunSettings, load_run_settings
from .store import LEASE_RENEW_SECONDS, Lease, LeaseLostError, Run, Store, lease_name

ORIGINAL_NAME: Final = "original.json"
REVIEW_NAME: Final = "review.json"
ARTIFACT_NAME: Final = "review.json"
CHUNK: Final = 64 * 1024
FINALIZED_BY: Final = ("show", "wait", "reconcile")


class FinalizeFailure(Exception):
    """A run-ending failure of finalize: a closed ``reason`` of the Reasons table, the closed
    ``failure`` object (never untrusted text) and, optionally, the exit code (the reason's own by
    default)."""

    def __init__(
        self,
        reason: str,
        *,
        failure: dict[str, Any] | None = None,
        errors: Sequence[Mapping[str, str]] = (),
        tool: str | None = None,
        lower_code: str | None = None,
    ) -> None:
        super().__init__(reason)
        info = REASONS[reason]
        self.reason = reason
        self.exit_code = info.exits[0]
        self.failure = failure or lc.failure_object(tool, lower_code, errors=errors)


# -- the seams -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionContext:
    """What the skill and tool checks (step 5) need."""

    run: Run
    settings: RunSettings
    result: lc.SpawnResult
    run_dir: Path


@dataclass(frozen=True)
class SessionCheck:
    """The outcome of step 5: closed warnings and the tool facts of the post-run payload
    (``None``: not checked)."""

    warnings: tuple[str, ...] = ()
    skill_checked: bool | None = None
    tools_checked: bool | None = None
    tools_unexpected: int | None = None


@dataclass(frozen=True)
class ArtifactContext:
    """What validation and meta derivation (steps 6 to 12) need. ``original`` is the verified
    copy's bytes (untrusted); ``original_sha256`` its hash."""

    run: Run
    settings: RunSettings
    result: lc.SpawnResult
    run_dir: Path
    original: bytes
    original_sha256: str
    session: SessionCheck


@dataclass(frozen=True)
class Processed:
    """The product of steps 6 to 12: the bytes of ``review.json`` (ASCII-escaped JSON), the run's
    ``summary`` (the closed shape of ``schemas/defs.json``) and closed warnings."""

    review: bytes
    summary: dict[str, Any]
    warnings: tuple[str, ...] = ()


SessionCheckFn = Callable[[SessionContext], SessionCheck]
ProcessFn = Callable[[ArtifactContext], Processed]


@dataclass(frozen=True)
class Seams:
    check_session: SessionCheckFn | None = None  # TODO(#22): skill and tool checks
    process: ProcessFn | None = None  # TODO(#20), TODO(#21): validation and meta

    @property
    def complete(self) -> bool:
        return self.check_session is not None and self.process is not None


# -- copy and verify -----------------------------------------------------------------------------


def _mismatch() -> FinalizeFailure:
    return FinalizeFailure("artifact_mismatch")


def _open_source(path: str) -> int:
    try:
        return os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        raise FinalizeFailure("artifact_missing") from None
    except OSError:  # a link (ELOOP), no permission, not a directory component, ...
        raise _mismatch() from None


def read_verified(path: Path, *, size: int, sha256: str) -> bytes:
    """The bytes of a private file of the run directory, opened without following links, a
    regular file of exactly ``size`` bytes and ``sha256``; ``artifact_mismatch`` otherwise."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        raise _mismatch() from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size != size:
            raise _mismatch()
        data = _read_at_most(fd, size + 1)
    finally:
        os.close(fd)
    if len(data) != size or hashlib.sha256(data).hexdigest() != sha256:
        raise _mismatch()
    return data


def _read_at_most(fd: int, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while total < limit:
        chunk = os.read(fd, min(CHUNK, limit - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


def copy_artifact(
    ref: lc.ArtifactRef,
    run_dir: Path,
    *,
    max_bytes: int,
    before_commit: Callable[[], None] | None = None,
) -> tuple[bytes, str]:
    """Step 4: copy the frozen artifact to ``<run dir>/original.json`` and return its verified
    bytes and sha256.

    ``artifact_missing``: the file does not exist. ``artifact_mismatch``: any other way the file is
    not the reported one (a link or other non-regular file, more than one hard link, a size or
    hash that differs, a read error). ``artifact_invalid``: the reported size is above
    ``max_bytes`` (nothing is read). The copy is written to a private temporary file (0600,
    ``O_EXCL|O_NOFOLLOW``), fsynced and renamed, then read back and hashed again; a destination
    error propagates (``internal``). ``before_commit`` runs after the temporary file is complete
    and before it replaces ``original.json`` (the lease check). Whenever the copy fails with an
    artifact failure no ``original.json`` is left: a file whose hash the row does not store."""
    try:
        return _copy_artifact(ref, run_dir, max_bytes, before_commit)
    except FinalizeFailure:
        if before_commit is not None:
            before_commit()  # a holder that lost the lease removes nothing (LeaseLostError wins)
        (run_dir / ORIGINAL_NAME).unlink(missing_ok=True)
        raise


def _copy_artifact(
    ref: lc.ArtifactRef,
    run_dir: Path,
    max_bytes: int,
    before_commit: Callable[[], None] | None,
) -> tuple[bytes, str]:
    if ref.name != ARTIFACT_NAME or os.path.basename(ref.path) != ref.name:
        raise FinalizeFailure("artifact_missing")
    if ref.size > max_bytes:
        raise FinalizeFailure("artifact_invalid", errors=[{"pointer": "", "kind": "length"}])
    source = _open_source(ref.path)
    tmp = run_dir / f".{ORIGINAL_NAME}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    try:
        info = os.fstat(source)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != ref.size:
            raise _mismatch()
        digest = hashlib.sha256()
        total = 0
        out = os.open(
            tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, FILE_MODE
        )
        try:
            os.fchmod(out, FILE_MODE)
            while total <= ref.size:  # one byte more than reported shows a file that grew
                try:
                    chunk = os.read(source, min(CHUNK, ref.size + 1 - total))
                except OSError:
                    raise _mismatch() from None
                if not chunk:
                    break
                digest.update(chunk)
                total += len(chunk)
                view = memoryview(chunk)
                while view:
                    view = view[os.write(out, view) :]
            if total != ref.size or digest.hexdigest() != ref.sha256:
                raise _mismatch()
            os.fsync(out)
        finally:
            os.close(out)
        if before_commit is not None:
            before_commit()
        os.replace(tmp, run_dir / ORIGINAL_NAME)
    finally:
        os.close(source)
        tmp.unlink(missing_ok=True)
    data = read_verified(run_dir / ORIGINAL_NAME, size=ref.size, sha256=ref.sha256)
    return data, ref.sha256


# -- the pipeline --------------------------------------------------------------------------------


def _usable(run: Run, result: lc.SpawnResult) -> bool:
    """Whether ``result`` is the outcome this run finalizes: its agent and session, a final run
    at least as high as the awaited one and above the run numbers already consumed."""
    number = result.run_number
    if number is None or run.agent_id is None or run.awaited_run is None:
        return False
    if result.agent_id != run.agent_id:
        return False
    if run.csession_id is not None and result.csession_id != run.csession_id:
        return False
    if number < run.awaited_run:
        return False
    return all(m is None or number > m for m in (run.escalated_run, run.finalized_run))


def _closed_warnings(codes: Sequence[str]) -> list[str]:
    for code in codes:
        if code not in WARNINGS:
            raise ReviewRunError("internal", "a finalize step produced an unknown warning")
    return list(codes)


def _check_processed(processed: Processed) -> None:
    if not isinstance(processed.review, bytes) or not processed.review.isascii():
        raise ReviewRunError("internal", "review.json is not ASCII bytes")
    if _summary_of(processed.summary) is None:
        raise ReviewRunError("internal", "the run summary is not of the closed shape")


def _check_session(check: SessionCheck) -> None:
    def flag(value: object) -> bool:
        return value is None or type(value) is bool  # noqa: E721

    count = check.tools_unexpected
    if (
        not isinstance(check, SessionCheck)
        or not flag(check.skill_checked)
        or not flag(check.tools_checked)
        or not (count is None or (type(count) is int and count >= 0))  # noqa: E721
        or not isinstance(check.warnings, tuple)
    ):
        raise ReviewRunError("internal", "the session check is not of the closed shape")


def finalize_session(
    ender: Ender,
    result: lc.SpawnResult,
    seams: Seams,
    *,
    verdict: str | None = None,
) -> Run:
    """Finalize the run of ``ender`` (its lease held) for ``result``, the last run's outcome.

    Returns the run as stored. A run that is not ``running`` in phase ``session`` or
    ``finalize``, or an outcome that is not the run's awaited one, changes nothing. ``verdict`` is
    the ownership verdict of the agent when the caller already has it (``lc.parked_verdict``); else
    ``agent-spawn show`` is read. ``live``: nothing changes. A ``show`` that fails raises
    ``lower_failed`` (exit 12, the run unchanged).

    Step 1 (``escalated`` for 13, ``failed`` for the other non-``done`` outcomes) ends the run
    without entering phase ``finalize``. A ``done`` outcome whose agent is gone (``torn_down``,
    unknown) ends the run ``agent_gone``, and a look-alike agent ``agent_gone`` with a warning,
    both before anything is copied. Otherwise the run moves into phase ``finalize`` (a
    compare-and-set that fails while a cancellation is recorded: the run then ends
    ``canceled``) and steps 2 and 3 (implicit done, missing artifact) and the rest follow."""
    store, run_id = ender.store, ender.run_id
    run = ender.row()
    if run.state != "running" or run.phase not in ("session", "finalize"):
        return run
    if not _usable(run, result):
        return run
    disposition = lc.session_disposition(result)
    if disposition.kind == "escalate":
        assert disposition.reason and disposition.exit_code
        return ender.escalate(result, disposition.reason, disposition.exit_code)
    if disposition.kind != "finalize":
        return ender.apply_failure(disposition)
    if not seams.complete:
        # TODO(#20, #21, #22): until the checks land a done session stays ``running`` in phase
        # ``session`` (agent stored), exactly as before this issue.
        raise ReviewRunError("internal", "finalize is not complete: artifact checks are pending")
    assert run.agent_id is not None
    if verdict is None:
        verdict = lc.check_parked(
            run.agent_id,
            run_id=run_id,
            pr=run.pr,
            repo=run.repo,
            csession_id=run.csession_id,
        )
    if verdict == "live":
        return run
    if verdict == "failed":
        raise ReviewRunError(
            "failed",
            "agent-spawn failed (failed)",
            reason="lower_failed",
            tool="agent-spawn",
            lower_code="failed",
        )
    if verdict == "mismatch":
        return ender.apply_failure(
            lc.Disposition("fail", "agent_gone", 12, agent=result), verdict=verdict
        )
    if verdict == "gone":
        return ender.apply_failure(lc.Disposition("fail", "agent_gone", 12), verdict=verdict)
    if run.cancel_requested_at is not None:
        return _canceled(ender, result)
    if run.phase == "session":
        ender.check_lease()
        if not store.begin_finalize(run_id, run.key, lease=ender.lease):
            run = ender.row()
            if run.state == "running" and run.cancel_requested_at is not None:
                return _canceled(ender, result)
            return run  # another process moved the row: its state stands
        ender.note("phase_change", "phase finalize", phase="finalize")
    return _finalize_done(ender, result, seams, verdict)


def _canceled(ender: Ender, result: lc.SpawnResult) -> Run:
    run = ender.row()
    code = run.cancel_exit_code or 143
    return ender.apply_failure(lc.Disposition("fail", "canceled", code, agent=result))


def _finalize_done(ender: Ender, result: lc.SpawnResult, seams: Seams, verdict: str) -> Run:
    """Steps 2 to 14 for a ``done`` outcome, the run in phase ``finalize``."""
    assert seams.check_session is not None and seams.process is not None
    run = ender.row()
    run_dir = ender.run_dir
    extra: dict[str, Any] = {}
    try:
        report = result.report
        if report is not None and report.implicit:
            raise FinalizeFailure("implicit_done")
        if report is None or report.artifact is None:
            raise FinalizeFailure("artifact_missing")
        max_bytes = int(ender.settings.value("limits.max_artifact_bytes"))
        ender.verify_lease()  # also when re-entering phase finalize after a crashed finalizer
        original, original_sha = copy_artifact(
            report.artifact, run_dir, max_bytes=max_bytes, before_commit=ender.verify_lease
        )
        extra["original_sha256"] = original_sha
        ender.check_lease()
        session = seams.check_session(SessionContext(run, ender.settings, result, run_dir))
        _check_session(session)
        ender.check_lease()
        processed = seams.process(
            ArtifactContext(run, ender.settings, result, run_dir, original, original_sha, session)
        )
        _check_processed(processed)
        warnings = _closed_warnings([*session.warnings, *processed.warnings])
        review_path = run_dir / REVIEW_NAME
        write_private_file(review_path, processed.review, before_commit=ender.verify_lease)
    except FinalizeFailure as exc:
        return ender.apply_failure(
            lc.Disposition("fail", exc.reason, exc.exit_code, exc.failure, agent=result),
            verdict=verdict,
            fields=extra,
        )
    except LeaseLostError:
        raise
    except Exception:  # an unexpected failure ends the run once, never a crash loop
        if ender.lost():
            raise  # not the lease holder any more: the holder decides
        return _internal(ender, result, verdict, extra)
    for code in warnings:
        ender.warn(code)
    try:
        run, moved = _end_done(ender, result, session, processed, extra, review_path)
    except LeaseLostError:
        raise
    except Exception:  # a payload that cannot be built ends the run once, never a crash loop
        if ender.lost():
            raise
        try:
            run = ender.row()
        except Exception:
            return _internal(ender, result, verdict, extra)
        if run.state not in ("done", "failed"):
            return _internal(ender, result, verdict, extra)
        moved = True  # the commit stood and a later step failed: delivery and teardown follow
    if not moved:
        return run  # another process ended the run: it owns the teardown and the delivery
    ender.deliver()  # right after the commit: a slow or failing teardown never delays the hook
    assert run.agent_id is not None
    if run.state == "failed":  # a cancellation recorded after the last check converted the end
        ender.teardown_after(run.agent_id, run.csession_id, run)
    else:
        ender.finish_teardown(verdict, run.agent_id, run)
    return ender.row()


def _end_done(
    ender: Ender,
    result: lc.SpawnResult,
    session: SessionCheck,
    processed: Processed,
    extra: Mapping[str, Any],
    review_path: Path,
) -> tuple[Run, bool]:
    return ender.end(
        "done",
        None,
        0,
        teardown_on_cancel=True,
        fields={
            **extra,
            "artifact_path": str(review_path),
            "artifact_sha256": hashlib.sha256(processed.review).hexdigest(),
            "artifact_size": len(processed.review),
            "summary": processed.summary,
            "finalized_run": result.run_number,
            "teardown": "pending",
        },
        summary=processed.summary,
        tools_checked=session.tools_checked,
        tools_unexpected=session.tools_unexpected,
    )


def _internal(ender: Ender, result: lc.SpawnResult, verdict: str, extra: dict[str, Any]) -> Run:
    """An unexpected exception in finalize ends the run ``failed`` (``internal``) once."""
    return ender.apply_failure(
        lc.Disposition("fail", "internal", 12, agent=result), verdict=verdict, fields=extra
    )


# -- the entry point of show, wait and reconcile ---------------------------------------------------


@dataclass
class AgentFinalize:
    """What ``finalize_agent`` needs beyond the run: who acts, and with which config."""

    finalized_by: str
    seams: Seams = field(default_factory=Seams)
    cause: str = "finalized"
    hooks_config: Mapping[str, Any] | None = None  # the acting process's config ``hooks``
    shell: Sequence[str] = hooks.SHELL
    lease_interval: float | None = None


def _show(agent_id: str) -> tuple[int, dict[str, Any]] | None:
    try:
        done = gateway.run(["agent-spawn", "show", agent_id, "--json"], timeout=lc.SHOW_TIMEOUT)
    except gateway.GatewayError:
        return None
    doc = lc.parse_document(done.stdout)
    if doc is None and done.returncode != 10:
        return None
    return done.returncode, doc or {}


def outcome_from_show(run: Run, doc: Mapping[str, Any]) -> lc.SpawnResult | None:
    """The last run's outcome from an ``agent-spawn show`` document, when the run can be
    finalized now: the agent is ``parked``, no supervisor is alive, the last run has an outcome
    and is the run's awaited one (or later). ``None`` otherwise (nothing to do yet)."""
    agent, runs = doc.get("agent"), doc.get("runs")
    if not isinstance(agent, dict) or agent.get("status") != "parked":
        return None
    supervisor = doc.get("supervisor")
    if isinstance(supervisor, dict) and supervisor.get("alive") is True:
        return None
    if not isinstance(runs, list) or not runs or not isinstance(runs[-1], dict):
        return None
    outcome = runs[-1].get("outcome")
    if not isinstance(outcome, dict) or outcome.get("phase") not in ("launch", "session"):
        return None
    code = outcome.get("exit_code")
    if type(code) is not int:  # noqa: E721
        return None
    shaped = {
        "agent": {
            "agent_id": agent.get("agent_id"),
            "csession_id": outcome.get("csession_id"),
            "status": agent.get("status"),
            "run": outcome.get("run"),
        },
        "phase": outcome.get("phase"),
        "outcome": outcome.get("outcome"),
        "end_reason": outcome.get("end_reason"),
        "exit_code": code,
        "run": outcome,
    }
    result = lc.parse_result(code, shaped)
    if result is None or not result.final or not _usable(run, result):
        return None
    return result


def finalize_agent(root: Path, run_id: str, opts: AgentFinalize) -> Run | None:
    """Finalize a run left ``running`` in phase ``session`` (or ``finalize``) whose agent has
    ended; the entry point of ``show``, ``wait`` and ``reconcile`` (TODO(#24), TODO(#29) call it).

    Returns the stored row after whatever happened, or ``None`` when the run does not exist. Nothing
    changes (the row is returned as read) when the run is not in a finalizable place, its lease is
    held by a live holder (never waited for), ``agent-spawn show`` fails, or the agent has not ended
    its awaited run (live, supervised, parked on an older run). A torn-down or unknown agent ends
    the run ``agent_gone``; a look-alike agent is never touched (``agent_gone`` and the warning
    ``agent_mismatch``)."""
    if opts.finalized_by not in FINALIZED_BY:
        raise ValueError("finalized_by is show, wait or reconcile")
    with Store.open(root) as store:
        run = store.get_run(run_id)
        if run is None or not _finalizable(run):
            return run
        lease = store.acquire_lease(lease_name(run_id))
        if lease is None:
            return run
        keeper = LeaseKeeper(root, lease, opts.lease_interval or float(LEASE_RENEW_SECONDS))
        keeper.start()
        try:
            return _finalize_held(root, store, run_id, lease, keeper, opts)
        finally:
            keeper.stop()
            store.release_lease(lease)


def _finalizable(run: Run) -> bool:
    return (
        run.state == "running"
        and run.phase in ("session", "finalize")
        and run.agent_id is not None
        and run.csession_id is not None
        and run.awaited_run is not None
    )


def _finalize_held(
    root: Path,
    store: Store,
    run_id: str,
    lease: Lease,
    keeper: LeaseKeeper,
    opts: AgentFinalize,
) -> Run | None:
    run = store.get_run(run_id)  # re-read under the lease: another finalizer may have finished
    if run is None or not _finalizable(run):
        return run
    assert run.agent_id is not None
    settings = load_run_settings(store.run_dir(run_id))
    ender = Ender(
        store,
        run_id,
        lease,
        AuditWriter(root),
        LogWriter(root),
        Caller.finalizer(),
        settings,
        hooks_config=opts.hooks_config,
        shell=opts.shell,
        lost=keeper.lost.is_set,
        teardown_actor=run.actor,
        payload_actor=run.actor or FINALIZER_ACTOR,
        finalized_by=opts.finalized_by,
        cause=opts.cause,
    )
    shown = _show(run.agent_id)
    if shown is None:
        return run
    code, doc = shown
    shown_agent = doc.get("agent")
    gone = code == 10 or (
        isinstance(shown_agent, dict) and shown_agent.get("status") == "torn_down"
    )
    if gone:
        return ender.apply_failure(lc.Disposition("fail", "agent_gone", 12))
    if code != 0:
        return run
    verdict = lc.parked_verdict(
        doc, run_id=run_id, pr=run.pr, repo=run.repo, csession_id=run.csession_id
    )
    if verdict == "mismatch":
        return ender.apply_failure(lc.Disposition("fail", "agent_gone", 12), verdict=verdict)
    result = outcome_from_show(run, doc)
    if result is None:
        return run
    return finalize_session(ender, result, opts.seams, verdict=verdict)


__all__ = [
    "AgentFinalize",
    "ArtifactContext",
    "FinalizeFailure",
    "Processed",
    "Seams",
    "SessionCheck",
    "SessionContext",
    "copy_artifact",
    "finalize_agent",
    "finalize_session",
    "outcome_from_show",
]
