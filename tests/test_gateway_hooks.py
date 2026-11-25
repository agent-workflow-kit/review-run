"""Gateway additions for hooks: a SIGTERM grace before the group kill and detached children."""

from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from review_run import gateway as gw
from review_run.gateway import CallTimeout, SystemGateway, ToolMissing
from tests.fake_gateway import FakeGateway, ok
from tests.test_gateway import py, wait_dead

PY = sys.executable


def test_term_grace_lets_a_child_clean_up_on_sigterm(tmp_path: Path) -> None:
    marker = tmp_path / "term-seen"
    code = f"""
import signal, sys, time
def bye(*_):
    open({str(marker)!r}, "w").write("x")
    sys.exit(0)
signal.signal(signal.SIGTERM, bye)
print("ready", flush=True)
time.sleep(60)
"""
    with pytest.raises(CallTimeout):
        SystemGateway().run(py(code), timeout=1.5, term_grace=5)
    assert marker.read_text() == "x"


def test_term_grace_ends_with_sigkill_for_a_child_and_group_that_ignore_sigterm(
    tmp_path: Path,
) -> None:
    pidfile = tmp_path / "pids"
    code = f"""
import os, signal, subprocess, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
open({str(pidfile)!r}, "w").write(f"{{os.getpid()}} {{g.pid}}")
time.sleep(60)
"""
    started = time.monotonic()
    with pytest.raises(CallTimeout):
        SystemGateway().run(py(code), timeout=1.5, term_grace=0.5)
    assert time.monotonic() - started < 10
    child, grandchild = (int(x) for x in pidfile.read_text().split())
    assert wait_dead(child)
    assert wait_dead(grandchild)


def test_term_grace_kills_a_member_that_outlives_a_leader_that_exits_on_sigterm(
    tmp_path: Path,
) -> None:
    pidfile = tmp_path / "pid"
    code = f"""
import os, signal, subprocess, sys, time
g = subprocess.Popen([sys.executable, "-c",
    "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"])
open({str(pidfile)!r}, "w").write(str(g.pid))
time.sleep(60)
"""
    with pytest.raises(CallTimeout):
        SystemGateway().run(py(code), timeout=1.5, term_grace=0.5)
    assert wait_dead(int(pidfile.read_text()))


@pytest.mark.parametrize("bad", [-1, float("nan"), float("inf"), True, "1"])
def test_a_bad_term_grace_is_a_value_error(bad: Any) -> None:
    with pytest.raises(ValueError):
        SystemGateway().run(py("pass"), timeout=5, term_grace=bad)


def test_the_fake_records_term_grace_and_validates_it(fake_gateway: FakeGateway) -> None:
    fake_gateway.script("x", ok())
    gw.run(["x"], timeout=5, term_grace=2)
    assert fake_gateway.calls[0].term_grace == 2
    with pytest.raises(ValueError):
        gw.run(["x"], timeout=5, term_grace=-1)


def test_detached_child_reads_stdin_from_a_file_and_outlives_the_call(tmp_path: Path) -> None:
    out = tmp_path / "got"
    code = f"import sys; open({str(out)!r}, 'w').write(sys.stdin.read())"
    handle = SystemGateway().spawn_detached(py(code), timeout=30, stdin="payload é")
    assert handle.join(10)
    assert out.read_text(encoding="utf-8") == "payload é"


def test_detached_child_has_its_own_group_and_no_output_pipes() -> None:
    handle = SystemGateway().spawn_detached(
        py("import os, sys; sys.stdout.write('x' * 10**6); sys.stderr.write('y' * 10**6)"),
        timeout=30,
    )
    assert handle.join(10)  # a full pipe would block it forever
    assert handle.pid > 0


def test_detached_child_without_stdin_sees_eof(tmp_path: Path) -> None:
    out = tmp_path / "got"
    code = f"import sys; open({str(out)!r}, 'w').write(repr(sys.stdin.read()))"
    handle = SystemGateway().spawn_detached(py(code), timeout=30)
    assert handle.join(10)
    assert out.read_text() == "''"


def test_the_watchdog_ends_the_whole_group_at_the_timeout(tmp_path: Path) -> None:
    pidfile = tmp_path / "pids"
    code = f"""
import os, signal, subprocess, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
open({str(pidfile)!r}, "w").write(f"{{os.getpid()}} {{g.pid}}")
time.sleep(60)
"""
    handle = SystemGateway().spawn_detached(py(code), timeout=1.0, term_grace=0.3)
    assert handle.join(15)
    child, grandchild = (int(x) for x in pidfile.read_text().split())
    assert wait_dead(child)
    assert wait_dead(grandchild)


def test_the_watchdog_sends_sigterm_first(tmp_path: Path) -> None:
    marker = tmp_path / "term-seen"
    code = f"""
import signal, sys, time
def bye(*_):
    open({str(marker)!r}, "w").write("x")
    sys.exit(0)
signal.signal(signal.SIGTERM, bye)
time.sleep(60)
"""
    handle = SystemGateway().spawn_detached(py(code), timeout=1.0, term_grace=5)
    assert handle.join(15)
    assert marker.read_text() == "x"


def test_a_missing_program_is_tool_missing_for_a_detached_call(tmp_path: Path) -> None:
    with pytest.raises(ToolMissing):
        SystemGateway().spawn_detached([str(tmp_path / "nope")], timeout=5)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"timeout": 0},
        {"timeout": float("nan")},
        {"timeout": 5, "term_grace": -1},
        {"timeout": None},
    ],
)
def test_bad_detached_arguments_raise_value_error(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        SystemGateway().spawn_detached(py("pass"), **kwargs)


def test_a_detached_child_that_exits_leaves_no_zombie_after_join() -> None:
    handle = SystemGateway().spawn_detached(py("pass"), timeout=30)
    assert handle.join(10)
    with pytest.raises(ProcessLookupError):
        os.kill(handle.pid, signal.SIG_DFL)  # reaped by the watchdog thread


def test_the_fake_records_a_detached_call(fake_gateway: FakeGateway) -> None:
    handle = gw.spawn_detached(["x", "a"], timeout=5, stdin="s", env={"A": "1"})
    assert handle.join(1)
    (call,) = fake_gateway.calls
    assert call.detached is True
    assert call.stdin == "s"
    assert call.argv == ("x", "a")


# -- the supervisor outlives the caller, signals end a running hook, stdin file errors -------

SRC = str(Path(__file__).resolve().parent.parent / "src")


def _run_parent(tmp_path: Path, body: str) -> tuple[Any, Path]:
    """Start a real Python parent (a script in ``tmp_path``) that imports the gateway."""
    import subprocess

    script = tmp_path / "parent.py"
    script.write_text(
        f"import sys\nsys.path.insert(0, {SRC!r})\nfrom review_run import gateway\n{body}"
    )
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "PYTHONPATH": SRC}
    proc = subprocess.Popen(
        [sys.executable, "-I", str(script)], env=env, stdout=subprocess.PIPE, text=True
    )
    return proc, script


def _wait_for(path: Path, limit: float = 15.0) -> str:
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if path.exists() and path.read_text().strip():
            return path.read_text()
        time.sleep(0.02)
    raise AssertionError(f"{path.name} never appeared")


def _kill_quietly(*pids: int) -> None:
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def test_a_detached_timeout_is_enforced_after_the_caller_has_exited(tmp_path: Path) -> None:
    pidfile = tmp_path / "pids"
    marker = tmp_path / "term-seen"
    hook = f"""
import os, signal, subprocess, sys, time
def bye(*_):
    open({str(marker)!r}, "w").write("x")
signal.signal(signal.SIGTERM, bye)
g = subprocess.Popen([sys.executable, "-c",
    "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"])
open({str(pidfile)!r}, "w").write(f"{{os.getpid()}} {{g.pid}}")
time.sleep(60)
"""
    parent, _ = _run_parent(
        tmp_path,
        f"h = gateway.SystemGateway().spawn_detached([sys.executable, '-c', {hook!r}],"
        " timeout=1.0, term_grace=0.5)\nprint(h.pid, flush=True)\n",
    )
    pids: list[int] = []
    try:
        assert parent.stdout is not None
        supervisor = int(parent.stdout.readline())
        assert parent.wait(15) == 0  # the caller is gone before the timeout fires
        child, grandchild = (int(x) for x in _wait_for(pidfile).split())
        pids = [supervisor, child, grandchild]
        assert wait_dead(child, 15)
        assert wait_dead(grandchild, 15)
        assert marker.read_text() == "x"  # SIGTERM first
    finally:
        _kill_quietly(*pids)
        parent.stdout.close() if parent.stdout else None


def test_a_missing_program_is_still_tool_missing_with_the_supervisor(tmp_path: Path) -> None:
    with pytest.raises(ToolMissing):
        SystemGateway().spawn_detached(["definitely-not-a-program"], timeout=5, env={"PATH": ""})


def test_sigterm_on_the_caller_ends_a_running_hook_and_exits_143(tmp_path: Path) -> None:
    pidfile = tmp_path / "hookpid"
    hook = f"import os, time; open({str(pidfile)!r}, 'w').write(str(os.getpid())); time.sleep(60)"
    parent, _ = _run_parent(
        tmp_path,
        f"gateway.SystemGateway().run([sys.executable, '-c', {hook!r}],"
        " timeout=60, term_grace=1)\n",
    )
    hook_pid = 0
    try:
        hook_pid = int(_wait_for(pidfile))
        os.kill(parent.pid, signal.SIGTERM)
        assert parent.wait(15) == 143
        assert wait_dead(hook_pid, 10)
    finally:
        _kill_quietly(hook_pid)


def test_sighup_on_the_caller_ends_a_running_hook_and_exits_130(tmp_path: Path) -> None:
    pidfile = tmp_path / "hookpid"
    hook = f"import os, time; open({str(pidfile)!r}, 'w').write(str(os.getpid())); time.sleep(60)"
    parent, _ = _run_parent(
        tmp_path,
        f"gateway.SystemGateway().run([sys.executable, '-c', {hook!r}], timeout=60)\n",
    )
    hook_pid = 0
    try:
        hook_pid = int(_wait_for(pidfile))
        os.kill(parent.pid, signal.SIGHUP)
        assert parent.wait(15) == 130
        assert wait_dead(hook_pid, 10)
    finally:
        _kill_quietly(hook_pid)


def test_signal_handlers_are_restored_after_a_call() -> None:
    before = (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGHUP))
    SystemGateway().run(py("pass"), timeout=30)
    assert (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGHUP)) == before


def test_a_caller_with_its_own_sigterm_handler_keeps_it_during_a_call() -> None:
    seen: list[object] = []

    def mine(*_: object) -> None:
        pass

    previous = signal.signal(signal.SIGTERM, mine)
    try:
        orig = gw._communicate

        def spy(*a: Any, **kw: Any) -> Any:
            seen.append(signal.getsignal(signal.SIGTERM))
            return orig(*a, **kw)

        gw._communicate = spy  # type: ignore[assignment]
        try:
            SystemGateway().run(py("pass"), timeout=30)
        finally:
            gw._communicate = orig  # type: ignore[assignment]
    finally:
        signal.signal(signal.SIGTERM, previous)
    assert seen == [mine]


def test_a_call_off_the_main_thread_installs_no_handlers() -> None:
    import threading

    box: list[object] = []

    def work() -> None:
        before = signal.getsignal(signal.SIGTERM)
        SystemGateway().run(py("pass"), timeout=30)
        box.append(before == signal.getsignal(signal.SIGTERM))

    thread = threading.Thread(target=work)
    thread.start()
    thread.join(30)
    assert box == [True]


def test_a_stdin_file_that_cannot_be_made_is_a_gateway_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tempfile

    def boom() -> Any:
        raise OSError(28, "no space")

    monkeypatch.setattr(tempfile, "TemporaryFile", boom)
    with pytest.raises(gw.GatewayError):
        SystemGateway().spawn_detached(py("pass"), timeout=5, stdin="x")
