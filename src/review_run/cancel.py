"""``cancel``: record the cancellation first, stop what is live, end the run ``canceled``
(interface.md, ``cancel`` and Runs, states and phases; issue #26).

The order of one invocation:

1. A terminal run is reported as it is (``changed: false``, ``action: none``).
2. The run is refreshed as ``show`` refreshes it, an ``escalated`` run too: one whose agent
   finished a newer run outside review-run is finalized (cause ``external_resume``) and reported
   with ``changed: false``, so a finished review is never discarded.
3. A run in phase ``finalize`` is waited for (at most ``LEASE_WAIT_SECONDS``) and reported with
   ``changed: false``.
4. The cancellation is recorded by the store's compare-and-set keyed on the ``(state, phase,
   awaited_run)`` read; a lost race goes back to step 1 with the fresh row. A cancellation that is
   already recorded (an earlier ``cancel`` that died, a signal) is not recorded again: the
   invocation finishes what was started, which makes ``cancel`` idempotent.
5. One action is chosen from a fresh read of the agent: ``stop`` (an agent that is not parked,
   after the ownership check), else SIGTERM to the verified live runner (``signal_runner``), else
   SIGTERM to the verified live launcher (``signal_launcher``), else ``none``. The action is
   taken *before* the lease is requested: a blocking ``run`` holds its lease for the whole
   session and only gives it up once its agent has ended, so waiting for the lease first would
   wait for the very thing ``stop`` causes. Then the lease is taken (a live holder is waited for
   at most ``LEASE_WAIT_SECONDS``: it sees the recorded cancellation and ends the run itself).
6. Holding the lease, the agent is read again and, if it is live, stopped by this invocation;
   a live runner or launcher is signaled and waited for; an agent id that only ``launch.json``
   knows is adopted. Only then the run ends ``canceled`` (``Ender.apply_failure``: the terminal
   transaction, the prompt files, the hook outbox) and the parked agent is torn down after the
   ownership check. A runner that outlives ``RUNNER_WAIT_SECONDS`` is not ended by ``cancel``: the
   row is printed as it is (``running``, ``cancel_requested_at`` set).

The pair of audit ``cancel`` records is opened for every invocation that is not a dry run; the
terminal transaction carries the closing record when this invocation ends the run, otherwise the
invocation writes it itself. Seams: signals while a run is in flight are #27, ``resume`` is #28
and the runner check and label recovery of ``reconcile`` are #29.
"""

from __future__ import annotations

import datetime as dt
import os
import signal
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from . import gateway, procid
from . import launch as lc
from .audit import AuditWriter, Caller, LogWriter
from .ending import Ender
from .errors import ReviewRunError
from .keeper import LeaseKeeper
from .runs import SCHEMA_PATTERNS, run_object
from .settings import load_run_settings
from .store import LEASE_RENEW_SECONDS, Lease, Run, Store, lease_name
from .waiting import Clock, _LazyHooks, refresh_run, resolve, wait_run

LEASE_WAIT_SECONDS: Final = 120.0
RUNNER_WAIT_SECONDS: Final = 120.0
LAUNCHER_WINDOW_SECONDS: Final = 120.0
POLL_SECONDS: Final = 1.0
STOP_GRACE: Final = "10s"
STOP_TIMEOUT: Final = 300.0
CANCEL_EXIT: Final = 143
MAX_ROUNDS: Final = 8
_TERMINAL: Final = ("done", "failed")
_CODE_WORD: Final = "abcdefghijklmnopqrstuvwxyz0123456789_"


def _kill(pid: int) -> None:
    os.kill(pid, signal.SIGTERM)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


@dataclass
class CancelOptions:
    """Who cancels, and the clock, the clock of the wall and the signal sender (tests replace
    them)."""

    caller: Caller
    dry_run: bool = False
    config_path: Path | None = None
    clock: Clock = field(default_factory=Clock)
    now: Callable[[], dt.datetime] = _now
    kill: Callable[[int], None] = _kill
    lease_interval: float = float(LEASE_RENEW_SECONDS)
    shell: tuple[str, ...] | None = None


@dataclass(frozen=True)
class CancelResult:
    """The run as stored after the command, and what the command did."""

    run: Run
    changed: bool
    action: str
    dry_run: bool


def _lower_failed(lower_code: str) -> ReviewRunError:
    return ReviewRunError(
        "failed",
        f"agent-spawn failed ({lower_code})",
        reason="lower_failed",
        tool="agent-spawn",
        lower_code=lower_code,
    )


def _closed_code(doc: dict[str, Any] | None) -> str:
    """The closed ``error.code`` word of an agent-spawn envelope, else ``failed``."""
    error = doc.get("error") if doc else None
    code = error.get("code") if isinstance(error, dict) else None
    if isinstance(code, str) and 1 <= len(code) <= 64 and set(code) <= set(_CODE_WORD):
        return code
    return "failed"


def stop_agent(agent_id: str, actor: str) -> None:
    """``agent-spawn stop <agent> --grace 10s --actor A --json``. Exit 10 (the agent is gone)
    continues; any other failure is ``lower_failed`` (exit 12) with the closed code."""
    argv = ["agent-spawn", "stop", agent_id, "--grace", STOP_GRACE, "--actor", actor, "--json"]
    try:
        done = gateway.run(argv, timeout=STOP_TIMEOUT)
    except gateway.GatewayError as exc:
        raise _lower_failed(exc.lower_code) from None
    if done.returncode in (0, 10):
        return
    raise _lower_failed(_closed_code(lc.parse_document(done.stdout)))


def _identity(pid: int | None, start: str | None) -> procid.ProcIdentity | None:
    if pid is None or start is None or pid == os.getpid():
        return None  # never this process: it is not another invocation's runner
    return procid.ProcIdentity(pid, start)


def _live(ident: procid.ProcIdentity | None) -> bool:
    return ident is not None and procid.check(ident) is True


class _Cancel:
    """One ``cancel`` invocation on one run."""

    def __init__(self, root: Path, run_id: str, store: Store, opts: CancelOptions) -> None:
        self.root, self.run_id, self.store, self.opts = root, run_id, store, opts
        self.caller, self.clock = opts.caller, opts.clock
        self.audit, self.log = AuditWriter(root), LogWriter(root)
        self.attempt: str | None = None
        self.pair_closed = False
        self.recorded = False
        self.action = "none"
        self.signaled: set[tuple[str, int]] = set()
        self.pair_inputs: dict[str, Any] = {}

    # -- reading

    def row(self) -> Run:
        run = self.store.get_run(self.run_id, tolerant=True)
        if run is None:
            raise ReviewRunError("not_found", "no such run")
        return run

    def runner(self, run: Run) -> procid.ProcIdentity | None:
        return _identity(run.runner_pid, run.runner_start)

    def launcher(self, run: Run) -> procid.ProcIdentity | None:
        return _identity(run.launcher_pid, run.launcher_start)

    def launcher_pending(self, run: Run) -> bool:
        """A launcher whose PID is not stored yet, within the window of its start."""
        if run.launcher_pid is not None or run.launch_started_at is None:
            return False
        try:
            started = dt.datetime.fromisoformat(run.launch_started_at)
        except ValueError:
            return False
        age = (self.opts.now() - started).total_seconds()
        return 0 <= age < LAUNCHER_WINDOW_SECONDS and not self.launch_file_usable(run)

    def launch_file_usable(self, run: Run) -> bool:
        return lc.interpret_file(self.store.run_dir(run.run_id) / lc.LAUNCH_FILE) is not None

    # -- the audit pair

    def begin(self, run: Run) -> None:
        obj = run_object(run)
        self.attempt = self.audit.begin(
            "cancel",
            caller=self.caller,
            run_id=run.run_id,
            pr=run.pr,
            inputs={"agent_id": obj["agent_id"], "state": run.state},
        )
        self.pair_inputs = {"agent_id": obj["agent_id"], "state": run.state}

    def close(self, run: Run, error: ReviewRunError | None = None) -> None:
        """Close the pair unless the terminal transaction carried the closing record. Never
        decides the command: a failing audit write is dropped (the opening record stands)."""
        if self.attempt is None or self.pair_closed:
            return
        self.pair_closed = True
        if error is not None:
            result: dict[str, Any] = {"status": "failed", "reason": None, "exit_code": 12}
        elif run.state == "done":
            result = {"status": "done", "reason": None, "exit_code": 0}
        elif run.state == "escalated":
            result = {"status": "escalated", "reason": run.reason, "exit_code": run.exit_code}
        elif run.state == "failed":
            result = {"status": "failed", "reason": run.reason, "exit_code": run.exit_code}
        else:  # still running with the cancellation recorded: it will end canceled
            result = {
                "status": "failed",
                "reason": "canceled",
                "exit_code": run.cancel_exit_code or CANCEL_EXIT,
            }
        try:
            self.audit.end(
                "cancel",
                self.attempt,
                caller=self.caller,
                run_id=run.run_id,
                pr=run.pr,
                inputs=self.pair_inputs,
                result=result,
            )
        except (OSError, ReviewRunError, ValueError):
            pass

    # -- the decision

    def verdict(self, run: Run) -> str:
        """The ownership verdict of the stored agent (``agent-spawn show``)."""
        assert run.agent_id is not None
        found = lc.check_parked(
            run.agent_id,
            run_id=run.run_id,
            pr=run.pr,
            repo=run.repo,
            csession_id=run.csession_id,
        )
        if found == "failed":
            raise _lower_failed("failed")
        return found

    def decide(self, run: Run) -> str:
        """The one action for the facts now (step 5), without taking it."""
        if run.agent_id is not None:
            return "stop" if self.verdict(run) == "live" else "none"
        if _live(self.runner(run)):
            return "signal_runner"
        if _live(self.launcher(run)) or self.launcher_pending(run):
            return "signal_launcher"
        return "none"

    def act(self, run: Run) -> str:
        """Take the action ``decide`` chooses; returns its name."""
        chosen = self.decide(run)
        if chosen == "stop":
            assert run.agent_id is not None
            stop_agent(run.agent_id, self.caller.actor)
        elif chosen in ("signal_runner", "signal_launcher"):
            ident = self.runner(run) if chosen == "signal_runner" else self.launcher(run)
            if ident is not None and (chosen, ident.pid) not in self.signaled and _live(ident):
                self.signaled.add((chosen, ident.pid))
                try:
                    self.opts.kill(ident.pid)
                except ProcessLookupError:
                    pass
        if self.action == "none":
            self.action = chosen
        return chosen

    # -- waiting

    def wait_lease(self) -> Lease | None:
        """The run's lease; a live holder is waited for at most ``LEASE_WAIT_SECONDS``."""
        deadline = self.clock.monotonic() + LEASE_WAIT_SECONDS
        while True:
            lease = self.store.acquire_lease(lease_name(self.run_id))
            if lease is not None:
                return lease
            if self.row().state in _TERMINAL:
                return None
            if self.clock.monotonic() >= deadline:
                return None
            self.clock.sleep(POLL_SECONDS)

    def wait_gone(self, ident: procid.ProcIdentity | None, *, pending: Callable[[], bool]) -> bool:
        """Wait at most ``RUNNER_WAIT_SECONDS`` until the process is gone."""
        deadline = self.clock.monotonic() + RUNNER_WAIT_SECONDS
        while _live(ident) or pending():
            if self.clock.monotonic() >= deadline:
                return False
            self.clock.sleep(POLL_SECONDS)
        return True

    # -- the steps

    def adopt_agent(self, run: Run, lease: Lease) -> Run:
        """An agent id that only ``launch.json`` knows (the runner died after the launcher
        started it) is stored, so that it is stopped and torn down like a stored one. The
        ownership check of the labels still applies before anything touches it."""
        if run.agent_id is not None:
            return run
        found = lc.interpret_file(self.store.run_dir(run.run_id) / lc.LAUNCH_FILE)
        if found is None:
            return run
        agent_id = found.agent_id or (found.envelope.agent_id if found.envelope else None)
        if not agent_id or SCHEMA_PATTERNS["agent_id"].fullmatch(agent_id) is None:
            return run
        csession = found.csession_id if found.envelope is None else None
        self.store.update_fields(
            run.run_id,
            run.key,
            lease=lease,
            agent_id=agent_id,
            csession_id=csession,
        )
        return self.row()

    def quiesce(self, run: Run, lease: Lease) -> Run | None:
        """With the lease held: stop or signal whatever is live. ``None`` when a runner or a
        launcher is still alive after the wait (the row stays as it is)."""
        for _ in range(4):
            run = self.row()
            if run.state in _TERMINAL:
                return run
            run = self.adopt_agent(run, lease)
            chosen = self.act(run)
            if chosen == "signal_runner":
                if not self.wait_gone(self.runner(run), pending=lambda: False):
                    return None
            elif chosen == "signal_launcher":
                if not self.wait_gone(
                    self.launcher(run), pending=lambda r=run: self.launcher_pending(self.row())
                ):
                    return None
            else:
                return self.row()  # none, or the stop of this invocation succeeded
        raise ReviewRunError("internal", "the run kept changing")

    def end(self, run: Run, lease: Lease, keeper: LeaseKeeper) -> Run:
        """Step 6: the terminal transaction and the teardown."""
        settings = load_run_settings(self.store.run_dir(run.run_id))
        hooks = _LazyHooks(self.opts.config_path, run.repo, self.log, run.run_id)
        extra: dict[str, Any] = {}
        if self.opts.shell is not None:
            extra["shell"] = self.opts.shell
        ender = Ender(
            self.store,
            run.run_id,
            lease,
            self.audit,
            self.log,
            self.caller,
            settings,
            attempt_id=self.attempt,
            hooks_config=hooks,
            lost=keeper.lost.is_set,
            teardown_actor=self.caller.actor,
            payload_actor=self.caller.actor,
            pair_action="cancel",
            pair_inputs=self.pair_inputs,
            **extra,
        )
        stored = ender.row()
        code = stored.cancel_exit_code or CANCEL_EXIT
        ended = ender.apply_failure(lc.Disposition("fail", "canceled", code))
        if ender.moved:
            self.pair_closed = True  # the closing record travels with the terminal transaction
            self.recorded = True
        return ended

    def settle(self, run: Run) -> Run:
        """Steps 5 and 6 for a run whose cancellation is recorded."""
        self.act(run)  # unblocks a lease holder: stop, or SIGTERM to the runner or launcher
        lease = self.wait_lease()
        if lease is None:
            return self.row()
        keeper = LeaseKeeper(self.root, lease, self.opts.lease_interval)
        keeper.start()
        try:
            run = self.row()
            if run.state in _TERMINAL:
                return run
            alive = self.quiesce(run, lease)
            if alive is None or alive.state in _TERMINAL:
                return self.row()
            return self.end(alive, lease, keeper)
        finally:
            keeper.stop()
            self.store.release_lease(lease)

    def refreshed(self, run: Run) -> Run:
        """Step 2: the refresh of ``show``, an escalated run included."""
        refresh_run(
            self.root,
            run.run_id,
            by="show",
            config_path=self.opts.config_path,
            escalated=True,
        )
        return self.row()

    def drive(self) -> CancelResult:
        for _ in range(MAX_ROUNDS):
            run = self.row()
            if run.state in _TERMINAL:  # step 1
                return CancelResult(run, False, "none", False)
            run = self.refreshed(run)  # step 2
            if run.state in _TERMINAL:
                return CancelResult(run, False, "none", False)
            if run.phase == "finalize":  # step 3
                wait_run(
                    self.root,
                    run.run_id,
                    timeout=LEASE_WAIT_SECONDS,
                    clock=self.clock,
                    config_path=self.opts.config_path,
                )
                return CancelResult(self.row(), False, "none", False)
            if run.cancel_requested_at is None:  # step 4
                if not self.store.request_cancel(
                    run.run_id, run.key, actor=self.caller.actor, exit_code=CANCEL_EXIT
                ):
                    continue  # the row moved: start again with the fresh one
                self.recorded = True
                run = self.row()
            ended = self.settle(run)  # steps 5 and 6
            return CancelResult(ended, self.recorded, self.action, False)
        raise ReviewRunError("internal", "the run kept changing")

    def plan(self, run: Run) -> CancelResult:
        """``--dry-run``: steps 1 to 3 without a write (no refresh) and the action it would take."""
        if run.state in _TERMINAL or run.phase == "finalize":
            return CancelResult(run, False, "none", True)
        return CancelResult(run, False, self.decide(run), True)


def cancel_run(root: Path, ref: str, opts: CancelOptions) -> CancelResult:
    """Cancel the run ``ref`` (an id or prefix). Raises ``ReviewRunError``: ``not_found`` (10),
    ``ambiguous`` (21), ``lower_failed`` (12: the stop failed, the cancellation stays recorded),
    ``wait_timeout`` (12: finalize did not end) or ``internal``."""
    run_id = resolve(root, ref)
    with Store.open(root) as store:
        job = _Cancel(root, run_id, store, opts)
        run = job.row()
        run_object(run)  # CorruptRunError for a row that cannot be shown: before any write
        if opts.dry_run:
            return job.plan(run)
        job.begin(run)
        try:
            result = job.drive()
        except ReviewRunError as err:
            job.close(job.row(), err)
            raise
        final = job.row()
        job.close(final)
        return CancelResult(final, result.changed, result.action, False)


__all__ = ["CancelOptions", "CancelResult", "cancel_run", "stop_agent"]
