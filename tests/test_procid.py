import os
import subprocess
import sys
import time

from review_run.procid import ProcIdentity, check, current, identity_of


def test_current_identity_is_alive() -> None:
    me = current()
    assert me.pid == os.getpid()
    assert check(me) is True


def test_identity_roundtrips_through_text() -> None:
    me = current()
    assert ProcIdentity.parse(me.text()) == me


def test_parse_rejects_garbage() -> None:
    for bad in ("", "x", "1", "0:abc", "-4:boot:1", "12", "a:b:c"):
        assert ProcIdentity.parse(bad) is None


def test_start_time_mismatch_means_dead() -> None:
    me = current()
    other = ProcIdentity(me.pid, me.start + "0")
    assert check(other) is False


def test_other_boot_means_dead() -> None:
    me = current()
    boot, _, ticks = me.start.rpartition(":")
    assert check(ProcIdentity(me.pid, f"{boot}x:{ticks}")) is False


def test_exited_child_is_dead_and_running_child_is_alive() -> None:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        ident = identity_of(child.pid)
        assert ident is not None and check(ident) is True
    finally:
        child.kill()
        child.wait(timeout=10)
    assert ident is not None
    deadline = time.monotonic() + 5
    while check(ident) is not False and time.monotonic() < deadline:
        time.sleep(0.05)
    assert check(ident) is False


def test_unknown_pid_has_no_identity() -> None:
    assert identity_of(2**22 + 12345) is None


def test_comm_with_parentheses_and_spaces_is_parsed() -> None:
    # /proc/<pid>/stat holds the command name in parentheses, which may contain anything.
    from review_run.procid import parse_start_ticks

    line = "123 (a) b (c d) S 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 777 23 24"
    assert parse_start_ticks(line) == "777"
