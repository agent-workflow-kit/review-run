"""Autouse process guard: no real external program runs, and every child is killed at teardown.

* ``Popen`` is replaced for every test by a subclass that refuses an ``argv[0]`` that is not the
  test interpreter, a script under the test's ``tmp_path`` or under ``tests/fakebin``. Bare names
  (``git``, ``gh``, ``claude``, ...) and absolute paths of real binaries (``/usr/bin/git``) are
  both refused, so a test can never reach the user's real tools or config.
* On 3.13 ``subprocess._USE_POSIX_SPAWN`` is turned off so ``Popen`` does not need the refused
  ``os.posix_spawn``.
* ``executable=`` is refused, symlinks are resolved before the allow check, and ``os.system``,
  ``os.posix_spawn*``, ``os.fork*``, ``os.exec*`` and ``pty.fork`` raise for the test's duration.
* Each child's PID is recorded at launch (with a pidfd where the kernel has one, which pins the
  process identity). At teardown exactly those children are killed: through the live handle (and
  its process group) while the leader is unreaped; for a reaped leader the group only if some live
  process in ``/proc`` is still in it (it keeps the id reserved). Accepted residual: that check
  could match an unrelated process that reused the pid and called setsid after every real member
  died (needs pid wraparound; no cheap sound fix). Nothing is found by name or pattern. A child
  that survives fails the test, naming its PID.
"""

from __future__ import annotations

import inspect
import os
import pty
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

FAKEBIN = Path(__file__).resolve().parent / "fakebin"


class RealProgramRefused(AssertionError):
    """A test tried to start a program that is not allowed under test."""


@dataclass
class _Child:
    popen: subprocess.Popen[Any]
    pid: int
    pidfd: int | None
    group: bool


def _proc_fields(pid: int) -> tuple[str, int, int] | None:
    """``(state, pgrp, session)`` of a live PID from ``/proc``, or ``None`` when it is gone."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    rest = stat[stat.rindex(")") + 2 :].split()
    return rest[0], int(rest[2]), int(rest[3])  # state, ppid skipped, pgrp, session


def group_members(pgid: int, *, live_only: bool = False) -> list[int]:
    """PIDs whose process group and session are both ``pgid`` (a group this guard's child led).

    A member keeps the id reserved, so while any exists it cannot have been reused by a stranger.
    """
    try:
        os.kill(-pgid, 0)  # killpg(pgid, 0); tests that record killpg calls must not see this
    except ProcessLookupError:
        return []  # no process in the group (not even a zombie): nothing to scan for
    except PermissionError:
        pass  # a member we may not signal: fall through to the scan
    found: list[int] = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        fields = _proc_fields(int(entry))
        if fields is None:
            continue
        state, pgrp, session = fields
        if pgrp == pgid and session == pgid and not (live_only and state == "Z"):
            found.append(int(entry))
    return found


@dataclass
class ProcTracker:
    allowed_roots: list[Path]
    children: list[_Child] = field(default_factory=list)

    def check(self, argv0: str) -> None:
        if not os.path.isabs(argv0):
            raise RealProgramRefused(f"refusing to exec by name: {os.path.basename(argv0)!r}")
        real = Path(os.path.realpath(argv0))  # symlinks are followed: only the target counts
        if real == Path(os.path.realpath(sys.executable)):
            return
        for root in self.allowed_roots:
            if real.is_relative_to(Path(os.path.realpath(root))):
                return
        raise RealProgramRefused(f"refusing to exec a real program: {argv0!r}")

    def record(self, popen: subprocess.Popen[Any], group: bool) -> None:
        pidfd: int | None = None
        try:
            pidfd = os.pidfd_open(popen.pid)
        except (AttributeError, OSError):
            pidfd = None
        self.children.append(_Child(popen, popen.pid, pidfd, group))

    def cleanup(self, wait: float = 10.0) -> None:
        """Kill exactly the recorded children; raise ``AssertionError`` naming any that survive."""
        survivors: list[int] = []
        for child in reversed(self.children):
            try:
                if child.popen.returncode is None:
                    # Unreaped: its PID (our group id) cannot be reused. Signal via the live handle.
                    if child.group:
                        try:
                            os.killpg(child.pid, signal.SIGKILL)
                        except (ProcessLookupError, PermissionError):
                            pass
                    if child.pidfd is not None:
                        try:
                            signal.pidfd_send_signal(child.pidfd, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    else:
                        child.popen.kill()
                elif child.group and group_members(child.pid):
                    # Reaped leader: a live member still reserves the id, so the group is ours.
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
                child.popen.wait(timeout=wait)
            except Exception:  # keep cleaning up: one failure must not skip the rest
                pass
            if child.popen.returncode is None:
                survivors.append(child.pid)
            elif child.group:
                deadline = time.monotonic() + min(wait, 2.0)
                while group_members(child.pid, live_only=True) and time.monotonic() < deadline:
                    time.sleep(0.02)
                survivors += group_members(child.pid, live_only=True)
            if child.pidfd is not None:
                os.close(child.pidfd)
            for stream in (child.popen.stdin, child.popen.stdout, child.popen.stderr):
                if stream is not None:
                    stream.close()
        if survivors:
            raise AssertionError(f"children survived the test teardown: {sorted(set(survivors))}")


def _refuser(name: str) -> Any:
    def refuse(*_a: Any, **_kw: Any) -> Any:
        raise RealProgramRefused(f"{name} is never allowed under test")

    return refuse


_OS_ESCAPES = (
    "system", "posix_spawn", "posix_spawnp", "fork", "forkpty", "execv", "execve", "execvp",
    "execvpe", "execl", "execle", "execlp", "execlpe",
)  # fmt: skip


@pytest.fixture(autouse=True)
def proc_tracker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[ProcTracker]:
    tracker = ProcTracker([tmp_path.resolve(), tmp_path.absolute(), FAKEBIN])
    real_popen = subprocess.Popen

    class GuardedPopen(real_popen):  # type: ignore[misc, valid-type]
        def __init__(self, args: Any, *a: Any, **kw: Any) -> None:
            argv0 = args if isinstance(args, (str, bytes, os.PathLike)) else args[0]
            # Bind like the real Popen so positional shell/executable cannot slip past.
            bound = inspect.signature(real_popen).bind(args, *a, **kw).arguments
            if bound.get("shell"):
                raise RealProgramRefused("shell=True is never allowed")
            # ``executable`` replaces argv[0] as the program that runs.
            if bound.get("executable") is not None:
                raise RealProgramRefused("the executable argument is never allowed")
            tracker.check(os.fsdecode(argv0))
            super().__init__(args, *a, **kw)
            tracker.record(self, bool(kw.get("start_new_session")))

    monkeypatch.setattr(subprocess, "Popen", GuardedPopen)
    for name in _OS_ESCAPES:
        monkeypatch.setattr(os, name, _refuser(f"os.{name}"), raising=False)
    monkeypatch.setattr(pty, "fork", _refuser("pty.fork"))
    # CPython 3.13 starts children through os.posix_spawn when it can; the refusal above would stop
    # every Popen. Use the fork_exec path (argv is still checked by GuardedPopen).
    monkeypatch.setattr(subprocess, "_USE_POSIX_SPAWN", False, raising=False)
    try:
        yield tracker
    finally:
        tracker.cleanup()
