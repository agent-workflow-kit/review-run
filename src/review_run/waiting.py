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
  blocks until its timeout. Also TODO(#29): ``wait`` without ``--timeout`` polls forever when
  ``agent-spawn show`` cannot run (``finalize_agent`` treats a gateway failure as "row
  unchanged"); the diagnosis of repeated refresh failures (``tool_missing``) belongs to the runner
  check and recovery work.
* Signals (#27): ``cli`` runs ``wait`` under ``signals.guarded``; a signal while sleeping or polling
  raises ``SignalExit`` (nothing printed, exit 130 or 143, nothing changed: no lease or process is
  held while sleeping), and one during a finalization waits for it to end (finalize is deferred).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from . import signals
from .audit import LogWriter
from .config import load_config
from .errors import ReviewRunError
from .finalize import AgentFinalize, finalize_agent
from .runs import SCHEMA_PATTERNS, CorruptRunError, run_object, show_run
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
    ``escalated``) whose stored agent id is an agent id (the same pattern ``run_object``
    applies; any other value never reaches a sibling's argv)."""
    return (
        (run.state == "running" or (escalated and run.state == "escalated"))
        and isinstance(run.agent_id, str)
        and SCHEMA_PATTERNS["agent_id"].fullmatch(run.agent_id) is not None
    )


class _LazyHooks(Mapping[str, Any]):
    """The ``hooks`` of the acting process's config, loaded on first use.

    ``finalize_agent`` reads it only when it delivers the hooks of a run it just ended, so a poll
    of a live agent never loads (or fails on) the config. A config that does not load raises
    ``ReviewRunError`` at that point; delivery treats it as not delivered (the events stay in the
    outbox for ``reconcile``) and the finalization stands.
    """

    def __init__(
        self, config_path: Path | None, repo: str, log: LogWriter | None = None, run_id: str = ""
    ) -> None:
        self._path, self._repo = config_path, repo
        self._log, self._run_id = log, run_id
        self._values: Mapping[str, Any] | None = None
        self._warned = False

    def _hooks(self) -> Mapping[str, Any]:
        if self._values is None:
            try:
                hooks = load_config(self._path).resolve(self._repo).values.get("hooks")
            except ReviewRunError as exc:
                self._warn(exc.code)
                raise
            self._values = hooks if isinstance(hooks, Mapping) else {}
        return self._values

    def _warn(self, code: str) -> None:
        """One warn record, with the closed error code of the failure and a fixed message."""
        if self._warned or self._log is None:
            return
        self._warned = True
        try:
            self._log.write(
                "warning",
                "hook delivery deferred: config did not load",
                level="warn",
                run_id=self._run_id,
                code=code,
            )
        except (OSError, ReviewRunError, ValueError):  # a log line never decides a run
            pass

    def __getitem__(self, key: str) -> Any:
        return self._hooks()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._hooks())

    def __len__(self) -> int:
        return len(self._hooks())

    def __bool__(self) -> bool:  # truth must not load the config
        return True


def refresh_run(
    root: Path,
    run_id: str,
    *,
    by: str,
    config_path: Path | None = None,
    escalated: bool = False,
) -> Run | None:
    """The run as stored after finalizing it when its agent has ended; ``None`` when it is gone.

    ``by`` is ``show`` or ``wait`` (the audit ``finalized_by``). A row that cannot be shown as a
    run raises ``CorruptRunError`` before any sibling is called. The config is read lazily, only
    when the finalization ends the run and hooks are delivered. A missing or corrupt
    ``settings.json`` and any other failure of the refresh (an unreadable stored value, the
    database) raise ``ReviewRunError`` (``internal`` for the unexpected ones, with a closed
    message) and leave the run unchanged; it recurs on every call, nothing loops inside.
    """
    run = _read(root, run_id)
    if run is None or not needs_refresh(run, escalated):
        return run
    run_object(run)  # CorruptRunError for a row that cannot be shown: no sibling call
    try:
        with signals.deferred():  # a finalization that began is finished; the signal comes after
            finalize_agent(
                root,
                run_id,
                AgentFinalize(
                    finalized_by=by,
                    hooks_config=_LazyHooks(config_path, run.repo, LogWriter(root), run_id),
                    escalated=escalated,
                ),
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
        refresh_run(root, run_id, by="show", config_path=config_path)
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
