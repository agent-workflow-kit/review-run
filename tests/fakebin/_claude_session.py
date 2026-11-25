"""Fake ``claude-session`` (pinned 0.45.2): ``status`` and ``forget``.

Sessions are the agents of the shared model (``_sessions.py``): ``status`` answers for the agent
whose ``csession_id`` it is given, ``forget`` refuses a live or pending session (exit 20,
``reason: live``) as the real tool does. Grammar and rules follow claude-session's
``docs/interface.md`` (global flags follow the command; only ``--version`` precedes it).
"""

from __future__ import annotations

import re
from typing import Any

from tests.fakebin import _sessions as sess
from tests.fakebin._common import (
    Call,
    CommandSpec,
    Grammar,
    Out,
    Parsed,
    State,
    Usage,
    actor_check,
    apply_step,
    decision_check,
    error_out,
    flag,
    iso,
    next_step,
    positive_int_check,
    run_tool,
)

TOOL = "claude-session"
_ID = re.compile(r"cs-[a-z2-7]{8}")

_GLOBAL = {
    "--json": flag(),
    "--config": flag(takes=True),
    "--verbose": flag(),
    "--quiet": flag(),
    "--yes": flag(),
    "--version": flag(),
    "--help": flag(),
}
GRAMMAR = Grammar(
    tool=TOOL,
    global_flags=_GLOBAL,
    commands={
        ("status",): CommandSpec(flags={"--run": flag(takes=True, check=positive_int_check)},
                                 min_args=1, max_args=1),  # fmt: skip
        ("forget",): CommandSpec(
            flags={
                "--purge": flag(),
                "--actor": flag(takes=True, check=actor_check),
                "--decision-id": flag(takes=True, repeat=True, check=decision_check),
                "--dry-run": flag(),
            },
            min_args=1,
            max_args=1,
        ),
    },
    unimplemented=frozenset(
        (c,)
        for c in (
            "exec start resume wait stop list report transcript label reconcile cron export "
            "kinds verify-secret hook-event"
        ).split()
    ),
    global_before_command=frozenset({"--version", "--help"}) | frozenset(),
)


def _find(state: dict[str, Any], csid: str) -> dict[str, Any] | None:
    return next((r for r in sess.agents(state).values() if r["csession_id"] == csid), None)


def _not_found(csid: str) -> Out:
    return error_out(TOOL, 10, {"code": "not_found", "message": "unknown or forgotten session"})


def _status_doc(rec: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    s = rec["settings"]
    final = run["final"]
    o = run["outcome"] or {}
    state, activity, _ = (o["state"], None, None) if final else ("working", "working", None)
    needs = "escalation" if final and o.get("outcome") == "needs-input" else None
    phase = o.get("phase", "session")
    now = iso()
    mode = run["mode"]
    run_doc = {
        "run": run["run"],
        "run_kind": s.get("kind") or "agent",
        "reads_third_party": s.get("kind") == "review",
        "implicit_done": not s.get("no_implicit_done", False),
        "mode": mode,
        "cwd": s.get("cwd") if str(s.get("cwd", "")).startswith("/") else None,
        "repo": s.get("repo"),
        "model": s.get("model"),
        "skill": s.get("skill"),
        "has_prompt": bool(s.get("has_prompt", True)),
        "sandbox": s.get("sandbox", "required"),
        "sandbox_active": s.get("sandbox", "required") != "off" and phase == "session",
        "permission_profile": None,
        "decision_ids": [],
        "input_files": list(s.get("input_names", [])),
        "pending": False,
        "expires_at": None,
        "started_at": None if phase == "launch" else run["started_at"],
        "session_started_at": None if phase == "launch" else run["started_at"],
        "ended_at": run["ended_at"],
        "state": state,
        "phase": phase,
        "message": o.get("message"),
        "stop_requested_at": None,
        "warnings": list(o.get("warnings", [])),
        "state_since": run["ended_at"] or run["started_at"],
        "wrapper_pid": None if final else 424243,
        "wrapper_alive": not final,
        "child_pid": None if final else 424244,
        "child_alive": not final,
        "backstop_used": False,
        "report": o.get("report"),
        "outcome": o.get("outcome"),
        "end_reason": o.get("end_reason"),
        "session_end_reason": None,
        "exit_code": o.get("exit_code"),
        "result_path": o.get("result_path"),
        "cost_usd": o.get("cost_usd"),
        "num_turns": o.get("num_turns"),
        "is_error": None if not final else False,
    }
    del now
    return {
        "csession_id": rec["csession_id"],
        "created_at": rec["created"],
        "launch_kind": s.get("kind") or "agent",
        "run_kind": s.get("kind") or "agent",
        "state": state,
        "activity": activity,
        "needs_input_reason": needs,
        "live": not final,
        "tainted": False,
        "tainted_at": None,
        "tainted_by_run": None,
        "forked_from": None,
        "labels": list(s.get("labels", [])),
        "repo": s.get("repo"),
        "mode": mode,
        "current_run": rec["run"],
        "pending": False,
        "expires_at": None,
        "updated_at": rec["updated"],
        "csession_dir": str(sess.session_dir(rec["csession_id"])),
        "claude_session_id": None if phase == "launch" else f"claude-{rec['csession_id']}",
        "claude_session_ids": [],
        "run": run_doc,
        "runs": len(rec["runs"]),
        "recent_events": [],
        "warnings": [],
    }


def _id(arg: str) -> str:
    if not _ID.fullmatch(arg):
        raise Usage("a session id is cs- followed by 8 characters of a-z2-7")
    return arg


def _status(call: Call, parsed: Parsed) -> Out:
    csid = _id(parsed.args[0])
    step = next_step(TOOL, "status")

    def default() -> Out:
        with State() as state:
            rec = _find(state, csid)
            if rec is None or rec.get("forgotten"):
                return _not_found(csid)
            want = parsed.get("--run")
            runs = {r["run"]: r for r in rec["runs"]}
            run = runs.get(int(want)) if want else sess.last_run(rec)
            if run is None:
                return error_out(TOOL, 10, {"code": "not_found", "message": "no such run"})
            doc = _status_doc(rec, run)
        if not parsed.has("--json"):
            return Out(text=f"{csid} {doc['state']} (fake human output; use --json)\n")
        return Out(doc=doc)

    return apply_step(TOOL, step, default, call, schema=f"{TOOL}/cli/status.json")


def _forget(call: Call, parsed: Parsed) -> Out:
    csid = _id(parsed.args[0])
    step = next_step(TOOL, "forget")

    def default() -> Out:
        with State() as state:
            rec = _find(state, csid)
            if rec is None or rec.get("forgotten"):
                return _not_found(csid)
            if sess.is_live(rec):
                return error_out(
                    TOOL,
                    20,
                    {"code": "refused", "message": "the session's run is live or pending",
                     "reason": "live"},
                )  # fmt: skip
            now = iso()
            if not parsed.has("--dry-run"):
                rec["forgotten"] = True
            entry: dict[str, Any] = {"csession_id": csid, "forgotten_at": now,
                                     "delete_after": "2099-01-01T00:00:00Z"}  # fmt: skip
            if parsed.has("--purge"):
                entry["purged"] = not parsed.has("--dry-run")
        return Out(doc={"dry_run": parsed.has("--dry-run"), "forgotten": [entry], "skipped": []})

    return apply_step(TOOL, step, default, call, schema=f"{TOOL}/cli/forget.json")


def handler(call: Call, parsed: Parsed) -> Out:
    if parsed.has("--verbose") and parsed.has("--quiet"):
        raise Usage("--verbose and --quiet are mutually exclusive")
    return {"status": _status, "forget": _forget}[parsed.command[0]](call, parsed)


def main(argv: list[str]) -> None:
    run_tool(TOOL, GRAMMAR, handler, argv)
