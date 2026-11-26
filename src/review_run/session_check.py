"""The skill and tool checks of finalize step 5 (interface.md, Finalize; issue #22).

``check_session`` is ``finalize.Seams.check_session``. It asks ``claude-session status
<csession_id> --run <awaited_run> --json`` for the run's ``mode`` and ``result_path`` and, for a
headless run, reads the first ``system``/``init`` object of the result file (opened without
following a final link, regular file, at most 1 MiB in all, each line parsed with the strict rules
of step 6):

* the init object's ``skills`` must contain the run's configured ``<plugin>:<skill>`` by exact
  string equality, else ``skill_missing`` (a missing or unreadable file, no init object within the
  bound and an init object of another shape are the same failure: closed, fail closed);
* with the bundled profile or its variant, its ``tools`` are compared with the pinned set
  (``Task`` and ``Agent`` normalized to one name); every other distinct name is one unexpected
  tool. Only the count is kept, never a name. A ``tools`` that is not a list means the comparison
  cannot be made (``tools_unchecked``);
* an interactive run has no result file: the checks are not made (``skill_unchecked`` and
  ``tools_unchecked``).

Everything read from the result file is untrusted. Nothing of it (names, text, counts of other
things) reaches an envelope, log, audit record or warning: the outputs are booleans, one count and
closed words.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Mapping
from typing import Any, Final

from .artifact import Invalid, parse_artifact
from .errors import ReviewRunError
from .finalize import FinalizeFailure, SessionCheck, SessionContext
from .prefetch import LowerFailure, call_json

MAX_RESULT_BYTES: Final = 1024 * 1024
STATUS_TIMEOUT: Final = 30.0
CHUNK: Final = 64 * 1024
# The tools the bundled profile leaves reachable (interface.md, Run settings); ``Task`` stands for
# the ``Task`` and ``Agent`` aliases and is part of the set only with ``subagents``.
PINNED_TOOLS: Final = frozenset(
    {"Skill", "Read", "Write", "Edit", "Glob", "Grep", "Bash", "ToolSearch", "TodoWrite"}
)
SUBAGENT_TOOL: Final = "Task"
ALIASES: Final = frozenset({"Task", "Agent"})
_CSESSION_ID: Final = re.compile(r"cs-[a-z2-7]{8}\Z")
_SKILL: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*:[A-Za-z0-9][A-Za-z0-9._-]*\Z")
MODES: Final = ("headless", "interactive")
UNCHECKED: Final = SessionCheck(
    warnings=("skill_unchecked", "tools_unchecked"),
    skill_checked=False,
    tools_checked=False,
    tools_unexpected=None,
)


def _internal(why: str) -> ReviewRunError:
    return ReviewRunError("internal", why)  # ``why`` is code's own text, never the session's


def read_init(path: object) -> dict[str, Any] | None:
    """The first ``system``/``init`` object of the result file at ``path``, or ``None`` (not a
    usable path, a link or not a regular file, unreadable, a line that is not strict UTF-8 JSON,
    a line cut by the 1 MiB bound, or no init object in the bound). Lines after the init object
    are not read."""
    if not isinstance(path, str) or not path.startswith("/") or "\x00" in path:
        return None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        pending = b""
        total = 0
        while total < MAX_RESULT_BYTES:
            chunk = os.read(fd, min(CHUNK, MAX_RESULT_BYTES - total))
            if not chunk:
                break
            total += len(chunk)
            pending += chunk
            *lines, pending = pending.split(b"\n")
            for line in lines:
                stop, found = _init_line(line)
                if stop:
                    return found
        # the last line has no newline (or the bound cut it: then it does not parse)
        if pending:
            return _init_line(pending)[1]
        return None
    except OSError:
        return None
    finally:
        os.close(fd)


def _init_line(line: bytes) -> tuple[bool, dict[str, Any] | None]:
    """``(stop, init)``: ``(False, None)`` reads on (a strict object that is not the init
    object); ``(True, None)`` stops with nothing usable (any line, an empty one too, that step 6
    does not parse: fail closed); ``(True, doc)`` is the init object."""
    try:
        doc = parse_artifact(line, max_bytes=MAX_RESULT_BYTES)
    except Invalid:
        return True, None
    if doc.get("type") == "system" and doc.get("subtype") == "init":
        return True, doc
    return False, None


def has_skill(init: Mapping[str, Any], expected: str) -> bool:
    skills = init.get("skills")
    if not isinstance(skills, list):
        return False
    return any(type(s) is str and s == expected for s in skills)  # noqa: E721


def unexpected_tools(init: Mapping[str, Any], *, subagents: bool) -> int | None:
    """The number of distinct tool names of the init object outside the pinned set, or ``None``
    when the list cannot be compared. An entry that is not a string counts as one unexpected
    tool; so does any name that differs in case, spelling or characters."""
    tools = init.get("tools")
    if not isinstance(tools, list):
        return None
    allowed = PINNED_TOOLS | ({SUBAGENT_TOOL} if subagents else frozenset())
    names: set[str] = set()
    other = 0
    for tool in tools:
        if type(tool) is not str:  # noqa: E721
            other += 1
            continue
        names.add(SUBAGENT_TOOL if tool in ALIASES else tool)
    return other + len(names - allowed)


def evaluate(
    status: Mapping[str, Any],
    *,
    csession_id: str,
    awaited_run: int,
    mode: str,
    skill: str,
    profile_kind: str,
    subagents: bool,
) -> SessionCheck:
    """Steps of the skill and tool checks for a parsed ``claude-session status`` document."""
    run = status.get("run")
    if (
        status.get("csession_id") != csession_id
        or not isinstance(run, Mapping)
        or type(run.get("run")) is not int  # noqa: E721
        or run.get("run") != awaited_run
        or run.get("mode") != mode
    ):
        raise FinalizeFailure("internal", tool="claude-session")  # not about the awaited run
    if mode == "interactive":
        return UNCHECKED
    init = read_init(run.get("result_path"))
    if init is None or not has_skill(init, skill):
        raise FinalizeFailure("skill_missing")
    if profile_kind not in ("bundled", "variant"):
        return SessionCheck(
            warnings=("tools_unchecked",),
            skill_checked=True,
            tools_checked=False,
            tools_unexpected=None,
        )
    count = unexpected_tools(init, subagents=subagents)
    if count is None:
        return SessionCheck(
            warnings=("tools_unchecked",),
            skill_checked=True,
            tools_checked=False,
            tools_unexpected=None,
        )
    return SessionCheck(
        warnings=("tools_unexpected",) if count else (),
        skill_checked=True,
        tools_checked=True,
        tools_unexpected=count,
    )


def check_session(ctx: SessionContext) -> SessionCheck:
    """``Seams.check_session``: the skill and tool checks of finalize step 5."""
    run, settings = ctx.run, ctx.settings
    csid, awaited = run.csession_id, run.awaited_run
    skill, mode = settings.value("skill"), run.mode
    if (
        not isinstance(csid, str)
        or not _CSESSION_ID.match(csid)
        or type(awaited) is not int  # noqa: E721
        or awaited < 1
        or mode not in MODES
        or not isinstance(skill, str)
        or not _SKILL.match(skill)
    ):
        raise _internal("the run cannot be checked: its session facts are not valid")
    try:
        status = call_json(
            "claude-session",
            ["status", csid, "--run", str(awaited), "--json"],
            timeout=STATUS_TIMEOUT,
        )
    except LowerFailure as exc:
        raise FinalizeFailure("internal", tool="claude-session", lower_code=exc.code) from None
    if not isinstance(status, dict):
        raise FinalizeFailure("internal", tool="claude-session", lower_code="invalid_output")
    return evaluate(
        status,
        csession_id=csid,
        awaited_run=awaited,
        mode=mode,
        skill=skill,
        profile_kind=settings.profile_kind,
        subagents=settings.value("subagents") is True,
    )


__all__ = ["check_session", "evaluate", "has_skill", "read_init", "unexpected_tools"]
