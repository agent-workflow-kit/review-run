"""The real ``SystemGateway`` against scripted child programs (the test interpreter)."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from review_run import gateway as gw
from review_run.gateway import (
    CallTimeout,
    CompletedRun,
    GatewayError,
    OutputTooLarge,
    SystemGateway,
    ToolMissing,
)
from tests.proc_guard import RealProgramRefused

PY = sys.executable


def py(code: str) -> list[str]:
    return [PY, "-c", code]


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def wait_dead(pid: int, limit: float = 5.0) -> bool:
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if not alive(pid):
            return True
        time.sleep(0.02)
    return not alive(pid)


def test_captures_output_and_exit_code() -> None:
    result = SystemGateway().run(
        py("import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"), timeout=30
    )
    assert result == CompletedRun(result.argv, 3, "out\n", "err\n")


def test_signal_death_is_a_negative_returncode() -> None:
    code = "import os, signal; os.kill(os.getpid(), signal.SIGTERM)"
    assert SystemGateway().run(py(code), timeout=30).returncode == -15


def test_stdin_round_trips_unicode_and_large_input() -> None:
    text = "héllo ‮ wörld\n" * 20000  # larger than a pipe buffer, both directions
    code = "import sys; sys.stdout.write(sys.stdin.read())"
    result = SystemGateway().run(py(code), stdin=text, timeout=30)
    assert result.stdout == text


def test_without_stdin_the_child_sees_eof() -> None:
    result = SystemGateway().run(py("import sys; print(repr(sys.stdin.read()))"), timeout=30)
    assert result.stdout == "''\n"


def test_invalid_utf8_output_is_decoded_with_surrogateescape() -> None:
    code = "import sys; sys.stdout.buffer.write(b'a\\xffb'); sys.stderr.buffer.write(b'\\xfe')"
    result = SystemGateway().run(py(code), timeout=30)
    assert result.stdout == "a\udcffb"
    assert result.stderr == "\udcfe"


def test_decoding_ignores_the_locale(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LC_ALL", "C")
    code = "import sys; sys.stdout.buffer.write('é'.encode())"
    env = {"LC_ALL": "C", "PYTHONUTF8": "1"}
    assert SystemGateway().run(py(code), timeout=30, env=env).stdout == "é"


def test_env_is_exactly_what_was_passed() -> None:
    code = "import os; print(sorted(os.environ))"
    result = SystemGateway().run(py(code), timeout=30, env={"ONLY_ONE": "1"})
    names = result.stdout.strip()
    assert "ONLY_ONE" in names and "HOME" not in names and "PATH" not in names


def test_cwd_is_applied(tmp_path: Path) -> None:
    result = SystemGateway().run(py("import os; print(os.getcwd())"), timeout=30, cwd=str(tmp_path))
    assert Path(result.stdout.strip()).resolve() == tmp_path.resolve()


def test_child_runs_in_its_own_session_and_group() -> None:
    code = "import os; print(os.getpgrp() == os.getpid(), os.getsid(0) == os.getpid())"
    assert SystemGateway().run(py(code), timeout=30).stdout.strip() == "True True"


def test_timeout_ends_a_child_that_never_exits() -> None:
    started = time.monotonic()
    with pytest.raises(CallTimeout) as info:
        SystemGateway().run(py("import time; time.sleep(60)"), timeout=0.5)
    assert info.value.lower_code == "timeout"
    assert time.monotonic() - started < 10


def test_timeout_ends_a_child_that_ignores_sigterm_and_its_whole_group(tmp_path: Path) -> None:
    pidfile = tmp_path / "pids"
    code = f"""
import os, signal, subprocess, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
open({str(pidfile)!r}, "w").write(f"{{os.getpid()}} {{g.pid}}")
time.sleep(60)
"""
    with pytest.raises(CallTimeout):
        SystemGateway().run(py(code), timeout=1.5)
    child, grandchild = (int(x) for x in pidfile.read_text().split())
    assert wait_dead(child)
    assert wait_dead(grandchild)


def test_a_timed_out_call_is_not_a_result() -> None:
    with pytest.raises(GatewayError):  # CallTimeout is a GatewayError
        SystemGateway().run(py("import time; time.sleep(60)"), timeout=0.3)


def test_output_flood_is_stopped_at_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gw, "MAX_OUTPUT", 100_000)
    code = "import sys\nwhile True: sys.stdout.write('x' * 65536)"
    with pytest.raises(OutputTooLarge) as info:
        SystemGateway().run(py(code), timeout=30)
    assert info.value.lower_code == "output_too_large"


def test_stderr_flood_is_stopped_at_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gw, "MAX_OUTPUT", 100_000)
    code = "import sys\nwhile True: sys.stderr.write('x' * 65536)"
    with pytest.raises(OutputTooLarge):
        SystemGateway().run(py(code), timeout=30)


def test_output_exactly_at_the_cap_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gw, "MAX_OUTPUT", 1000)
    result = SystemGateway().run(py("print('x' * 999)"), timeout=30)  # 999 + newline
    assert len(result.stdout) == 1000


def test_a_grandchild_holding_the_pipes_does_not_hold_the_call(tmp_path: Path) -> None:
    pidfile = tmp_path / "gc"
    code = f"""
import subprocess, sys
g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdout=sys.stdout)
open({str(pidfile)!r}, "w").write(str(g.pid))
print("done")
"""
    started = time.monotonic()
    result = SystemGateway().run(py(code), timeout=30)
    assert result.stdout == "done\n"
    assert time.monotonic() - started < 10  # the daemon sleeps 60 s


def test_a_missing_program_is_tool_missing(tmp_path: Path) -> None:
    with pytest.raises(ToolMissing) as info:
        SystemGateway().run([str(tmp_path / "nope")], timeout=30)
    assert info.value.lower_code == "tool_missing"
    assert str(tmp_path) not in str(info.value)  # the message names the program only


def test_a_non_executable_file_is_tool_missing(tmp_path: Path) -> None:
    path = tmp_path / "plain"
    path.write_text("not a program")
    with pytest.raises(ToolMissing):
        SystemGateway().run([str(path)], timeout=30)


def test_the_module_function_uses_the_installed_gateway() -> None:
    class Stub:
        def run(self, argv: Any, **kw: Any) -> CompletedRun:
            return CompletedRun(tuple(argv), 7, "stub", "")

    previous = gw.set_gateway(Stub())
    try:
        assert gw.run(["x"], timeout=1).stdout == "stub"
    finally:
        assert isinstance(gw.set_gateway(previous), Stub)
    assert gw.get_gateway() is previous


# --- argument validation: a call without a usable timeout raises -------------------------------


def test_a_call_without_a_timeout_raises() -> None:
    with pytest.raises(TypeError):
        SystemGateway().run(py("pass"))  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        gw.run(py("pass"))  # type: ignore[call-arg]


@pytest.mark.parametrize("bad", [None, 0, -1, float("nan"), float("inf"), True, "5"])
def test_an_unusable_timeout_raises(bad: Any) -> None:
    with pytest.raises(ValueError):
        SystemGateway().run(py("pass"), timeout=bad)


@pytest.mark.parametrize("bad", ["git status", [], [""], ["a\0b"], [PY, 3]])
def test_a_bad_argv_raises(bad: Any) -> None:
    with pytest.raises(ValueError):
        SystemGateway().run(bad, timeout=5)


# --- the test guard itself ----------------------------------------------------------------------


def test_the_guard_refuses_a_real_program_by_name() -> None:
    with pytest.raises(RealProgramRefused):
        subprocess.run(["git", "--version"], check=False)


def test_the_guard_refuses_a_real_program_by_absolute_path() -> None:
    real = next(p for p in ("/usr/bin/git", "/bin/ls", "/usr/bin/ls") if os.path.exists(p))
    with pytest.raises(RealProgramRefused):
        SystemGateway().run([real, "--version"], timeout=5)


def test_the_guard_refuses_a_shell() -> None:
    with pytest.raises(RealProgramRefused):
        subprocess.run("echo hi", shell=True, check=False)  # noqa: S602
