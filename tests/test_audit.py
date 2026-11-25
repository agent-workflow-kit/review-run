"""Audit and log writers, the outbox flush and ``export`` (issue #9)."""

from __future__ import annotations

import datetime as dt
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from review_run.audit import (
    MAX_LINE_BYTES,
    AuditWriter,
    Caller,
    LogWriter,
    export_lines,
    flush_outbox,
    make_audit_record,
    new_attempt_id,
)
from review_run.errors import ReviewRunError
from review_run.store import Expect, NewEvent, NewRun, Store, lease_name
from tests.conftest import validate_against
from tests.golden_harness import GOLDEN
from tests.helpers import run_cli

WORKER = Path(__file__).resolve().parent / "audit_worker.py"
NOW = dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=dt.UTC)
RUN = "rr-abcdefgh"
PR = "host/o/r#7"
SHA = "a" * 40
H64 = "b" * 64
MARKER = "UNTRUSTED-MARKER-7f3a"


def clock() -> dt.datetime:
    return NOW


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "state"


@pytest.fixture
def writer(root: Path) -> AuditWriter:
    return AuditWriter(root, now=clock)


CALLER = Caller("human:alice", "flag", True, ("d-1",), ("team=core",))
RUN_INPUTS: dict[str, Any] = {
    "head_sha": None,
    "config_sha256": H64,
    "skill": "review-run:review-pr",
    "previous_run": None,
    "permission_profile_sha256": H64,
    "session_sandbox": "required",
    "setting_sources": [],
    "plugin_dirs_sha256": None,
    "override_escalation": False,
}
DONE = {"status": "done", "reason": None, "exit_code": 0}


def lines_of(path: Path) -> list[bytes]:
    return path.read_bytes().splitlines(keepends=True)


def records_of(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in lines_of(path)]


# -- goldens: one record (pair) per action ---------------------------------------------------


def golden_records(writer: AuditWriter) -> None:
    fin_inputs = {"cause": "runner_lost", "finalized_by": "reconcile"}
    resume_inputs = {k: v for k, v in RUN_INPUTS.items() if k != "override_escalation"}
    with_head = {**RUN_INPUTS, "head_sha": SHA}
    for action, inputs, end_inputs in (
        ("run", RUN_INPUTS, with_head),
        ("resume", resume_inputs, {**resume_inputs, "head_sha": SHA}),
        ("cancel", {"agent_id": None, "state": "running"}, {"agent_id": None, "state": "running"}),
    ):
        attempt = writer.begin(action, caller=CALLER, run_id=RUN, pr=PR, inputs=inputs)  # type: ignore[arg-type]
        assert attempt  # generated
        writer.end(
            action,  # type: ignore[arg-type]
            attempt,
            caller=CALLER,
            run_id=RUN,
            pr=PR,
            inputs=end_inputs,
            result=DONE,
        )
    writer.record(
        "run",
        caller=CALLER,
        run_id=RUN,
        pr=PR,
        inputs=RUN_INPUTS,
        result={"status": "vetoed", "reason": None, "exit_code": 11},
    )
    writer.record(
        "run",
        caller=CALLER,
        run_id=RUN,
        pr=PR,
        inputs=RUN_INPUTS,
        result={"status": "escalated", "reason": None, "exit_code": 13},
    )
    writer.record(
        "finalize",
        caller=Caller.finalizer(),
        run_id=RUN,
        pr=PR,
        inputs=fin_inputs,
        result={"status": "failed", "reason": "runner_lost", "exit_code": 17},
    )
    writer.record(
        "code_run",
        caller=CALLER,
        run_id=RUN,
        pr=PR,
        inputs={"throwaway_id": "tw-1", "argv_sha256": H64, "sandbox": "required"},
        result={"status": "ok", "reason": None},
    )
    writer.record(
        "teardown",
        caller=CALLER,
        run_id=RUN,
        pr=PR,
        inputs={"agent_id": "ag-abcdefgh"},
        result={"status": "done"},
    )
    writer.record(
        "prune",
        caller=CALLER,
        run_id=None,
        pr=None,
        inputs={"older_than": "30d"},
        result={"status": "done", "deleted_runs": 3},
    )


def normalize_attempts(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Replace the random attempt ids by stable ones (same id stays the same)."""
    seen: dict[str, str] = {}
    for rec in records:
        old = rec["attempt_id"]
        rec["attempt_id"] = seen.setdefault(old, f"attempt-{len(seen):04d}")
    return records


def check_jsonl_golden(name: str, schema: str, records: list[dict[str, Any]]) -> None:
    for rec in records:
        validate_against(schema, rec)
    text = "".join(json.dumps(r, separators=(",", ":"), ensure_ascii=True) + "\n" for r in records)
    path = GOLDEN / name
    if os.environ.get("UPDATE_GOLDEN") == "1":
        path.write_text(text, encoding="utf-8")
    assert path.read_text(encoding="utf-8") == text


def test_audit_goldens_one_record_per_action_and_pairs(root: Path, writer: AuditWriter) -> None:
    golden_records(writer)
    records = normalize_attempts(records_of(writer.path))
    assert {r["action"] for r in records} == {
        "run",
        "resume",
        "cancel",
        "finalize",
        "code_run",
        "teardown",
        "prune",
    }
    check_jsonl_golden("audit.jsonl", "audit.json", records)
    # a pair shares its attempt_id; the opening half has result.status attempted
    runs = [r for r in records if r["action"] == "run"][:2]
    assert runs[0]["attempt_id"] == runs[1]["attempt_id"]
    assert runs[0]["result"]["status"] == "attempted" and runs[1]["result"]["status"] == "done"
    assert records[0]["inputs"]["head_sha"] is None and records[1]["inputs"]["head_sha"] == SHA


def test_log_golden_every_event_validates(root: Path) -> None:
    log = LogWriter(root, now=clock)
    for event in (
        "phase_change",
        "state_change",
        "warning",
        "normalization",
        "sibling_error",
        "hook_failed",
        "hook_skipped",
        "teardown_retry",
        "reconcile",
        "throwaway_leaked",
    ):
        log.write(event, f"{event} ok", run_id=RUN, pr=PR, phase="session", state="running")
    log.write("sibling_error", "forgectl failed", level="error", tool="forgectl", code="timeout")
    log.write("normalization", "fixed 3", count=3)
    records = records_of(log.path)
    check_jsonl_golden("log.jsonl", "log.json", records)


def test_finalize_record_ends_a_run_whose_pair_stays_open(root: Path, writer: AuditWriter) -> None:
    attempt = writer.begin("run", caller=CALLER, run_id=RUN, pr=PR, inputs=RUN_INPUTS)
    writer.record(
        "finalize",
        caller=Caller.finalizer(),
        run_id=RUN,
        pr=PR,
        inputs={"cause": "runner_lost", "finalized_by": "show"},
        result={"status": "failed", "reason": "runner_lost", "exit_code": 17},
    )
    recs = records_of(writer.path)
    assert [r["attempt_id"] for r in recs if r["attempt_id"] == attempt] == [attempt]
    assert recs[1]["attempt_id"] != attempt and recs[1]["actor"] == "auto:review-run"
    validate_against("audit.json", recs[1])


# -- caller ----------------------------------------------------------------------------------


def test_caller_actor_resolution() -> None:
    assert Caller.resolve("auto:policy", stdin_tty=False).actor_source == "flag"
    tty = Caller.resolve(None, stdin_tty=True, user="a b/c\n")
    assert (tty.actor, tty.actor_source) == ("human:a_b_c_", "tty")
    none = Caller.resolve(None, stdin_tty=False)
    assert (none.actor, none.actor_source) == ("auto:unknown", "default")
    assert len(new_attempt_id()) == 19


# -- shape guards: closed values only --------------------------------------------------------


def test_untrusted_text_is_refused_not_written(root: Path, writer: AuditWriter) -> None:
    bad_inputs = [
        {**RUN_INPUTS, "skill": f"s\n{MARKER}"},  # control character
        {**RUN_INPUTS, "skill": "caf\u00e9"},  # non-ASCII
        {**RUN_INPUTS, "skill": "x" * 400},  # long
        {**RUN_INPUTS, "extra": MARKER},  # an extra field
    ]
    for inputs in bad_inputs:
        with pytest.raises(ValueError):
            writer.record("run", caller=CALLER, run_id=RUN, pr=PR, inputs=inputs, result=DONE)
    with pytest.raises(ValueError):
        writer.record(
            "run", caller=CALLER, run_id=RUN, pr=PR, inputs=RUN_INPUTS, result={**DONE, "x": MARKER}
        )
    with pytest.raises(ValueError):  # resume has no override_escalation
        writer.record("resume", caller=CALLER, run_id=RUN, pr=PR, inputs=RUN_INPUTS, result=DONE)
    with pytest.raises(ValueError):
        LogWriter(root).write("warning", f"line\x1b[31m {MARKER}")
    with pytest.raises(ValueError):
        writer.append({"ts": "2026-01-01T00:00:00Z"})
    log_path = root / "log.jsonl"
    for path in (writer.path, log_path):
        assert not path.exists() or MARKER not in path.read_text()


def test_oversize_record_is_refused(writer: AuditWriter) -> None:
    big = {**RUN_INPUTS, "setting_sources": ["user"] * (MAX_LINE_BYTES // 4)}
    with pytest.raises(ValueError):
        writer.record("run", caller=CALLER, run_id=RUN, pr=PR, inputs=big, result=DONE)


# -- files -----------------------------------------------------------------------------------


def test_files_are_0600_in_a_0700_root(root: Path, writer: AuditWriter) -> None:
    os.umask(0o022)  # the writer must not depend on the process umask
    golden_records(writer)
    LogWriter(root).write("warning", "w")
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    for name in ("audit.jsonl", "log.jsonl"):
        assert stat.S_IMODE((root / name).stat().st_mode) == 0o600


def test_a_loose_existing_file_is_tightened(root: Path, writer: AuditWriter) -> None:
    root.mkdir(mode=0o700)
    (root / "audit.jsonl").write_bytes(b"")
    (root / "audit.jsonl").chmod(0o666)
    golden_records(writer)
    assert stat.S_IMODE((root / "audit.jsonl").stat().st_mode) == 0o600


def test_symlinked_file_is_refused_and_not_followed(
    root: Path, tmp_path: Path, writer: AuditWriter
) -> None:
    root.mkdir(mode=0o700)
    target = tmp_path / "victim"
    target.write_text("keep\n")
    (root / "audit.jsonl").symlink_to(target)
    with pytest.raises(ReviewRunError) as err:
        golden_records(writer)
    assert err.value.reason == "state_unsafe"
    assert target.read_text() == "keep\n"


def test_symlinked_root_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(ReviewRunError) as err:
        golden_records(AuditWriter(link, now=clock))
    assert err.value.reason == "state_unsafe"
    assert list(real.iterdir()) == []
    with pytest.raises(ReviewRunError):
        list(export_lines(link))


def test_a_torn_tail_is_closed_before_the_next_record(root: Path, writer: AuditWriter) -> None:
    golden_records(writer)
    with writer.path.open("ab") as handle:
        handle.write(b'{"ts":"2026-01-01T00:00:00.000Z","attem')  # no newline: a torn write
    writer.record(
        "prune",
        caller=CALLER,
        run_id=None,
        pr=None,
        inputs={"older_than": "1d"},
        result={"status": "done", "deleted_runs": 0},
    )
    raw = lines_of(writer.path)
    assert all(line.endswith(b"\n") for line in raw)
    parsed = []
    for line in raw:
        try:
            parsed.append(json.loads(line))
        except ValueError:
            pass
    assert parsed[-1]["inputs"] == {"older_than": "1d"}
    assert len(parsed) == len(raw) - 1  # only the torn fragment is unparsable
    assert len(list(export_lines(root))) == len(parsed)


# -- real concurrent writers and a killed writer ---------------------------------------------


def spawn(root: Path, go: Path, count: int, tag: str, *extra: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, str(WORKER), str(root), str(go), str(count), tag, *extra],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_concurrent_processes_never_interleave_lines(root: Path, tmp_path: Path) -> None:
    AuditWriter(root).path.parent.mkdir(parents=True, exist_ok=True)
    go = tmp_path / "go"
    procs = [spawn(root, go, 60, f"w{i}") for i in range(5)]
    go.write_text("go")
    for proc in procs:
        _, stderr = proc.communicate(timeout=60)
        assert proc.returncode == 0, stderr
    recs = records_of(root / "audit.jsonl")  # every line parses
    assert len(recs) == 300
    for rec in recs:
        validate_against("audit.json", rec)
    ids = [r["attempt_id"] for r in recs]
    assert len(set(ids)) == 300
    for i in range(5):  # each writer's own order is kept
        mine = [r["result"]["deleted_runs"] for r in recs if r["attempt_id"].startswith(f"w{i}-")]
        assert mine == list(range(60))


def test_a_killed_writer_leaves_valid_jsonl_and_the_next_append_works(
    root: Path, tmp_path: Path
) -> None:
    go = tmp_path / "go"
    go.write_text("go")
    proc = spawn(root, go, 10_000, "kill", "slow")
    assert proc.stdout is not None
    for _ in range(5):  # wait until some records exist
        assert proc.stdout.readline()
    proc.kill()  # exactly the process this test started
    proc.communicate(timeout=30)
    recs = records_of(root / "audit.jsonl")  # every line is complete valid JSON
    assert len(recs) >= 5
    assert all(line.endswith(b"\n") for line in lines_of(root / "audit.jsonl"))
    AuditWriter(root, now=clock).record(
        "prune",
        caller=CALLER,
        run_id=None,
        pr=None,
        inputs={"older_than": "1d"},
        result={"status": "done", "deleted_runs": 0},
    )
    assert len(records_of(root / "audit.jsonl")) == len(recs) + 1


# -- the outbox ------------------------------------------------------------------------------


def make_event(event_id: str, n: int = 2) -> NewEvent:
    records = tuple(
        make_audit_record(
            "run",
            caller=CALLER,
            run_id=RUN,
            pr=PR,
            inputs={**RUN_INPUTS, "head_sha": SHA},
            result=DONE,
            event_id=event_id,
            attempt_id=f"attempt-{i:04d}",
            ts="2026-01-01T12:00:00.000Z",
        )
        for i in range(n)
    )
    return NewEvent(event_id, "post-run", {"state": "done"}, records)


def end_with(store: Store, event: NewEvent) -> str:
    run_id = store.reserve_id()
    store.create_run(NewRun(run_id, PR, "host/o/r", SHA, "headless", "s"))
    lease = store.acquire_lease(lease_name(run_id))
    assert lease is not None
    assert store.begin_finalize(run_id, Expect("running", "prefetch", None), lease=lease)
    stored = NewEvent(event.event_id, event.hook, event.payload, event.audit)
    assert store.end_run(
        run_id,
        Expect("running", "finalize", None),
        lease=lease,
        state="done",
        reason=None,
        exit_code=0,
        events=[stored],
    )
    return run_id


def test_flush_writes_each_event_once_even_after_a_crash_before_the_mark(
    root: Path, writer: AuditWriter
) -> None:
    with Store.open(root) as store:
        run_id = end_with(store, make_event("rr-abcdefgh-post-run-1"))
        assert flush_outbox(store, writer) == ["rr-abcdefgh-post-run-1"]
        assert len(lines_of(writer.path)) == 2
        assert store.pending_events(run_id)[0]["audit_pending"] is False
        # the crash: the lines are in the file but the mark was lost
        with store.transaction() as conn:
            conn.execute("UPDATE events SET audit_pending = 1")
        assert flush_outbox(store, writer) == ["rr-abcdefgh-post-run-1"]
        assert len(lines_of(writer.path)) == 2  # not appended twice
        assert flush_outbox(store, writer) == []  # nothing pending now


def test_flush_completes_a_partially_written_event(root: Path, writer: AuditWriter) -> None:
    event = make_event("rr-abcdefgh-post-run-1", 3)
    with Store.open(root) as store:
        end_with(store, event)
        writer.append(event.audit[0])  # died after the first of three records
        flush_outbox(store, writer)
    recs = records_of(writer.path)
    assert [r["attempt_id"] for r in recs] == [f"attempt-{i:04d}" for i in range(3)]
    assert all(r["event_id"] == "rr-abcdefgh-post-run-1" for r in recs)


def test_flush_is_race_safe_between_processes(root: Path, writer: AuditWriter) -> None:
    event = make_event("rr-abcdefgh-post-run-1")
    with Store.open(root) as store:
        end_with(store, event)
    flusher = (
        "import sys\n"
        "from pathlib import Path\n"
        "from review_run.audit import AuditWriter, flush_outbox\n"
        "from review_run.store import Store\n"
        "root = Path(sys.argv[1])\n"
        "with Store.open(root) as s:\n"
        "    flush_outbox(s, AuditWriter(root))\n"
    )
    procs = [
        subprocess.Popen([sys.executable, "-c", flusher, str(root)], stderr=subprocess.PIPE)
        for _ in range(4)
    ]
    for proc in procs:
        _, err = proc.communicate(timeout=60)
        assert proc.returncode == 0, err
    assert len(lines_of(root / "audit.jsonl")) == 2


def test_flush_refuses_a_record_of_another_event(root: Path, writer: AuditWriter) -> None:
    rec = make_event("rr-abcdefgh-post-run-1").audit[0]
    with pytest.raises(ValueError):
        writer.append_event_records("rr-zzzzzzzz-post-run-1", [rec])
    assert not writer.path.exists()  # nothing valid to write: the file is not even created


# -- export ----------------------------------------------------------------------------------


def stamped(root: Path, stamps: list[str]) -> None:
    w = AuditWriter(root)
    for i, ts in enumerate(stamps):
        w.append(
            make_audit_record(
                "prune",
                caller=CALLER,
                run_id=None,
                pr=None,
                inputs={"older_than": "1d"},
                result={"status": "done", "deleted_runs": i},
                ts=ts,
            )
        )


def test_export_since_rfc3339_and_duration(root: Path) -> None:
    stamped(
        root,
        ["2026-01-01T09:00:00.000Z", "2026-01-01T11:30:00.000Z", "2026-01-01T11:59:59.999Z"],
    )
    got = lambda since: [  # noqa: E731
        json.loads(x)["result"]["deleted_runs"] for x in export_lines(root, since=since, now=clock)
    ]
    assert got(None) == [0, 1, 2]
    assert got(dt.timedelta(hours=1)) == [1, 2]
    assert got(dt.timedelta(0)) == []
    assert got(dt.datetime(2026, 1, 1, 11, 30, tzinfo=dt.UTC)) == [1, 2]
    assert got(dt.datetime(2026, 1, 1, 11, 30, 0, 1000, tzinfo=dt.UTC)) == [2]
    assert got(dt.timedelta(days=36500)) == [0, 1, 2]


def test_export_skips_foreign_and_torn_lines_and_escapes(root: Path) -> None:
    stamped(root, ["2026-01-01T09:00:00.000Z"])
    with (root / "audit.jsonl").open("ab") as handle:
        handle.write(b"not json\n[1,2]\n{}\n")
        handle.write(b'{"ts":"nope"}\n{"ts":5}\n')
        handle.write(b'{"ts":"2026-01-01T10:00:00Z","x":"\\u001b[31m\xc3\xa9"}\n')
        handle.write(b"x" * (MAX_LINE_BYTES + 10) + b"\n")  # over the cap: skipped
        handle.write(b'{"ts":"2026-01-01T10:00:00Z"')  # torn, no newline
    out = list(export_lines(root))
    assert len(out) == 2
    assert out[1] == b'{"ts":"2026-01-01T10:00:00Z","x":"\\u001b[31m\\u00e9"}\n'
    assert all(b"\x1b" not in line for line in out)


def test_export_of_a_missing_state_creates_nothing(root: Path) -> None:
    assert list(export_lines(root)) == []
    assert not root.exists()
    root.mkdir(mode=0o700)
    assert list(export_lines(root, log=True)) == []
    assert list(root.iterdir()) == []


def test_export_command_prints_audit_and_log_without_an_envelope(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from review_run.store import default_state_root

    state = default_state_root()
    golden_records(AuditWriter(state, now=clock))
    LogWriter(state, now=clock).write("warning", "w1", run_id=RUN)
    res = run_cli(capsys, "export")
    assert res.code == 0 and res.err == ""
    lines = res.out.splitlines()
    assert len(lines) == 12
    for line in lines:
        validate_against("audit.json", json.loads(line))
    res = run_cli(capsys, "export", "--log")
    (line,) = res.out.splitlines()
    validate_against("log.json", json.loads(line))
    assert run_cli(capsys, "export", "--since", "1h").out == ""  # the golden clock is 2026-01-01
    assert len(run_cli(capsys, "export", "--since", "2025-01-01T00:00:00Z").out.splitlines()) == 12
    assert run_cli(capsys, "export", "--since", "bogus").code == 2


def test_export_command_on_a_fresh_state_prints_nothing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    res = run_cli(capsys, "export")
    assert (res.code, res.out, res.err) == (0, "", "")


def test_export_golden_is_the_audit_file(root: Path, writer: AuditWriter) -> None:
    golden_records(writer)
    exported = [json.loads(x) for x in export_lines(root)]
    assert exported == records_of(writer.path)
