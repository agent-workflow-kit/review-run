"""Ending a run: the terminal (or ``escalated``) transaction, the teardown after it, the hook
delivery after that. Shared by ``run`` (``runner``) and the finalizers of ``show``, ``wait`` and
``reconcile`` (``finalize``), so every writer of a transition builds the same records.

One ``Ender`` belongs to one run and one lease. Its ``end`` is one compare-and-set on the run's
row, with the closing audit record and the hook outbox event in the same transaction; a recorded
cancellation turns any ending into ``failed`` ``canceled``. Teardown (ownership-checked, never
``--force``) and delivery follow the commit and are the duty of the process whose ``end`` moved the
row: ``end`` returns whether it did.

The closing audit record is a ``run`` (or, for ``cancel``, a ``cancel``) record that closes the
invocation's pair when the process that opened the pair ends the run (``attempt_id``), else a
``finalize`` record by ``auto:review-run`` naming the cause and the command (``finalized_by``).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from . import hooks, signals
from . import launch as lc
from .audit import FINALIZER_ACTOR, AuditWriter, Caller, LogWriter, make_audit_record
from .errors import ReviewRunError
from .prompt import remove_prompt_files
from .runs import SCHEMA_PATTERNS
from .settings import RunSettings
from .store import Expect, Lease, NewEvent, Run, Store


def run_inputs(run: Run, settings: RunSettings, *, override: bool) -> dict[str, Any]:
    """The ``inputs`` of the audit ``run`` pair (``schemas/audit.json``)."""
    return {
        "head_sha": run.head_sha,
        "config_sha256": settings.config_sha256,
        "skill": settings.value("skill"),
        "previous_run": run.previous_run,
        "permission_profile_sha256": settings.profile_sha256,
        "session_sandbox": settings.value("sandbox"),
        "setting_sources": list(settings.value("setting_sources")),
        "plugin_dirs_sha256": settings.plugin_dirs_sha256
        if len(settings.plugin_dirs) > 1
        else None,
        "override_escalation": override,
    }


def resume_inputs(run: Run, settings: RunSettings) -> dict[str, Any]:
    """The ``inputs`` of the audit ``resume`` pair: those of ``run`` without
    ``override_escalation`` (a resume has none)."""
    inputs = run_inputs(run, settings, override=False)
    del inputs["override_escalation"]
    return inputs


def valid_agent_id(value: object) -> str | None:
    """``value`` when it is an agent id, else ``None`` (a corrupt stored value counts as absent)."""
    if isinstance(value, str) and SCHEMA_PATTERNS["agent_id"].fullmatch(value) is not None:
        return value
    return None


class Ender:
    """Ends one run under one lease. See the module docstring."""

    def __init__(
        self,
        store: Store,
        run_id: str,
        lease: Lease,
        audit: AuditWriter,
        log: LogWriter,
        caller: Caller,
        settings: RunSettings,
        *,
        attempt_id: str | None = None,
        override: bool = False,
        hooks_config: Mapping[str, Any] | None = None,
        shell: Sequence[str] = hooks.SHELL,
        lost: Callable[[], bool] = lambda: False,
        teardown_actor: str | None = None,
        payload_actor: str | None = None,
        finalized_by: str = "show",
        cause: str = "finalized",
        pair_action: Literal["run", "resume", "cancel"] = "run",
        pair_inputs: Mapping[str, Any] | None = None,
    ) -> None:
        self.store, self.run_id, self.lease = store, run_id, lease
        self.audit, self.log, self.caller, self.settings = audit, log, caller, settings
        self.attempt_id, self.override = attempt_id, override
        self.hooks_config, self.shell, self.lost = hooks_config, shell, lost
        self.teardown_actor = teardown_actor
        self.payload_actor = payload_actor or caller.actor
        self.finalized_by, self.cause = finalized_by, cause
        # ``cancel`` and ``resume`` close their own pairs
        self.pair_action: Literal["run", "resume", "cancel"] = pair_action
        self.pair_inputs = pair_inputs
        self.moved = False  # whether an ``end`` of this Ender moved the row
        self.run_dir: Path = store.run_dir(run_id)

    # -- reading and logging

    def row(self) -> Run:
        run = self.store.get_run(self.run_id)
        if run is None:
            raise ReviewRunError("internal", "the run record is gone")
        return run

    def note(self, event: str, message: str, **kw: Any) -> None:
        try:
            self.log.write(event, message, run_id=self.run_id, **kw)
        except (OSError, ReviewRunError, ValueError):  # a log line never decides a run
            pass

    def check_lease(self) -> None:
        if self.lost():
            raise ReviewRunError("internal", "the run lease is no longer held by this process")

    def verify_lease(self) -> None:
        """The lease check of the database (the keeper's flag lags): before a file of the run
        directory is replaced. ``LeaseLostError`` when another process holds the run."""
        self.check_lease()
        self.store.assert_holds(self.run_id, self.lease)

    def warn(self, code: str) -> None:
        """A closed run warning, with a ``warning`` log record."""
        self.store.add_warning(self.run_id, code, lease=self.lease)
        self.note("warning", f"run warning {code}", level="warn", code=code)

    # -- the terminal transaction

    def _record(
        self, run: Run, state: str, reason: str | None, exit_code: int, event_id: str
    ) -> dict[str, Any]:
        result = {"status": state, "reason": reason, "exit_code": exit_code}
        if self.attempt_id is not None:
            if self.pair_action == "cancel":
                inputs = dict(self.pair_inputs or {})
            elif self.pair_action == "resume":
                inputs = resume_inputs(run, self.settings)
            else:
                inputs = run_inputs(run, self.settings, override=self.override)
            return make_audit_record(
                self.pair_action,
                caller=self.caller,
                run_id=self.run_id,
                pr=run.pr,
                inputs=inputs,
                result=result,
                attempt_id=self.attempt_id,
                event_id=event_id,
            )
        return make_audit_record(
            "finalize",
            caller=Caller.finalizer(),
            run_id=self.run_id,
            pr=run.pr,
            inputs={
                "cause": "agent_gone"
                if reason == "agent_gone" and self.cause == "finalized"
                else self.cause,
                "finalized_by": self.finalized_by,
            },
            result=result,
            event_id=event_id,
        )

    def end(
        self,
        state: str,
        reason: str | None,
        exit_code: int,
        *,
        failure: dict[str, Any] | None = None,
        escalated_run: int | None = None,
        fields: Mapping[str, Any] | None = None,
        teardown_on_cancel: bool = False,
        summary: Mapping[str, Any] | None = None,
        tools_checked: bool | None = None,
        tools_unexpected: int | None = None,
        expect_key: Expect | None = None,
    ) -> tuple[Run, bool]:
        """The terminal (or ``escalated``) transaction: state, the closing audit record and the
        outbox event in one compare-and-set, then the prompt files go. A recorded cancellation
        turns any ending into ``failed`` ``canceled`` with the stored exit code (default 143) and
        no ``failure``; ``teardown_on_cancel`` marks the teardown ``pending`` then (the run has
        an agent). Returns the stored row and whether this call moved it: when another process
        already moved the row, it owns the teardown, warnings and delivery."""
        self.check_lease()
        run = self.row()
        extra = dict(fields or {})
        if run.cancel_requested_at is not None:
            state, reason = "failed", "canceled"
            exit_code = run.cancel_exit_code or 143
            failure = None
            summary = None
            extra = {k: v for k, v in extra.items() if k in ("agent_id", "csession_id")}
            if teardown_on_cancel:
                extra["teardown"] = "pending"
        if state == "failed":
            extra["failure"] = failure
        hook = "post-escalate" if state == "escalated" else "post-run"
        number = escalated_run if state == "escalated" and escalated_run else 1
        event_id = hooks.event_id_for(self.run_id, hook, number)
        record = self._record(run, state, reason, exit_code, event_id)
        agent_id = valid_agent_id(extra.get("agent_id", run.agent_id))  # hook payloads only
        warnings = list(run.warnings)
        if state == "escalated":
            payload = hooks.post_escalate_payload(
                run_id=self.run_id,
                repo=run.repo,
                labels=run.labels,
                event_id=event_id,
                reason=str(reason),
                pr=run.pr,
                agent_id=agent_id,
                actor=self.payload_actor,
                decision_ids=list(run.decision_ids),
                session_sandbox=str(self.settings.value("sandbox")),
                warnings=warnings,
            )
        else:
            anchor = run.anchor
            payload = hooks.post_run_payload(
                run_id=self.run_id,
                repo=run.repo,
                labels=run.labels,
                event_id=event_id,
                state=state,
                reason=reason,
                exit_code=exit_code,
                pr=run.pr,
                agent_id=agent_id,
                draft_id=run.draft_id,
                anchor_head_sha=anchor.get("head_sha") if isinstance(anchor, dict) else None,
                actor=self.payload_actor,
                decision_ids=list(run.decision_ids),
                warnings=warnings,
                summary=summary,
                session_sandbox=str(self.settings.value("sandbox")),
                tools_checked=tools_checked,
                tools_unexpected=tools_unexpected,
            )
        if escalated_run is not None and state == "escalated":
            extra["escalated_run"] = escalated_run
        # A signal that arrived in this section is raised at its end, after the commit and before
        # the caller's teardown or hook delivery began: the run is terminal, ``teardown`` stays
        # ``pending`` and the outbox event waits (todo(#29): ``reconcile`` finishes both).
        with signals.deferred():  # the commit, the prompt files and ``moved`` are one unit
            moved = self.store.end_run(
                self.run_id,
                expect_key if expect_key is not None else run.key,
                lease=self.lease,
                state=state,
                reason=reason,
                exit_code=exit_code,
                fields=extra,
                events=[NewEvent(event_id, hook, payload, (record,))],
            )
            try:
                remove_prompt_files(self.run_dir)
            except OSError:  # the commit stands: teardown and delivery still follow
                self.note("warning", "prompt files could not be removed", level="warn")
            if moved:
                self.moved = True
                self.note("state_change", f"run ended {state} ({reason})", state=state)
            return self.row(), moved

    def deliver(self) -> None:
        """Right after the commit: the audit records of the transition, then the hooks."""
        try:
            hooks.deliver_pending(
                self.store,
                self.audit,
                self.log,
                self.hooks_config,
                run_id=self.run_id,
                shell=self.shell,
            )
        except (OSError, ReviewRunError):  # redelivered by the next reconcile
            pass

    # -- teardown

    def teardown_after(self, agent_id: str, csession_id: str | None, run: Run) -> None:
        """Tear down the parked agent a failed run left (ownership-checked)."""
        if run.state != "failed":
            return
        verdict = lc.check_parked(
            agent_id, run_id=self.run_id, pr=run.pr, repo=run.repo, csession_id=csession_id
        )
        self.finish_teardown(verdict, agent_id, run)

    def finish_teardown(self, verdict: str, agent_id: str, run: Run) -> None:
        """Act on the ownership verdict: ``owned`` is torn down (one audit ``teardown`` record for
        the attempt), the others only recorded: ``teardown`` on the row and a closed warning. A
        ``mismatch`` is never touched; a ``live`` agent is never torn down (agent-spawn refuses)."""
        outcome = verdict
        if verdict == "owned":
            # the verdict may be old (finalize takes a while): check ownership again right now
            outcome = lc.teardown_parked(
                agent_id,
                run_id=self.run_id,
                pr=run.pr,
                repo=run.repo,
                csession_id=run.csession_id,
                actor=self.teardown_actor,
            )
            if outcome not in ("mismatch", "live"):
                self.audit_teardown(
                    agent_id, run, "done" if outcome in ("done", "gone") else "failed"
                )
        status = "done" if outcome in ("done", "gone") else "failed"
        self.store.set_teardown(self.run_id, status, lease=self.lease)
        warning = {
            "refused": "teardown_refused",
            "live": "teardown_refused",
            "failed": "teardown_failed",
            "mismatch": "agent_mismatch",
        }.get(outcome)
        if warning:
            self.warn(warning)

    def audit_teardown(self, agent_id: str, run: Run, status: str) -> None:
        try:
            self.audit.record(
                "teardown",
                caller=self.caller,
                run_id=self.run_id,
                pr=run.pr,
                inputs={"agent_id": agent_id},
                result={"status": status},
            )
        except (OSError, ReviewRunError, ValueError):  # the audit trail never decides a run
            pass

    # -- the endings

    def end_failed(self, reason: str, exit_code: int, failure: dict[str, Any] | None = None) -> Run:
        run, moved = self.end("failed", reason, exit_code, failure=failure)
        if moved:
            self.deliver()
        return run

    def escalate(self, agent: lc.SpawnResult, reason: str, exit_code: int) -> Run:
        """End the run ``escalated`` and keep its agent (a recorded cancellation ends it
        ``canceled`` instead, and its agent is torn down)."""
        run, moved = self.end(
            "escalated", reason, exit_code, escalated_run=agent.run_number, teardown_on_cancel=True
        )
        if moved:
            if run.state == "failed" and agent.agent_id:  # a cancellation was recorded
                self.teardown_after(agent.agent_id, agent.csession_id, run)
            self.deliver()
        return self.row()

    def apply_failure(
        self,
        disposition: lc.Disposition,
        *,
        verdict: str | None = None,
        fields: Mapping[str, Any] | None = None,
    ) -> Run:
        """End the run ``failed``. An agent the run knows (the result's, the envelope's or the
        stored one) is checked for ownership first (``verdict`` when the caller already has it):
        a look-alike is never stored, stopped or torn down and the run ends ``agent_gone`` with
        the warning ``agent_mismatch``."""
        assert disposition.reason is not None and disposition.exit_code is not None
        reason, exit_code, failure = disposition.reason, disposition.exit_code, disposition.failure
        stored = self.row()
        # a stored value that is not an agent id never reaches argv: it counts as absent
        stored_agent = valid_agent_id(stored.agent_id)
        agent = disposition.agent
        agent_id = disposition.teardown_agent or (agent.agent_id if agent else None)
        csession = agent.csession_id if agent else None
        if agent_id is None or (stored_agent is not None and agent_id != stored_agent):
            # never store or tear down an id other than the stored one
            agent_id, csession = stored_agent, (stored.csession_id if stored_agent else None)
        out: dict[str, Any] = dict(fields or {})
        if agent_id is not None and (reason in lc.TEARDOWN_REASONS or verdict == "mismatch"):
            if verdict is None:
                verdict = lc.check_parked(
                    agent_id,
                    run_id=self.run_id,
                    pr=stored.pr,
                    repo=stored.repo,
                    csession_id=csession,
                )
            if verdict == "mismatch":
                reason, exit_code, failure = "agent_gone", 12, None
                out.pop("original_sha256", None)
            else:
                out.update(agent_id=agent_id, teardown="pending")
                if csession:
                    out["csession_id"] = csession
        else:
            verdict = None
            if agent_id is not None and agent_id != stored_agent:
                out["agent_id"] = agent_id
                if csession:
                    out["csession_id"] = csession
        run, moved = self.end(
            "failed",
            reason,
            exit_code,
            failure=failure,
            fields=out,
            teardown_on_cancel=agent_id is not None and verdict != "mismatch",
        )
        if not moved:
            return run  # another process ended the run: it owns the teardown and the delivery
        self.deliver()  # right after the commit; a slow or failing teardown never delays the hook
        if verdict == "mismatch":
            self.warn("agent_mismatch")
        elif verdict is not None and agent_id is not None:
            self.finish_teardown(verdict, agent_id, run)
        elif run.reason == "canceled" and run.teardown == "pending" and agent_id is not None:
            self.teardown_after(agent_id, csession, run)  # a cancellation was recorded meanwhile
        return self.row()


__all__ = ["FINALIZER_ACTOR", "Ender", "resume_inputs", "run_inputs", "valid_agent_id"]
