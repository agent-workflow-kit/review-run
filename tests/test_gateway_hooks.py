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
