"""Repairs of one run against reality: the part of ``reconcile`` that ``show`` and ``wait`` apply to
the one run they look at (interface.md, ``reconcile``, ``show`` and ``wait``; issue #29).

``recover_run`` looks at one ``running`` run (an ``escalated`` one too with ``escalated``) and does
at most the repairs below, each under the run's lease taken without waiting (a live holder means
"not now"; exactly one actor), after reading the facts again under it:

* a run with an agent: ``finalize.finalize_agent`` (a finished session, a torn-down or look-alike
  agent, a crashed finalizer);
* an ``escalated`` run: the same finalize check (an agent that finished a newer run), then
  ``agent_gone`` (a torn-down, unknown or look-alike agent) and ``expired`` (older than
  ``escalation_ttl``; a live agent, a human answering it, is skipped);
* a ``running`` run with no agent whose runner and launcher are both gone: adopt the agent the
  launcher's ``launch.json`` names (after the ownership check), map a failed launch the way ``run``
  would have, or end it ``runner_lost``; while the runner or the launcher is alive, or cannot be
  told (no ``/proc``), or the launcher is within its 120 s window, nothing changes;
* a recorded cancellation whose runner, launcher and agent are no longer live: end ``canceled``;
* a ``resume`` that died after moving the run and before storing the started session: adopt the
  run the agent did start, or revert with ``Store.resume_revert``.

Nothing here signals a process or stops an agent. Every ending goes through ``ending.Ender`` (the
terminal transaction with its audit record and outbox event, then delivery, then teardown), so
the exactly-once rules are the ones of ``finalize``. ``dry_run`` performs the reads and reports what
it would change.

Seams, each with its owner:

* TODO(#30): label recovery. A run in phase ``session`` that began its launch (``launch_started_at``
  set), has no agent and no usable ``launch.json`` may own an agent nobody stored; it is left
  ``running`` (never ended ``runner_lost``) until the label lookup exists.
* TODO(#32): the removal of a throwaway of a run that ended ``runner_lost`` in ``prefetch`` or
  ``code_run``.
"""

from __future__ import annotations

import datetime as dt
import os
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, cast

from . import hooks as hooks_mod
from . import launch as lc
from . import signals
from .audit import FINALIZER_ACTOR, AuditWriter, Caller, LogWriter
from .config import load_config
from .durations import DurationError, parse_duration
from .ending import Ender, valid_agent_id
from .errors import ReviewRunError
from .finalize import AgentFinalize, Seams, finalize_agent, outcome_from_show, show_agent
from .keeper import LeaseKeeper
from .lazyhooks import LazyHooks
from .liveness import (
    launcher_in_window,
    launcher_state,
    parse_time,
    runner_state,
    stored_agent,
)
from .prompt import remove_prompt_files
from .runs import REASONS, STATES, TEARDOWNS, run_object
from .settings import RunSettings, load_run_settings
from .store import LEASE_RENEW_SECONDS, Lease, Run, Store, is_run_id, lease_name

LAUNCH_WINDOW: Final = dt.timedelta(seconds=120)
_REASON_KIND: Final = {
    "runner_lost": "runner_lost",
    "agent_gone": "agent_gone",
    "expired": "expired",
    "canceled": "canceled",
}


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class _Lazy:
    pass


LAZY: Final = _Lazy()


@dataclass(frozen=True)
class Repair:
    """One change (or, in a dry run, planned change): ``kind`` is a value of the ``changes[].kind``
    enum of ``schemas/cli/reconcile.json``; ``details`` hold closed codes and ids only."""

    kind: str
    run_id: str | None
    details: dict[str, Any] = field(default_factory=dict[str, Any])

    def as_json(self) -> dict[str, Any]:
        return {"run_id": self.run_id, "kind": self.kind, "details": dict(self.details)}


@dataclass
class RecoverOptions:
    """Who acts and how. ``by`` is the audit ``finalized_by`` (``show``, ``wait``, ``reconcile``).
    ``hooks`` is the acting process's config ``hooks`` (``None``: read lazily, per run);
    ``ttl`` is ``escalation_ttl`` (``LAZY``: read from the config the first time it is needed; a
    config that does not load means no expiry)."""

    by: str = "show"
    config_path: Path | None = None
    hooks: Mapping[str, Any] | None = None
    ttl: dt.timedelta | None | _Lazy = LAZY
    now: Callable[[], dt.datetime] = _utcnow
    dry_run: bool = False
    lease_interval: float = float(LEASE_RENEW_SECONDS)
    shell: Sequence[str] = hooks_mod.SHELL
    seams: Seams | None = None
    # also judge an ``escalated`` run by its agent and age (``agent_gone``, ``expired``): ``show``
    # and ``reconcile`` do; ``cancel`` and ``resume`` (which decide those themselves) finalize only
    judge_escalated: bool = False

    def escalation_ttl(self) -> dt.timedelta | None:
        """``escalation_ttl`` as a duration; only ``null`` means no expiry (``0`` expires an
        escalated run whose age is above zero, at the first pass after its escalation)."""
        if not isinstance(self.ttl, _Lazy):
            return self.ttl
        value: dt.timedelta | None = None
        try:
            raw = load_config(self.config_path).resolve(None).values.get("escalation_ttl")
            if isinstance(raw, str):
                value = parse_duration(raw)
        except (ReviewRunError, DurationError):
            value = None
        self.ttl = value
        return value


@dataclass(frozen=True)
class _Plan:
    repair: Repair
    data: dict[str, Any] = field(default_factory=dict[str, Any])


@dataclass
class _Held:
    lease: Lease | None
    keeper: LeaseKeeper | None


def _lower_failed(code: str) -> ReviewRunError:
    return ReviewRunError(
        "failed",
        f"agent-spawn failed ({code})",
        reason="lower_failed",
        tool="agent-spawn",
        lower_code=code,
    )


def closed(value: object, allowed: Sequence[str] | Mapping[str, Any]) -> str | None:
    """``value`` when it is one of the closed ``allowed`` words, ``None`` for ``None``, else
    ``"unknown"``: stored text never reaches the output as it is stored."""
    if value is None:
        return None
    return value if isinstance(value, str) and value in allowed else "unknown"


def _ended(row: Run) -> Repair:
    """The change of a run that ended (or escalated) in this pass, from the stored row."""
    kind = _REASON_KIND.get(row.reason or "", "finalized") if row.state == "failed" else "finalized"
    return Repair(
        kind,
        row.run_id,
        {"state": closed(row.state, STATES), "reason": closed(row.reason, REASONS)},
    )


_parse = parse_time


class _Recovery:
    def __init__(self, root: Path, store: Store, opts: RecoverOptions) -> None:
        self.root, self.store, self.opts = root, store, opts
        self.dry = opts.dry_run
        self.audit, self.log = AuditWriter(root), LogWriter(root)

    # -- reading

    def row(self, run_id: str) -> Run | None:
        return self.store.get_run(run_id, tolerant=True)

    def launch_path(self, run_id: str) -> Path:
        """``launch.json`` of the run; a read never creates the run directory."""
        if not is_run_id(run_id):
            raise ValueError("not a run id")
        return self.root / "runs" / run_id / lc.LAUNCH_FILE

    def hooks_for(self, run: Run) -> Mapping[str, Any]:
        if self.opts.hooks is not None:
            return self.opts.hooks
        return LazyHooks(self.opts.config_path, run.repo, self.log, run.run_id)

    # -- the lease and the guarded step

    @contextmanager
    def held(self, run_id: str) -> Iterator[_Held | None]:
        """The run's lease, never waited for; ``None`` when a live process holds it. A dry run
        only checks that it could be taken."""
        name = lease_name(run_id)
        if self.dry:
            yield _Held(None, None) if self.store.lease_available(name) else None
            return
        lease = self.store.acquire_lease(name)
        if lease is None:
            yield None
            return
        keeper = LeaseKeeper(self.root, lease, self.opts.lease_interval)
        keeper.start()
        try:
            yield _Held(lease, keeper)
        finally:
            keeper.stop()
            self.store.release_lease(lease)

    def guarded(
        self,
        run: Run,
        decide: Callable[[Run], _Plan | None],
        act: Callable[[Run, _Plan, _Held], list[Repair]],
    ) -> list[Repair]:
        """Decide from reads, take the lease, read and decide again under it, then act. A row that
        moved in between is left for the next pass."""
        plan = decide(run)
        if plan is None:
            return []
        with self.held(run.run_id) as held:
            if held is None:
                return []
            if self.dry:
                self.dry_ready(plan.repair)
                return [plan.repair]
            fresh = self.row(run.run_id)
            if fresh is None or fresh.key != run.key:
                return []
            plan = decide(fresh)
            if plan is None:
                return []
            return self.uninterrupted(lambda: act(fresh, plan, held))

    def dry_ready(self, repair: Repair) -> None:
        """What the real repair needs before it writes, checked in a dry run too, so that a run the
        real pass skips (a missing run directory or ``settings.json``) is not planned."""
        if repair.run_id is None:
            return
        if repair.kind in ("runner_lost", "agent_gone", "expired", "canceled", "finalized"):
            load_run_settings(self.need_dir(repair.run_id))
        elif repair.kind == "teardown_retried":
            self.need_dir(repair.run_id)

    def need_dir(self, run_id: str) -> Path:
        """The run's directory, which must exist (a repair never creates one)."""
        path = self.root / "runs" / run_id
        if not path.is_dir():
            raise ReviewRunError("internal", "the run directory is missing")
        return path

    @staticmethod
    def uninterrupted(work: Callable[[], list[Repair]]) -> list[Repair]:
        """From the first write on a signal does not interrupt a repair (interface.md, Signals:
        a ``wait`` that ends the run itself keeps its outcome); when nothing changed a pending
        signal is raised now and later ones stay accepted. It acts only where a guard is
        installed (``wait``): ``reconcile`` and ``show`` install none, so a repair they began that
        is cut short is recovered by the next pass."""
        try:
            with signals.deferred(raise_on_exit=False):
                repairs = work()
        except BaseException:
            signals.ignore_rest()
            raise
        if repairs:
            signals.ignore_rest()
        else:
            signals.checkpoint()
        return repairs

    def ender(self, run: Run, held: _Held, cause: str) -> Ender:
        assert held.lease is not None and held.keeper is not None
        settings = load_run_settings(self.need_dir(run.run_id))
        return self._ender(run, held, settings, cause)

    def _ender(self, run: Run, held: _Held, settings: RunSettings, cause: str) -> Ender:
        assert held.lease is not None and held.keeper is not None
        self.need_dir(run.run_id)
        return Ender(
            self.store,
            run.run_id,
            held.lease,
            self.audit,
            self.log,
            Caller.finalizer(),
            settings,
            hooks_config=self.hooks_for(run),
            shell=self.opts.shell,
            lost=held.keeper.lost.is_set,
            teardown_actor=run.actor,
            payload_actor=run.actor or FINALIZER_ACTOR,
            finalized_by=self.opts.by,
            cause=cause,
        )

    def finish(self, before: Run, after: Run | None, *, moved: bool) -> list[Repair]:
        """The change this call made: reported only when ``moved`` (this call's own terminal
        transition), never for what another process finalized meanwhile."""
        if not moved or after is None or after.state == "running":
            return []
        return [_ended(after)]

    # -- a run with an agent

    def agent_finalize(self, run: Run, *, escalated: bool = False) -> AgentFinalize:
        extra: dict[str, Any] = {}
        if self.opts.shell is not hooks_mod.SHELL:
            extra["shell"] = self.opts.shell
        if self.opts.seams is not None:
            extra["seams"] = self.opts.seams
        return AgentFinalize(
            finalized_by=self.opts.by,
            hooks_config=self.hooks_for(run),
            escalated=escalated,
            **extra,
        )

    def with_agent(self, run: Run) -> list[Repair]:
        if run.phase not in ("session", "finalize"):
            return []
        if self.dry:
            return self._plan_agent(run)
        opts = self.agent_finalize(run)
        after = finalize_agent(self.root, run.run_id, opts)
        return self.finish(run, after, moved=opts.moved)

    def _plan_agent(self, run: Run) -> list[Repair]:
        agent = stored_agent(run)
        assert agent is not None
        shown = show_agent(agent)
        if shown is None:
            return []
        code, doc = shown
        agent_doc = doc.get("agent")
        gone = code == 10 or (
            isinstance(agent_doc, dict) and agent_doc.get("status") == "torn_down"
        )
        kind: str | None = None
        reason: str | None = None
        if gone:
            kind, reason = "agent_gone", "agent_gone"
        elif code == 0 and run.csession_id is not None:
            verdict = lc.parked_verdict(
                doc, run_id=run.run_id, pr=run.pr, repo=run.repo, csession_id=run.csession_id
            )
            if verdict == "mismatch":
                kind, reason = "agent_gone", "agent_gone"
            elif outcome_from_show(run, doc) is not None:
                kind = "finalized"
        if kind is None:
            return []
        with self.held(run.run_id) as held:
            if held is None:
                return []
        planned = Repair(kind, run.run_id, {"state": None if kind == "finalized" else "failed",
                                          "reason": reason})  # fmt: skip
        self.dry_ready(planned)
        return [planned]

    # -- a run with no agent

    def without_agent(self, run: Run) -> list[Repair]:
        repairs = self.guarded(run, self._decide_no_agent, self._act_no_agent)
        if not self.dry and any(r.kind == "agent_adopted" for r in repairs):
            fresh = self.row(run.run_id)
            if fresh is not None and fresh.state == "running":
                repairs += self.with_agent(fresh)
        return repairs

    def _gone(self, run: Run) -> bool:
        """The runner and the launcher are both verifiably gone, and the launcher is not still
        within its start window (``unknown`` counts as alive)."""
        if runner_state(run) != "gone" or launcher_state(run) != "gone":
            return False
        usable = lc.interpret_file(self.launch_path(run.run_id)) is not None
        return not launcher_in_window(run, self.opts.now(), launch_file_usable=usable)

    def _decide_no_agent(self, run: Run) -> _Plan | None:
        if run.state != "running" or run.agent_id is not None:
            return None
        if not self._gone(run):
            return None
        if run.cancel_requested_at is not None:
            # a cancellation recorded after the first read is decided like any recorded one
            plan = self._decide_canceled(run, no_agent=True)
            return None if plan is None else _Plan(plan.repair, {**plan.data, "canceled": True})
        found = lc.interpret_file(self.launch_path(run.run_id))
        if found is not None:
            return self._plan_launch_file(run, found)
        if run.launch_started_at is not None:
            return None  # TODO(#30): an agent may exist; label recovery is not implemented yet
        return _Plan(
            Repair("runner_lost", run.run_id, {"state": "failed", "reason": "runner_lost"}),
            {"end": "runner_lost"},
        )

    def _plan_launch_file(self, run: Run, found: lc.SpawnResult) -> _Plan | None:
        agent_id = valid_agent_id(
            found.agent_id or (found.envelope.agent_id if found.envelope else None)
        )
        disposition = lc.map_launch(found, found.exit_code)
        failed = Repair("finalized", run.run_id, {"state": "failed", "reason": disposition.reason})
        if agent_id is None:
            return (
                _Plan(failed, {"disposition": disposition}) if disposition.kind == "fail" else None
            )
        csession = found.csession_id if found.envelope is None else None
        verdict = lc.check_parked(
            agent_id, run_id=run.run_id, pr=run.pr, repo=run.repo, csession_id=csession
        )
        if verdict == "failed":
            raise _lower_failed("failed")
        if verdict in ("mismatch", "gone"):
            gone = Repair("agent_gone", run.run_id, {"state": "failed", "reason": "agent_gone"})
            return _Plan(gone, {"gone": verdict, "agent_id": agent_id})
        if disposition.kind == "fail":
            return _Plan(failed, {"disposition": disposition})
        awaited = found.run_number if found.final else found.agent_run
        if awaited is None:
            return None
        adopted = Repair("agent_adopted", run.run_id, {"agent_id": agent_id})
        data = {"adopt": agent_id, "csession": csession, "awaited": run.awaited_run or awaited}
        return _Plan(adopted, data)

    def _act_no_agent(self, run: Run, plan: _Plan, held: _Held) -> list[Repair]:
        data = plan.data
        if data.get("canceled"):
            return self._end_canceled(run, plan, held)
        if "adopt" in data:
            assert held.lease is not None
            stored = self.store.update_fields(
                run.run_id,
                run.key,
                lease=held.lease,
                no_cancel=True,
                agent_id=data["adopt"],
                csession_id=data["csession"],
                awaited_run=data["awaited"],
            )
            return [plan.repair] if stored else []
        ender = self.ender(run, held, "runner_lost" if "end" in data else "finalized")
        if "gone" in data:
            ender.cause = "agent_gone"
            disposition = lc.Disposition(
                "fail",
                "agent_gone",
                12,
                teardown_agent=data["agent_id"] if data["gone"] == "mismatch" else None,
            )
            after = ender.apply_failure(
                disposition, verdict="mismatch" if data["gone"] == "mismatch" else None
            )
        elif "disposition" in data:
            after = ender.apply_failure(data["disposition"])
        else:
            after = ender.end_failed("runner_lost", 12)
        return self.finish(run, after, moved=ender.moved)

    # -- a recorded cancellation

    def canceled(self, run: Run) -> list[Repair]:
        if run.state == "running" and not self._gone(run):
            return []
        return self.guarded(run, self._decide_canceled, self._end_canceled)

    def _decide_canceled(self, run: Run, *, no_agent: bool = False) -> _Plan | None:
        """End ``canceled`` once nothing of the run is live: no runner or launcher (checked by
        the caller for a ``running`` run) and an agent that is not live."""
        if run.cancel_requested_at is None:
            return None
        if run.state == "running" and (
            (run.phase == "finalize" and not no_agent) or not self._gone(run)
        ):
            return None
        plan = _Plan(Repair("canceled", run.run_id, {"state": "failed", "reason": "canceled"}))
        agent = stored_agent(run)
        if agent is None and run.agent_id is not None:
            return None  # a stored value that is not an agent id: corrupt, left alone
        csession = run.csession_id
        look_alike: str | None = None
        adopt: tuple[str, str | None] | None = None
        if agent is None:
            found = lc.interpret_file(self.launch_path(run.run_id))
            if found is None:
                if run.launch_started_at is not None and run.state == "running":
                    return None  # TODO(#30): an agent may exist
                return plan
            agent = valid_agent_id(
                found.agent_id or (found.envelope.agent_id if found.envelope else None)
            )
            if agent is None:
                return plan
            csession = found.csession_id if found.envelope is None else None
            adopt = (agent, csession)
        verdict = lc.check_parked(
            agent, run_id=run.run_id, pr=run.pr, repo=run.repo, csession_id=csession
        )
        if verdict in ("live", "failed"):
            return None  # never ended with a live agent; a failed read is tried again
        if verdict == "mismatch" and adopt is not None:
            look_alike, adopt = agent, None
        elif verdict == "gone" and adopt is not None:
            adopt = None
        return _Plan(plan.repair, {"adopt": adopt, "look_alike": look_alike})

    def _end_canceled(self, run: Run, plan: _Plan, held: _Held) -> list[Repair]:
        assert held.lease is not None
        adopt = plan.data.get("adopt")
        if adopt is not None:  # stored first, so that the agent is torn down like a stored one
            self.store.update_fields(
                run.run_id, run.key, lease=held.lease, agent_id=adopt[0], csession_id=adopt[1]
            )
        ender = self.ender(run, held, "canceled")
        look_alike = plan.data.get("look_alike")
        after = ender.apply_failure(
            lc.Disposition(
                "fail", "canceled", run.cancel_exit_code or 143, teardown_agent=look_alike
            ),
            verdict="mismatch" if look_alike is not None else None,
        )
        return self.finish(run, after, moved=ender.moved)

    # -- a resume that died

    def dying_resume(self, run: Run) -> list[Repair]:
        repairs = self.guarded(run, self._decide_resume, self._act_resume)
        if not self.dry and any(r.kind == "agent_adopted" for r in repairs):
            fresh = self.row(run.run_id)
            if fresh is not None and fresh.state == "running":
                repairs += self.with_agent(fresh)
        return repairs

    def _decide_resume(self, run: Run) -> _Plan | None:
        agent = stored_agent(run)
        awaited = run.awaited_run
        if agent is None or run.csession_id is None or awaited is None:
            return None
        if launcher_state(run) != "gone":
            return None
        started = _parse(run.launch_started_at)
        if started is not None and self.opts.now() - started < LAUNCH_WINDOW:
            return None  # a launcher that is not stored yet may still be starting
        shown = show_agent(agent)
        if shown is None:
            return None
        code, doc = shown
        agent_doc = doc.get("agent")
        if code == 10 or (isinstance(agent_doc, dict) and agent_doc.get("status") == "torn_down"):
            return self._gone_plan(run, "gone")
        if code != 0:
            return None
        verdict = lc.parked_verdict(
            doc, run_id=run.run_id, pr=run.pr, repo=run.repo, csession_id=run.csession_id
        )
        if verdict in ("gone", "mismatch"):
            return self._gone_plan(run, verdict)
        number = agent_doc.get("run") if isinstance(agent_doc, dict) else None
        if type(number) is not int:  # noqa: E721
            return None
        if number >= awaited:  # the resume did start a run the row does not know
            adopted = Repair("agent_adopted", run.run_id, {"agent_id": agent})
            return _Plan(adopted, {"clear": True})
        if verdict != "owned":
            return None  # live on an older run: someone else is working on it
        reverted = Repair(
            "resume_reverted",
            run.run_id,
            {"state": "escalated", "reason": closed(run.escalation_reason, REASONS)},
        )
        return _Plan(reverted, {"revert": True})

    def _gone_plan(self, run: Run, verdict: str) -> _Plan:
        gone = Repair("agent_gone", run.run_id, {"state": "failed", "reason": "agent_gone"})
        return _Plan(gone, {"gone": verdict, "agent_id": stored_agent(run)})

    def _act_resume(self, run: Run, plan: _Plan, held: _Held) -> list[Repair]:
        assert held.lease is not None and run.awaited_run is not None
        data = plan.data
        if "gone" in data:
            return self._act_gone(run, plan, held)
        try:
            remove_prompt_files(self.need_dir(run.run_id))
        except OSError:
            pass
        if "clear" in data:
            ok = self.store.resume_started(
                run.run_id,
                lease=held.lease,
                previous_awaited=run.awaited_run,
                awaited_run=run.awaited_run,
            )
            repairs = [plan.repair] if ok else []
        else:
            ok = self.store.resume_revert(run.run_id, lease=held.lease, awaited_run=run.awaited_run)
            return [plan.repair] if ok else []
        return repairs

    def _act_gone(self, run: Run, plan: _Plan, held: _Held) -> list[Repair]:
        ender = self.ender(run, held, "agent_gone")
        mismatch = plan.data["gone"] == "mismatch"
        after = ender.apply_failure(
            lc.Disposition("fail", "agent_gone", 12), verdict="mismatch" if mismatch else None
        )
        return self.finish(run, after, moved=ender.moved)

    # -- an escalated run

    def escalated(self, run: Run) -> list[Repair]:
        if run.cancel_requested_at is not None:
            return self.canceled(run) if self.opts.judge_escalated else []
        if stored_agent(run) is None:
            return []
        repairs: list[Repair] = []
        if not self.dry:
            opts = self.agent_finalize(run, escalated=True)
            after = finalize_agent(self.root, run.run_id, opts)
            repairs = self.finish(run, after, moved=opts.moved)
            if after is None or after.state != "escalated":
                return repairs
            run = after
        if not self.opts.judge_escalated:
            return repairs
        return repairs + self.guarded(run, self._decide_escalated, self._act_escalated)

    def _decide_escalated(self, run: Run) -> _Plan | None:
        agent = stored_agent(run)
        if agent is None or run.state != "escalated" or run.cancel_requested_at is not None:
            return None
        shown = show_agent(agent)
        if shown is None:
            return None
        code, doc = shown
        agent_doc = doc.get("agent")
        if code == 10 or (isinstance(agent_doc, dict) and agent_doc.get("status") == "torn_down"):
            return self._gone_plan(run, "gone")
        if code != 0:
            return None
        verdict = lc.parked_verdict(
            doc, run_id=run.run_id, pr=run.pr, repo=run.repo, csession_id=run.csession_id
        )
        if verdict == "mismatch":
            return self._gone_plan(run, verdict)
        if verdict != "owned":
            return None  # live (a human answers it) or not known: never expired
        if outcome_from_show(run, doc) is not None:
            if self.dry:
                return _Plan(Repair("finalized", run.run_id, {"state": None, "reason": None}))
            return None  # the finalize check above lost a race or a lease: the next pass
        ttl = self.opts.escalation_ttl()
        since = _parse(run.escalated_at)
        if ttl is None or since is None or self.opts.now() - since <= ttl:
            return None
        expired = Repair("expired", run.run_id, {"state": "failed", "reason": "expired"})
        return _Plan(expired, {"expire": True})

    def _act_escalated(self, run: Run, plan: _Plan, held: _Held) -> list[Repair]:
        if "gone" in plan.data:
            return self._act_gone(run, plan, held)
        ender = self.ender(run, held, "expired")
        after = ender.apply_failure(lc.Disposition("fail", "expired", 12), verdict="owned")
        return self.finish(run, after, moved=ender.moved)

    # -- the dispatcher

    def recover(self, run: Run, *, escalated: bool) -> list[Repair]:
        if run.state == "escalated":
            return self.escalated(run) if escalated else []
        if run.state != "running":
            return []
        if run.cancel_requested_at is not None and run.phase != "finalize":
            return self.canceled(run)
        agent = stored_agent(run)
        if run.agent_id is not None and agent is None:
            return []  # a stored value that is not an agent id never reaches a sibling
        if agent is None:
            return self.without_agent(run)
        if run.phase == "session" and run.escalation_at is not None:
            return self.dying_resume(run)
        return self.with_agent(run)

    # -- teardown of a terminal run

    def teardown(self, run: Run) -> list[Repair]:
        return self.guarded(run, self._decide_teardown, self._act_teardown)

    def _decide_teardown(self, run: Run) -> _Plan | None:
        if run.state not in ("done", "failed") or run.teardown not in ("pending", "failed"):
            return None
        if stored_agent(run) is None or "agent_mismatch" in run.warnings:
            return None  # a look-alike is never touched, and never retried
        return _Plan(
            Repair("teardown_retried", run.run_id, {"teardown": closed(run.teardown, TEARDOWNS)})
        )

    def _act_teardown(self, run: Run, plan: _Plan, held: _Held) -> list[Repair]:
        agent = stored_agent(run)
        assert agent is not None
        try:
            settings = load_run_settings(self.need_dir(run.run_id))
        except ReviewRunError:
            # finish_teardown reads no setting: a run whose settings are gone is still torn down
            settings = cast("RunSettings", None)
        ender = self._ender(run, held, settings, "finalized")
        verdict = lc.check_parked(
            agent, run_id=run.run_id, pr=run.pr, repo=run.repo, csession_id=run.csession_id
        )
        ender.finish_teardown(verdict, agent, run)
        after = self.row(run.run_id)
        status = after.teardown if after is not None else None
        if status == "done":
            self.store.clear_attempts("teardown", run.run_id)
        else:
            self.store.bump_attempts("teardown", run.run_id)
        return [Repair("teardown_retried", run.run_id, {"teardown": closed(status, TEARDOWNS)})]

    # -- stale prompt files

    def sweep(self, run: Run) -> list[Repair]:
        return self.guarded(run, self._decide_sweep, self._act_sweep)

    def _decide_sweep(self, run: Run) -> _Plan | None:
        """``prompt.md`` and ``inputs/`` of a run that is terminal, or whose runner and launcher
        are both gone (taken under the run's lease: a ``resume`` in flight holds it and its
        prompt is never swept). Exact names only, inside the run's own directory."""
        base = self.root / "runs" / run.run_id
        if not (os.path.lexists(base / "prompt.md") or os.path.lexists(base / "inputs")):
            return None
        if run.state == "running" and not self._gone(run):
            return None
        return _Plan(Repair("cleaned", run.run_id, {"what": "prompt_files"}))

    def _act_sweep(self, run: Run, plan: _Plan, held: _Held) -> list[Repair]:
        remove_prompt_files(self.store.run_dir(run.run_id))
        return [plan.repair]


def recover_run(
    root: Path, run_id: str, opts: RecoverOptions, *, escalated: bool = False
) -> list[Repair]:
    """The repairs of one run (see the module docstring); ``[]`` when it needs none or does not
    exist. Raises ``ReviewRunError`` for a failure the caller reports (``lower_failed``,
    ``tool_missing``, an unreadable ``settings.json``)."""
    with Store.open(root, now=opts.now) as store:
        run = store.get_run(run_id, tolerant=True)
        if run is None:
            return []
        run_object(run)  # CorruptRunError for a row that cannot be shown: before any sibling call
        return _Recovery(root, store, opts).recover(run, escalated=escalated)


def sweep_prompt_files(root: Path, run_id: str, opts: RecoverOptions) -> list[Repair]:
    """Remove the stale prompt files of one run (``reconcile`` only)."""
    with Store.open(root, now=opts.now) as store:
        run = store.get_run(run_id, tolerant=True)
        if run is None or run.state not in ("running", "escalated", "done", "failed"):
            return []
        return _Recovery(root, store, opts).sweep(run)


def retry_teardown(root: Path, run_id: str, opts: RecoverOptions) -> list[Repair]:
    """The teardown retry of one terminal run (``reconcile`` only)."""
    with Store.open(root, now=opts.now) as store:
        run = store.get_run(run_id, tolerant=True)
        if run is None:
            return []
        run_object(run)
        return _Recovery(root, store, opts).teardown(run)


__all__ = [
    "LAZY",
    "RecoverOptions",
    "Repair",
    "recover_run",
    "retry_teardown",
    "sweep_prompt_files",
]
