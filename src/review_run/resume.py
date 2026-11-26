"""``resume``: answer an ``escalated`` run (interface.md, Resume; issue #28).

The order of one invocation (the step numbers are the interface's):

0. The ``--hook`` entries are checked (usage), the run is resolved, the answer is read (a file,
   or stdin for ``-``; at most twice ``limits.max_answer_bytes`` of the run's stored settings, as
   bytes) and the resume prompt is built (step 4's sanitizing and cap: an answer that is too large
   or empty is a usage error, exit 2, before any state changes). The answer travels in the prompt
   file only, never in argv or env.
1. The run is refreshed as ``show`` refreshes it, an ``escalated`` run too (an agent that already
   finished a newer run is finalized, cause ``external_resume``), then the run's lease is taken (a
   holder that is a finalizer is waited for a few seconds), and the run must still be
   ``escalated`` with no cancellation recorded, else exit 20 ``not_escalated``. From here the
   audit ``resume`` pair is open.
2. and 3. ``agent-spawn show``: a torn-down, unknown or look-alike agent ends the run ``failed``
   (``agent_gone``, exit 12, the run object); a live one is exit 20 ``live``.
4. The plugin directories are re-hashed (``plugin_changed``, exit 12, the run unchanged).
5. ``store.begin_resume``: the one compare-and-set from ``escalated`` to ``running``; the loser of
   a race exits 20. No call to agent-spawn happens before it.
6. ``agent-spawn resume`` (``launch.resume_argv``, every value from the stored settings).
7. A started run: ``store.resume_started`` (with the resume's actor, hooks and decision ids);
   then the cancellation check, the blocking wait and the finalize of ``run`` (``--no-wait``
   returns here). Anything else is reverted with ``store.resume_revert`` and exits with the lower
   tool's code as an envelope (the run is unchanged).

Seams, each with its owner:

* TODO(#29): a ``resume`` that dies between step 5 and the start of the launcher (or whose launcher
  was ended by the gateway with no usable ``resume.json`` while the agent's last run is not
  settled) leaves the run ``running`` in phase ``session`` with ``awaited_run`` one above the
  agent's last run and the escalation record kept; ``reconcile`` finds the agent still parked on
  the older run and calls ``store.resume_revert`` (or adopts the run the agent did start).
* The audit pair of a ``resume`` that returns ``--no-wait``, or that fails after the session
  started, stays open (as ``run``'s does): a later ``show``, ``wait`` or ``reconcile`` writes the
  ``finalize`` record.
"""

from __future__ import annotations

import datetime as dt
import os
import signal
import stat
import sys
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from . import gateway, hooks, procid, signals
from . import launch as lc
from .audit import AuditWriter, LogWriter
from .ending import Ender, resume_inputs, valid_agent_id
from .errors import ReviewRunError
from .keeper import LeaseKeeper
from .prompt import (
    AnswerTooLarge,
    PromptError,
    PromptFiles,
    build_resume_prompt,
    remove_prompt_files,
    write_resume_prompt,
)
from .runner import (
    Interrupted,
    Options,
    SessionFlow,
    _end_canceled,
    _interrupted,
    _merge_ids,
    _record_cancel,
)
from .runs import CorruptRunError, run_object
from .settings import (
    PluginChanged,
    launch_env,
    load_run_settings,
    plugin_changed_error,
    verify_plugin_dirs,
)
from .store import Expect, Lease, Run, Store, format_time, lease_name
from .waiting import _LazyHooks, refresh_run, resolve

DEFAULT_ANSWER_LIMIT: Final = 65536  # limits.max_answer_bytes' default (the stored value wins)
LEASE_ATTEMPTS: Final = 30  # a finalizer that holds the lease is waited for this many polls
LEASE_POLL_SECONDS: Final = 1.0
LATE_RESUME_ATTEMPTS: Final = 3  # refresh rounds when an external resume lands in the window
_SIBLINGS: Final = frozenset(
    {"forgectl", "pr-comment", "agent-spawn", "claude-session", "wtctl", "termctl"}
)
_AGENT_SPAWN_REASONS: Final = frozenset(
    {"live", "tearing_down", "third_party_cwd", "third_party_run"}
)


@dataclass
class ResumeOptions(Options):
    """What the command line decides: ``Options`` (caller, ``--hook`` entries, ``--no-wait``, the
    seams and the clock of tests), the ``--actor`` flag as given (agent-spawn receives it only
    when it was given) and the ``--config`` path (read lazily, for ``hooks`` only)."""

    actor_flag: str | None = None
    config_path: Path | None = None


class ResumeFlow(SessionFlow):
    """One ``resume`` in this process. ``handed`` is true while the run is ``running`` because of
    this invocation: the audit pair is then closed by whichever ending moves the row."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.awaited = 0  # the run number this resume waits for (the agent's last run plus one)
        self.handed = False
        self.closed = False  # the pair was closed by this invocation
        self.final_result: lc.SpawnResult | None = None  # a result that already ended its run

    @property
    def pair_open(self) -> bool:
        return not (self.closed or self.handed or self.ender.moved)


def _now() -> str:
    return format_time(dt.datetime.now(dt.UTC))


# -- errors --------------------------------------------------------------------------------------


def _not_escalated(run: Run) -> ReviewRunError:
    return ReviewRunError(
        "refused",
        "the run is not escalated",
        reason="not_escalated",
        details={"state": run.state},
    )


def _lower_failed(lower_code: str, retryable: bool = False) -> ReviewRunError:
    return ReviewRunError(
        "failed",
        f"agent-spawn failed ({lower_code})",
        reason="lower_failed",
        tool="agent-spawn",
        lower_code=lower_code,
        details={"retryable": True} if retryable else None,
    )


def _lower_error(code: int, tool: str, lower_code: str, retryable: bool = False) -> ReviewRunError:
    """The envelope of an ``agent-spawn resume`` that did not start a run (Resume, failures):
    20 is ``refused`` (``lower_refused``), 11 ``vetoed`` and 13 ``escalated`` (both with
    ``hook: null`` and the lower tool), anything else ``lower_failed``."""
    if code == 20:
        return ReviewRunError(
            "refused",
            f"{tool} refused the resume ({lower_code})",
            reason="lower_refused",
            tool=tool,
            lower_code=lower_code,
        )
    if code == 11:
        return ReviewRunError(
            "vetoed", f"{tool} vetoed the resume", tool=tool, lower_code=lower_code
        )
    if code == 13:
        return ReviewRunError(
            "escalated", f"{tool} escalated the resume", tool=tool, lower_code=lower_code
        )
    return ReviewRunError(
        "failed",
        f"{tool} failed ({lower_code})",
        reason="lower_failed",
        tool=tool,
        lower_code=lower_code,
        details={"retryable": True} if retryable else None,
    )


def _envelope_error(code: int, env: lc.Envelope) -> ReviewRunError:
    tool = env.tool if env.tool in _SIBLINGS else None
    lower = env.lower_code or env.code
    if code == 20:
        lower = env.reason or lower
        if tool is None:
            tool = "agent-spawn" if lower in _AGENT_SPAWN_REASONS else "claude-session"
    return _lower_error(code, tool or "agent-spawn", lower, env.retryable)


def _result_error(code: int, found: lc.SpawnResult) -> ReviewRunError:
    """A run that ended in its launch phase (``not-started``, ``vetoed``, ``escalated``)."""
    outcome = (found.outcome or "not-started").replace("-", "_")
    hook = found.outcome in ("vetoed", "escalated") or (found.end_reason or "").startswith("hook_")
    return _lower_error(code, "agent-spawn" if hook else "claude-session", outcome)


# -- the answer ----------------------------------------------------------------------------------


def _unreadable() -> ReviewRunError:
    return ReviewRunError("usage", "the prompt file cannot be read")


def read_answer(source: str, limit: int) -> bytes:
    """The answer's bytes: the regular file ``source``, or stdin for ``-``; at most
    ``2 * limit + 1`` bytes are read (sanitizing never shrinks text below half its size, so more
    can never fit: ``build_resume_prompt`` refuses it). Any failure to read is a usage error with
    a closed message (the path is not echoed); an answer with nothing but whitespace is one too."""
    size = 2 * limit + 1
    try:
        if source == "-":
            data = sys.stdin.buffer.read(size)
        else:
            fd = os.open(source, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise _unreadable()
                with os.fdopen(fd, "rb", closefd=False) as handle:
                    data = handle.read(size)
            finally:
                os.close(fd)
    except (OSError, ValueError, AttributeError):
        raise _unreadable() from None
    if _blank(data):
        raise ReviewRunError("usage", "the answer is empty")
    return data


def _blank(data: bytes) -> bool:
    """Nothing but whitespace (Unicode included) and control or format characters, NULs among
    them, which the sanitizer would only mark: the answer says nothing."""
    text = data.decode("utf-8", errors="replace")
    return all(ch.isspace() or unicodedata.category(ch).startswith("C") for ch in text)


def _answer_limit(run_dir: Path) -> int:
    """``limits.max_answer_bytes`` of the run's stored settings (the default when they cannot be
    read: the strict load of step 1 reports that later)."""
    try:
        return int(load_run_settings(run_dir).value("limits.max_answer_bytes"))
    except (ReviewRunError, KeyError, ValueError, TypeError):
        return DEFAULT_ANSWER_LIMIT


def build_prompt(run_id: str, answer: bytes, limit: int) -> PromptFiles:
    """The resume prompt; ``AnswerTooLarge`` is a usage error (exit 2)."""
    try:
        return build_resume_prompt(run_id=run_id, answer=answer, max_answer_bytes=limit)
    except AnswerTooLarge:
        raise ReviewRunError(
            "usage", "the answer is above limits.max_answer_bytes", details={"max": limit}
        ) from None
    except PromptError:
        raise ReviewRunError("internal", "the resume prompt could not be built") from None


# -- step 1: the refresh and the lease -------------------------------------------------------------


def _refresh(root: Path, run_id: str, opts: ResumeOptions) -> None:
    """The refresh of ``show``, an ``escalated`` run included (``cancel`` does the same): a failed
    refresh continues with the stored row (the checks below read the agent again); a row that
    cannot be shown raises."""
    try:
        refresh_run(root, run_id, by="show", config_path=opts.config_path, escalated=True)
    except CorruptRunError:
        raise
    except ReviewRunError:
        pass


def _take_lease(
    store: Store, run_id: str, opts: ResumeOptions, held: list[Lease]
) -> tuple[Lease, Run]:
    """The lease of an ``escalated`` run with no recorded cancellation, and the row read under
    it. A holder (a finalizer: ``show``, ``wait``, ``reconcile``, another ``resume``) is waited
    for ``LEASE_ATTEMPTS`` polls; one that is still there then is exit 12 ``wait_timeout`` (the run
    unchanged), a row that is no longer ``escalated`` exit 20 ``not_escalated``. A lease taken
    is in ``held`` at once (no signal falls between taking it and the caller's ``finally``)."""
    for attempt in range(LEASE_ATTEMPTS + 1):
        row = store.get_run(run_id, tolerant=True)
        if row is None:
            raise ReviewRunError("not_found", "no such run")
        if row.state != "escalated" or row.cancel_requested_at is not None:
            raise _not_escalated(row)
        with signals.deferred(raise_on_exit=False):
            lease = store.acquire_lease(lease_name(run_id))
            if lease is not None:
                held.append(lease)
        if lease is not None:
            under = store.get_run(run_id, tolerant=True)
            if (
                under is not None
                and under.state == "escalated"
                and under.cancel_requested_at is None
            ):
                return lease, under
            raise _not_escalated(under or row)  # the caller's ``finally`` releases the lease
        if attempt == LEASE_ATTEMPTS:
            raise ReviewRunError(
                "failed",
                "the run's lease is held by another process",
                reason="wait_timeout",
                details={"retryable": True},
            )
        opts.sleep(LEASE_POLL_SECONDS)
    raise AssertionError("unreachable")  # pragma: no cover


class _Stale(ReviewRunError):
    """The agent finished a newer run after the refresh (a late external resume): the run is
    refreshed again (finalizing it) before anything else happens."""

    def __init__(self) -> None:
        super().__init__(
            "refused", "the agent moved on", reason="not_escalated", details={"state": "escalated"}
        )


# -- the agent -----------------------------------------------------------------------------------


def _show_agent(run: Run, agent_id: str) -> tuple[str, int]:
    """``agent-spawn show`` and the ownership check (Labels): the verdict (``owned``, ``gone``,
    ``mismatch``, ``live``) and the agent's last run number. ``lower_failed`` when ``show`` fails
    or its answer has no run number."""
    try:
        shown = gateway.run(["agent-spawn", "show", agent_id, "--json"], timeout=lc.SHOW_TIMEOUT)
    except gateway.GatewayError as exc:
        raise _lower_failed(exc.lower_code) from None
    if shown.returncode == 10:
        return "gone", 0
    doc = lc.parse_document(shown.stdout)
    if shown.returncode != 0 or doc is None:
        raise _lower_failed("failed")
    verdict = lc.parked_verdict(
        doc, run_id=run.run_id, pr=run.pr, repo=run.repo, csession_id=run.csession_id
    )
    if verdict == "gone":
        return "gone", 0
    agent = doc.get("agent")
    number = agent.get("run") if isinstance(agent, dict) else None
    if type(number) is not int or number < 1:  # noqa: E721
        raise _lower_failed("invalid_output")
    return verdict, number


# -- closing the pair ----------------------------------------------------------------------------


def _error_result(err: ReviewRunError) -> dict[str, Any]:
    status = {"vetoed": "vetoed", "escalated": "escalated"}.get(err.code, "failed")
    return {"status": status, "reason": None, "exit_code": err.exit_code}


def _run_result(run: Run) -> dict[str, Any]:
    if run.state in ("done", "failed", "escalated"):
        return {"status": run.state, "reason": run.reason, "exit_code": run.exit_code}
    return {"status": "failed", "reason": None, "exit_code": 12}


def _close_pair(flow: ResumeFlow, result: Mapping[str, Any]) -> None:
    """Close the pair this invocation opened, unless the terminal transaction carried the closing
    record. Never decides the command: a failing audit write is dropped."""
    if not flow.pair_open:
        return
    flow.closed = True
    try:
        row = flow.row()
        flow.audit.end(
            "resume",
            flow.attempt_id,
            caller=flow.opts.caller,
            run_id=flow.run_id,
            pr=row.pr,
            inputs=resume_inputs(row, flow.settings),
            result=result,
        )
    except (OSError, ReviewRunError, ValueError):
        pass


# -- the revert ----------------------------------------------------------------------------------


def _revert(flow: ResumeFlow, err: ReviewRunError, *, escalated_run: int | None = None) -> Run:
    """The reverse compare-and-set: the run goes back to ``escalated`` with its escalation
    values and ``err`` is raised (an envelope: the run did not change). When a cancellation was
    recorded meanwhile the revert is refused and the run ends ``canceled`` (the agent is parked),
    which is returned. ``escalated_run`` records a run that ended in its launch phase, so that no
    finalizer takes it as the outcome."""
    if escalated_run is not None:
        flow.store.update_fields(
            flow.run_id,
            Expect("running", "session", flow.awaited),
            lease=flow.lease,
            escalated_run=escalated_run,
        )
    if flow.store.resume_revert(flow.run_id, lease=flow.lease, awaited_run=flow.awaited):
        flow.handed = False
        flow.launched = False
        flow.note("state_change", "resume reverted", state="escalated")
        raise err
    row = flow.row()
    if row.state == "running" and row.cancel_requested_at is not None:
        return _end_canceled(flow, row.cancel_exit_code or 143)
    raise ReviewRunError("internal", "the run changed while the resume was reverted")


def _gone(flow: ResumeFlow, verdict: str) -> Run:
    """The agent is torn down, unknown or a look-alike: the run ends ``failed`` (``agent_gone``);
    a look-alike is never touched and the run gets the warning ``agent_mismatch``."""
    return flow.ender.apply_failure(
        lc.Disposition("fail", "agent_gone", 12),
        verdict="mismatch" if verdict == "mismatch" else None,
    )


# -- the steps -----------------------------------------------------------------------------------


def _ids(row: Run, opts: ResumeOptions) -> list[str]:
    return _merge_ids([x for x in row.decision_ids if isinstance(x, str)], opts.caller.decision_ids)


def _launch_resume(flow: ResumeFlow, row: Run, agent_id: str, ids: Sequence[str]) -> Run | int:
    """Step 6: the call. Returns the number of the run that started (the run is not final yet), or
    the run as stored when this step ended it; raises the envelope after a revert."""
    opts = flow.opts
    assert isinstance(opts, ResumeOptions)
    argv = lc.resume_argv(
        flow.settings,
        agent_id,
        run_dir=flow.run_dir,
        actor=opts.actor_flag,
        decision_ids=ids,
    )
    result_file = flow.run_dir / lc.RESUME_FILE
    result_file.unlink(missing_ok=True)  # a stale file must not stand in for this call
    key = Expect("running", "session", flow.awaited)

    def started(pid: int) -> None:
        ident = procid.identity_of(pid)
        flow.store.update_fields(
            flow.run_id,
            key,
            lease=flow.lease,
            launcher_pid=pid,
            launcher_start=None if ident is None else ident.start,
        )

    flow.check_lease()
    try:
        done = gateway.run(
            argv,
            timeout=lc.LAUNCH_TIMEOUT,
            env=launch_env(flow.settings),
            on_start=started,
            signal_stop=gateway.SignalStop(signal.SIGTERM, lc.LAUNCH_TERM_GRACE),
        )
    except gateway.ToolMissing:
        return _revert(
            flow,
            ReviewRunError(
                "failed",
                "agent-spawn is not installed",
                reason="tool_missing",
                tool="agent-spawn",
                lower_code="tool_missing",
            ),
        )
    except gateway.GatewayError as exc:
        found = lc.interpret_file(result_file)
        if found is None:
            return _unsettled(flow, row, agent_id, exc)
        code, found_result = found.exit_code, found
    else:
        code = done.returncode
        found_result = lc.interpret(code, done.stdout, result_file)
    return _classify(flow, row, agent_id, code, found_result)


def _unsettled(flow: ResumeFlow, row: Run, agent_id: str, exc: gateway.GatewayError) -> Run:
    """The call was ended by the gateway (a timeout, too much output) and left no usable
    ``resume.json``: a run may have started. When the agent is parked on an older run than
    ``awaited``, none did and the resume is reverted; otherwise the run stays ``running`` (the next
    finalizer or ``reconcile`` (TODO(#29)) settles it) and ``lower_failed`` is raised."""
    err = _lower_failed(exc.lower_code)
    try:
        verdict, last = _show_agent(row, agent_id)
    except ReviewRunError:
        raise err from None
    if verdict == "owned" and last < flow.awaited:
        return _revert(flow, err)
    raise err


def _classify(
    flow: ResumeFlow,
    row: Run,
    agent_id: str,
    code: int,
    found: lc.SpawnResult | None,
) -> Run | int:
    """What agent-spawn's answer means (Resume step 7 and the failures table)."""
    if found is None:
        return _revert(flow, _lower_failed("invalid_output"))
    if found.envelope is not None:
        if code == 10:
            return _gone(flow, "gone")
        return _revert(flow, _envelope_error(code, found.envelope))
    if found.agent_id != agent_id or found.csession_id != row.csession_id:
        return _revert(flow, _lower_failed("invalid_output"))  # not this run's session
    if found.phase == "launch" and found.final:
        # the run ended in its launch phase: it is the escalation's newer run number
        return _revert(flow, _result_error(code, found), escalated_run=found.run_number)
    number = found.run_number if found.final else found.agent_run
    if number is None or number < flow.awaited or (not found.final and code != 0):
        return _revert(flow, _lower_failed("invalid_output"))
    if found.final:
        flow.final_result = found
    return number


def _steps(flow: ResumeFlow, row: Run, files: PromptFiles, entries: Sequence[str]) -> Run:
    """Steps 2 to 7."""
    opts = flow.opts
    assert isinstance(opts, ResumeOptions)
    store = flow.store
    agent_id = valid_agent_id(row.agent_id)
    if agent_id is None or row.csession_id is None:
        raise ReviewRunError("internal", "the run has no stored agent")
    verdict, last = _show_agent(row, agent_id)
    if verdict in ("gone", "mismatch"):
        return _gone(flow, verdict)  # step 2: the run ends, exit 12 with the run object
    if verdict == "live":
        raise ReviewRunError("refused", "the agent is live", reason="live")  # step 3
    if row.awaited_run is not None and last > row.awaited_run:
        raise _Stale()  # a late external resume: the refresh finalizes it, the retry reports it
    try:
        verify_plugin_dirs(flow.settings)  # step 4
    except PluginChanged:
        raise plugin_changed_error() from None
    flow.awaited = last + 1
    flow.check_lease()
    # step 5: the first effect; no call to agent-spawn happens before it
    try:
        moved = store.begin_resume(flow.run_id, row.key, lease=flow.lease, awaited_run=flow.awaited)
    except signals.SignalExit:
        # the signal may be the deferred exit of the commit: when the row moved because of this
        # resume the run is ours to end (the handed path), otherwise nothing changed
        current = flow.row()
        if (
            current.state == "running"
            and current.phase == "session"
            and current.awaited_run == flow.awaited
        ):
            flow.handed = True
        raise
    if not moved:
        raise _not_escalated(flow.row())
    flow.handed = True
    flow.note("state_change", "run resumed", state="running")
    try:
        write_resume_prompt(flow.run_dir, files)
    except OSError:
        return _revert(flow, ReviewRunError("internal", "the prompt file could not be written"))
    key = Expect("running", "session", flow.awaited)
    if not store.update_fields(
        flow.run_id, key, lease=flow.lease, no_cancel=True, launch_started_at=_now()
    ):
        current = flow.row()
        if current.state == "running" and current.cancel_requested_at is not None:
            return _end_canceled(flow, current.cancel_exit_code or 143)
        return _revert(
            flow, ReviewRunError("internal", "the run changed before the resume was launched")
        )
    flow.launched = True
    ids = _ids(row, opts)
    launched = _launch_resume(flow, row, agent_id, ids)  # step 6
    if isinstance(launched, Run):
        return launched
    # step 7
    fields = {
        "actor": opts.caller.actor,
        "hooks": hooks.add_flag_hooks(row.hooks, entries),
        "decision_ids": ids,
    }
    if not store.resume_started(
        flow.run_id,
        lease=flow.lease,
        previous_awaited=flow.awaited,
        awaited_run=launched,
        fields=fields,
    ):
        raise ReviewRunError("internal", "the run changed while the resume was stored")
    flow.ender.payload_actor = opts.caller.actor  # the session started: this resume's actor
    flow.note("phase_change", "phase session", phase="session")
    current = flow.row()
    if current.cancel_requested_at is not None:
        return _end_canceled(flow, current.cancel_exit_code or 143)
    final = flow.final_result
    if final is not None:  # a run that already ended (agent-spawn did not leave it started)
        ended = flow.apply(lc.session_disposition(final))
        return flow.row() if ended is None else ended
    if opts.no_wait:
        return current
    return flow.wait_for_session()


def _drive(flow: ResumeFlow, row: Run, files: PromptFiles, entries: Sequence[str]) -> Run:
    """``_steps`` under the signal handling and the pair bookkeeping."""
    try:
        signals.checkpoint()  # a signal that arrived while the pair was opened
        with signals.on_exit(lambda sig: _record_cancel(flow, sig)):
            run = _steps(flow, row, files, entries)
        _close_pair(flow, _run_result(run))
        return run
    except Interrupted:
        raise
    except signals.SignalExit as sig:
        if not flow.handed:
            _close_pair(
                flow, {"status": "failed", "reason": None, "exit_code": int(sig.code or 143)}
            )
            raise
        return _interrupted(flow, sig)
    except ReviewRunError as err:
        _close_pair(flow, _error_result(err))
        raise
    except Exception:
        err = ReviewRunError("internal", "unexpected error")
        if flow.handed and not flow.launched:
            try:
                flow.store.resume_revert(flow.run_id, lease=flow.lease, awaited_run=flow.awaited)
                flow.handed = False
            except Exception:  # noqa: BLE001 - the original failure is the one reported
                pass
        _close_pair(flow, _error_result(err))
        raise err from None
    finally:
        remove_prompt_files(flow.run_dir)  # on every path: the answer never stays on disk


def _resume_once(
    root: Path, run_id: str, files: PromptFiles, entries: Sequence[str], opts: ResumeOptions
) -> Run:
    """Steps 1 (the lease) to 7 once; ``_Stale`` when the agent moved on after the refresh."""
    with Store.open(root) as store:
        held: list[Lease] = []
        keeper: LeaseKeeper | None = None
        flow: ResumeFlow | None = None
        driven = False
        try:
            lease, row = _take_lease(store, run_id, opts, held)
            with signals.deferred(raise_on_exit=False):
                keeper = LeaseKeeper(root, lease, opts.lease_interval)
                keeper.start()
            run_object(row)  # CorruptRunError for a row that cannot be shown: before any write
            settings = load_run_settings(store.run_dir(run_id))
            audit, log = AuditWriter(root), LogWriter(root)
            with signals.deferred(raise_on_exit=False):
                attempt = audit.begin(
                    "resume",
                    caller=opts.caller,
                    run_id=run_id,
                    pr=row.pr,
                    inputs=resume_inputs(row, settings),
                )
                ender = Ender(
                    store,
                    run_id,
                    lease,
                    audit,
                    log,
                    opts.caller,
                    settings,
                    attempt_id=attempt,
                    hooks_config=_LazyHooks(opts.config_path, row.repo, log, run_id),
                    shell=opts.shell,
                    lost=keeper.lost.is_set,
                    teardown_actor=opts.actor_flag,
                    # an ending before the session starts reports the run's stored actor
                    payload_actor=row.actor,
                    pair_action="resume",
                )
                flow = ResumeFlow(
                    store, audit, log, opts, run_id, lease, keeper, attempt, root, settings, ender
                )
            driven = True
            return _drive(flow, row, files, entries)
        except signals.SignalExit as sig:
            if flow is None or driven:
                raise
            return _interrupted(flow, sig)  # the window between the section and ``_drive``
        finally:
            signals.ignore_rest()  # the lease is released whatever arrives now
            if keeper is not None:
                keeper.stop()
            for lease_held in held:
                store.release_lease(lease_held)


def resume_run(root: Path, ref: str, source: str, opts: ResumeOptions) -> Run:
    """``resume`` for the run ``ref`` with the answer in ``source`` (a path, or ``-``); returns the
    run as stored when this process is done with it: ``done``, ``failed`` (also ``agent_gone`` and
    ``canceled``), ``escalated`` again or, for ``--no-wait``, ``running`` with its session
    started. Raises ``ReviewRunError`` (an envelope: the run did not change, except
    ``lower_failed`` after the session started) and ``Interrupted`` for a waiter ended by a
    signal."""
    entries = hooks.add_flag_hooks([], opts.flag_hooks)  # usage (exit 2) before anything is read
    for entry in entries:
        if entry.split("=", 1)[0] not in hooks.POST_HOOKS:
            raise ReviewRunError("usage", "resume accepts only post-run and post-escalate hooks")
    run_id = resolve(root, ref)
    with Store.open(root) as store:
        limit = _answer_limit(store.run_dir(run_id))
    files = build_prompt(run_id, read_answer(source, limit), limit)
    for _ in range(LATE_RESUME_ATTEMPTS):
        _refresh(root, run_id, opts)  # step 1, before this process holds a connection
        try:
            return _resume_once(root, run_id, files, entries, opts)
        except _Stale:
            continue
    _refresh(root, run_id, opts)
    with Store.open(root) as store:
        row = store.get_run(run_id, tolerant=True)
    if row is None:
        raise ReviewRunError("not_found", "no such run")
    raise _not_escalated(row)


__all__ = ["ResumeOptions", "build_prompt", "read_answer", "resume_run"]
