"""Seeding for the hook delivery tests: a run ended with one outbox event."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from review_run import hooks
from review_run.audit import Caller, make_audit_record
from review_run.store import Expect, NewEvent, NewRun, Store, lease_name

PR = "host/o/r#7"
REPO = "host/o/r"
SHA = "a" * 40
H64 = "b" * 64
PY = sys.executable
CALLER = Caller("human:alice", "flag", True, ("d-1",), ())


def audit_records(event_id: str, run_id: str, n: int = 2) -> tuple[dict[str, Any], ...]:
    return tuple(
        make_audit_record(
            "run",
            caller=CALLER,
            run_id=run_id,
            pr=PR,
            inputs={
                "head_sha": SHA,
                "config_sha256": H64,
                "skill": "review-run:review-pr",
                "previous_run": None,
                "permission_profile_sha256": H64,
                "session_sandbox": "required",
                "setting_sources": [],
                "plugin_dirs_sha256": None,
                "override_escalation": False,
            },
            result={"status": "done", "reason": None, "exit_code": 0},
            event_id=event_id,
            attempt_id=f"attempt-{i:04d}",
            ts="2026-01-01T12:00:00.000Z",
        )
        for i in range(n)
    )


def seed(
    store: Store,
    *,
    flag_hooks: tuple[str, ...] = (),
    hook: str = "post-run",
    number: int = 1,
    payload: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """A run ended (``done`` for post-run, ``escalated`` for post-escalate) with one event."""
    run_id = store.reserve_id()
    store.create_run(NewRun(run_id, PR, REPO, SHA, "headless", "s", hooks=flag_hooks))
    event_id = hooks.event_id_for(run_id, hook, number)
    if payload is None:
        if hook == "post-run":
            payload = hooks.post_run_payload(
                run_id=run_id,
                repo=REPO,
                labels=[],
                event_id=event_id,
                state="done",
                reason=None,
                exit_code=0,
                pr=PR,
                agent_id="ag-abcdefgh",
                draft_id="0" * 26,
                anchor_head_sha=SHA,
                actor="auto:unknown",
                decision_ids=[],
                warnings=[],
                summary={
                    "verdict": "comment",
                    "severity_counts": dict(
                        blocker=0, major=0, minor=0, nit=0, info=0, unspecified=0
                    ),
                    "risk": "low",
                    "coverage_status": "full",
                    "code_run_status": "ok",
                    "head_moved": False,
                },
                session_sandbox="required",
                tools_checked=True,
                tools_unexpected=0,
            )
        else:
            payload = hooks.post_escalate_payload(
                run_id=run_id,
                repo=REPO,
                labels=[],
                event_id=event_id,
                reason="needs_input",
                pr=PR,
                agent_id="ag-abcdefgh",
                actor="auto:unknown",
                decision_ids=[],
                session_sandbox="required",
                warnings=[],
            )
    lease = store.acquire_lease(lease_name(run_id))
    assert lease is not None
    assert store.begin_finalize(run_id, Expect("running", "prefetch", None), lease=lease)
    ended = hook == "post-run"
    assert store.end_run(
        run_id,
        Expect("running", "finalize", None),
        lease=lease,
        state="done" if ended else "escalated",
        reason=None if ended else "needs_input",
        exit_code=0 if ended else 13,
        events=[NewEvent(event_id, hook, payload, audit_records(event_id, run_id))],
    )
    store.release_lease(lease)
    return run_id, event_id


def event_row(store: Store, event_id: str) -> tuple[int, int, int]:
    """``(attempts, delivered, audit_pending)`` straight from the table."""
    row = store._conn.execute(  # noqa: SLF001
        "SELECT attempts, delivered, audit_pending FROM events WHERE event_id = ?", (event_id,)
    ).fetchone()
    assert row is not None
    return int(row[0]), int(row[1]), int(row[2])


def appender(out: Path) -> str:
    """Python source for a hook (run with ``PY -c``) that appends its event id to ``out``."""
    return f"import os; open({str(out)!r}, 'a').write(os.environ['REVIEW_RUN_EVENT_ID'] + '\\n')"
