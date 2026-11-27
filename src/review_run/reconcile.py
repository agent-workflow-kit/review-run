"""``reconcile``: repair state against reality (interface.md, ``reconcile``; issue #29).

One invocation, from cron or by hand, under the ``reconcile`` lease (a second concurrent one
reports ``already_running`` and does nothing; a dead holder's lease is taken over):

1. every ``running`` and ``escalated`` run goes through ``recovery.recover_run`` (finalize, runner
   check, adoption from ``launch.json``, ``agent_gone``, ``expired``, a recorded cancellation, a
   resume that died);
2. terminal runs whose agent teardown is ``pending`` or ``failed`` are retried
   (``recovery.retry_teardown``; a look-alike agent is never touched);
3. stale ``prompt.md`` and ``inputs/`` are removed from runs that are terminal or whose runner and
   launcher are both gone, and leftover ``tmp/dry-*`` scratch directories of killed dry runs;
4. the outbox: the audit records of pending events are appended, then the undelivered hooks are
   delivered (``hooks.deliver_pending``), oldest first, each event under its own lease.

Every repair takes the run's lease without waiting and skips a run another process holds; every
run is handled in isolation (an unreadable row or a failing sibling never stops the others, and
is logged as a closed code); work per invocation is bounded (``MAX_RUNS`` per scan). Nothing in
the output or the log is stored text: closed codes, ids and counts. ``--dry-run`` takes no lease,
writes nothing and reports what it would change. It never signals a process or stops an agent.

Seams, each with its owner:

* TODO(#30): label recovery of a run that died before storing its agent (``recovery``).
* TODO(#32): removal of leaked throwaways (``throwaway_removed``, ``throwaway_leaked``).
"""

from __future__ import annotations

import datetime as dt
import os
import shutil
import stat
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from . import hooks as hooks_mod
from .audit import AuditWriter, LogWriter, flush_outbox
from .errors import ReviewRunError
from .finalize import Seams
from .keeper import LeaseKeeper
from .lazyhooks import LazyHooks
from .recovery import (
    LAZY,
    RecoverOptions,
    Repair,
    recover_run,
    retry_teardown,
    sweep_prompt_files,
)
from .store import LEASE_RENEW_SECONDS, RECONCILE_LEASE, Store

SCHEMA_VERSION: Final = 1
MAX_RUNS: Final = 1000  # runs per scan, oldest first
MAX_SWEEP: Final = 500  # the newest terminal runs checked for stale prompt files
STUCK_AFTER: Final = 5  # failed passes before ``teardown_stuck`` and ``audit_stuck``
SCRATCH_AGE: Final = dt.timedelta(hours=1)
SCRATCH_PREFIX: Final = "dry-"
SCRATCH_SUFFIX_LEN: Final = 16


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


@dataclass
class ReconcileOptions:
    config_path: Path | None = None
    dry_run: bool = False
    now: Callable[[], dt.datetime] = _utcnow
    shell: Sequence[str] = hooks_mod.SHELL
    lease_interval: float = float(LEASE_RENEW_SECONDS)
    seams: Seams | None = None


@dataclass
class ReconcileResult:
    changes: list[Repair] = field(default_factory=list[Repair])
    warnings: list[str] = field(default_factory=list[str])
    stuck_teardowns: int = 0
    stuck_audit: int = 0
    already_running: bool = False
    dry_run: bool = False

    def as_json(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "dry_run": self.dry_run,
            "already_running": self.already_running,
            "changes": [c.as_json() for c in self.changes],
            "warnings": list(self.warnings),
            "stuck_teardowns": self.stuck_teardowns,
            "stuck_audit": self.stuck_audit,
        }

    def human(self) -> str:
        if self.already_running:
            return "reconcile: another reconcile is running\n"
        head = "dry run: would change" if self.dry_run else "changed"
        lines = [f"reconcile: {head} {len(self.changes)} thing(s)"]
        for change in self.changes:
            lines.append(f"  {change.run_id or '-'} {change.kind}")
        for warning in self.warnings:
            lines.append(f"warning: {warning}")
        return "\n".join(lines) + "\n"


class _Pass:
    def __init__(self, root: Path, store: Store, opts: ReconcileOptions) -> None:
        self.root, self.store, self.opts = root, store, opts
        self.dry = opts.dry_run
        self.log = LogWriter(root)
        self.audit = AuditWriter(root)
        self.hooks = LazyHooks(opts.config_path, None, self.log)
        self.changes: list[Repair] = []
        self.recover = RecoverOptions(
            by="reconcile",
            config_path=opts.config_path,
            hooks=self.hooks,
            ttl=LAZY,
            now=opts.now,
            dry_run=opts.dry_run,
            lease_interval=opts.lease_interval,
            shell=opts.shell,
            seams=opts.seams,
            judge_escalated=True,
        )

    def isolated(self, run_id: str | None, step: Callable[[], list[Repair]]) -> None:
        """One unit of work whose failure never stops the pass: a closed code goes to the log."""
        try:
            self.changes.extend(step())
        except Exception as exc:  # per-run isolation: nothing but a closed code is kept
            code = exc.code if isinstance(exc, ReviewRunError) else "internal"
            try:
                self.log.write(
                    "reconcile",
                    "a run was skipped",
                    level="warn",
                    run_id=run_id,
                    code=code,
                )
            except (OSError, ReviewRunError, ValueError):
                pass

    # -- the scans

    def runs(self) -> None:
        for run_id in self.store.live_run_ids(MAX_RUNS):
            self.isolated(
                run_id, lambda r=run_id: recover_run(self.root, r, self.recover, escalated=True)
            )

    def teardowns(self) -> None:
        # TODO(#32): retry the removal of throwaways recorded as leaked (throwaway_removed,
        # throwaway_leaked) and remove those of a run that ended runner_lost in phase prefetch
        # or code_run.
        for run_id in self.store.teardown_run_ids(MAX_RUNS):
            self.isolated(run_id, lambda r=run_id: retry_teardown(self.root, r, self.recover))

    def prompts(self) -> None:
        ids = list(dict.fromkeys([*self.store.live_run_ids(MAX_RUNS),
                                  *self.store.terminal_run_ids(MAX_SWEEP)]))  # fmt: skip
        for run_id in ids:
            self.isolated(run_id, lambda r=run_id: sweep_prompt_files(self.root, r, self.recover))

    def scratch(self) -> None:
        """``tmp/dry-<16 hex>`` directories (of a ``run --dry-run`` that was killed) older than
        ``SCRATCH_AGE``; exact names, never a link, never outside ``tmp/``."""
        tmp = self.root / "tmp"
        try:
            entries = list(os.scandir(tmp))
        except OSError:
            return
        cutoff = self.opts.now().timestamp() - SCRATCH_AGE.total_seconds()
        for entry in entries:
            name = entry.name
            suffix = name[len(SCRATCH_PREFIX) :]
            if not (
                name.startswith(SCRATCH_PREFIX)
                and len(suffix) == SCRATCH_SUFFIX_LEN
                and all(c in "0123456789abcdef" for c in suffix)
            ):
                continue
            try:
                info = os.lstat(entry.path)
            except OSError:
                continue
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
                continue
            if info.st_mtime > cutoff:
                continue
            if not self.dry:
                shutil.rmtree(entry.path, ignore_errors=True)
            self.changes.append(Repair("cleaned", None, {"what": "dry_run_scratch"}))

    # -- the outbox

    def outbox(self) -> None:
        deliverable = True
        try:
            len(self.hooks)  # loads the config: a config that does not load defers delivery
        except ReviewRunError:
            deliverable = False
        for run_id in self.store.pending_event_run_ids(MAX_RUNS):
            self.isolated(run_id, lambda r=run_id: self._outbox_run(r, deliverable))

    def _outbox_run(self, run_id: str, deliverable: bool) -> list[Repair]:
        events = self.store.pending_events(run_id)
        if self.dry:
            return [
                Repair(
                    "hook_redelivered" if not e["delivered"] else "audit_flushed",
                    run_id,
                    {"hook": e["hook"]},
                )
                for e in events
            ]
        hook_of = {str(e["event_id"]): str(e["hook"]) for e in events}
        wanted = [str(e["event_id"]) for e in events if e["audit_pending"]]
        repairs: list[Repair] = []
        try:
            done = set(flush_outbox(self.store, self.audit, run_id))
        except (OSError, ReviewRunError):
            done = set()
        for event_id in wanted:
            if event_id in done:
                self.store.clear_attempts("audit", event_id)
                repairs.append(Repair("audit_flushed", run_id, {"hook": hook_of[event_id]}))
            else:
                self.store.bump_attempts("audit", event_id)
        if deliverable:
            try:
                results = hooks_mod.deliver_pending(
                    self.store,
                    self.audit,
                    self.log,
                    self.hooks,
                    run_id=run_id,
                    shell=self.opts.shell,
                )
            except (OSError, ReviewRunError):
                results = []
            for result in results:
                if result.status in ("delivered", "abandoned"):
                    hook = hook_of.get(result.event_id, "unknown")
                    repairs.append(
                        Repair(
                            "hook_redelivered",
                            run_id,
                            {"hook": hook, "outcome": result.status},
                        )
                    )
        return repairs


def reconcile(root: Path, opts: ReconcileOptions) -> ReconcileResult:
    """One pass (see the module docstring). Raises ``ReviewRunError`` only for a state directory
    that cannot be opened (``state_unsafe``, ``schema_newer``, a database error)."""
    with Store.open(root, now=opts.now) as store:
        lease = None if opts.dry_run else store.acquire_lease(RECONCILE_LEASE)
        if lease is None and not opts.dry_run:
            return ReconcileResult(already_running=True)
        keeper = None
        if lease is not None:
            keeper = LeaseKeeper(root, lease, opts.lease_interval)
            keeper.start()
        try:
            work = _Pass(root, store, opts)
            work.runs()
            work.teardowns()
            work.prompts()
            work.scratch()
            work.outbox()
            teardowns = store.stuck_teardowns(STUCK_AFTER)
            audit = store.stuck_audit(STUCK_AFTER)
        finally:
            if keeper is not None:
                keeper.stop()
            if lease is not None:
                store.release_lease(lease)
    warnings: list[str] = []
    if teardowns > 0:
        warnings.append("teardown_stuck")
    if audit > 0:
        warnings.append("audit_stuck")
    return ReconcileResult(
        changes=work.changes,
        warnings=warnings,
        stuck_teardowns=teardowns,
        stuck_audit=audit,
        dry_run=opts.dry_run,
    )


__all__ = ["ReconcileOptions", "ReconcileResult", "reconcile"]
