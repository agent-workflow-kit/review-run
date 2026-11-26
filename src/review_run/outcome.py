"""The outcome-to-output mapping of a run object (interface.md, Output by exit code).

One function decides what ``run``, ``resume``, ``wait`` and ``show`` print on stdout and which exit
code they return for a run object; the error envelope (a failure with no run, or a read that
failed) is never built here: it is ``ReviewRunError`` and the dispatcher's business.

* ``run``, ``resume``, ``wait``: the run object (``--json``) or its sanitized human summary, and
  the run's own ``exit_code`` (0 ``done``, 13 ``escalated``, 12 and 14 to 18 and 22 ``failed``,
  143 or 130 ``canceled``). A run still ``running`` is printed only after ``--no-wait`` (the
  session started: the run id, or the object with ``--json``, exit 0); anywhere else it is a bug
  and ends as ``internal`` (exit 1) rather than as a made-up code.
* ``show``: whatever it printed is exit 0; the outcome is the object's ``exit_code``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import runs
from .errors import ReviewRunError

RUN_COMMANDS = ("run", "resume", "wait")


@dataclass(frozen=True)
class Rendered:
    """What a command prints on stdout and the exit code it returns."""

    text: str
    code: int


def render_run(
    obj: dict[str, Any], *, command: str, as_json: bool, no_wait: bool = False
) -> Rendered:
    """Map a run object to its stdout text and exit code for ``command``."""
    text = runs.dump(obj) if as_json else None
    if command == "show":
        return Rendered(text or runs.human_run(obj), 0)
    if command not in RUN_COMMANDS:
        raise ValueError("command is run, resume, wait or show")
    if obj["state"] == "running":
        if not no_wait or command == "wait":
            raise ReviewRunError("internal", "the run is still running")
        return Rendered(text or f"{obj['run_id']}\n", 0)
    code = obj["exit_code"]
    if type(code) is not int:  # noqa: E721 - run_object always sets it for an ended run
        raise ReviewRunError("internal", "an ended run has no exit code")
    return Rendered(text or runs.human_run(obj), code)


__all__ = ["RUN_COMMANDS", "Rendered", "render_run"]
