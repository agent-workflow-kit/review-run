"""The refresh of ``show`` and the polling of ``wait`` (interface.md, ``show`` and ``wait``).

A run left ``running`` in phase ``session`` (or ``finalize``) whose agent has ended is finalized by
the first process that looks at it: ``finalize.finalize_agent`` takes the run's lease without
waiting (exactly one finalizer; a live holder means "not now") and changes nothing unless the agent
has finished its awaited run. ``show`` refreshes once and never waits; ``wait`` repeats the refresh
every ``POLL_SECONDS`` until the run is no longer ``running`` or the timeout passes.

Seams, each with its owner:

* TODO(#29): the refresh of an ``escalated`` run (an agent that finished a newer run is finalized;
  an expired one ends ``expired``) and the runner check of ``reconcile`` (a run whose runner and
  launcher are gone ends ``runner_lost``; label recovery of a missing agent id). ``refresh_run``
  is the one place they join; until then a ``wait`` on a run whose runner died before the launch
  blocks until its timeout.
* TODO(#27): signals; an interrupted waiter prints nothing (``KeyboardInterrupt``) and changes
  nothing: no lease or process is held while sleeping.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from .config import load_config
from .errors import ReviewRunError
from .finalize import AgentFinalize, finalize_agent
from .runs import run_object
from .store import Run, Store

POLL_SECONDS: Final = 2.0

Refresh = Callable[[str], Run | None]


@dataclass
class Clock:
    """The sleep and the monotonic clock of ``wait`` (tests substitute both)."""

    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic


def _read(root: Path, run_id: str) -> Run | None:
    with Store.open(root) as store:
        return store.get_run(run_id, tolerant=True)


def resolve(root: Path, ref: str) -> str:
    """The run id of ``ref`` (an id or prefix): usage, ``not_found`` or ``ambiguous`` errors."""
    with Store.open(root) as store:
        return store.resolve_run_id(ref)


def needs_refresh(run: Run) -> bool:
    """Whether a refresh can do anything: a ``running`` run that has an agent."""
    return run.state == "running" and run.agent_id is not None


def refresh_run(root: Path, run_id: str, *, by: str, config_path: Path | None = None) -> Run | None:
    """The run as stored after finalizing it when its agent has ended; ``None`` when it is gone.

    ``by`` is ``show`` or ``wait`` (the audit ``finalized_by``). The config is loaded only when a
    finalization is possible, for the ``post-run`` hooks of the run's repo; a config that does not
    load, a missing or corrupt ``settings.json`` and the like raise ``ReviewRunError`` and leave
    the run unchanged (it recurs on every call; nothing loops inside).
    """
    run = _read(root, run_id)
    if run is None or not needs_refresh(run):
        return run
    config = load_config(config_path)
    hooks = config.resolve(run.repo).values.get("hooks")
    finalize_agent(root, run_id, AgentFinalize(finalized_by=by, hooks_config=hooks))
    return _read(root, run_id)


def show_refreshed(
    root: Path,
    ref: str,
    *,
    refresh: bool,
    config_path: Path | None = None,
    on_refresh_error: Callable[[ReviewRunError], None] | None = None,
) -> dict[str, Any]:
    """The run object of ``show``. A refresh that fails is reported through ``on_refresh_error``
    and the object is built from the row as stored: ``show`` still prints what it knows."""
    run_id = resolve(root, ref)
    run = _read(root, run_id)
    if run is not None and refresh:
        try:
            run = refresh_run(root, run_id, by="show", config_path=config_path)
        except ReviewRunError as err:
            if on_refresh_error is not None:
                on_refresh_error(err)
            run = _read(root, run_id)
    if run is None:
        raise ReviewRunError("not_found", "no such run")
    return run_object(run)


def wait_run(
    root: Path,
    ref: str,
    *,
    timeout: float | None,
    refresh: Refresh | None = None,
    clock: Clock | None = None,
    config_path: Path | None = None,
) -> dict[str, Any]:
    """Poll until the run is not ``running`` (an ``escalated`` run is reported at once) and return
    its run object. ``timeout`` seconds (``None``: no limit) end the wait with ``wait_timeout``
    (exit 12, retryable, the run unchanged); the last refresh happens at the deadline."""
    clock = clock or Clock()
    run_id = resolve(root, ref)

    def default_refresh(rid: str) -> Run | None:
        return refresh_run(root, rid, by="wait", config_path=config_path)

    poll = refresh or default_refresh

    deadline = None if timeout is None else clock.monotonic() + timeout
    while True:
        run = poll(run_id)
        if run is None:
            raise ReviewRunError("not_found", "no such run")
        obj = run_object(run)
        if obj["state"] != "running":
            return obj
        pause = POLL_SECONDS
        if deadline is not None:
            remaining = deadline - clock.monotonic()
            if remaining <= 0:
                raise ReviewRunError(
                    "failed",
                    "the wait timed out",
                    reason="wait_timeout",
                    details={"retryable": True},
                )
            pause = min(pause, remaining)
        clock.sleep(pause)


__all__ = [
    "POLL_SECONDS",
    "Clock",
    "needs_refresh",
    "refresh_run",
    "resolve",
    "show_refreshed",
    "wait_run",
]
