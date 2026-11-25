"""Autouse process guard: no real external program runs, and every child is killed at teardown.

* ``Popen`` is replaced for every test by a subclass that refuses an ``argv[0]`` that is not the
  test interpreter, a script under the test's ``tmp_path`` or under ``tests/fakebin``. Bare names
  (``git``, ``gh``, ``claude``, ...) and absolute paths of real binaries (``/usr/bin/git``) are
  both refused, so a test can never reach the user's real tools or config.
* Each child's PID is recorded at launch (with a pidfd where the kernel has one, which pins the
  process identity). At teardown exactly those children are killed: the pidfd while the child is
  unreaped, plus the process group the test asked for with ``start_new_session`` (its id is the
  child's PID, reserved by the kernel while any member lives). Nothing is found by name or pattern.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
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


@dataclass
class ProcTracker:
    allowed_roots: list[Path]
    children: list[_Child] = field(default_factory=list)

    def check(self, argv0: str) -> None:
        if not os.path.isabs(argv0):
            raise RealProgramRefused(f"refusing to exec by name: {os.path.basename(argv0)!r}")
        candidates = {Path(os.path.abspath(argv0)), Path(os.path.realpath(argv0))}
        interpreters = {
            Path(os.path.abspath(sys.executable)),
            Path(os.path.realpath(sys.executable)),
        }
        if candidates & interpreters:
            return
        for path in candidates:
            for root in self.allowed_roots:
                if path.is_relative_to(root):
                    return
        raise RealProgramRefused(f"refusing to exec a real program: {argv0!r}")

    def record(self, popen: subprocess.Popen[Any], group: bool) -> None:
        pidfd: int | None = None
        try:
            pidfd = os.pidfd_open(popen.pid)
        except (AttributeError, OSError):
            pidfd = None
        self.children.append(_Child(popen, popen.pid, pidfd, group))

    def cleanup(self) -> None:
        for child in reversed(self.children):
            try:
                if child.group:
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
                if child.popen.returncode is None:  # unreaped: the PID is still ours
                    if child.pidfd is not None:
                        try:
                            signal.pidfd_send_signal(child.pidfd, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    else:
                        child.popen.kill()
                child.popen.wait(timeout=10)
            except Exception:  # keep cleaning up: one failure must not skip the rest
                pass
            finally:
                if child.pidfd is not None:
                    os.close(child.pidfd)
                for stream in (child.popen.stdin, child.popen.stdout, child.popen.stderr):
                    if stream is not None:
                        stream.close()


@pytest.fixture(autouse=True)
def proc_tracker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[ProcTracker]:
    tracker = ProcTracker([tmp_path.resolve(), tmp_path.absolute(), FAKEBIN])
    real_popen = subprocess.Popen

    class GuardedPopen(real_popen):  # type: ignore[misc, valid-type]
        def __init__(self, args: Any, *a: Any, **kw: Any) -> None:
            argv0 = args if isinstance(args, (str, bytes, os.PathLike)) else args[0]
            if kw.get("shell"):
                raise RealProgramRefused("shell=True is never allowed")
            tracker.check(os.fsdecode(argv0))
            super().__init__(args, *a, **kw)
            tracker.record(self, bool(kw.get("start_new_session")))

    monkeypatch.setattr(subprocess, "Popen", GuardedPopen)
    try:
        yield tracker
    finally:
        tracker.cleanup()
