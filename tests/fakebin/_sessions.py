"""The agent and session model shared by the fake ``agent-spawn`` and ``claude-session``.

Agents live in ``state.json`` under ``"agents"`` (``state["agents"][agent_id]``); a session is the
agent's ``csession_id`` and is answered by the fake claude-session from the same record, so the two
fakes never disagree (``agent-spawn teardown`` of a live agent is refused, and so is
``claude-session forget`` of its live or pending session).

A run's *plan* says how it ends (a scenario sets it, see ``tests/fake_scenarios.py``)::

    {"outcome": "done",             # a key of OUTCOMES
     "end_reason": None,            # default from OUTCOMES
     "summary": "...",              # report.summary (done)
     "artifact_text": "...",        # frozen artifact content (written under the session dir)
     "artifact": {...} | None,      # explicit artifact object (wins), or None for no artifact
     "implicit": False,
     "result_lines": [...],         # lines of the headless result file (init object by default)
     "result_path": "default" | "none" | "missing",
     "init": {"skills": [...], "tools": [...]},
     "auto_resume": {plan},         # exited-unreported, then the automatic report resume
     "message": None, "cost_usd": 0.12, "num_turns": 7, "warnings": []}
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from tests.fakebin._common import base32_id, fake_dir, iso

#: outcome -> (exit code, final claude-session state, default end_reason, phase)
OUTCOMES: dict[str, tuple[int, str, str, str]] = {
    "done": (0, "exited", "normal", "session"),
    "blocked": (13, "exited", "normal", "session"),
    "needs-input": (13, "exited", "normal", "session"),
    "failed": (18, "exited", "normal", "session"),
    "exited-unreported": (14, "exited-unreported", "no_report", "session"),
    "killed": (15, "killed", "stopped", "session"),
    "timeout": (16, "timeout", "timeout", "session"),
    "lost": (17, "lost", "lost", "session"),
    "not-started": (12, "not-started", "never_adopted", "launch"),
    "vetoed": (11, "not-started", "hook_veto", "launch"),
    "escalated": (13, "not-started", "hook_escalate", "launch"),
}
#: outcome -> (park_reason, needs_attention)
PARKING: dict[str, tuple[str | None, str | None]] = {
    "done": ("done", None),
    "blocked": ("blocked", "blocked"),
    "needs-input": ("escalated", "escalated"),
    "failed": ("failed", "failed"),
    "exited-unreported": ("unreported", "unreported"),
    "killed": ("killed", None),
    "timeout": ("timeout", "timeout"),
    "lost": ("lost", "lost"),
    "not-started": ("launch_failed", "launch_failed"),
    "vetoed": ("vetoed", None),
    "escalated": ("escalated", "escalated"),
}
REPORTED = ("done", "blocked", "needs-input", "failed")
DEFAULT_TOOLS = ["Skill", "Read", "Write", "Edit", "Glob", "Grep", "Bash", "ToolSearch",
                 "TodoWrite"]  # fmt: skip
DEFAULT_PLAN: dict[str, Any] = {"outcome": "done"}


def agents(state: dict[str, Any]) -> dict[str, Any]:
    return state.setdefault("agents", {})


def session_dir(csession_id: str) -> Path:
    return fake_dir() / "sessions" / csession_id


def resolved_plan(plan: dict[str, Any] | None) -> dict[str, Any]:
    merged = {**DEFAULT_PLAN, **(plan or {})}
    if merged["outcome"] not in OUTCOMES:
        raise SystemExit(98)
    return merged


def new_ids(state: dict[str, Any]) -> tuple[str, str]:
    n = state["_agent_n"] = state.get("_agent_n", 0) + 1
    return base32_id("ag-", f"agent-{n}"), base32_id("cs-", f"session-{n}")


def new_agent(state: dict[str, Any], settings: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    agent_id, csession_id = new_ids(state)
    now = iso()
    rec = {
        "agent_id": agent_id,
        "csession_id": csession_id,
        "status": "live",
        "settings": settings,
        "run": 1,
        "runs": [new_run(1, plan, settings["mode"], now)],
        "park_reason": None,
        "needs_attention": None,
        "last_outcome": None,
        "last_end_reason": None,
        "created": now,
        "updated": now,
        "last_live_at": None,
        "torn_down_at": None,
        "supervisor_pid": 424242,
    }
    agents(state)[agent_id] = rec
    return rec


def new_run(n: int, plan: dict[str, Any], mode: str, now: str, auto: bool = False) -> dict[str, Any]:
    return {"run": n, "plan": resolved_plan(plan), "mode": mode, "final": False,
            "started_at": now, "ended_at": None, "auto_resumed": auto, "outcome": None}  # fmt: skip


def last_run(rec: dict[str, Any]) -> dict[str, Any]:
    return rec["runs"][-1]


def write_artifact(rec: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any] | None:
    if "artifact" in plan:  # explicit: an object (verbatim, however wrong) or None
        return plan["artifact"]
    text = plan.get("artifact_text", '{"fake": "review"}\n')
    base = session_dir(rec["csession_id"]) / "artifacts"
    base.mkdir(parents=True, exist_ok=True)
    data = text.encode("utf-8")
    path = base / "review.json"
    path.write_bytes(data)
    artifact = {"name": "review.json", "path": str(path),
                "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}  # fmt: skip
    if plan.get("mismatch"):  # the report lies about the frozen file
        artifact["size"] += int(plan["mismatch"])
        artifact["sha256"] = hashlib.sha256(data + b"?").hexdigest()
    return artifact


def result_path_for(rec: dict[str, Any], run: dict[str, Any]) -> str | None:
    """Write the headless result file (a stream-json style transcript) and return its path."""
    plan, mode = run["plan"], run["mode"]
    if mode != "headless" or plan.get("result_path") == "none":
        return None
    path = session_dir(rec["csession_id"]) / "private" / f"result-{run['run']}.json"
    if plan.get("result_path") == "missing":
        return str(path)
    lines = plan.get("result_lines")
    if lines is None:
        init = {"type": "system", "subtype": "init", "session_id": "claude-fake",
                "tools": DEFAULT_TOOLS, "skills": [rec["settings"].get("skill") or "none"],
                **plan.get("init", {})}  # fmt: skip
        lines = [init, {"type": "result", "subtype": "success", "is_error": False}]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join((ln if isinstance(ln, str) else json.dumps(ln)) + "\n" for ln in lines),
        encoding="utf-8",
    )
    return str(path)


def finalize(rec: dict[str, Any], run: dict[str, Any]) -> None:
    """End ``run`` as its plan says; park the agent per the outcome table."""
    plan = run["plan"]
    outcome = plan["outcome"]
    code, state, end_reason, phase = OUTCOMES[outcome]
    now = iso()
    report = None
    if outcome in REPORTED:
        report = {
            "status": outcome,
            "implicit": bool(plan.get("implicit", False)),
            "summary": plan.get("summary", "Fake summary."),
            "reason": plan.get("reason") if outcome != "done" else None,
            "artifact": write_artifact(rec, plan) if outcome == "done" else None,
            "reported_at": now,
        }
        if plan.get("implicit"):
            report["summary"] = None
            report["artifact"] = {"name": "result.md", "path": "/nonexistent/result.md",
                                  "sha256": "0" * 64, "size": 0}  # fmt: skip
    run.update(
        final=True,
        ended_at=now,
        outcome={
            "phase": phase,
            "outcome": outcome,
            "end_reason": plan.get("end_reason", end_reason),
            "exit_code": code,
            "state": state,
            "report": report,
            "session_started_at": None if phase == "launch" else run["started_at"],
            "cost_usd": None if phase == "launch" else plan.get("cost_usd", 0.12),
            "num_turns": None if phase == "launch" else plan.get("num_turns", 7),
            "message": plan.get("message"),
            "warnings": list(plan.get("warnings", [])),
            "first_working_at": None if phase == "launch" else run["started_at"],
            "result_path": result_path_for(rec, run) if phase == "session" else None,
        },
    )
    rec["last_outcome"] = outcome
    rec["last_end_reason"] = run["outcome"]["end_reason"]
    rec["last_live_at"] = now
    rec["updated"] = now
    if plan.get("auto_resume") and outcome == "exited-unreported":
        # agent-spawn's single automatic report-resume: the same agent, the next run
        nxt = new_run(run["run"] + 1, plan["auto_resume"], run["mode"], now, auto=True)
        rec["runs"].append(nxt)
        rec["run"] = nxt["run"]
        finalize(rec, nxt)
        return
    rec["status"] = "parked"
    rec["park_reason"], rec["needs_attention"] = PARKING[outcome]


def run_outcome_doc(rec: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    """agent-spawn's outcome object (``defs.json`` ``$defs/outcome``) for a final run."""
    o = run["outcome"]
    return {
        "agent_id": rec["agent_id"],
        "csession_id": rec["csession_id"],
        "run": run["run"],
        "forked_from": None,
        "claude_session_id": None if o["phase"] == "launch" else f"claude-{rec['csession_id']}",
        "phase": o["phase"],
        "outcome": o["outcome"],
        "end_reason": o["end_reason"],
        "exit_code": o["exit_code"],
        "state": o["state"],
        "report": o["report"],
        "session_started_at": o["session_started_at"],
        "cost_usd": o["cost_usd"],
        "message": o["message"],
        "warnings": o["warnings"],
        "promoted": False,
        "auto_resumed": run["auto_resumed"],
        "first_working_at": o["first_working_at"],
    }


def cs_state(rec: dict[str, Any]) -> tuple[str, str | None, str | None]:
    """(state, activity, needs_input_reason) of the current run."""
    run = last_run(rec)
    if run["final"]:
        return run["outcome"]["state"], None, None
    return "working", "working", None


def agent_doc(rec: dict[str, Any]) -> dict[str, Any]:
    s = rec["settings"]
    state, activity, needs = cs_state(rec)
    run = last_run(rec)
    final = run["final"]
    if final and run["outcome"]["outcome"] == "needs-input":
        needs = "escalation"
    return {
        "agent_id": rec["agent_id"],
        "status": rec["status"],
        "needs_attention": rec["needs_attention"],
        "park_reason": rec["park_reason"],
        "kind": s.get("kind"),
        "run_kind": s.get("kind"),
        "escalated_by": None,
        "mode": s["mode"],
        "placement": "window" if s["mode"] == "interactive" else None,
        "term_placement": None,
        "name": s.get("name") or f"{s.get('kind') or 'agent'} {rec['agent_id'][:6]}",
        "repo": s.get("repo"),
        "cwd": s.get("cwd") or "session-dir",
        "labels": list(s.get("labels", [])),
        "csession_id": rec["csession_id"],
        "run": rec["run"],
        "session_state": state,
        "activity": activity,
        "needs_input_reason": needs,
        "forked_from": None,
        "worktree": None,
        "holds": [],
        "window": None,
        "promote": s.get("promote"),
        "promotion_pending": False,
        "ephemeral": bool(s.get("ephemeral", False)),
        "last_outcome": rec["last_outcome"],
        "last_end_reason": rec["last_end_reason"],
        "tainted": False,
        "supervisor_lost": False,
        "teardown_step": None,
        "created": rec["created"],
        "last_live_at": rec["last_live_at"],
        "updated": rec["updated"],
        "torn_down_at": rec["torn_down_at"],
        "warnings": [],
    }


def is_live(rec: dict[str, Any]) -> bool:
    return rec["status"] in ("live", "launching") or not last_run(rec)["final"]
