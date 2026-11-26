"""Signals of ``run``, ``resume`` and ``wait`` (interface.md, Signals; issue #27).

One ``SignalGuard`` is installed for the duration of a command, on the main thread only
(``install``). Its handler does nothing but set flags and, at a safe moment, raise ``SignalExit``:

* **First signal.** The handler records the signal and raises ``SignalExit`` (a ``SystemExit``
  carrying the exit code: 130 for SIGINT and SIGHUP, 143 for SIGTERM) at once, unless the code is
  inside a *deferred section*, where the raise waits for the section's end. Nothing else happens in
  the handler: the cleanup runs in ``finally`` blocks and ``except SignalExit`` clauses, in normal
  code, with the signal no longer special.
* **Further signals.** Once the first has been raised (or while it is deferred) every further
  signal, the same or another, is ignored. That is what makes a second signal during cleanup safe
  (hook delivery, teardown, ``agent-spawn stop``): only SIGKILL ends a cleanup early, and a run
  killed that way keeps its recorded cancellation for the next finalizer.
* **Deferred sections** (``deferred``) make a critical piece of work atomic with respect to the
  signals: every store transaction, the terminal transaction with its bookkeeping, a process
  start (between ``fork`` and the return of ``Popen`` the child's group is not known yet), an
  audit append, the finalize pipeline of a run. Sections nest; the signal is raised when the
  outermost one ends (``raise_on_exit``), or never for the finalize pipeline, whose own outcome
  stands.
* **Threads.** Handlers exist on the main thread only (``signal.signal`` refuses anything else);
  ``deferred`` is a no-op on any other thread, so the lease keeper's transactions never touch the
  main thread's counters.

``ignore_rest`` ends the signal handling of the command for good: after a run ended (its own
outcome stands, it is being printed) or while the lease is released.
"""

from __future__ import annotations

import signal
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from types import TracebackType
from typing import Final

from .errors import ExitCode

#: The signals a command handles, and the exit code each gives.
EXIT_CODES: Final = {
    signal.SIGINT: int(ExitCode.SIGINT),
    signal.SIGHUP: int(ExitCode.SIGINT),
    signal.SIGTERM: int(ExitCode.SIGTERM),
}


class SignalExit(SystemExit):
    """Raised by the handler: the command was signaled. ``code`` is 130 (SIGINT, SIGHUP) or 143
    (SIGTERM); ``signum`` the signal. Never an ``Exception``, so no ``except Exception`` of the
    code swallows it; uncaught it exits the process with ``code``."""

    def __init__(self, code: int, signum: int) -> None:
        super().__init__(code)
        self.signum = signum

    @property
    def hangup(self) -> bool:
        return self.signum == signal.SIGHUP


class _Section:
    """One deferred section (a class, not a generator: nothing can interleave between the
    counter and the ``with`` block it guards)."""

    def __init__(self, guard: SignalGuard, raise_on_exit: bool) -> None:
        self._guard, self._raise = guard, raise_on_exit
        self._counted = False

    def __enter__(self) -> None:
        if threading.get_ident() == self._guard.main_ident:
            self._guard.depth += 1
            self._counted = True

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if not self._counted:
            return
        guard = self._guard
        guard.depth -= 1
        if guard.depth == 0 and self._raise:
            guard.checkpoint()


class SignalGuard:
    """The state of one command's signal handling (see the module docstring)."""

    def __init__(self) -> None:
        self.main_ident = threading.get_ident()
        self.depth = 0  # deferred sections open now
        self.first: int | None = None  # the first signal received
        self.raised = False  # SignalExit was raised: every further signal is ignored
        self.ignoring = False  # the command is past its interruptible part
        self.installed: dict[int, object] = {}

    # -- the handler

    def handle(self, signum: int, _frame: object = None) -> None:
        if self.first is None:
            self.first = signum
        if self.raised or self.ignoring or self.depth > 0:
            return  # a second signal, a signal after the end, or a deferred section
        self.raised = True
        raise SignalExit(EXIT_CODES[signal.Signals(self.first)], self.first)

    # -- the safe points

    @property
    def pending(self) -> bool:
        """A signal arrived inside a deferred section and was not raised yet."""
        return self.first is not None and not self.raised and not self.ignoring

    def checkpoint(self) -> None:
        """Raise the pending signal, if any, at this safe point."""
        if self.pending and self.depth == 0:
            self.raised = True
            assert self.first is not None
            raise SignalExit(EXIT_CODES[signal.Signals(self.first)], self.first)

    def deferred(self, *, raise_on_exit: bool = True) -> _Section:
        return _Section(self, raise_on_exit)

    def ignore_rest(self) -> None:
        self.ignoring = True

    @property
    def exit_code(self) -> int | None:
        return None if self.first is None else EXIT_CODES[signal.Signals(self.first)]


_active: SignalGuard | None = None


def active() -> SignalGuard | None:
    """The installed guard of this process, or ``None``."""
    return _active


def deferred(*, raise_on_exit: bool = True) -> _Section | nullcontext[None]:
    """A deferred section of the installed guard; a no-op without one."""
    guard = _active
    return nullcontext() if guard is None else guard.deferred(raise_on_exit=raise_on_exit)


def checkpoint() -> None:
    if _active is not None:
        _active.checkpoint()


def ignore_rest() -> None:
    if _active is not None:
        _active.ignore_rest()


@contextmanager
def install() -> Iterator[SignalGuard]:
    """Install the guard for SIGINT, SIGTERM and SIGHUP (main thread only; elsewhere, or when a
    guard is installed already, the existing or an inert guard is yielded) and restore the
    previous handlers on the way out."""
    global _active
    if _active is not None or threading.current_thread() is not threading.main_thread():
        yield _active if _active is not None else SignalGuard()
        return
    guard = SignalGuard()
    try:
        for signum in EXIT_CODES:
            guard.installed[signum] = signal.signal(signum, guard.handle)
        _active = guard
        yield guard
    finally:
        guard.ignoring = True  # nothing may raise while the handlers are put back
        for signum, previous in guard.installed.items():
            try:
                signal.signal(signum, signal.SIG_DFL if previous is None else previous)  # type: ignore[arg-type]
            except (ValueError, OSError):
                pass
        _active = None


def guarded(handler: Callable[..., int]) -> Callable[..., int]:
    """Run a command handler under the guard: a ``SignalExit`` that reaches here ends the
    command with its exit code, printing nothing."""

    def run(*args: object, **kwargs: object) -> int:
        with install():
            try:
                return handler(*args, **kwargs)
            except SignalExit as stop:
                return int(stop.code) if isinstance(stop.code, int) else int(ExitCode.SIGTERM)

    run.__name__ = getattr(handler, "__name__", "guarded")
    return run


__all__ = [
    "EXIT_CODES",
    "SignalExit",
    "SignalGuard",
    "active",
    "checkpoint",
    "deferred",
    "guarded",
    "ignore_rest",
    "install",
]
