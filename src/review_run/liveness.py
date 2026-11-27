"""Who is still there: the stored runner and launcher, read as identities (PID plus start time).

Shared by ``cancel`` (which signals what it finds) and the recovery of ``show``, ``wait`` and
``reconcile`` (which never signal anything). A process is ``alive`` only when ``/proc`` says the
exact process (PID and start time) exists, ``gone`` when it verifiably does not (or nothing
plausible is stored), and ``unknown`` when it cannot be told (no ``/proc``): a caller that would end
a run on "gone" must treat ``unknown`` like ``alive``.
"""

from __future__ import annotations

import datetime as dt
import os
from typing import Final, Literal

from . import procid
from .runs import SCHEMA_PATTERNS
from .store import Run

MAX_PID: Final = 2**31
LAUNCHER_WINDOW_SECONDS: Final = 120.0

Proc = Literal["alive", "gone", "unknown"]


def identity(pid: object, start: object) -> procid.ProcIdentity | None:
    """The stored identity, or ``None`` for anything that is not a plausible one (a text or bytes
    value in the INTEGER column, a PID outside 1..2**31-1, no start) and for this process."""
    if type(pid) is not int or not 0 < pid < MAX_PID or not isinstance(start, str):
        return None
    if pid == os.getpid():
        return None  # never this process: it is not another invocation's runner
    return procid.ProcIdentity(pid, start)


def stored_agent(run: Run) -> str | None:
    """The stored agent id, or ``None`` when absent or not an agent id (it never reaches argv)."""
    agent = run.agent_id
    if isinstance(agent, str) and SCHEMA_PATTERNS["agent_id"].fullmatch(agent) is not None:
        return agent
    return None


def state_of(ident: procid.ProcIdentity | None) -> Proc:
    """``gone`` for no identity at all, else what ``procid.check`` says."""
    if ident is None:
        return "gone"
    found = procid.check(ident)
    if found is None:
        return "unknown"
    return "alive" if found else "gone"


def runner_state(run: Run) -> Proc:
    return state_of(identity(run.runner_pid, run.runner_start))


def launcher_state(run: Run) -> Proc:
    return state_of(identity(run.launcher_pid, run.launcher_start))


def launcher_in_window(run: Run, now: dt.datetime, *, launch_file_usable: bool) -> bool:
    """A launcher whose PID or start is not stored (so it cannot be verified), within the window
    of its start: never signaled, and the run is not ended before the window ends."""
    if run.launcher_pid is not None and run.launcher_start is not None:
        return False
    if run.launch_started_at is None:
        return False
    try:
        started = dt.datetime.fromisoformat(run.launch_started_at)
    except ValueError:
        return False
    age = (now - started).total_seconds()
    return 0 <= age < LAUNCHER_WINDOW_SECONDS and not launch_file_usable


__all__ = [
    "LAUNCHER_WINDOW_SECONDS",
    "MAX_PID",
    "Proc",
    "identity",
    "launcher_in_window",
    "launcher_state",
    "runner_state",
    "state_of",
    "stored_agent",
]
