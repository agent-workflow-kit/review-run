"""The refresh of ``show`` and the polling of ``wait`` (interface.md, ``show`` and ``wait``).

A run left ``running`` in phase ``session`` (or ``finalize``) whose agent has ended is finalized by
the first process that looks at it: ``finalize.finalize_agent`` takes the run's lease without
waiting (exactly one finalizer; a live holder means "not now") and changes nothing unless the agent
has finished its awaited run. ``show`` refreshes once and never waits; ``wait`` repeats the refresh
every ``POLL_SECONDS`` until the run is no longer ``running`` or the timeout passes.

Beyond the finalize check, ``refresh_run`` applies the repairs of ``recovery`` to the one run:
the runner check (``runner_lost``), adoption of an agent from ``launch.json``, and, for ``show``,
the ``agent_gone`` and ``escalation_ttl`` expiry of an ``escalated`` run. A gateway failure leaves
the row unchanged, except a missing agent-spawn, which is ``tool_missing`` (so ``wait`` without
``--timeout`` does not poll forever). Label recovery of a run that died before storing its agent
is TODO(#30).

Seams, each with its owner:

* Signals (#27): ``cli`` runs ``wait`` under ``signals.guarded``; a signal while sleeping or polling
  raises ``SignalExit`` (nothing printed, exit 130 or 143, nothing changed: no lease or process is
  held while sleeping), also while a read-only poll (``agent-spawn show``) runs (its child is
  ended); once a finalization begins (``finalize_agent``) a signal waits for it and is dropped:
  the ``wait`` that finalized the run prints its outcome and exits with its own exit code.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from .errors import ReviewRunError
from .recovery import RecoverOptions, recover_run
from .runs import CorruptRunError, run_object, show_run
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


def needs_refresh(run: Run, escalated: bool = False) -> bool:
    """Whether a refresh can do anything: a ``running`` run (an ``escalated`` one too with
    ``escalated``). A ``running`` run with no agent gets the runner check; one whose stored agent
    id is not an agent id is left alone (it never reaches a sibling's argv)."""
    return run.state == "running" or (escalated and run.state == "escalated")


def refresh_run(
    root: Path,
    run_id: str,
    *,
    by: str,
    config_path: Path | None = None,
    escalated: bool = False,
    judge_escalated: bool = False,
) -> Run | None:
    """The run as stored after the repairs of ``recovery`` (finalize when its agent has ended, the
    runner check, adoption of an agent, ``agent_gone`` and ``escalation_ttl`` expiry of an
    ``escalated`` run); ``None`` when it is gone.

    ``by`` is ``show`` or ``wait`` (the audit ``finalized_by``). A row that cannot be shown as a
    run raises ``CorruptRunError`` before any sibling is called. The config is read lazily, only
    when an ending delivers hooks or an ``escalated`` run is checked against its TTL. A missing or
    corrupt ``settings.json`` and any other failure of the refresh (an unreadable stored value,
    the database, a missing agent-spawn) raise ``ReviewRunError`` (``internal`` for the unexpected
    ones, with a closed message) and leave the run unchanged; it recurs on every call, nothing
    loops inside.
    """
    run = _read(root, run_id)
    if run is None or not needs_refresh(run, escalated):
        return run
    run_object(run)  # CorruptRunError for a row that cannot be shown: no sibling call
    try:
        # ``finalize_agent`` defers a finalization once it begins; its reads stay interruptible.
        recover_run(
            root,
            run_id,
            RecoverOptions(by=by, config_path=config_path, judge_escalated=judge_escalated),
            escalated=escalated,
        )
    except ReviewRunError:
        raise
    except Exception:  # a ValueError of a stored value, sqlite3.Error, ...; never a traceback
        raise ReviewRunError("internal", "the run could not be refreshed") from None
    return _read(root, run_id)


def refresh_for_show(
    root: Path,
    ref: str,
    *,
    config_path: Path | None = None,
    on_refresh_error: Callable[[ReviewRunError], None] | None = None,
) -> str:
    """The run id of ``ref`` after the refresh of ``show``. A refresh that fails is reported
    through ``on_refresh_error`` and the run stays as stored; a corrupt row still raises."""
    run_id = resolve(root, ref)
    try:
        refresh_run(
            root,
            run_id,
            by="show",
            config_path=config_path,
            escalated=True,
            judge_escalated=True,
        )
    except CorruptRunError:
        raise
    except ReviewRunError as err:
        if on_refresh_error is not None:
            on_refresh_error(err)
    return run_id


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
    run_id = (
        refresh_for_show(root, ref, config_path=config_path, on_refresh_error=on_refresh_error)
        if refresh
        else resolve(root, ref)
    )
    with Store.open(root) as store:
        return show_run(store, run_id)


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
    "refresh_for_show",
    "refresh_run",
    "resolve",
    "show_refreshed",
    "wait_run",
]
