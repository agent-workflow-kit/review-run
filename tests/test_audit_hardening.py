"""Closed value grammars, per-event outbox flush and safe file opens (issue #9, review round 1)."""

from __future__ import annotations

import copy
import datetime as dt
import json
import os
import pwd
import signal
import stat
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from review_run import audit
from review_run.audit import (
    AuditWriter,
    Caller,
    LogWriter,
    export_lines,
    flush_outbox,
    make_audit_record,
    make_log_record,
    validate_audit_record,
    validate_log_record,
)
from review_run.errors import ReviewRunError
from review_run.store import NewEvent, Store, default_state_root
from tests.helpers import run_cli
from tests.test_audit import (
    CALLER,
    DONE,
    MARKER,
    PR,
    RUN,
    RUN_INPUTS,
    clock,
    end_with,
    lines_of,
    make_event,
    records_of,
)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "state"


@pytest.fixture
def writer(root: Path) -> AuditWriter:
    return AuditWriter(root, now=clock)


@contextmanager
def bounded(seconds: int = 10) -> Iterator[None]:
    """Fail (instead of hang) when the body blocks, e.g. an open on a FIFO."""

    def on_alarm(_signum: int, _frame: object) -> None:
        raise AssertionError("blocked: a FIFO open was not bounded")

    previous = signal.signal(signal.SIGALRM, on_alarm)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def base_record(action: str = "run") -> dict[str, Any]:
    if action == "run":
        return make_audit_record(
            "run", caller=CALLER, run_id=RUN, pr=PR, inputs=RUN_INPUTS, result=DONE, now=clock
        )
    if action == "prune":
        return make_audit_record(
            "prune",
            caller=CALLER,
            run_id=None,
            pr=None,
            inputs={"older_than": "30d"},
            result={"status": "done", "deleted_runs": 1},
            now=clock,
        )
    if action == "finalize":
        return make_audit_record(
            "finalize",
            caller=Caller.finalizer(),
            run_id=RUN,
            pr=PR,
            inputs={"cause": "runner_lost", "finalized_by": "show"},
            result={"status": "failed", "reason": "runner_lost", "exit_code": 17},
            now=clock,
        )
    if action == "code_run":
        return make_audit_record(
            "code_run",
            caller=CALLER,
            run_id=RUN,
            pr=PR,
            inputs={"throwaway_id": "tw-1", "argv_sha256": "b" * 64, "sandbox": "off"},
            result={"status": "ok", "reason": None},
            now=clock,
        )
    raise AssertionError(action)


def mutated(action: str, mutate: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    rec = copy.deepcopy(base_record(action))
    mutate(rec)
    return rec


def setter(path: tuple[str, ...], value: Any) -> Callable[[dict[str, Any]], None]:
    def apply(rec: dict[str, Any]) -> None:
        node = rec
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value

    return apply


BAD_AUDIT: list[tuple[str, tuple[str, ...], Any]] = [
    ("run", ("attempt_id",), "short"),
    ("run", ("attempt_id",), "has space ok"),
    ("run", ("attempt_id",), 12345678),
    ("run", ("actor",), "human:"),
    ("run", ("actor",), "root"),
    ("run", ("actor",), "robot:x"),
    ("run", ("actor",), "human:" + "a" * 65),
    ("run", ("actor",), 5),
    ("run", ("actor_source",), "other"),
    ("run", ("stdin_tty",), 1),
    ("run", ("stdin_tty",), "true"),
    ("run", ("decision_id",), ["bad id"]),
    ("run", ("decision_id",), "d-1"),
    ("run", ("decision_id",), [5]),
    ("run", ("decision_id",), ["a"] * 33),
    ("run", ("labels",), ["Upper=x"]),
    ("run", ("labels",), ["nolabel"]),
    ("run", ("labels",), [5]),
    ("run", ("event_id",), "short"),
    ("run", ("event_id",), 7),
    ("run", ("ts",), "2026-02-30T00:00:00Z"),
    ("run", ("ts",), "2026-01-01T25:00:00Z"),
    ("run", ("ts",), 5),
    ("run", ("target", "run_id"), "rr-ABCDEFGH"),
    ("run", ("target", "run_id"), None),
    ("run", ("target", "pr"), "not a pr"),
    ("run", ("target", "pr"), None),
    ("run", ("target", "run_id"), 5),
    ("run", ("inputs", "head_sha"), "A" * 40),
    ("run", ("inputs", "head_sha"), 5),
    ("run", ("inputs", "config_sha256"), "xyz"),
    ("run", ("inputs", "config_sha256"), None),
    ("run", ("inputs", "skill"), "-bad"),
    ("run", ("inputs", "skill"), MARKER + " spaced out"),
    ("run", ("inputs", "skill"), None),
    ("run", ("inputs", "previous_run"), "rr-1"),
    ("run", ("inputs", "session_sandbox"), "bogus"),
    ("run", ("inputs", "setting_sources"), ["user", "user"]),
    ("run", ("inputs", "setting_sources"), ["bogus"]),
    ("run", ("inputs", "setting_sources"), "user"),
    ("run", ("inputs", "plugin_dirs_sha256"), "x"),
    ("run", ("inputs", "override_escalation"), 1),
    ("run", ("inputs", "override_escalation"), None),
    ("run", ("result", "status"), "bogus"),
    ("run", ("result", "status"), None),
    ("run", ("result", "reason"), "free text " + MARKER),
    ("run", ("result", "reason"), MARKER),
    ("run", ("result", "exit_code"), True),
    ("run", ("result", "exit_code"), 3),
    ("run", ("result", "exit_code"), "0"),
    ("run", ("result", "exit_code"), 0.0),
    ("finalize", ("actor",), "human:alice"),
    ("finalize", ("inputs", "cause"), "bogus"),
    ("finalize", ("inputs", "finalized_by"), "cron"),
    ("finalize", ("result", "status"), "attempted"),
    ("finalize", ("result", "status"), "vetoed"),
    ("finalize", ("result", "exit_code"), 11),
    ("finalize", ("result", "exit_code"), None),
    ("prune", ("result", "deleted_runs"), -1),
    ("prune", ("result", "deleted_runs"), True),
    ("prune", ("result", "deleted_runs"), "1"),
    ("prune", ("result", "status"), "failed"),
    ("prune", ("inputs", "older_than"), "soon"),
    ("prune", ("inputs", "older_than"), 5),
    ("code_run", ("result", "status"), "done"),
    ("code_run", ("result", "reason"), MARKER),
    ("code_run", ("inputs", "sandbox"), "maybe"),
    ("code_run", ("inputs", "argv_sha256"), "zz"),
    ("code_run", ("inputs", "throwaway_id"), ""),
    ("code_run", ("inputs", "throwaway_id"), 5),
    ("code_run", ("inputs", "throwaway_id"), "x" * 129),
    ("code_run", ("target", "run_id"), None),
]


@pytest.mark.parametrize(("action", "path", "value"), BAD_AUDIT)
def test_audit_record_outside_the_closed_grammars_is_refused(
    action: str, path: tuple[str, ...], value: Any
) -> None:
    with pytest.raises(ValueError):
        validate_audit_record(mutated(action, setter(path, value)))


def test_vetoed_is_only_a_run_with_exit_11_and_no_reason() -> None:
    ok = {"status": "vetoed", "reason": None, "exit_code": 11}
    rec = mutated("run", setter(("result",), ok))
    validate_audit_record(rec)
    for bad in ({**ok, "exit_code": 0}, {**ok, "reason": "timeout"}):
        with pytest.raises(ValueError):
            validate_audit_record(mutated("run", setter(("result",), bad)))


def test_cancel_resume_and_teardown_values_are_checked() -> None:
    cancel = make_audit_record(
        "cancel",
        caller=CALLER,
        run_id=RUN,
        pr=PR,
        inputs={"agent_id": "ag-abcdefgh", "state": "running"},
        result=DONE,
    )
    validate_audit_record(cancel)
    for key, value in (("agent_id", "ag-1"), ("state", "bogus")):
        bad = copy.deepcopy(cancel)
        bad["inputs"][key] = value
        with pytest.raises(ValueError):
            validate_audit_record(bad)
    tear = make_audit_record(
        "teardown",
        caller=CALLER,
        run_id=RUN,
        pr=PR,
        inputs={"agent_id": "ag-abcdefgh"},
        result={"status": "done"},
    )
    validate_audit_record(tear)
    bad = copy.deepcopy(tear)
    bad["inputs"]["agent_id"] = None
    with pytest.raises(ValueError):
        validate_audit_record(bad)


def test_deep_nesting_is_a_value_error_not_a_recursion_error() -> None:
    deep: Any = []
    for _ in range(5000):
        deep = [deep]
    with pytest.raises(ValueError):
        validate_audit_record(mutated("run", setter(("inputs", "setting_sources"), deep)))
    with pytest.raises(ValueError):
        validate_log_record(
            {"ts": "2026-01-01T00:00:00Z", "level": "info", "event": "x", "m": deep}
        )


def test_end_refuses_attempted_and_nothing_is_written(root: Path, writer: AuditWriter) -> None:
    attempt = writer.begin("run", caller=CALLER, run_id=RUN, pr=PR, inputs=RUN_INPUTS)
    before = writer.path.read_bytes()
    attempted = {"status": "attempted", "reason": None, "exit_code": None}
    with pytest.raises(ValueError):
        writer.end(
            "run", attempt, caller=CALLER, run_id=RUN, pr=PR, inputs=RUN_INPUTS, result=attempted
        )
    assert writer.path.read_bytes() == before


BAD_LOG: list[dict[str, Any]] = [
    {"level": "fatal"},
    {"level": 5},
    {"event": "not_an_event"},
    {"event": MARKER},
    {"tool": "git"},
    {"tool": MARKER},
    {"state": "bogus"},
    {"phase": "bogus"},
    {"run_id": "bad"},
    {"run_id": 5},
    {"pr": "nope"},
    {"count": -1},
    {"count": True},
    {"count": "3"},
    {"code": "Bad Code"},
    {"code": 5},
    {"message": "x" * 301},
    {"message": 5},
    {"ts": "2026-13-01T00:00:00Z"},
]


@pytest.mark.parametrize("override", BAD_LOG)
def test_log_record_outside_the_closed_grammars_is_refused(override: dict[str, Any]) -> None:
    rec: dict[str, Any] = {"ts": "2026-01-01T00:00:00Z", "level": "info", "event": "warning"}
    rec["message"] = "m"
    rec.update(override)
    with pytest.raises(ValueError):
        validate_log_record(rec)


def test_log_message_cap_is_300() -> None:
    rec = make_log_record("warning", "x" * 300, now=clock)
    validate_log_record(rec)
    with pytest.raises(ValueError):
        make_log_record("warning", "x" * 301, now=clock)
    schema = json.loads((Path(audit.__file__).parents[2] / "schemas" / "log.json").read_text())
    assert schema["properties"]["message"]["maxLength"] == audit.MAX_STRING == 300


def test_planted_printable_markers_are_never_written(root: Path, writer: AuditWriter) -> None:
    for path in (("result", "reason"), ("inputs", "skill"), ("labels",)):
        value: Any = [f"k={MARKER} spaced"] if path == ("labels",) else MARKER + " x"
        with pytest.raises(ValueError):
            writer.append(mutated("run", setter(path, value)))
    log = LogWriter(root, now=clock)
    for override in ({"event": MARKER}, {"code": MARKER}, {"tool": MARKER}):
        with pytest.raises(ValueError):
            log.write(**{"event": "warning", "message": "m", **override})  # type: ignore[arg-type]
    for name in ("audit.jsonl", "log.jsonl"):
        path = root / name
        assert not path.exists() or MARKER.encode() not in path.read_bytes()


def test_caller_resolve_enforces_the_grammars() -> None:
    for bad in ("human:", "nobody", "human:a b"):
        with pytest.raises(ValueError):
            Caller.resolve(bad, stdin_tty=False)
    with pytest.raises(ValueError):
        Caller.resolve("auto:x", stdin_tty=False, decision_ids=["bad id"])
    with pytest.raises(ValueError):
        Caller.resolve("auto:x", stdin_tty=False, labels=["NOPE"])


def test_caller_without_a_passwd_entry_falls_back_to_the_uid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(_uid: int) -> Any:
        raise KeyError("getpwuid(): uid not found")

    monkeypatch.setattr(pwd, "getpwuid", missing)
    caller = Caller.resolve(None, stdin_tty=True)
    assert caller.actor == f"human:uid{os.geteuid()}"
    make_audit_record(
        "prune",
        caller=caller,
        run_id=None,
        pr=None,
        inputs={"older_than": "1d"},
        result={"status": "done", "deleted_runs": 0},
    )


# -- outbox: one bad event does not block the others ----------------------------------------


def bad_event(event_id: str) -> NewEvent:
    good = make_event(event_id, 1)
    broken = dict(good.audit[0])
    broken["actor"] = "nobody"
    return NewEvent(event_id, "post-run", {"state": "done"}, (broken,))


def test_flush_writes_the_valid_event_and_leaves_the_invalid_one_pending(
    root: Path, writer: AuditWriter
) -> None:
    with Store.open(root) as store:
        bad_run = end_with(store, bad_event("rr-aaaaaaaa-post-run-1"))
        good_run = end_with(store, make_event("rr-bbbbbbbb-post-run-1"))
        result = flush_outbox(store, writer)
        assert list(result) == ["rr-bbbbbbbb-post-run-1"]
        assert result.stuck == ["rr-aaaaaaaa-post-run-1"]
        assert store.pending_events(bad_run)[0]["audit_pending"] is True
        assert store.pending_events(good_run)[0]["audit_pending"] is False
    assert {r["event_id"] for r in records_of(writer.path)} == {"rr-bbbbbbbb-post-run-1"}


def test_flush_isolates_a_bad_event_within_one_run_scope(root: Path, writer: AuditWriter) -> None:
    with Store.open(root) as store:
        run_id = end_with(store, bad_event("rr-aaaaaaaa-post-run-1"))
        result = flush_outbox(store, writer, run_id)
        assert list(result) == [] and result.stuck == ["rr-aaaaaaaa-post-run-1"]
        assert not writer.path.exists()  # nothing valid: no file is created


def test_append_events_writes_the_valid_ones_then_refuses_the_rest(
    root: Path, writer: AuditWriter
) -> None:
    good = make_event("rr-bbbbbbbb-post-run-1").audit
    bad = bad_event("rr-aaaaaaaa-post-run-1").audit
    with pytest.raises(ValueError, match="rr-aaaaaaaa-post-run-1"):
        writer.append_events({"rr-aaaaaaaa-post-run-1": bad, "rr-bbbbbbbb-post-run-1": good})
    assert len(lines_of(writer.path)) == len(good)
    report = writer.append_events_report({"rr-aaaaaaaa-post-run-1": bad})
    assert report.written == 0 and set(report.rejected) == {"rr-aaaaaaaa-post-run-1"}


# -- unsafe files: FIFO, hardlink, directory, replaced inode --------------------------------


def unsafe(err: pytest.ExceptionInfo[ReviewRunError]) -> None:
    assert err.value.reason == "state_unsafe"


@pytest.mark.parametrize("name", ["audit.jsonl", "log.jsonl"])
def test_a_fifo_is_refused_by_writers_and_export_without_hanging(root: Path, name: str) -> None:
    root.mkdir(mode=0o700)
    os.mkfifo(root / name, 0o600)
    with bounded():
        with pytest.raises(ReviewRunError) as err:  # no reader: ENXIO
            if name == "audit.jsonl":
                AuditWriter(root, now=clock).append(base_record("prune"))
            else:
                LogWriter(root, now=clock).write("warning", "w")
        unsafe(err)
        reader = os.open(root / name, os.O_RDONLY | os.O_NONBLOCK)  # a reader: not a regular file
        try:
            with pytest.raises(ReviewRunError) as err:
                if name == "audit.jsonl":
                    AuditWriter(root, now=clock).append(base_record("prune"))
                else:
                    LogWriter(root, now=clock).write("warning", "w")
            unsafe(err)
        finally:
            os.close(reader)
        with pytest.raises(ReviewRunError) as err:  # export would block forever on a FIFO
            list(export_lines(root, log=name == "log.jsonl"))
        unsafe(err)


def test_export_command_exits_12_for_a_fifo(capsys: pytest.CaptureFixture[str]) -> None:
    state = default_state_root()
    state.mkdir(parents=True, mode=0o700)
    os.mkfifo(state / "audit.jsonl", 0o600)
    with bounded():
        assert run_cli(capsys, "export").code == 12


def test_a_hardlinked_file_is_refused_and_not_chmodded(
    root: Path, tmp_path: Path, writer: AuditWriter
) -> None:
    root.mkdir(mode=0o700)
    other = tmp_path / "other"
    other.write_text("keep\n")
    other.chmod(0o644)
    os.link(other, root / "audit.jsonl")
    with pytest.raises(ReviewRunError) as err:
        writer.append(base_record("prune"))
    unsafe(err)
    assert stat.S_IMODE(other.stat().st_mode) == 0o644 and other.read_text() == "keep\n"
    with pytest.raises(ReviewRunError) as err:
        list(export_lines(root))
    unsafe(err)


@pytest.mark.parametrize("name", ["audit.jsonl", "log.jsonl"])
def test_a_directory_at_the_file_path_is_state_unsafe(root: Path, name: str) -> None:
    root.mkdir(mode=0o700)
    (root / name).mkdir()
    with pytest.raises(ReviewRunError) as err:
        if name == "audit.jsonl":
            AuditWriter(root).append(base_record("prune"))
        else:
            LogWriter(root).write("warning", "w")
    unsafe(err)
    with pytest.raises(ReviewRunError) as err:
        list(export_lines(root, log=name == "log.jsonl"))
    unsafe(err)


def test_export_command_exits_12_for_unsafe_paths(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    state = default_state_root()
    state.mkdir(parents=True, mode=0o700)
    victim = tmp_path / "victim"
    victim.write_text('{"ts":"2026-01-01T00:00:00Z"}\n')
    (state / "audit.jsonl").symlink_to(victim)
    (state / "log.jsonl").symlink_to(victim)
    assert run_cli(capsys, "export").code == 12
    assert run_cli(capsys, "export", "--log").code == 12
    (state / "audit.jsonl").unlink()
    (state / "log.jsonl").unlink()
    state.rmdir()
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    state.symlink_to(real)
    assert run_cli(capsys, "export").code == 12
    assert run_cli(capsys, "export", "--log").code == 12


def test_a_file_replaced_between_open_and_lock_is_refused(
    root: Path, writer: AuditWriter, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer.append(base_record("prune"))
    real_lock = audit._lock  # pyright: ignore[reportPrivateUsage]

    def swap_then_lock(fd: int) -> None:
        (root / "audit.jsonl").rename(root / "moved")
        (root / "audit.jsonl").write_bytes(b"")
        real_lock(fd)

    monkeypatch.setattr(audit, "_lock", swap_then_lock)
    with pytest.raises(ReviewRunError) as err:
        writer.append(base_record("prune"))
    unsafe(err)
    assert (root / "audit.jsonl").read_bytes() == b""
    assert len(lines_of(root / "moved")) == 1


def test_a_failed_last_byte_read_raises_instead_of_skipping(
    root: Path, writer: AuditWriter, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer.append(base_record("prune"))

    def boom(*_args: Any) -> bytes:
        raise OSError(5, "boom")

    monkeypatch.setattr(os, "pread", boom)
    with pytest.raises(OSError):
        writer.append(base_record("prune"))
    monkeypatch.undo()
    assert len(lines_of(writer.path)) == 1


# -- lock timeout ---------------------------------------------------------------------------


def test_a_held_lock_times_out_as_failed(
    root: Path, writer: AuditWriter, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fcntl

    writer.append(base_record("prune"))
    monkeypatch.setattr(audit, "LOCK_WAIT_SECONDS", 0.2)
    held = threading.Event()
    release = threading.Event()

    def holder() -> None:
        fd = os.open(root / "audit.jsonl", os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            held.set()
            release.wait(10)
        finally:
            os.close(fd)

    thread = threading.Thread(target=holder)
    thread.start()
    try:
        assert held.wait(10)
        started = time.monotonic()
        with pytest.raises(ReviewRunError) as err:
            writer.append(base_record("prune"))
        assert err.value.code == "failed" and time.monotonic() - started < 5
    finally:
        release.set()
        thread.join(10)
    writer.append(base_record("prune"))  # the lock is free again
    assert len(lines_of(writer.path)) == 2


# -- export keeps a valid foreign line whose ASCII form is long -----------------------------


def test_export_keeps_a_long_non_ascii_line_under_the_raw_cap(root: Path) -> None:
    root.mkdir(mode=0o700)
    text = "é" * 20_000  # 40 KB raw (UTF-8), about 120 KB once ASCII-escaped
    raw = json.dumps({"ts": "2026-01-01T00:00:00Z", "x": text}, ensure_ascii=False).encode()
    assert 35_000 < len(raw) < audit.MAX_LINE_BYTES
    (root / "audit.jsonl").write_bytes(raw + b"\n")
    (out,) = list(export_lines(root, now=lambda: dt.datetime(2026, 1, 2, tzinfo=dt.UTC)))
    assert json.loads(out)["x"] == text and out.isascii()
