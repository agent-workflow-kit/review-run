"""The process guard refuses every way around it and cleans up exactly what tests started."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from tests.proc_guard import ProcTracker, RealProgramRefused, _Child

PY = sys.executable


def _real_binary() -> str:
    return next(p for p in ("/usr/bin/git", "/bin/ls", "/usr/bin/ls") if os.path.exists(p))


def test_the_executable_keyword_is_refused() -> None:
    with pytest.raises(RealProgramRefused):
        subprocess.Popen([PY, "-c", "pass"], executable=_real_binary())


def test_the_positional_executable_is_refused() -> None:
    with pytest.raises(RealProgramRefused):
        subprocess.Popen([PY, "-c", "pass"], -1, _real_binary())


def test_a_symlink_to_a_real_program_is_refused(tmp_path: Path) -> None:
    link = tmp_path / "innocent"
    link.symlink_to(_real_binary())
    with pytest.raises(RealProgramRefused):
        subprocess.Popen([str(link), "--version"])


def test_the_test_interpreter_still_runs() -> None:
    proc = subprocess.run([PY, "-c", "pass"], check=False)
    assert proc.returncode == 0


@pytest.mark.parametrize(
    "name",
    [
        "system", "posix_spawn", "posix_spawnp", "fork", "forkpty", "execv", "execve", "execvp",
        "execvpe", "execl", "execle", "execlp", "execlpe",
    ],
)  # fmt: skip
def test_os_process_escapes_are_refused(name: str) -> None:
    func = getattr(os, name)
    with pytest.raises(RealProgramRefused):
        func("/bin/true")


def test_pty_fork_is_refused() -> None:
    import pty

    with pytest.raises(RealProgramRefused):
        pty.fork()


# --- teardown ------------------------------------------------------------------------------------


def _alive(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat[stat.rindex(")") + 2] != "Z"


def _wait_gone(pid: int) -> bool:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.02)
    return not _alive(pid)


def _own_tracker(proc: subprocess.Popen[Any]) -> ProcTracker:
    tracker = ProcTracker([])
    tracker.children.append(_Child(proc, proc.pid, None, True))
    return tracker


def test_a_reaped_leader_with_a_dead_group_gets_no_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = subprocess.Popen([PY, "-c", "pass"], start_new_session=True)
    proc.wait()
    sent: list[int] = []
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: sent.append(pgid))
    _own_tracker(proc).cleanup()
    assert sent == []


def test_a_reaped_leader_with_a_live_group_member_gets_the_group_killed(tmp_path: Path) -> None:
    pidfile = tmp_path / "gc"
    code = f"""
import subprocess, sys
g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
open({str(pidfile)!r}, "w").write(str(g.pid))
"""
    proc = subprocess.Popen([PY, "-c", code], start_new_session=True)
    proc.wait()
    grandchild = int(pidfile.read_text())
    assert _alive(grandchild)
    _own_tracker(proc).cleanup()
    assert _wait_gone(grandchild)


def test_an_unreaped_leader_is_killed_through_its_group_and_handle() -> None:
    proc = subprocess.Popen([PY, "-c", "import time; time.sleep(60)"], start_new_session=True)
    _own_tracker(proc).cleanup()
    assert proc.returncode is not None


def test_cleanup_fails_naming_children_that_survive(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = subprocess.Popen([PY, "-c", "import time; time.sleep(60)"], start_new_session=True)
    tracker = _own_tracker(proc)
    with monkeypatch.context() as patch:
        patch.setattr(os, "killpg", lambda pgid, sig: None)
        patch.setattr(proc, "kill", lambda: None)
        with pytest.raises(AssertionError, match=str(proc.pid)):
            tracker.cleanup(wait=0.2)
    # the real fixture's cleanup (same child, tracked there too) still ends it
