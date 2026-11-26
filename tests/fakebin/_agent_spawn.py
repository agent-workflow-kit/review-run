"""Fake ``agent-spawn`` (pinned 0.38.1): launch, resume, wait, show, stop, teardown, list.

Stateful: an agent is created by ``launch`` and stays ``live`` until ``wait`` (or ``launch
--wait``) ends its run as the plan says (``_sessions.py``); ``teardown`` of a live agent exits 20
``reason: live`` and removes a parked one. Grammar, flag checks, exit codes, the error envelope
and ``--result-file`` follow agent-spawn's ``docs/interface.md`` (Launch flags, Commands, Exit
codes, Output by exit code, Result file) and the pinned ``schemas/cli/*.json``.
"""

from __future__ import annotations

import os
import re
import signal
import time
from typing import Any

from tests.fakebin import _sessions as sess
from tests.fakebin._common import (
    ACTOR,
    Call,
    CommandSpec,
    Grammar,
    Out,
    Parsed,
    State,
    Unimplemented,
    Usage,
    actor_check,
    apply_step,
    decision_check,
    duration_check,
    error_out,
    flag,
    iso,
    label_check,
    next_step,
    positive_int_check,
    result_file_write,
    run_tool,
    scenario,
    sha_check,
)

TOOL = "agent-spawn"
_AGENT_ID = re.compile(r"ag-[a-z2-7]{8}")
_REPO = re.compile(
    r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*(?::[0-9]{1,5})?"
    r"(?:/(?!\.\.?(?:/|$))[A-Za-z0-9._-]+)+"
)
_NAME_OF_HOOK = ("pre-launch", "post-launch", "pre-teardown", "post-teardown")
_RESERVED_LABELS = ("agent", "kind")
_STATUSES = ("launching", "live", "parked", "tearing_down", "torn_down")


def _repo_check(value: str) -> str | None:
    return None if _REPO.fullmatch(value) else "is not a repo id"


def _hook_check(allowed: tuple[str, ...]):  # type: ignore[no-untyped-def]
    def check(value: str) -> str | None:
        name, sep, cmd = value.partition("=")
        return None if sep and cmd and name in allowed else "is not NAME=CMD of an allowed hook"

    return check


def _timeout_check(value: str) -> str | None:
    """claude-session's ``--timeout <duration>|none`` (passed through; ``0`` is refused)."""
    return None if value == "none" else duration_check(value)


_GLOBAL = {
    "--json": flag(),
    "--config": flag(takes=True),
    "--verbose": flag(),
    "--quiet": flag(),
    "--version": flag(),
    "--help": flag(),
    "--yes": flag(),
    "--actor": flag(takes=True, check=actor_check),
    "--decision-id": flag(takes=True, repeat=True, check=decision_check),
}

_LAUNCH_FLAGS = {
    # claude-session pass-throughs
    "--kind": flag(
        takes=True,
        check=lambda v: None if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", v) else "is not a kind",
    ),  # fmt: skip
    "--mode": flag(takes=True, choices=("interactive", "headless")),
    "--skill": flag(takes=True),
    "--plugin-dir": flag(takes=True, repeat=True),
    "--model": flag(takes=True),
    "--timeout": flag(takes=True, check=_timeout_check),
    "--max-turns": flag(takes=True, check=positive_int_check),
    "--permission-profile": flag(takes=True),
    "--sandbox": flag(takes=True, choices=("required", "preferred", "off")),
    "--setting-sources": flag(takes=True),
    "--reads-third-party": flag(),
    "--env-allow": flag(takes=True, repeat=True),
    "--env-allowlist-only": flag(takes=True, repeat=True),
    "--auto-exit": flag(),
    "--no-auto-exit": flag(),
    "--implicit-done": flag(),
    "--no-implicit-done": flag(),
    "--reset": flag(takes=True, repeat=True),
    "--allow-tainted": flag(),
    # agent-spawn's own
    "--repo": flag(takes=True, check=_repo_check),
    "--worktree": flag(takes=True),
    "--branch": flag(takes=True),
    "--base": flag(takes=True),
    "--ref": flag(takes=True),
    "--detach": flag(),
    "--worktree-name": flag(takes=True),
    "--no-bootstrap": flag(),
    "--copy-env": flag(),
    "--cwd": flag(takes=True),
    "--prompt": flag(takes=True),
    "--prompt-file": flag(takes=True),
    "--input-file": flag(takes=True, repeat=True),
    "--placement": flag(takes=True, choices=("window", "here")),
    "--term-placement": flag(takes=True),
    "--name": flag(takes=True),
    "--label": flag(takes=True, repeat=True, check=label_check),
    "--promote": flag(),
    "--no-promote": flag(),
    "--ephemeral": flag(),
    "--wait": flag(),
    "--wait-timeout": flag(takes=True, check=duration_check),
    "--result-file": flag(takes=True),
    "--hook": flag(takes=True, repeat=True, check=_hook_check(("pre-launch", "post-launch"))),
    "--override-escalation": flag(),
    "--dry-run": flag(),
}
_RESUME_FLAGS = {
    **{
        k: v
        for k, v in _LAUNCH_FLAGS.items()
        if k
        not in (
            "--repo",
            "--worktree",
            "--branch",
            "--base",
            "--ref",
            "--detach",
            "--worktree-name",
            "--no-bootstrap",
            "--copy-env",
            "--ephemeral",
            "--hook",
        )
    },  # fmt: skip
    "--fork": flag(),
    "--select": flag(takes=True, repeat=True),
    "--hook": flag(takes=True, repeat=True, check=_hook_check(("post-launch",))),
}

GRAMMAR = Grammar(
    tool=TOOL,
    global_flags=_GLOBAL,
    commands={
        ("launch",): CommandSpec(flags=_LAUNCH_FLAGS),
        ("resume",): CommandSpec(flags=_RESUME_FLAGS, min_args=0, max_args=1),
        ("wait",): CommandSpec(
            flags={
                "--timeout": flag(takes=True, check=duration_check),
                "--result-file": flag(takes=True),
            },  # fmt: skip
            min_args=1,
            max_args=1,
        ),
        ("show",): CommandSpec(flags={"--no-liveness": flag()}, min_args=1, max_args=1),
        ("stop",): CommandSpec(
            flags={"--grace": flag(takes=True, check=duration_check), "--dry-run": flag()},
            min_args=1,
            max_args=1,
        ),
        ("teardown",): CommandSpec(
            flags={
                "--merged-head": flag(takes=True, check=sha_check),
                "--force": flag(),
                "--override-escalation": flag(),
                "--hook": flag(
                    takes=True, repeat=True, check=_hook_check(("pre-teardown", "post-teardown"))
                ),  # fmt: skip
                "--dry-run": flag(),
            },
            min_args=1,
            max_args=1,
        ),
        ("list",): CommandSpec(
            flags={
                "--label": flag(takes=True, repeat=True, check=label_check),
                "--needs-attention": flag(),
                "--status": flag(takes=True, repeat=True, choices=_STATUSES),
                "--session-state": flag(takes=True, repeat=True),
                "--kind": flag(takes=True),
                "--repo": flag(takes=True, check=_repo_check),
                "--mode": flag(takes=True, choices=("interactive", "headless")),
                "--forked-from": flag(takes=True),
                "--since": flag(takes=True),
                "--no-liveness": flag(),
            }
        ),
    },
    duplicates="last_wins",  # plain argparse: a repeated scalar flag keeps the last value
    unimplemented=frozenset(
        [
            ("attach",),
            ("ack",),
            ("label", "add"),
            ("label", "remove"),
            ("prune",),
            ("reconcile",),
            ("cron", "install"),
            ("cron", "uninstall"),
            ("export",),
            ("browse",),
            ("validate",),
        ]
    ),
    mutually_exclusive=(
        ("--implicit-done", "--no-implicit-done"),
        ("--auto-exit", "--no-auto-exit"),
        ("--promote", "--no-promote"),
        ("--verbose", "--quiet"),
        ("--prompt", "--prompt-file"),
    ),
)


# --------------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------------


def _human(parsed: Parsed) -> bool:
    actor = parsed.get("--actor")
    return actor is not None and actor.startswith("human:") and bool(ACTOR.fullmatch(actor))


def _agent_id(arg: str) -> str:
    if not _AGENT_ID.fullmatch(arg):
        raise Usage("an agent id is ag- followed by 8 characters of a-z2-7")
    return arg


def _seconds(value: str) -> float:
    m = re.fullmatch(r"([0-9.]+)(ms|s|m|h|d)?", value)
    assert m
    scale = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2) or "s"]
    return float(m.group(1)) * scale


_REFUSED_RESULT_FILE: list[str] = []
_INPUT_NAME = re.compile(r"[A-Za-z0-9._-]{1,128}")
MAX_LABELS = 64  # agent-spawn's labels.MAX_LABELS: distinct user pairs, ``job=`` not counted
MAX_DECISION_IDS = 32


def _third_party_reading(parsed: Parsed) -> bool:
    return parsed.get("--kind") == "review" or parsed.has("--reads-third-party")


def _inside(path: str, directory: str) -> bool:
    path, directory = os.path.realpath(path), os.path.realpath(directory)
    return path == directory or path.startswith(directory.rstrip("/") + "/")


def _check_result_file(parsed: Parsed, session_dir: str | None) -> None:
    """The third-party path rule: a third-party-reading run's result file may not be written
    into the run's cwd or its session directory (exit 2). Only the cwds the fake can know are
    checked: an absolute ``--cwd`` or ``--worktree <path>``, and the session directory."""
    result = parsed.get("--result-file")
    if result is None or not _third_party_reading(parsed):
        return
    parent = os.path.dirname(os.path.abspath(result))
    cwd = parsed.get("--cwd")
    worktree = parsed.get("--worktree", "none")
    bases = [session_dir] if session_dir else []
    if worktree not in ("new", "none", None):
        bases.append(worktree)
    elif cwd is not None and cwd != "session-dir" and os.path.isabs(cwd):
        bases.append(cwd)
    if any(_inside(parent, base) for base in bases if base):
        _REFUSED_RESULT_FILE.append(result)  # such a path is never written, not even the error
        raise Usage("--result-file may not lie inside a third-party-reading run's cwd or session")


def _check_launch_flags(parsed: Parsed, *, resume: bool, session_dir: str | None = None) -> None:
    for name in ("--override-escalation", "--allow-tainted"):
        if parsed.has(name) and not _human(parsed):
            raise Usage(f"{name} is human-only: pass --actor human:<user>")
    labels = list(dict.fromkeys(parsed.all("--label")))
    for lab in labels:
        if lab.split("=", 1)[0] in _RESERVED_LABELS:
            raise Usage("the label keys 'agent' and 'kind' are reserved")
    if len([lab for lab in labels if not lab.startswith("job=")]) > MAX_LABELS:
        raise Usage(f"at most {MAX_LABELS} labels per agent")
    for name in ("--prompt-file", "--input-file", "--permission-profile"):
        for path in parsed.all(name):
            if name == "--prompt-file" and path == "-":
                continue  # the prompt is read from stdin
            if not os.path.isfile(path):
                raise Usage(f"{name}: no such file")
    names = [os.path.basename(p) for p in parsed.all("--input-file")]
    if any(not _INPUT_NAME.fullmatch(n) for n in names) or len(set(names)) != len(names):
        raise Usage("--input-file names are 1 to 128 of [A-Za-z0-9._-] and must be distinct")
    for path in parsed.all("--plugin-dir"):
        if not os.path.isdir(path):
            raise Usage("--plugin-dir: no such directory")
    if parsed.has("--wait-timeout") and not (
        parsed.has("--wait") or parsed.get("--placement") == "here"
    ):
        raise Usage("--wait-timeout needs --wait")
    _check_result_file(parsed, session_dir)
    if resume:
        if parsed.has("--select"):
            raise Unimplemented("resume --select")
        return
    worktree = parsed.get("--worktree", "none")
    assert worktree is not None
    if worktree not in ("new", "none") and not os.path.isabs(worktree):
        raise Usage("--worktree takes new, none or an absolute path")
    if worktree == "new" and not parsed.has("--repo"):
        raise Usage("--repo is required with --worktree new")
    if worktree != "new":
        for name in ("--branch", "--base", "--ref", "--detach", "--worktree-name",
                     "--no-bootstrap", "--copy-env"):  # fmt: skip
            if parsed.has(name):
                raise Usage(f"{name} needs --worktree new")
    if (
        parsed.get("--mode", "interactive") == "headless"
        and not parsed.has("--skill")
        and not (parsed.has("--prompt") or parsed.has("--prompt-file"))
    ):
        raise Usage("a headless launch needs --skill or a prompt")
    cwd = parsed.get("--cwd")
    if cwd == "session-dir" and worktree != "none":
        raise Usage("--cwd session-dir cannot be used with a worktree")
    if cwd is not None and cwd != "session-dir" and worktree == "none" and not os.path.isabs(cwd):
        raise Usage("--cwd must be absolute without a worktree")


def _settings(parsed: Parsed) -> dict[str, Any]:
    return {
        "kind": parsed.get("--kind"),
        "mode": parsed.get("--mode", "interactive"),
        "skill": parsed.get("--skill"),
        "repo": parsed.get("--repo"),
        "cwd": parsed.get("--cwd"),
        "labels": list(dict.fromkeys(parsed.all("--label"))),  # first-seen order, once each
        "name": parsed.get("--name"),
        "promote": True
        if parsed.has("--promote")
        else False
        if parsed.has("--no-promote")
        else None,
        "ephemeral": parsed.has("--ephemeral"),
        "model": parsed.get("--model"),
        "sandbox": parsed.get("--sandbox", "required"),
        "no_implicit_done": parsed.has("--no-implicit-done"),
        "has_prompt": parsed.has("--prompt") or parsed.has("--prompt-file"),
        "input_names": [os.path.basename(p) for p in parsed.all("--input-file")],
        "plugin_dirs": parsed.all("--plugin-dir"),
    }


def _result_doc(
    rec: dict[str, Any], run: dict[str, Any] | None, *, timed_out: bool = False
) -> dict[str, Any]:
    final = run is not None and run["final"]
    o = run["outcome"] if final and run is not None else None
    return {
        "agent": sess.agent_doc(rec),
        "phase": o["phase"] if o else None,
        "outcome": o["outcome"] if o else None,
        "end_reason": o["end_reason"] if o else None,
        "exit_code": o["exit_code"] if o else None,
        "run": sess.run_outcome_doc(rec, run) if final and run is not None else None,
        "first_working_at": o["first_working_at"] if o else None,
        "timed_out": timed_out,
        "warnings": [],
    }


def _result_out(rec: dict[str, Any], run: dict[str, Any] | None) -> Out:
    doc = _result_doc(rec, run)
    return Out(exit=doc["exit_code"] or 0, doc=doc)


def _as_error(exit_code: int, code: str, message: str, **extra: Any) -> Out:
    return error_out(TOOL, exit_code, {"code": code, "message": message, **extra})


def _not_found(agent_id: str, **details: Any) -> Out:
    return _as_error(10, "not_found", "no such agent", agent_id=None,
                     details=details or None)  # fmt: skip


def _plan_from(step: dict[str, Any] | None, key: str) -> dict[str, Any]:
    if step is not None and "plan" in step:
        return dict(step["plan"])
    return dict(scenario("sessions").get(key, {}))


def _write_result_file(argv: list[str]):  # type: ignore[no-untyped-def]
    """``--result-file`` receives exactly what ``--json`` prints, whatever the exit code.

    The path comes from a pre-scan of the raw argv (so argparse-style usage errors are covered)
    for ``launch``, ``resume`` and ``wait`` only.
    """
    command = next((a for a in argv if not a.startswith("-")), "")
    path = None
    if command in ("launch", "resume", "wait"):
        words = argv[: argv.index("--")] if "--" in argv else argv
        for i, tok in enumerate(words):
            if tok == "--result-file" and i + 1 < len(words):
                path = words[i + 1]
            elif tok.startswith("--result-file="):
                path = tok.partition("=")[2]

    def hook(out: Out) -> None:
        if path and path not in _REFUSED_RESULT_FILE and out.doc is not None:
            import json

            result_file_write(os.path.abspath(path), json.dumps(out.doc) + "\n")

    return hook


# --------------------------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------------------------


def _interrupted(signum: int) -> Out:
    """A launcher interrupted before any supervisor existed: the envelope, exit 143 for SIGTERM
    and 130 for SIGHUP and SIGINT (interface.md, Signals)."""
    return _as_error(143 if signum == signal.SIGTERM else 130, "launch_failed", "interrupted",
                     details={"cause": "interrupted"})  # fmt: skip


def _dry_run_doc(parsed: Parsed, resume: bool) -> dict[str, Any]:
    s = _settings(parsed)
    return {
        "dry_run": True,
        "resume": resume,
        "agent_settings": {
            "kind": s["kind"], "mode": s["mode"],
            "placement": "window" if s["mode"] == "interactive" else None,
            "term_placement": parsed.get("--term-placement"), "name": s["name"],
            "repo": s["repo"], "worktree": parsed.get("--worktree", "none"),
            "branch": parsed.get("--branch"), "base": parsed.get("--base"),
            "ref": parsed.get("--ref"), "detach": parsed.has("--detach"),
            "worktree_name": parsed.get("--worktree-name"),
            "no_bootstrap": parsed.has("--no-bootstrap"), "copy_env": parsed.has("--copy-env"),
            "cwd": s["cwd"], "labels": s["labels"], "skill": s["skill"],
            "has_prompt": s["has_prompt"], "input_names": s["input_names"],
            "plugin_dirs": s["plugin_dirs"], "promote": s["promote"],
            "ephemeral": s["ephemeral"], "wait": parsed.has("--wait"),
            "wait_timeout": parsed.get("--wait-timeout"),
        },
        "reads_third_party": s["kind"] == "review" or parsed.has("--reads-third-party"),
        "session_plan": {},
        "would_refuse": None,
        "tools": {"claude-session": ["claude-session"], "termctl": None, "wtctl": None},
        "worktree_plan": None,
        "term_plan": None,
        "session_start_argv": ["claude-session", "start", "--json"],
        "hooks": [],
        "warnings": [],
    }  # fmt: skip


def _launch(call: Call, parsed: Parsed, *, resume: bool) -> Out:
    call.on_signal = _interrupted
    _check_launch_flags(
        parsed, resume=resume,
        session_dir=sess.peek_session_dir(parsed.args[0] if resume and parsed.args else None),
    )  # fmt: skip
    command = "resume" if resume else "launch"
    if parsed.has("--dry-run"):
        step = next_step(TOOL, f"{command} --dry-run")
        return apply_step(
            TOOL, step, lambda: Out(doc=_dry_run_doc(parsed, resume)), call,
            schema=f"{TOOL}/cli/launch-dry-run.json",
        )  # fmt: skip
    if resume and not parsed.args:
        raise Usage("resume needs an agent id or --select")
    step = next_step(TOOL, command)
    plan = _plan_from(step, "resume_plan" if resume else "plan")

    def default() -> Out:
        wait = parsed.has("--wait") or parsed.get("--placement") == "here"
        with State() as state:
            if resume:
                rec = sess.agents(state).get(_agent_id(parsed.args[0]))
                if rec is None or rec["status"] == "torn_down":
                    return _not_found(parsed.args[0], **({"status": "torn_down"} if rec else {}))
                if sess.is_live(rec):
                    return _as_error(20, "refused", "the agent is live", reason="live",
                                     agent_id=rec["agent_id"])  # fmt: skip
                n = rec["run"] + 1
                run = sess.new_run(n, plan, rec["settings"]["mode"], iso())
                rec["runs"].append(run)
                rec["run"] = n
                rec["status"] = "launching" if sess.is_pending(run) else "live"
                rec["park_reason"], rec["needs_attention"] = None, None
            else:
                rec = sess.new_agent(state, _settings(parsed), plan)
                run = sess.last_run(rec)
            if not sess.is_pending(run) and (
                run["plan"]["outcome"] in ("not-started", "vetoed", "escalated") or wait
            ):
                sess.finalize(rec, run)
                run = sess.last_run(rec)
            out = _result_out(rec, run if run["final"] else None)
        return out

    return apply_step(TOOL, step, default, call, schema=f"{TOOL}/cli/{command}.json")


def _wait(call: Call, parsed: Parsed) -> Out:
    agent_id = _agent_id(parsed.args[0])
    step = next_step(TOOL, "wait")
    timeout = _seconds(parsed.get("--timeout") or "0") if parsed.has("--timeout") else None

    def default() -> Out:
        with State() as state:
            rec = sess.agents(state).get(agent_id)
            if rec is None:
                return _not_found(agent_id)
            run = sess.last_run(rec)
            if not run["final"] and (run["plan"].get("never_ends") or sess.is_pending(run)):
                live = True
            else:
                live = False
                if not run["final"]:
                    sess.finalize(rec, run)
                    run = sess.last_run(rec)
            if not live:
                return _result_out(rec, run)
            doc = _result_doc(rec, None, timed_out=True)
        if timeout is None:
            while True:  # waits forever, as the real tool does for a run that never ends
                time.sleep(0.05)
        time.sleep(min(timeout, 30))
        return Out(exit=22, doc=doc)

    return apply_step(TOOL, step, default, call, schema=f"{TOOL}/cli/wait.json")


def _show(call: Call, parsed: Parsed) -> Out:
    agent_id = _agent_id(parsed.args[0])
    step = next_step(TOOL, "show")

    def default() -> Out:
        with State() as state:
            rec = sess.agents(state).get(agent_id)
            if rec is None:
                return _not_found(agent_id)
            run = sess.last_run(rec)
            tomb = rec["status"] == "torn_down"
            live = not run["final"]
            st, activity, needs = sess.cs_state(rec)
            doc: dict[str, Any] = {
                "agent": sess.agent_doc(rec),
                "runs": [
                    {
                        "run": r["run"],
                        "csession_id": rec["csession_id"],
                        "run_kind": rec["settings"].get("kind"),
                        "mode": r["mode"],
                        "promoted": False,
                        "auto_resumed": r["auto_resumed"],
                        "adopted_at": r["started_at"],
                        "ended_at": r["ended_at"],
                        "first_working_at": r["started_at"],
                        "window": None,
                        "outcome": sess.run_outcome_doc(rec, r) if r["final"] else None,
                    }  # fmt: skip
                    for r in rec["runs"]
                ],
                "session": None
                if tomb
                else {
                    "state": st,
                    "needs_input_reason": needs,
                    "activity": activity,
                    "run": run["run"],
                    "report_status": (run["outcome"]["report"] or {}).get("status")
                    if run["final"]
                    else None,
                    "sandbox_active": True,
                    "tainted": False,
                },  # fmt: skip
                "supervisor": None
                if tomb
                else {
                    "pid": 424242 if live else None,
                    "alive": None if parsed.has("--no-liveness") else live,
                    "exec_pid": 424243 if live else None,
                },
                "third_party_run": False,
                "repo_config_changed": None,
                "warnings": [],
            }
            if parsed.has("--no-liveness"):
                doc["liveness_checked"] = False
        return Out(doc=doc)

    return apply_step(TOOL, step, default, call, schema=f"{TOOL}/cli/show.json")


def _stop(call: Call, parsed: Parsed) -> Out:
    agent_id = _agent_id(parsed.args[0])
    step = next_step(TOOL, "stop")
    dry = parsed.has("--dry-run")

    def default() -> Out:
        with State() as state:
            rec = sess.agents(state).get(agent_id)
            if rec is None or rec["status"] == "torn_down":
                return _not_found(agent_id, **({"status": "torn_down"} if rec else {}))
            run = sess.last_run(rec)
            live = not run["final"]
            session_stop = None
            action = "session_stop" if live else "none"
            if live:
                session_stop = {"csession_id": rec["csession_id"], "run": run["run"],
                                "ended": not dry, "already_final": False,
                                "dry_run": dry, "outcome": None if dry else "killed"}  # fmt: skip
                if not dry:
                    run["plan"] = {**run["plan"], "outcome": "killed"}
                    sess.finalize(rec, run)
            doc = {"agent": sess.agent_doc(rec), "action": action, "session_stop": session_stop,
                   "already_parked": not live, "removed": False, "dry_run": dry,
                   "warnings": []}  # fmt: skip
        return Out(doc=doc)

    return apply_step(TOOL, step, default, call, schema=f"{TOOL}/cli/stop.json")


def _teardown(call: Call, parsed: Parsed) -> Out:
    for name in ("--force", "--override-escalation"):
        if parsed.has(name) and not _human(parsed):
            raise Usage(f"{name} is human-only: pass --actor human:<user>")
    agent_id = _agent_id(parsed.args[0])
    step = next_step(TOOL, "teardown")
    dry = parsed.has("--dry-run")

    def default() -> Out:
        with State() as state:
            rec = sess.agents(state).get(agent_id)
            if rec is None or rec["status"] == "torn_down":
                return _not_found(agent_id, **({"status": "torn_down"} if rec else {}))
            live = sess.is_live(rec)
            if dry:
                doc: dict[str, Any] = {"dry_run": True, "agent_id": agent_id,
                                       "would_refuse": "live" if live else None,
                                       "worktrees": []}  # fmt: skip
                return Out(exit=20 if live else 0, doc=doc,
                           schema=f"{TOOL}/cli/teardown-dry-run.json")  # fmt: skip
            if live:
                return _as_error(20, "refused", "the agent is live or launching", reason="live",
                                 agent_id=agent_id)  # fmt: skip
            rec["status"], rec["torn_down_at"] = "torn_down", iso()
            rec["updated"] = rec["torn_down_at"]
            rec["needs_attention"] = None
            rec["forgotten"] = True
            doc = {"agent": sess.agent_doc(rec), "worktrees": [], "session_forgotten": True,
                   "windows_closed": [], "warnings": []}  # fmt: skip
        return Out(doc=doc)

    return apply_step(TOOL, step, default, call, schema=f"{TOOL}/cli/teardown.json")


def _list(call: Call, parsed: Parsed) -> Out:
    step = next_step(TOOL, "list")

    def default() -> Out:
        with State() as state:
            recs = list(sess.agents(state).values())
        want = set(parsed.all("--status"))
        labels = parsed.all("--label")
        kept = []
        for rec in recs:
            if want and rec["status"] not in want:
                continue
            if not want and rec["status"] == "torn_down":
                continue
            if any(lab not in rec["settings"].get("labels", []) for lab in labels):
                continue
            if parsed.has("--needs-attention") and not rec["needs_attention"]:
                continue
            if parsed.has("--kind") and rec["settings"].get("kind") != parsed.get("--kind"):
                continue
            if parsed.has("--repo") and rec["settings"].get("repo") != parsed.get("--repo"):
                continue
            if parsed.has("--mode") and rec["settings"]["mode"] != parsed.get("--mode"):
                continue
            kept.append(rec)
        kept.sort(key=lambda r: (r["created"], r["agent_id"]), reverse=True)
        doc: dict[str, Any] = {"agents": [sess.agent_doc(r) for r in kept]}
        if parsed.has("--no-liveness"):
            doc["liveness_checked"] = False
        return Out(doc=doc)

    return apply_step(TOOL, step, default, call, schema=f"{TOOL}/cli/list.json")


def handler(call: Call, parsed: Parsed) -> Out:
    if parsed.has("--verbose") and parsed.has("--quiet"):
        raise Usage("--verbose and --quiet are mutually exclusive")
    if len(parsed.all("--decision-id")) > MAX_DECISION_IDS:
        raise Usage(f"at most {MAX_DECISION_IDS} --decision-id values")
    command = parsed.command[0]
    if command == "wait":  # a waiter interrupted by SIGHUP exits 130 like SIGINT (Signals)
        call.signal_exit[signal.SIGHUP] = 130
    if command == "launch":
        return _launch(call, parsed, resume=False)
    if command == "resume":
        return _launch(call, parsed, resume=True)
    return {"wait": _wait, "show": _show, "stop": _stop, "teardown": _teardown,
            "list": _list}[command](call, parsed)  # fmt: skip


def main(argv: list[str]) -> None:
    def setup(call: Call) -> None:
        call.before_finish = _write_result_file(argv)

    run_tool(TOOL, GRAMMAR, handler, argv, setup=setup)
