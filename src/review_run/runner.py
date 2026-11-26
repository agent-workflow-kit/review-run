"""``run`` after preflight: run order steps 3 to 7 (issues #17 and #18).

The runner reserves the run id, fires the ``pre-run`` hooks, creates the run record, takes and
renews the run's lease, writes the run files, pre-fetches, builds the manifest and the prompt files,
launches the session through agent-spawn, stores the agent, waits and maps the result onto the run
(``launch`` holds the table). Every failure path ends the run once with its reason and exit code
and removes the prompt files; ``run`` never ends a run it does not hold the lease of.

Seams, each with its owner:

* ``finalize`` (a ``done`` session): TODO(#19) the artifact checks, normalization, meta and
  ``review.json``. The default leaves the run ``running`` in phase ``session`` (its agent and
  ``awaited_run`` stored, so ``show``, ``wait`` and ``reconcile`` can finalize it later) and
  raises ``internal``.
* The code run (phase ``code_run``, ``code-run.json``): TODO(#35). Until it lands a repo that
  configures ``run_pr_code`` is reviewed without one (a ``warning`` log record says so) and the
  prompt says no code run is configured.
* Signals (record the cancellation, stop the launcher and the waiter): TODO(#27). Until it lands
  a SIGTERM or SIGINT outside a gateway call kills the process with the lease held and the prompt
  files on disk; #27 installs handlers around the whole ``run_review``, removes the prompt files on
  any ``BaseException``, records the cancellation and stops the launcher.
* The mapping of an outcome to the printed output and exit code: TODO(#24); ``cli`` prints the run
  object and exits with its code in the meantime.
"""

from __future__ import annotations

import datetime as dt
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from . import gateway, hooks, procid
from . import launch as lc
from .audit import AuditWriter, Caller, LogWriter, make_audit_record
from .config_semantic import read_guidelines_file
from .errors import MAX_DECISION_IDS, ReviewRunError
from .manifest import Manifest, build_manifest
from .prefetch import PrefetchFailure, load_previous, persist_prefetch, prefetch
from .preflight import RunPlan
from .prompt import (
    CodeRunInput,
    PromptFiles,
    build_prompt,
    input_paths,
    remove_prompt_files,
    write_prompt_files,
)
from .settings import (
    PluginChanged,
    RunSettings,
    bundled_header_template,
    launch_env,
    load_run_settings,
    verify_plugin_dirs,
    write_run_files,
)
from .store import (
    LEASE_RENEW_SECONDS,
    Lease,
    LeaseLostError,
    NewEvent,
    NewRun,
    Run,
    Store,
    format_time,
    lease_name,
)

DEFAULT_MAX_OUTPUT_BYTES: Final = 1024 * 1024  # run_pr_code.max_output_bytes' default
_LIMIT_KEYS: Final = (
    "max_file_bytes",
    "max_total_bytes",
    "max_files",
    "hard_ceiling_bytes",
    "max_answer_bytes",
)


class Interrupted(Exception):
    """The waiter ended on a signal (exit 130 or 143): nothing is printed, the run is unchanged."""

    def __init__(self, exit_code: int) -> None:
        super().__init__("interrupted")
        self.exit_code = exit_code


# -- the lease -----------------------------------------------------------------------------------


RENEW_FAILURES_LIMIT: Final = 3  # consecutive failed renewals (TTL 300 s, interval 60 s)


class LeaseKeeper:
    """Renews the run's lease from a thread of its own (its own connection) every
    ``interval`` seconds while the main thread is blocked in a call. It sets ``lost`` and stops
    when ``renew_lease`` returns ``None`` (the lease was taken over) or after
    ``RENEW_FAILURES_LIMIT`` consecutive failures (a transient error such as a locked database
    is retried at the next tick)."""

    def __init__(
        self, root: Path, lease: Lease, interval: float = float(LEASE_RENEW_SECONDS)
    ) -> None:
        self.root = Path(root)
        self.lease = lease
        self.interval = interval
        self.lost = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="review-run-lease", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=30)

    def _loop(self) -> None:
        store: Store | None = None
        failures = 0
        try:
            while not self._stop.wait(self.interval):
                try:
                    if store is None:
                        store = Store.open(self.root)
                    renewed = store.renew_lease(self.lease)
                except Exception:  # transient: retried at the next tick
                    failures += 1
                    if failures >= RENEW_FAILURES_LIMIT:
                        self.lost.set()
                        return
                    continue
                if renewed is None:
                    self.lost.set()
                    return
                failures = 0
        finally:
            if store is not None:
                try:
                    store.close()
                except Exception:  # nothing to do for a connection that will not close
                    pass


# -- the context ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class FinalizeRequest:
    """What a finalizer needs for a ``done`` session (the seam of #19)."""

    store: Store
    run_id: str
    lease: Lease
    settings: RunSettings
    result: lc.SpawnResult


FinalizeFn = Callable[["FinalizeRequest"], Run]


def finalize_not_implemented(request: FinalizeRequest) -> Run:
    # TODO(#19): finalize (artifact checks, normalization, meta, review.json, the terminal
    # transaction, teardown). The run stays ``running`` with its agent stored.
    raise ReviewRunError("internal", "finalize is not implemented yet")


@dataclass
class Options:
    """What the command line decides beyond the plan."""

    caller: Caller
    flag_hooks: Sequence[str] = ()
    no_wait: bool = False
    finalize: FinalizeFn = finalize_not_implemented
    shell: Sequence[str] = hooks.SHELL
    sleep: Callable[[float], None] = time.sleep
    lease_interval: float = float(LEASE_RENEW_SECONDS)


def _now() -> str:
    return format_time(dt.datetime.now(dt.UTC))


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


# -- pre-run -------------------------------------------------------------------------------------


def _merge_ids(*groups: Sequence[str]) -> list[str]:
    out: list[str] = []
    for group in groups:
        for item in group:
            if item not in out and len(out) < MAX_DECISION_IDS:
                out.append(item)
    return out


def pre_run_hooks(
    plan: RunPlan,
    store: Store,
    audit: AuditWriter,
    opts: Options,
    stored: Sequence[str],
) -> tuple[str, list[str]]:
    """Run order steps 2 (the id) and 3: reserve the run id, run the ``pre-run`` hooks. A veto
    exits 11, an escalation 13 (unless overridden); each writes its audit ``run`` record
    (target: the reserved id and the PR) and no run record exists. Returns the id and the
    decision ids (the caller's and the hooks')."""
    run_id = store.reserve_id()
    settings, facts, request = plan.settings, plan.facts, plan.request
    values = plan.resolved.values
    code = values.get("run_pr_code")
    payload = hooks.pre_run_payload(
        run_id=run_id,
        repo=facts.repo,
        labels=request.labels,
        pr=facts.pr,
        skill=plan.skill,
        mode=plan.mode,
        previous_run=plan.previous.run_id if plan.previous else None,
        head_sha=facts.head_sha,
        pr_facts=facts.run_facts(),
        will_run_code=code is not None,
        code_run_sandbox=None if code is None else code.get("sandbox", "required"),
        override_escalation=request.override_escalation,
        session_sandbox=str(settings.value("sandbox")),
        subagents=settings.value("subagents") is True,
        permission_profile=settings.profile_kind,
        setting_sources=list(settings.value("setting_sources")),
        sizes={
            "additions": facts.additions,
            "deletions": facts.deletions,
            "changed_files": facts.changed_files,
        },
        actor=opts.caller.actor,
        decision_ids=list(opts.caller.decision_ids),
    )
    specs = hooks.specs_for("pre-run", values.get("hooks"), stored)
    result = hooks.run_pre_run(
        specs,
        payload,
        object_id=run_id,
        override_escalation=request.override_escalation,
        shell=opts.shell,
    )
    if result.outcome != "continue":
        record_inputs = {
            "head_sha": facts.head_sha,
            "config_sha256": settings.config_sha256,
            "skill": plan.skill,
            "previous_run": plan.previous.run_id if plan.previous else None,
            "permission_profile_sha256": settings.profile_sha256,
            "session_sandbox": str(settings.value("sandbox")),
            "setting_sources": list(settings.value("setting_sources")),
            "plugin_dirs_sha256": settings.plugin_dirs_sha256
            if len(settings.plugin_dirs) > 1
            else None,
            "override_escalation": request.override_escalation,
        }
        audit.append(
            hooks.pre_run_audit_record(opts.caller, result, run_id, facts.pr, record_inputs)
        )
        ids = _merge_ids(opts.caller.decision_ids, result.decision_ids)
        code_name = "vetoed" if result.outcome == "veto" else "escalated"
        raise ReviewRunError(
            code_name,
            f"a pre-run hook {'vetoed' if code_name == 'vetoed' else 'escalated'} the run",
            hook="pre-run",
            decision_ids=ids or None,
        )
    return run_id, _merge_ids(opts.caller.decision_ids, result.decision_ids)


# -- the run -------------------------------------------------------------------------------------


class _Flow:
    """The state of one run in this process: the store, the lease and the audit pair."""

    def __init__(
        self,
        plan: RunPlan,
        store: Store,
        audit: AuditWriter,
        log: LogWriter,
        opts: Options,
        run_id: str,
        lease: Lease,
        keeper: LeaseKeeper,
        attempt_id: str,
        root: Path,
    ) -> None:
        self.plan, self.store, self.audit, self.log = plan, store, audit, log
        self.opts, self.run_id, self.lease, self.keeper = opts, run_id, lease, keeper
        self.attempt_id, self.root = attempt_id, root
        self.run_dir = store.run_dir(run_id)
        self.settings = plan.settings
        self.hooks_config = plan.resolved.values.get("hooks")
        self.launched = False  # set once the launcher may exist: later failures leave the run

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
        if self.keeper.lost.is_set():
            raise ReviewRunError("internal", "the run lease is no longer held by this process")

    # -- ending the run

    def end(
        self,
        state: str,
        reason: str,
        exit_code: int,
        *,
        failure: dict[str, Any] | None = None,
        escalated_run: int | None = None,
        fields: Mapping[str, Any] | None = None,
        teardown_on_cancel: bool = False,
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
            if teardown_on_cancel:
                extra["teardown"] = "pending"
        if state == "failed":
            extra["failure"] = failure
        hook = "post-escalate" if state == "escalated" else "post-run"
        number = escalated_run if state == "escalated" and escalated_run else 1
        event_id = hooks.event_id_for(self.run_id, hook, number)
        record = make_audit_record(
            "run",
            caller=self.opts.caller,
            run_id=self.run_id,
            pr=run.pr,
            inputs=run_inputs(run, self.settings, override=self.plan.request.override_escalation),
            result={"status": state, "reason": reason, "exit_code": exit_code},
            attempt_id=self.attempt_id,
            event_id=event_id,
        )
        agent_id = extra.get("agent_id", run.agent_id)
        warnings = list(run.warnings)
        if state == "escalated":
            payload = hooks.post_escalate_payload(
                run_id=self.run_id,
                repo=run.repo,
                labels=run.labels,
                event_id=event_id,
                reason=reason,
                pr=run.pr,
                agent_id=agent_id,
                actor=self.opts.caller.actor,
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
                actor=self.opts.caller.actor,
                decision_ids=list(run.decision_ids),
                warnings=warnings,
            )
        if escalated_run is not None and state == "escalated":
            extra["escalated_run"] = escalated_run
        moved = self.store.end_run(
            self.run_id,
            run.key,
            lease=self.lease,
            state=state,
            reason=reason,
            exit_code=exit_code,
            fields=extra,
            events=[NewEvent(event_id, hook, payload, (record,))],
        )
        remove_prompt_files(self.run_dir)
        if moved:
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
                shell=self.opts.shell,
            )
        except (OSError, ReviewRunError):  # redelivered by the next reconcile
            pass

    def warn(self, code: str) -> None:
        """A closed run warning, with a ``warning`` log record."""
        self.store.add_warning(self.run_id, code, lease=self.lease)
        self.note("warning", f"run warning {code}", level="warn", code=code)

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
        ``mismatch`` is never touched."""
        outcome = verdict
        if verdict == "owned":
            outcome = lc.teardown_owned(agent_id, actor=self.plan.request.actor)
            self.audit_teardown(agent_id, run, "done" if outcome in ("done", "gone") else "failed")
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
                caller=self.opts.caller,
                run_id=self.run_id,
                pr=run.pr,
                inputs={"agent_id": agent_id},
                result={"status": status},
            )
        except (OSError, ReviewRunError, ValueError):  # the audit trail never decides a run
            pass

    def end_failed(self, reason: str, exit_code: int, failure: dict[str, Any] | None = None) -> Run:
        run, moved = self.end("failed", reason, exit_code, failure=failure)
        if moved:
            self.deliver()
        return run

    # -- the steps

    def canceled_row(self) -> Run | None:
        """When a cancellation is recorded, end the run ``canceled`` (TODO(#26) and TODO(#27)
        own recording one; this process only honors it)."""
        run = self.row()
        if run.cancel_requested_at is None:
            return None
        return self.end_failed("canceled", run.cancel_exit_code or 143)

    def prefetch_and_prompt(self) -> tuple[PromptFiles, Manifest] | Run:
        """Run order steps 5 and 6 and the prompt: pre-fetch, manifest, prompt files. Returns the
        ended run when a step failed."""
        values = self.plan.resolved.values
        facts, s = self.plan.facts, self.settings
        limits = {k: int(s.value(f"limits.{k}")) for k in _LIMIT_KEYS}
        try:
            previous = None
            if self.plan.previous is not None:
                previous = load_previous(
                    self.store,
                    self.plan.previous.run_id,
                    max_bytes=int(s.value("limits.max_artifact_bytes")),
                )
            fetched = prefetch(facts.pr, hard_ceiling_bytes=limits["hard_ceiling_bytes"])
            manifest = build_manifest(
                fetched.files,
                fetched.diff,
                max_file_bytes=limits["max_file_bytes"],
                max_total_bytes=limits["max_total_bytes"],
                max_files=limits["max_files"],
                hard_ceiling_bytes=limits["hard_ceiling_bytes"],
            )
        except PrefetchFailure as exc:
            return self.end_failed(exc.reason, exc.exit_code, exc.failure())
        run = self.row()
        if not persist_prefetch(self.store, self.run_id, run.key, facts, fetched, lease=self.lease):
            return self.row()  # another process moved the row: its state stands
        if (ended := self.canceled_row()) is not None:
            return ended
        guidelines_path = s.value("guidelines_file")
        guidelines = None if guidelines_path is None else read_guidelines_file(guidelines_path)
        code_run = values.get("run_pr_code") or {}
        # TODO(#35): the code run (phase code_run, throwaway, sandbox, code-run.json); until then
        # no output exists and the prompt says no code run is configured. The "in every case"
        # step 6 (write code-run.json, whatever the outcome) is TODO(#35) too: no fake file is
        # written here.
        if values.get("run_pr_code") is not None:
            self.note(
                "warning",
                "run_pr_code is configured but the code run is not implemented yet",
                level="warn",
                code="code_run_pending",
            )
        files = build_prompt(
            template=bundled_header_template(),
            run_id=self.run_id,
            pr_facts=facts,
            ci=fetched.ci,
            manifest=manifest,
            code_run=CodeRunInput.not_configured(),
            previous_review=None if previous is None else previous.review,
            guidelines=guidelines,
            nits=bool(s.value("nits")),
            limits=limits,
            max_output_bytes=int(code_run.get("max_output_bytes", DEFAULT_MAX_OUTPUT_BYTES)),
        )
        write_prompt_files(self.run_dir, files, manifest)
        return files, manifest

    def launch(self, files: PromptFiles) -> lc.Disposition | Run:
        """Run order step 7, first half: the plugin hash, the argv, the launcher. Returns the
        disposition of its answer, or the ended run for a failure that needs none."""
        s = self.settings
        self.check_lease()
        run = self.row()
        if not self.store.advance_phase(self.run_id, run.key, "session", lease=self.lease):
            return self.row()
        self.note("phase_change", "phase session", phase="session")
        try:
            verify_plugin_dirs(s)
        except PluginChanged:
            failure = lc.failure_object(None, "plugin_changed")
            return self.end_failed("launch_failed", 12, failure)
        if (ended := self.canceled_row()) is not None:
            return ended
        argv = lc.launch_argv(
            s,
            repo=run.repo,
            pr=run.pr,
            run_id=self.run_id,
            run_dir=self.run_dir,
            input_paths=input_paths(self.run_dir, files),
            labels=self.plan.request.labels,
            actor=self.plan.request.actor,
            decision_ids=run.decision_ids,
        )
        self.check_lease()
        key = self.row().key
        if not self.store.update_fields(
            self.run_id, key, lease=self.lease, launch_started_at=_now()
        ):
            return self.row()  # another process moved the row: its state stands
        self.launched = True

        def started(pid: int) -> None:
            ident = procid.identity_of(pid)
            self.store.update_fields(
                self.run_id,
                key,
                lease=self.lease,
                launcher_pid=pid,
                launcher_start=None if ident is None else ident.start,
            )

        try:
            done = gateway.run(argv, timeout=lc.LAUNCH_TIMEOUT, env=launch_env(s), on_start=started)
        except gateway.ToolMissing as exc:
            return self.end_failed(
                "launch_failed", 12, lc.failure_object("agent-spawn", exc.lower_code)
            )
        except gateway.GatewayError as exc:
            found = lc.interpret_file(self.run_dir / lc.LAUNCH_FILE)
            if found is None:
                # The launcher was ended by the gateway (a timeout, too much output) and left no
                # usable launch.json: an agent may exist. The run stays ``running`` so that
                # ``reconcile`` adopts it by its labels (TODO(#29)). A launch.json that holds an
                # error envelope or a result is adopted below, like stdout.
                raise ReviewRunError(
                    "failed",
                    f"agent-spawn failed ({exc.lower_code})",
                    reason="lower_failed",
                    tool="agent-spawn",
                    lower_code=exc.lower_code,
                ) from None
            return lc.map_launch(found, found.exit_code)
        finally:
            remove_prompt_files(self.run_dir)  # agent-spawn copied them at classification
        found = lc.interpret(done.returncode, done.stdout, self.run_dir / lc.LAUNCH_FILE)
        return lc.map_launch(found, done.returncode)

    def adopt(self, agent: lc.SpawnResult) -> Run:
        """Store ``agent_id``, ``csession_id`` and ``awaited_run`` in one transaction. A result
        that names another agent, session or run than the stored ones never overwrites them."""
        self.check_lease()
        run = self.row()
        awaited = agent.run_number if agent.final else agent.agent_run
        if (
            (run.agent_id is not None and run.agent_id != agent.agent_id)
            or (run.csession_id is not None and run.csession_id != agent.csession_id)
            or (run.awaited_run is not None and run.awaited_run != awaited)
        ):
            raise ReviewRunError("internal", "the result is not the stored session's")
        if not self.store.update_fields(
            self.run_id,
            run.key,
            lease=self.lease,
            agent_id=agent.agent_id,
            csession_id=agent.csession_id,
            awaited_run=awaited,
        ):
            raise ReviewRunError("internal", "the run changed while its agent was stored")
        return self.row()

    def apply(self, disposition: lc.Disposition) -> Run | None:
        """Apply a mapping row. ``None`` means the run continues (a started session)."""
        kind = disposition.kind
        if kind == "started":
            if disposition.agent is None:
                raise ReviewRunError("internal", "a started session without a result")
            self.adopt(disposition.agent)
            return None
        agent = disposition.agent
        if kind in ("finalize", "escalate"):
            if agent is None:
                raise ReviewRunError("internal", "a session outcome without a result")
            self.adopt(agent)
        if kind == "finalize":
            assert agent is not None
            return self.opts.finalize(
                FinalizeRequest(self.store, self.run_id, self.lease, self.settings, agent)
            )
        if kind == "escalate":
            assert agent is not None and disposition.reason and disposition.exit_code
            run, moved = self.end(
                "escalated",
                disposition.reason,
                disposition.exit_code,
                escalated_run=agent.run_number,
                teardown_on_cancel=True,
            )
            if moved:
                if run.state == "failed" and agent.agent_id:  # a cancellation was recorded
                    self.teardown_after(agent.agent_id, agent.csession_id, run)
                self.deliver()
            return self.row()
        if kind == "interrupted":
            raise Interrupted(disposition.exit_code or 143)
        return self.apply_failure(disposition)

    def apply_failure(self, disposition: lc.Disposition) -> Run:
        """End the run ``failed``. An agent the run knows (the result's, the envelope's or the
        stored one) is checked for ownership first: a look-alike is never stored, stopped or torn
        down and the run ends ``agent_gone`` with the warning ``agent_mismatch``."""
        assert disposition.reason is not None and disposition.exit_code is not None
        reason, exit_code, failure = disposition.reason, disposition.exit_code, disposition.failure
        stored = self.row()
        agent = disposition.agent
        agent_id = disposition.teardown_agent or (agent.agent_id if agent else None)
        csession = agent.csession_id if agent else None
        if agent_id is None:
            agent_id, csession = stored.agent_id, stored.csession_id
        fields: dict[str, Any] = {}
        verdict: str | None = None
        if agent_id is not None and reason in lc.TEARDOWN_REASONS:
            verdict = lc.check_parked(
                agent_id, run_id=self.run_id, pr=stored.pr, repo=stored.repo, csession_id=csession
            )
            if verdict == "mismatch":
                reason, exit_code, failure = "agent_gone", 12, None
            else:
                fields = {"agent_id": agent_id, "teardown": "pending"}
                if csession:
                    fields["csession_id"] = csession
        elif agent_id is not None and agent_id != stored.agent_id:
            fields["agent_id"] = agent_id
            if csession:
                fields["csession_id"] = csession
        run, moved = self.end(
            "failed",
            reason,
            exit_code,
            failure=failure,
            fields=fields,
            teardown_on_cancel=agent_id is not None and verdict != "mismatch",
        )
        if not moved:
            return run  # another process ended the run: it owns the teardown and the delivery
        if verdict == "mismatch":
            self.warn("agent_mismatch")
        elif verdict is not None and agent_id is not None:
            self.finish_teardown(verdict, agent_id, run)
        elif run.reason == "canceled" and run.teardown == "pending" and agent_id is not None:
            self.teardown_after(agent_id, csession, run)  # a cancellation was recorded meanwhile
        self.deliver()
        return self.row()


def run_review(plan: RunPlan, root: Path, opts: Options) -> Run:
    """Run order steps 3 to 7 for a planned run; returns the run as stored when this process is
    done with it: ended (``done`` through the finalize seam, ``escalated``, ``failed``) or, for
    ``--no-wait``, ``running`` with its session started. Raises ``ReviewRunError`` for a veto
    (11), an escalation (13), a ``lower_failed`` wait (12, the run unchanged) and ``internal``."""
    stored = hooks.add_flag_hooks([], opts.flag_hooks)
    audit, log = AuditWriter(root), LogWriter(root)
    with Store.open(root) as store:
        run_id, decision_ids = pre_run_hooks(plan, store, audit, opts, stored)
        facts, settings = plan.facts, plan.settings
        run = store.create_run(
            NewRun(
                run_id=run_id,
                pr=facts.pr,
                repo=facts.repo,
                head_sha=facts.head_sha,
                mode=plan.mode,
                skill=plan.skill,
                labels=plan.request.labels,
                previous_run=plan.previous.run_id if plan.previous else None,
                hooks=tuple(stored),
                actor=opts.caller.actor,
                decision_ids=tuple(decision_ids),
                config_sha256=settings.config_sha256,
            )
        )
        lease = store.acquire_lease(lease_name(run_id))
        if lease is None:  # a fresh id: nobody else can hold it
            raise ReviewRunError("internal", "the new run's lease is taken")
        keeper = LeaseKeeper(root, lease, opts.lease_interval)
        keeper.start()
        try:
            attempt = audit.begin(
                "run",
                caller=opts.caller,
                run_id=run_id,
                pr=facts.pr,
                inputs=run_inputs(run, settings, override=plan.request.override_escalation),
            )
            flow = _Flow(plan, store, audit, log, opts, run_id, lease, keeper, attempt, root)
            return _drive(flow)
        finally:
            keeper.stop()
            store.release_lease(lease)


def _drive(flow: _Flow) -> Run:
    """Steps 4 (the run files) to 7 and the wait; every ending goes through ``flow.end``."""
    try:
        write_run_files(flow.run_dir, flow.plan.settings, flow.plan.profile)
        # The run reads its own stored files from here on, never the plan in memory.
        flow.settings = load_run_settings(flow.run_dir)
        ready = flow.prefetch_and_prompt()
        if isinstance(ready, Run):
            return ready
        files, _manifest = ready
        launched = flow.launch(files)
        if isinstance(launched, Run):
            return launched
        ended = flow.apply(launched)
        if ended is not None:
            return ended
        if flow.opts.no_wait:
            return flow.row()
        run = flow.row()
        if run.agent_id is None:
            raise ReviewRunError("internal", "the started session has no stored agent")
        flow.check_lease()
        disposition = lc.wait_session(
            run.agent_id,
            flow.run_dir,
            timeout=lc.wait_timeout_seconds(flow.settings),
            sleep=flow.opts.sleep,
            csession_id=run.csession_id,
            awaited_run=run.awaited_run,
        )
        ended = flow.apply(disposition)
        return flow.row() if ended is None else ended
    except Interrupted:
        raise
    except Exception as exc:
        if flow.launched:
            # The session may exist: whatever failed (a store error included), the run stays
            # ``running`` for ``reconcile`` and nothing is torn down.
            if isinstance(exc, ReviewRunError):
                raise
            raise ReviewRunError("internal", "unexpected error after the launch") from None
        if isinstance(exc, LeaseLostError):
            raise
        # An unexpected failure before the launcher ran ends the run once, never a crash loop.
        try:
            return flow.end_failed("internal", 12)
        except Exception:
            raise ReviewRunError("internal", "unexpected error") from None
    finally:
        remove_prompt_files(flow.run_dir)


__all__ = [
    "FinalizeRequest",
    "Interrupted",
    "LeaseKeeper",
    "Options",
    "finalize_not_implemented",
    "pre_run_hooks",
    "run_inputs",
    "run_review",
]
