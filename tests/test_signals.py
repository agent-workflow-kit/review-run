"""Signal handling of ``run`` and ``wait`` (issue #27; interface.md, Signals).

Signals are delivered with ``signal.raise_signal`` to this very process, but only while the
command's own guard is installed (the guard's handler is the only thing that can receive them), or
with ``Popen.send_signal`` to a child the test started. Nothing else is ever signaled.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from review_run import audit as audit_mod
from review_run import finalize as fin_mod
from review_run import gateway as gw
from review_run import hooks, runner, signals, waiting
from review_run import launch as lc
from review_run.runner import Interrupted, run_review
from review_run.store import Run, Store, lease_name
from tests.conftest import validate_against
from tests.helpers import Result, run_cli
from tests.proc_guard import ProcTracker
from tests.run_helpers import CALLER, PR_URL, World
from tests.test_meta import artifact_text

SIGNALS = [
    (signal.SIGTERM, 143),
    (signal.SIGINT, 130),
    (signal.SIGHUP, 130),
]
CANCELING = [(signal.SIGTERM, 143), (signal.SIGINT, 130)]


def fire(signum: int) -> None:
    """Raise ``signum`` in this process, only while the command's own guard handles it."""
    guard = signals.active()
    assert guard is not None, "no guard is installed: the signal would reach pytest"
    assert signal.getsignal(signum) == guard.handle, "the guard's handler is not installed"
    signal.raise_signal(signum)


ORIG_KILL, ORIG_KILLPG = os.kill, os.killpg


def _pidfd_target(fd: int) -> int | None:
    try:
        for line in Path(f"/proc/self/fdinfo/{fd}").read_text().splitlines():
            if line.startswith("Pid:"):
                return int(line.split()[1])
    except (OSError, ValueError):
        pass
    return None


class KillGuard:
    """The pids (and group ids) these tests may signal: the children ``review_run`` itself
    started (recorded when its ``Popen`` is created) and what a test records explicitly."""

    def __init__(self) -> None:
        self.recorded: set[int] = set()

    def record(self, pid: int) -> None:
        self.recorded.add(pid)

    def check(self, target: int, sig: int) -> None:
        if sig != 0 and target not in self.recorded:
            raise AssertionError(f"signal {sig} to {target}, which no test recorded")


@pytest.fixture(autouse=True)
def kill_guard(monkeypatch: pytest.MonkeyPatch) -> Iterator[KillGuard]:
    """Refuse any ``os.kill``, ``os.killpg`` or ``pidfd_send_signal`` (whoever calls it) whose
    target is not a recorded pid or group id; a signal 0 probe is allowed. Undone before the
    process guard's teardown, which kills what is left of its own children."""
    guard = KillGuard()
    real_popen = subprocess.Popen
    real_pidfd = getattr(signal, "pidfd_send_signal", None)

    class Recording(real_popen):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            if sys._getframe(1).f_globals.get("__name__", "").startswith("review_run"):  # noqa: SLF001
                guard.record(self.pid)  # a child of the product code (its group id is its pid)

    def kill(pid: int, sig: int) -> None:
        if pid <= 0 and sig != 0:
            raise AssertionError("a signal to pid 0 or a group through os.kill")
        guard.check(pid, sig)
        ORIG_KILL(pid, sig)

    def killpg(pgid: int, sig: int) -> None:
        guard.check(pgid, sig)
        ORIG_KILLPG(pgid, sig)

    def pidfd(fd: int, sig: int, *rest: Any) -> None:
        target = _pidfd_target(fd)
        guard.check(-1 if target is None else target, sig)
        assert real_pidfd is not None
        real_pidfd(fd, sig, *rest)

    with monkeypatch.context() as patch:
        patch.setattr(subprocess, "Popen", Recording)
        patch.setattr(os, "kill", kill)
        patch.setattr(os, "killpg", killpg)
        if real_pidfd is not None:
            patch.setattr(signal, "pidfd_send_signal", pidfd)
        yield guard


def wait_for_file(path: Path) -> None:
    """The rendezvous with a child: a file it writes once its handlers are armed (bounded)."""
    deadline = time.monotonic() + 30
    while not path.exists():
        assert time.monotonic() < deadline, f"{path.name} never appeared"
        time.sleep(0.01)


def alive(pid: int) -> bool:
    return Path(f"/proc/{pid}").exists()


def run_guarded(world: World, **opts: Any) -> Run:
    plan = world.plan()
    with signals.install():
        return run_review(plan, world.root, world.options(**opts))


def lease_is_free(world: World, run: Run) -> bool:
    with world.store() as store:
        lease = store.acquire_lease(lease_name(run.run_id))
        if lease is None:
            return False
        store.release_lease(lease)
        return True


def assert_canceled(world: World, run: Run, code: int) -> None:
    assert (run.state, run.reason, run.exit_code) == ("failed", "canceled", code)
    assert run.cancel_requested_at is not None
    assert (run.cancel_actor, run.cancel_exit_code) == (CALLER.actor, code)
    base = world.root / "runs" / run.run_id
    assert not (base / "prompt.md").exists() and not (base / "inputs").exists()
    assert lease_is_free(world, run)


# -- the guard ---------------------------------------------------------------------------------


@pytest.mark.parametrize(("signum", "code"), SIGNALS)
def test_the_first_signal_raises_signal_exit_with_its_exit_code(signum: int, code: int) -> None:
    with signals.install():
        with pytest.raises(signals.SignalExit) as stop:
            fire(signum)
    assert stop.value.code == code and stop.value.signum == signum


def test_handlers_are_restored_afterwards_and_after_a_signal() -> None:
    before = {s: signal.getsignal(s) for s in signals.EXIT_CODES}
    with signals.install():
        assert signal.getsignal(signal.SIGTERM) != before[signal.SIGTERM]
    assert {s: signal.getsignal(s) for s in signals.EXIT_CODES} == before
    with pytest.raises(signals.SignalExit), signals.install():
        fire(signal.SIGTERM)
    assert {s: signal.getsignal(s) for s in signals.EXIT_CODES} == before
    assert signals.active() is None


def test_a_second_signal_is_ignored_whatever_it_is() -> None:
    with signals.install():
        with pytest.raises(signals.SignalExit) as stop:
            fire(signal.SIGTERM)
        fire(signal.SIGTERM)  # identical
        fire(signal.SIGINT)  # another one, during the cleanup
        fire(signal.SIGHUP)
    assert stop.value.code == 143


def test_a_deferred_section_raises_the_signal_when_it_ends() -> None:
    steps: list[str] = []
    with signals.install():
        with pytest.raises(signals.SignalExit):
            with signals.deferred():
                fire(signal.SIGTERM)
                with signals.deferred():  # nested: still deferred
                    fire(signal.SIGINT)
                steps.append("inner done")
                steps.append("still running")
        steps.append("raised after the section")
    assert steps == ["inner done", "still running", "raised after the section"]


def test_the_first_signal_of_a_deferred_section_decides_the_exit_code() -> None:
    with signals.install():
        with pytest.raises(signals.SignalExit) as stop:
            with signals.deferred():
                fire(signal.SIGINT)
                fire(signal.SIGTERM)
    assert stop.value.code == 130


def test_a_section_that_must_not_raise_swallows_the_signal() -> None:
    with signals.install():
        with signals.deferred(raise_on_exit=False):
            fire(signal.SIGTERM)
        signals.ignore_rest()
        fire(signal.SIGTERM)
        signals.checkpoint()


def test_a_pending_signal_is_raised_by_the_checkpoint() -> None:
    with signals.install():
        with signals.deferred(raise_on_exit=False):
            fire(signal.SIGHUP)
        with pytest.raises(signals.SignalExit) as stop:
            signals.checkpoint()
    assert stop.value.code == 130 and stop.value.hangup


def test_nothing_changes_without_a_guard() -> None:
    with signals.deferred():
        signals.checkpoint()
        signals.ignore_rest()
    assert signals.active() is None


def test_other_threads_install_nothing_and_defer_nothing() -> None:
    seen: list[object] = []

    def work() -> None:
        before = signal.getsignal(signal.SIGTERM)
        with signals.install():
            seen.append(signal.getsignal(signal.SIGTERM) == before)
        with signals.install() as guard, guard.deferred():
            seen.append(guard.depth)

    with signals.install() as main:
        thread = threading.Thread(target=work)
        thread.start()
        thread.join(10)
        assert main.depth == 0
    assert seen == [True, 0]


def test_guarded_returns_the_exit_code_and_prints_nothing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def handler(args: object) -> int:
        fire(signal.SIGTERM)
        print("never")
        return 0

    assert signals.guarded(handler)(None) == 143
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


def test_a_store_transaction_is_finished_before_the_signal_is_raised(tmp_path: Path) -> None:
    with Store.open(tmp_path / "state") as store:
        with signals.install():
            with pytest.raises(signals.SignalExit):
                with store.transaction() as conn:
                    fire(signal.SIGTERM)
                    conn.execute(
                        "INSERT INTO ids (run_id, reserved_at) VALUES ('rr-abcdefgh', 'now')"
                    )
        row = store._conn.execute(  # noqa: SLF001
            "SELECT count(*) FROM ids WHERE run_id = 'rr-abcdefgh'"
        ).fetchone()
        assert row[0] == 1
        assert not store._conn.in_transaction  # noqa: SLF001


def test_an_audit_append_is_finished_before_the_signal_is_raised(tmp_path: Path) -> None:
    root = tmp_path / "state"
    appender = audit_mod._Appender(root, audit_mod.AUDIT_NAME, durable=True)  # noqa: SLF001
    with signals.install():
        with pytest.raises(signals.SignalExit):
            with appender.locked() as fd:
                fire(signal.SIGTERM)
                os.write(fd, b'{"whole": true}\n')
    assert (root / "audit.jsonl").read_text() == '{"whole": true}\n'


def test_a_signal_right_after_the_fork_still_ends_the_child(
    monkeypatch: pytest.MonkeyPatch, kill_guard: KillGuard
) -> None:
    from review_run import gateway as gw

    real = gw.subprocess.Popen
    started: list[int] = []

    class Racing(real):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            started.append(self.pid)
            kill_guard.record(self.pid)  # the gateway ends this child's group
            fire(signal.SIGTERM)  # the child exists, ``Popen`` has not returned yet

    monkeypatch.setattr(gw.subprocess, "Popen", Racing)
    with signals.install(), pytest.raises(signals.SignalExit):
        gw.SystemGateway().run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=60)
    assert started and not alive(started[0])


# -- before the run record ---------------------------------------------------------------------


@pytest.mark.parametrize(("signum", "code"), SIGNALS)
def test_a_signal_while_resolving_leaves_no_record(world: World, signum: int, code: int) -> None:
    world.router.on_run = lambda argv: fire(signum) if argv[0] == "forgectl" else None
    with signals.install(), pytest.raises(signals.SignalExit) as stop:
        world.plan()
    assert stop.value.code == code
    if world.root.exists():
        with world.store() as store:
            assert store.list_run_ids()[0] == []


@pytest.mark.parametrize(("signum", "code"), SIGNALS)
def test_a_signal_during_a_pre_run_hook_leaves_no_record(
    world: World, monkeypatch: pytest.MonkeyPatch, signum: int, code: int
) -> None:
    def hook(*args: Any, **kw: Any) -> Any:
        fire(signum)
        raise AssertionError("not reached")

    monkeypatch.setattr(hooks, "run_pre_run", hook)
    with pytest.raises(signals.SignalExit) as stop:
        run_guarded(world)
    assert stop.value.code == code
    with world.store() as store:
        assert store.list_run_ids()[0] == []


# -- prefetch and launch -----------------------------------------------------------------------


@pytest.mark.parametrize(("signum", "code"), SIGNALS)
def test_a_signal_in_prefetch_ends_the_run_canceled(world: World, signum: int, code: int) -> None:
    plan = world.plan()
    world.router.on_run = lambda argv: (
        fire(signum) if argv[0] in ("forgectl", "pr-comment") else None
    )
    with signals.install():
        run = run_review(plan, world.root, world.options())
    assert_canceled(world, run, code)
    assert world.fb.calls("agent-spawn") == []


def test_a_signal_while_the_run_comes_into_being_is_raised_first_thing(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = audit_mod.AuditWriter.begin

    def begin(self: audit_mod.AuditWriter, *args: Any, **kw: Any) -> str:
        fire(signal.SIGTERM)  # inside the deferred block: nothing is raised yet
        return original(self, *args, **kw)

    monkeypatch.setattr(audit_mod.AuditWriter, "begin", begin)
    run = run_guarded(world)
    assert_canceled(world, run, 143)
    assert world.fb.calls("agent-spawn", "launch") == []  # never got as far as the launch


@pytest.mark.parametrize(("signum", "code"), SIGNALS)
def test_a_signal_while_the_launcher_runs_ends_its_group_and_cancels(
    world: World, signum: int, code: int
) -> None:
    world.fb.use("agent-spawn", "hang", command="launch")
    pids: list[int] = []

    def started(argv: Sequence[str], pid: int) -> None:
        if argv[0] == "agent-spawn" and argv[1] == "launch":
            pids.append(pid)
            fire(signum)

    plan = world.plan()
    world.router.on_started = started
    with signals.install():
        if signum == signal.SIGHUP:
            with pytest.raises(Interrupted) as stop:  # SIGHUP after the launch: stop waiting
                run_review(plan, world.root, world.options())
            assert stop.value.exit_code == code
            run = world.only_run()
            assert (run.state, run.cancel_requested_at) == ("running", None)
            assert lease_is_free(world, run)
        else:
            run = run_review(plan, world.root, world.options())
            assert_canceled(world, run, code)
    (pid,) = pids
    assert not alive(pid)
    assert world.fb.calls("agent-spawn", "stop") == []
    base = world.root / "runs" / run.run_id
    assert not (base / "prompt.md").exists() and not (base / "inputs").exists()


# -- the session wait --------------------------------------------------------------------------


def fire_in_wait(world: World, signum: int, pids: list[int]) -> None:
    def started(argv: Sequence[str], pid: int) -> None:
        if argv[0] == "agent-spawn" and argv[1] == "wait":
            pids.append(pid)
            fire(signum)

    world.router.on_started = started


@pytest.mark.parametrize(("signum", "code"), CANCELING)
def test_a_signal_while_waiting_stops_the_agent_tears_it_down_and_cancels(
    world: World, signum: int, code: int
) -> None:
    world.fb.use("agent-spawn", "never_ends")
    pids: list[int] = []
    fire_in_wait(world, signum, pids)
    run = run_guarded(world)
    assert_canceled(world, run, code)
    assert run.agent_id is not None and run.teardown == "done"
    (stop_call,) = world.fb.calls("agent-spawn", "stop")
    assert stop_call.argv[1] == run.agent_id and "--actor" in stop_call.argv
    assert len(world.fb.calls("agent-spawn", "teardown")) == 1
    assert not alive(pids[0])  # the waiter's group was ended


def test_sighup_while_waiting_stops_waiting_and_changes_nothing(world: World) -> None:
    world.fb.use("agent-spawn", "never_ends")
    pids: list[int] = []
    fire_in_wait(world, signal.SIGHUP, pids)
    with pytest.raises(Interrupted) as stop:
        run_guarded(world)
    assert stop.value.exit_code == 130
    run = world.only_run()
    assert (run.state, run.phase, run.cancel_requested_at) == ("running", "session", None)
    assert run.agent_id is not None
    assert world.fb.calls("agent-spawn", "stop") == []
    assert world.fb.calls("agent-spawn", "teardown") == []
    assert lease_is_free(world, run) and not alive(pids[0])


def test_a_recorded_cancellation_makes_sighup_end_the_run_canceled(world: World) -> None:
    world.fb.use("agent-spawn", "never_ends")

    def started(argv: Sequence[str], pid: int) -> None:
        if argv[0] == "agent-spawn" and argv[1] == "wait":
            with world.store() as store:
                run = world.only_run()
                assert store.request_cancel(run.run_id, run.key, actor="human:bob", exit_code=143)
            fire(signal.SIGHUP)

    world.router.on_started = started
    run = run_guarded(world)
    assert (run.state, run.reason, run.exit_code) == ("failed", "canceled", 143)
    assert run.cancel_actor == "human:bob"  # the recorded cancellation stands


def test_a_repeated_signal_during_the_cleanup_is_ignored(world: World) -> None:
    world.fb.use("agent-spawn", "never_ends")
    fire_in_wait(world, signal.SIGTERM, [])
    seen: list[str] = []

    def during_cleanup(argv: Sequence[str]) -> None:
        if argv[0] == "agent-spawn" and argv[1] in ("stop", "show", "teardown"):
            seen.append(argv[1])
            fire(signal.SIGTERM)
            fire(signal.SIGINT)

    world.router.on_run = during_cleanup
    run = run_guarded(world)
    assert_canceled(world, run, 143)
    assert {"stop", "teardown"} <= set(seen) and run.teardown == "done"


def test_a_failing_stop_leaves_the_run_running_with_the_cancellation_recorded(
    world: World,
) -> None:
    world.fb.use("agent-spawn", "never_ends")
    world.fb.script(
        "agent-spawn", "stop", {"error": {"code": "failed", "message": "fake"}, "exit": 12}
    )
    fire_in_wait(world, signal.SIGTERM, [])
    with pytest.raises(Interrupted) as stop:
        run_guarded(world)
    assert stop.value.exit_code == 143
    run = world.only_run()
    assert (run.state, run.cancel_requested_at is not None) == ("running", True)
    assert run.cancel_exit_code == 143
    assert world.fb.calls("agent-spawn", "teardown") == []
    assert lease_is_free(world, run)


# -- finalize ----------------------------------------------------------------------------------


@pytest.mark.parametrize(("signum", "code"), SIGNALS)
def test_a_signal_during_finalize_is_deferred_and_the_run_keeps_its_outcome(
    world: World, signum: int, code: int
) -> None:
    world.fb.use("agent-spawn", "outcome", outcome="done", artifact_text=artifact_text())

    def finalize(request: runner.FinalizeRequest) -> Run:
        fire(signum)
        return runner.default_finalize(request)

    run = run_guarded(world, finalize=finalize)
    assert run.state == "done"
    assert run.cancel_requested_at is None
    assert lease_is_free(world, run)


def test_a_signal_after_the_run_ended_changes_nothing(world: World) -> None:
    world.fb.use("agent-spawn", "outcome", outcome="done", artifact_text=artifact_text())
    plan = world.plan()
    with signals.install():
        run = run_review(plan, world.root, world.options())
        fire(signal.SIGTERM)  # printing the outcome is not interrupted
    assert run.state == "done"


# -- real processes ----------------------------------------------------------------------------


def _env(world: World) -> dict[str, str]:
    env = world.fb.env()
    for name in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
        env[name] = os.environ[name]
    env["XDG_STATE_HOME"] = str(world.root.parent)
    env["PYTHONPATH"] = os.pathsep.join(sys.path)
    return env


@pytest.mark.parametrize(("signum", "code"), SIGNALS)
def test_a_waiting_wait_command_exits_with_the_signal_code_and_prints_nothing(
    world: World, kill_guard: KillGuard, signum: int, code: int
) -> None:
    world.fb.use("agent-spawn", "never_ends")
    world.root = world.tmp / "xdg-state" / "review-run"  # where the command looks for its state
    run = world.run(no_wait=True)
    assert run.state == "running"
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-m", "review_run", "wait", run.run_id, "--json"],
        env=_env(world),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
    )
    kill_guard.record(proc.pid)  # the signal goes to this child, started here
    try:
        deadline = time.monotonic() + 60
        while not world.fb.calls("agent-spawn", "show") and time.monotonic() < deadline:
            time.sleep(0.05)
        if not world.fb.calls("agent-spawn", "show"):
            proc.kill()
            pytest.fail(f"wait never polled: {proc.communicate()}")
        proc.send_signal(signum)  # the child this test started
        out, err = proc.communicate(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert proc.returncode == code
    assert out == b""
    assert b"Traceback" not in err
    assert world.only_run().state == "running"


# -- round 1: the guard, inherited ignores ------------------------------------------------------


def _private_env(tmp: Path) -> dict[str, str]:
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp),
        "XDG_CONFIG_HOME": str(tmp),
        "PYTHONPATH": os.pathsep.join(sys.path),
    }


def test_an_inherited_ignore_of_sighup_and_sigint_is_kept_and_sigterm_is_still_handled(
    tmp_path: Path,
) -> None:
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import json, signal\n"
        "from review_run import signals\n"
        "ign = signal.SIG_IGN\n"
        "signal.signal(signal.SIGHUP, ign)\n"
        "signal.signal(signal.SIGINT, ign)\n"
        "with signals.install() as guard:\n"
        "    inside = {\n"
        "        'hup': signal.getsignal(signal.SIGHUP) == ign,\n"
        "        'int': signal.getsignal(signal.SIGINT) == ign,\n"
        "        'term': signal.getsignal(signal.SIGTERM) == guard.handle,\n"
        "    }\n"
        "after = {\n"
        "    'hup': signal.getsignal(signal.SIGHUP) == ign,\n"
        "    'int': signal.getsignal(signal.SIGINT) == ign,\n"
        "    'term': signal.getsignal(signal.SIGTERM) == signal.SIG_DFL,\n"
        "}\n"
        "print(json.dumps([inside, after]))\n",
        encoding="utf-8",
    )
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-E", str(probe)],
        env=_private_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    import json

    inside, after = json.loads(done.stdout)
    assert inside == {"hup": True, "int": True, "term": True}
    assert after == {"hup": True, "int": True, "term": True}


def test_the_kill_guard_refuses_an_unrecorded_target_and_allows_a_recorded_one(
    kill_guard: KillGuard,
) -> None:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])  # noqa: S603
    try:
        with pytest.raises(AssertionError, match="no test recorded"):
            os.kill(child.pid, signal.SIGTERM)
        with pytest.raises(AssertionError, match="no test recorded"):
            os.killpg(child.pid, signal.SIGTERM)
        assert child.poll() is None  # the refused signal was never sent
        os.kill(child.pid, 0)  # a probe is not a signal
        kill_guard.record(child.pid)
    finally:
        child.kill()  # through the handle, after recording it
        child.wait(10)


# -- round 1: the launcher and the cancellation first --------------------------------------


def launcher_scenario(world: World, **extra: Any) -> tuple[Path, Path]:
    ready, seen = world.tmp / "launcher.ready", world.tmp / "launcher.signals"
    world.fb.script(
        "agent-spawn",
        "launch",
        {"hang": True, "ready_file": str(ready), "signal_file": str(seen), **extra},
    )
    return ready, seen


def fire_once_armed(world: World, ready: Path, signum: int, pids: list[int]) -> None:
    def started(argv: Sequence[str], pid: int) -> None:
        if argv[0] == "agent-spawn" and argv[1] == "launch":
            pids.append(pid)
            wait_for_file(ready)
            fire(signum)

    world.router.on_started = started


def test_the_launcher_gets_sigterm_at_its_exact_pid_with_the_grace_and_the_run_is_canceled(
    world: World,
) -> None:
    ready, seen = launcher_scenario(world)
    pids: list[int] = []
    fire_once_armed(world, ready, signal.SIGTERM, pids)
    run = run_guarded(world)
    assert_canceled(world, run, 143)
    assert seen.read_text() == f"{int(signal.SIGTERM)}\n"  # SIGTERM, then it exited: no SIGKILL
    assert not alive(pids[0])
    (call,) = [x for x in world.router.stops if x[0][:2] == ("agent-spawn", "launch")]
    assert call[1] == gw.SignalStop(signal.SIGTERM, lc.LAUNCH_TERM_GRACE)
    assert call[2] == 0.0  # the timeout path keeps its group kill, with no grace


def test_the_launch_term_grace_is_sixty_seconds() -> None:
    assert lc.LAUNCH_TERM_GRACE == 60.0


def test_a_launcher_that_ignores_sigterm_is_left_alive_the_run_stays_running_cancellation_first(
    world: World, monkeypatch: pytest.MonkeyPatch, proc_tracker: ProcTracker
) -> None:
    monkeypatch.setattr(lc, "LAUNCH_TERM_GRACE", 0.4)  # the real value is 60 s
    ready, seen = launcher_scenario(world, ignore_term=True)
    pids: list[int] = []
    fire_once_armed(world, ready, signal.SIGTERM, pids)
    recorded_at_stop: list[str | None] = []
    real_stop = gw._stop_pid  # noqa: SLF001

    def spy(proc: Any, stop: gw.SignalStop) -> None:
        recorded_at_stop.append(world.only_run().cancel_requested_at)  # before the stop
        real_stop(proc, stop)

    monkeypatch.setattr(gw, "_stop_pid", spy)
    plan = world.plan()
    try:
        with signals.install(), pytest.raises(Interrupted) as stop:
            run_review(plan, world.root, world.options())
        (pid,) = pids
        assert stop.value.exit_code == 143
        assert alive(pid)  # not SIGKILLed after the grace
        assert seen.read_text() == f"{int(signal.SIGTERM)}\n"
        run = world.only_run()
        assert (run.state, run.phase, run.agent_id) == ("running", "session", None)
        assert run.cancel_requested_at is not None and run.cancel_exit_code == 143
        assert recorded_at_stop and recorded_at_stop[0] is not None
        assert lease_is_free(world, run)
    finally:
        for pid in pids:  # the leftover child, by its recorded PID
            ORIG_KILL(pid, signal.SIGKILL)
        for child in proc_tracker.children:
            if child.pid in pids:
                child.popen.wait(10)


def test_the_waiter_is_killed_at_its_exact_pid(world: World) -> None:
    world.fb.use("agent-spawn", "never_ends")
    pids: list[int] = []
    fire_in_wait(world, signal.SIGTERM, pids)
    run = run_guarded(world)
    assert_canceled(world, run, 143)
    (call,) = [x for x in world.router.stops if x[0][:2] == ("agent-spawn", "wait")]
    assert call[1] == gw.SignalStop(signal.SIGKILL)
    assert not alive(pids[0])


def test_a_launcher_that_exited_with_a_live_agent_is_adopted_stopped_and_torn_down(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.fb.use("agent-spawn", "never_ends")
    original = runner._Flow.adopt  # noqa: SLF001

    def adopt(self: runner._Flow, agent: Any) -> Run:  # noqa: SLF001
        fire(signal.SIGTERM)  # the launcher exited 0; its agent is not stored yet
        return original(self, agent)

    monkeypatch.setattr(runner._Flow, "adopt", adopt)  # noqa: SLF001
    run = run_guarded(world)
    assert_canceled(world, run, 143)
    assert run.agent_id is not None and run.teardown == "done"
    (stop_call,) = world.fb.calls("agent-spawn", "stop")
    assert stop_call.argv[1] == run.agent_id
    assert len(world.fb.calls("agent-spawn", "teardown")) == 1


def test_a_look_alike_agent_in_launch_json_is_never_stopped_or_torn_down(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.fb.use("agent-spawn", "never_ends")
    original = runner._Flow.adopt  # noqa: SLF001

    def adopt(self: runner._Flow, agent: Any) -> Run:  # noqa: SLF001
        state = world.fb.state()  # the agent is not this run's: other labels
        state["agents"][agent.agent_id]["settings"]["labels"] = [
            "owner=review-run",
            "run=rr-bbbbbbbb",
        ]
        world.fb.seed_state(state)
        fire(signal.SIGTERM)
        return original(self, agent)

    monkeypatch.setattr(runner._Flow, "adopt", adopt)  # noqa: SLF001
    run = run_guarded(world)
    assert (run.state, run.reason) == ("failed", "canceled")
    assert run.cancel_requested_at is not None
    assert run.agent_id is None and "agent_mismatch" in run.warnings
    assert world.fb.calls("agent-spawn", "stop") == []
    assert world.fb.calls("agent-spawn", "teardown") == []


@pytest.fixture
def cli(world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("XDG_STATE_HOME", str(world.tmp / "xdg-state"))
    world.root = world.tmp / "xdg-state" / "review-run"  # where the command looks for its state
    world.config()

    def call(*argv: str) -> Result:
        return run_cli(capsys, "--config", str(world.tmp / "config.yaml"), *argv)

    return call


@pytest.mark.parametrize(("signum", "code"), CANCELING)
def test_an_agent_still_live_after_the_stop_keeps_the_run_running_and_prints_nothing(
    world: World, cli, signum: int, code: int
) -> None:  # type: ignore[no-untyped-def]
    world.fb.use("agent-spawn", "never_ends")
    world.fb.script("agent-spawn", "stop", {"json": {}, "exit": 0, "schema": None})  # no effect
    fire_in_wait(world, signum, [])
    res = cli("--json", "run", PR_URL)
    assert (res.code, res.out) == (code, "")
    run = world.only_run()
    assert (run.state, run.cancel_exit_code) == ("running", code)
    assert run.cancel_requested_at is not None
    assert len(world.fb.calls("agent-spawn", "stop")) == 1
    assert world.fb.calls("agent-spawn", "teardown") == []
    assert lease_is_free(world, run)


def test_a_signal_just_after_the_run_came_into_being_ends_it_canceled_and_frees_the_lease(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = runner.signals
    fired: list[bool] = []

    class Firing:
        """The section of ``run_review`` that raises right after it has ended (the window)."""

        def __init__(self, section: Any) -> None:
            self.section = section

        def __enter__(self) -> None:
            self.section.__enter__()

        def __exit__(self, *exc: Any) -> None:
            self.section.__exit__(*exc)
            if not fired:
                fired.append(True)
                fire(signal.SIGTERM)

    class Proxy:
        def __getattr__(self, name: str) -> Any:
            return getattr(real, name)

        def deferred(self, *, raise_on_exit: bool = True) -> Any:
            section = real.deferred(raise_on_exit=raise_on_exit)
            return section if raise_on_exit else Firing(section)

    monkeypatch.setattr(runner, "signals", Proxy())
    run = run_guarded(world)
    assert fired
    assert_canceled(world, run, 143)
    assert world.fb.calls("agent-spawn", "launch") == []


# -- round 1: wait -------------------------------------------------------------------------


def parked_running(world: World) -> str:
    """A run left ``running`` whose agent has ended: the next ``wait`` finalizes it."""
    world.fb.use("agent-spawn", "outcome", outcome="done", artifact_text=artifact_text())
    run = world.run(no_wait=True)
    assert (run.state, run.phase) == ("running", "session") and run.agent_id
    world.fb.run("agent-spawn", "wait", run.agent_id, "--json")
    return run.run_id


@pytest.mark.parametrize(("signum", "code"), SIGNALS)
def test_a_wait_that_finalizes_the_run_keeps_and_prints_its_outcome(
    world: World, cli, monkeypatch: pytest.MonkeyPatch, signum: int, code: int
) -> None:  # type: ignore[no-untyped-def]
    run_id = parked_running(world)
    real = fin_mod.finalize_session

    def finalize(*args: Any, **kw: Any) -> Run:
        fire(signum)  # a signal during the finalization
        return real(*args, **kw)

    monkeypatch.setattr(fin_mod, "finalize_session", finalize)
    res = cli("--json", "wait", run_id)
    assert res.code == 0, res.err
    doc = res.envelope()
    validate_against("cli/run.json", doc)
    assert doc["state"] == "done"
    assert world.only_run().state == "done"


@pytest.mark.parametrize(("signum", "code"), SIGNALS)
def test_a_signal_while_a_read_only_poll_hangs_ends_wait_at_once_and_changes_nothing(
    world: World, cli, monkeypatch: pytest.MonkeyPatch, signum: int, code: int
) -> None:  # type: ignore[no-untyped-def]
    run_id = parked_running(world)
    monkeypatch.setattr(lc, "SHOW_TIMEOUT", 30.0)  # the signal must not wait for it
    world.fb.script("agent-spawn", "show", {"hang": True})
    pids: list[int] = []

    def started(argv: Sequence[str], pid: int) -> None:
        if argv[0] == "agent-spawn" and argv[1] == "show":
            pids.append(pid)
            fire(signum)

    world.router.on_started = started
    began = time.monotonic()
    res = cli("--json", "wait", run_id)
    assert time.monotonic() - began < 20
    assert (res.code, res.out) == (code, "")
    assert pids and not alive(pids[0])  # the tool call's child was ended
    run = world.only_run()
    assert (run.state, run.phase, run.cancel_requested_at) == ("running", "session", None)
    assert lease_is_free(world, run)


def test_wait_run_drops_a_signal_of_its_own_finalization(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = parked_running(world)
    real = fin_mod.finalize_session

    def finalize(*args: Any, **kw: Any) -> Run:
        fire(signal.SIGTERM)
        return real(*args, **kw)

    monkeypatch.setattr(fin_mod, "finalize_session", finalize)
    with signals.install() as guard:
        obj = waiting.wait_run(world.root, run_id, timeout=None)
    assert obj["state"] == "done" and guard.ignoring


def _unfinished_finalization(
    world: World, monkeypatch: pytest.MonkeyPatch, during: int | None
) -> list[int]:
    """Make every finalization return the still-``running`` row (a live parked verdict); it fires
    ``during`` (when given) inside the deferred finalization. Returns the call counter."""
    calls: list[int] = []

    def finalize(*args: Any, **kw: Any) -> Run:
        calls.append(1)
        if during is not None:
            fire(during)
        return world.only_run()

    monkeypatch.setattr(fin_mod, "finalize_session", finalize)
    return calls


@pytest.mark.parametrize(("signum", "code"), SIGNALS)
def test_a_finalization_that_does_not_end_the_run_leaves_wait_interruptible(
    world: World,
    cli,
    monkeypatch: pytest.MonkeyPatch,
    signum: int,
    code: int,
) -> None:  # type: ignore[no-untyped-def]
    run_id = parked_running(world)
    _unfinished_finalization(world, monkeypatch, during=None)
    slept: list[float] = []

    def sleep(_pause: float) -> None:
        slept.append(_pause)
        fire(signum)  # the signal of the next poll

    clock = waiting.Clock(sleep=sleep)
    with signals.install() as guard:
        with pytest.raises(signals.SignalExit) as stop:
            waiting.wait_run(world.root, run_id, timeout=None, clock=clock)
        assert not guard.ignoring
    assert stop.value.code == code and slept
    assert world.only_run().state == "running"


@pytest.mark.parametrize(("signum", "code"), SIGNALS)
def test_a_signal_during_a_finalization_that_does_not_end_the_run_ends_wait(
    world: World,
    cli,
    monkeypatch: pytest.MonkeyPatch,
    signum: int,
    code: int,
) -> None:  # type: ignore[no-untyped-def]
    run_id = parked_running(world)
    _unfinished_finalization(world, monkeypatch, during=signum)
    res = cli("--json", "wait", run_id)
    assert (res.code, res.out) == (code, "")
    assert world.only_run().state == "running"
