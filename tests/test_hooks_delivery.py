"""Outbox delivery: at least once, bounded attempts, crash-safe, one deliverer at a time."""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from review_run import gateway as gw
from review_run import hooks
from review_run.audit import AuditWriter, LogWriter
from review_run.gateway import CompletedRun, ToolMissing
from review_run.store import Store, format_time
from tests.fake_gateway import Call, FakeGateway, ok
from tests.hooks_helpers import PY, appender, event_row, seed

SH = "/bin/sh"
WORKER = Path(__file__).resolve().parent / "hooks_worker.py"
CMD_MARKER = "SECRET-CMD-MARKER-91"
OUT_MARKER = "HOOK-OUTPUT-MARKER-55"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "state"


@pytest.fixture
def store(root: Path) -> Iterator[Store]:
    with Store.open(root) as s:
        yield s


def deliver(
    store: Store, root: Path, config: dict[str, Any] | None = None, **kw: Any
) -> list[hooks.DeliveryResult]:
    return hooks.deliver_pending(
        store, AuditWriter(root), LogWriter(root), config, base_env={"PATH": "/x"}, **kw
    )


def post_run_config(*entries: dict[str, Any]) -> dict[str, Any]:
    return {"post-run": list(entries)}


def audit_lines(root: Path) -> list[str]:
    path = root / "audit.jsonl"
    return path.read_text().splitlines() if path.exists() else []


def log_records(root: Path) -> list[dict[str, Any]]:
    path = root / "log.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def events_of(fake: FakeGateway) -> list[str]:
    return [c.env["REVIEW_RUN_EVENT_ID"] for c in fake.calls if c.env]


def test_a_delivery_appends_the_audit_runs_the_hook_and_marks_it(
    store: Store, root: Path, fake_gateway: FakeGateway
) -> None:
    fake_gateway.script(SH, ok())
    run_id, event_id = seed(store)
    results = deliver(store, root, post_run_config({"cmd": "notify"}))
    assert results == [hooks.DeliveryResult(event_id, "delivered")]
    assert len(audit_lines(root)) == 2
    (call,) = fake_gateway.calls
    assert call.argv == (SH, "-c", "notify")
    assert call.env is not None
    assert call.env["REVIEW_RUN_EVENT_ID"] == event_id and call.env["REVIEW_RUN_ID"] == run_id
    assert call.env["REVIEW_RUN_HOOK"] == "post-run" and call.env["PATH"] == "/x"
    assert json.loads(call.stdin or "")["event_id"] == event_id
    assert event_row(store, event_id) == (1, 1, 0)
    assert deliver(store, root, post_run_config({"cmd": "notify"})) == []
    assert len(fake_gateway.calls) == 1


def test_an_event_with_no_hook_is_delivered_and_its_audit_appended(
    store: Store, root: Path, fake_gateway: FakeGateway
) -> None:
    _, event_id = seed(store)
    assert deliver(store, root, None)[0].status == "delivered"
    assert fake_gateway.calls == []
    assert len(audit_lines(root)) == 2
    assert event_row(store, event_id) == (0, 1, 0)


def test_post_escalate_events_run_the_post_escalate_hooks_only(
    store: Store, root: Path, fake_gateway: FakeGateway
) -> None:
    fake_gateway.script(SH, ok())
    _, event_id = seed(store, hook="post-escalate", number=2)
    config = {"post-run": [{"cmd": "r"}], "post-escalate": [{"cmd": "e"}]}
    assert deliver(store, root, config)[0].event_id == event_id
    (call,) = fake_gateway.calls
    assert call.argv[2] == "e"
    assert call.env is not None and call.env["REVIEW_RUN_HOOK"] == "post-escalate"
    assert event_id.endswith("-post-escalate-2")


def test_config_and_stored_entries_each_fire_once_config_first(
    store: Store, root: Path, fake_gateway: FakeGateway
) -> None:
    fake_gateway.script(SH, ok())
    seed(store, flag_hooks=("post-run=from-run", "post-run=from-resume", "pre-run=ignored"))
    deliver(store, root, post_run_config({"cmd": "from-config"}))
    assert [c.argv[2] for c in fake_gateway.calls] == ["from-config", "from-run", "from-resume"]


def test_a_failing_hook_is_retried_and_abandoned_after_five_attempts(
    store: Store, root: Path, fake_gateway: FakeGateway
) -> None:
    fake_gateway.script(SH, lambda c: CompletedRun(c.argv, 1, OUT_MARKER, OUT_MARKER))
    run_id, event_id = seed(store)
    config = post_run_config({"cmd": f"echo {CMD_MARKER}"})
    statuses = [deliver(store, root, config)[0].status for _ in range(5)]
    assert statuses == ["retry", "retry", "retry", "retry", "abandoned"]
    assert len(fake_gateway.calls) == 5
    assert event_row(store, event_id) == (5, 1, 0)
    assert len(audit_lines(root)) == 2  # appended on the first pass, never twice
    run = store.get_run(run_id)
    assert run is not None and "hook_failed" in run.warnings
    failed = [r for r in log_records(root) if r["event"] == "hook_failed"]
    assert failed and all(r["run_id"] == run_id for r in failed)
    assert failed[-1]["level"] == "error" and failed[-1]["count"] == 5
    assert deliver(store, root, config) == []
    assert len(fake_gateway.calls) == 5


def test_commands_and_hook_output_are_never_logged_or_audited(
    store: Store, root: Path, fake_gateway: FakeGateway
) -> None:
    fake_gateway.script(SH, lambda c: CompletedRun(c.argv, 1, OUT_MARKER, OUT_MARKER))
    run_id, _ = seed(store, flag_hooks=(f"post-run=echo {CMD_MARKER}",))
    for _ in range(5):
        deliver(store, root, post_run_config({"cmd": f"echo {CMD_MARKER}"}))
    text = (root / "log.jsonl").read_text() + (root / "audit.jsonl").read_text()
    text += json.dumps(store.get_run(run_id).warnings)  # type: ignore[union-attr]
    for marker in (CMD_MARKER, OUT_MARKER):
        assert marker not in text


def test_a_timeout_or_a_missing_shell_counts_as_a_failed_attempt(
    store: Store, root: Path, fake_gateway: FakeGateway
) -> None:
    fake_gateway.script(SH, gw.CallTimeout("t"), ToolMissing("m"), ok())
    _, event_id = seed(store)
    config = post_run_config({"cmd": "x"})
    assert [deliver(store, root, config)[0].status for _ in range(3)] == [
        "retry",
        "retry",
        "delivered",
    ]
    assert event_row(store, event_id)[0] == 3
    assert {r["code"] for r in log_records(root) if r["event"] == "hook_failed"} == {
        "timeout",
        "tool_missing",
    }


def test_one_failing_entry_does_not_stop_the_others_and_all_rerun_at_least_once(
    store: Store, root: Path, fake_gateway: FakeGateway
) -> None:
    fake_gateway.script(SH, ok(returncode=1), ok(), ok(), ok())
    seed(store)
    config = post_run_config({"cmd": "bad"}, {"cmd": "good"})
    assert deliver(store, root, config)[0].status == "retry"
    assert [c.argv[2] for c in fake_gateway.calls] == ["bad", "good"]
    assert deliver(store, root, config)[0].status == "delivered"
    assert [c.argv[2] for c in fake_gateway.calls] == ["bad", "good", "bad", "good"]


def test_a_crash_between_the_hook_and_the_mark_redelivers_with_the_same_event_id(
    store: Store, root: Path, fake_gateway: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_gateway.script(SH, ok())
    _, event_id = seed(store)
    original = store.mark_event

    def crash(event_id: str, **kw: bool) -> None:
        if kw.get("delivered"):
            raise RuntimeError("killed")
        original(event_id, **kw)

    monkeypatch.setattr(store, "mark_event", crash)
    with pytest.raises(RuntimeError):
        deliver(store, root, post_run_config({"cmd": "n"}))
    monkeypatch.setattr(store, "mark_event", original)
    assert store.lease_info(hooks.event_lease_name(event_id)) is None  # released
    assert deliver(store, root, post_run_config({"cmd": "n"}))[0].status == "delivered"
    assert events_of(fake_gateway) == [event_id, event_id]
    assert len(audit_lines(root)) == 2


def test_a_crash_after_the_commit_before_any_delivery_is_repaired_by_the_next_pass(
    store: Store, root: Path, fake_gateway: FakeGateway
) -> None:
    fake_gateway.script(SH, ok())
    _, event_id = seed(store)
    assert event_row(store, event_id) == (0, 0, 1)  # as the terminal transaction left it
    deliver(store, root, post_run_config({"cmd": "n"}))
    assert len(audit_lines(root)) == 2
    assert events_of(fake_gateway) == [event_id]


def test_an_event_delivered_but_with_pending_audit_only_appends_the_audit(
    store: Store, root: Path, fake_gateway: FakeGateway
) -> None:
    _, event_id = seed(store)
    store.mark_event(event_id, delivered=True)
    assert deliver(store, root, post_run_config({"cmd": "n"})) == []
    assert len(audit_lines(root)) == 2
    assert fake_gateway.calls == []


def test_events_are_delivered_oldest_first(
    store: Store, root: Path, fake_gateway: FakeGateway
) -> None:
    fake_gateway.script(SH, ok())
    ids = [seed(store)[1] for _ in range(3)]
    stamps = ["2026-01-01T00:00:03.000Z", "2026-01-01T00:00:01.000Z", "2026-01-01T00:00:02.000Z"]
    for event_id, stamp in zip(ids, stamps, strict=True):
        with store.transaction() as conn:
            conn.execute("UPDATE events SET created_at = ? WHERE event_id = ?", (stamp, event_id))
    deliver(store, root, post_run_config({"cmd": "n"}))
    assert events_of(fake_gateway) == [ids[1], ids[2], ids[0]]


def test_a_pass_can_be_limited_to_one_run(
    store: Store, root: Path, fake_gateway: FakeGateway
) -> None:
    fake_gateway.script(SH, ok())
    (run_a, event_a), (_, event_b) = seed(store), seed(store)
    deliver(store, root, post_run_config({"cmd": "n"}), run_id=run_a)
    assert events_of(fake_gateway) == [event_a]
    assert event_row(store, event_b)[1] == 0


def test_async_entries_start_detached_only_when_the_sync_ones_succeeded(
    store: Store, root: Path, fake_gateway: FakeGateway
) -> None:
    fake_gateway.script(SH, ok(returncode=1), ok())
    _, event_id = seed(store)
    config = post_run_config({"cmd": "sync"}, {"cmd": "bg", "async": True, "timeout": "9s"})
    assert deliver(store, root, config)[0].status == "retry"
    assert [c.detached for c in fake_gateway.calls] == [False]
    assert deliver(store, root, config)[0].status == "delivered"
    assert [c.detached for c in fake_gateway.calls] == [False, False, True]
    bg = fake_gateway.calls[-1]
    assert bg.timeout == 9.0 and bg.env is not None and bg.env["REVIEW_RUN_EVENT_ID"] == event_id


def test_async_entries_start_on_the_final_attempt_even_when_a_sync_one_keeps_failing(
    store: Store, root: Path, fake_gateway: FakeGateway
) -> None:
    fake_gateway.script(SH, ok(returncode=1))
    seed(store)
    config = post_run_config({"cmd": "sync"}, {"cmd": "bg", "async": True})
    statuses = [deliver(store, root, config)[0].status for _ in range(5)]
    assert statuses[-1] == "abandoned"
    assert [c.detached for c in fake_gateway.calls].count(True) == 1


def test_a_detached_start_that_fails_is_retried(
    store: Store, root: Path, fake_gateway: FakeGateway
) -> None:
    fake_gateway.script(SH, ToolMissing("m"))
    seed(store)
    config = post_run_config({"cmd": "bg", "async": True})
    assert deliver(store, root, config)[0].status == "retry"


def test_an_event_another_process_holds_is_left_alone(
    store: Store, root: Path, fake_gateway: FakeGateway
) -> None:
    _, event_id = seed(store)
    held = store.acquire_lease(hooks.event_lease_name(event_id))
    assert held is not None
    assert deliver(store, root, post_run_config({"cmd": "n"})) == [
        hooks.DeliveryResult(event_id, "busy")
    ]
    assert fake_gateway.calls == []
    assert event_row(store, event_id)[:2] == (0, 0)


def test_an_event_finished_by_another_process_before_the_lease_is_skipped(
    store: Store, root: Path, fake_gateway: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, event_id = seed(store)
    take = store.acquire_lease

    def racing(name: str, **kw: Any) -> Any:
        store.mark_event(event_id, delivered=True)  # the other deliverer finishes first
        return take(name, **kw)

    monkeypatch.setattr(store, "acquire_lease", racing)
    results = deliver(store, root, post_run_config({"cmd": "n"}))
    assert results == [hooks.DeliveryResult(event_id, "skipped")]
    assert fake_gateway.calls == []


def test_the_event_lease_outlasts_the_hook_timeouts_and_is_released(
    store: Store, root: Path, fake_gateway: FakeGateway
) -> None:
    _, event_id = seed(store)
    seen: list[str] = []

    def peek(call: Call) -> CompletedRun:
        info = store.lease_info(hooks.event_lease_name(event_id))
        assert info is not None
        seen.append(info.expires_at)
        return CompletedRun(call.argv, 0, "", "")

    fake_gateway.script(SH, peek)
    deliver(store, root, post_run_config({"cmd": "slow", "timeout": "20m"}))
    need = format_time(dt.datetime.now(dt.UTC) + dt.timedelta(seconds=1200))
    assert seen[0] > need
    assert store.lease_info(hooks.event_lease_name(event_id)) is None


def test_a_bad_payload_is_abandoned_at_once_without_running_anything(
    store: Store, root: Path, fake_gateway: FakeGateway
) -> None:
    run_id, event_id = seed(store, payload={"state": "done", "pr_title": OUT_MARKER})
    assert deliver(store, root, post_run_config({"cmd": "n"}))[0].status == "abandoned"
    assert fake_gateway.calls == []
    run = store.get_run(run_id)
    assert run is not None and "hook_failed" in run.warnings
    assert event_row(store, event_id)[1] == 1
    assert OUT_MARKER not in (root / "log.jsonl").read_text()


def test_an_unknown_hook_name_in_the_outbox_is_logged_and_dropped(
    store: Store, root: Path, fake_gateway: FakeGateway
) -> None:
    run_id, event_id = seed(store)
    with store.transaction() as conn:
        conn.execute("UPDATE events SET hook = 'weird' WHERE event_id = ?", (event_id,))
    deliver(store, root, post_run_config({"cmd": "n"}))
    assert fake_gateway.calls == []
    assert any(r["event"] == "hook_skipped" for r in log_records(root))
    run = store.get_run(run_id)
    assert run is not None and "hook_failed" in run.warnings
    assert event_row(store, event_id)[1] == 1


def test_an_audit_failure_does_not_stop_the_hooks_and_is_raised_afterwards(
    store: Store, root: Path, fake_gateway: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_gateway.script(SH, ok())
    _, event_id = seed(store)

    def broken(self: AuditWriter, events: Any) -> int:
        raise OSError("disk full")

    monkeypatch.setattr(AuditWriter, "append_events", broken)
    with pytest.raises(OSError):
        deliver(store, root, post_run_config({"cmd": "n"}))
    assert events_of(fake_gateway) == [event_id]
    assert event_row(store, event_id) == (1, 1, 1)  # delivered, audit still pending


def test_a_payload_that_names_another_event_or_run_is_abandoned(
    store: Store, root: Path, fake_gateway: FakeGateway
) -> None:
    run_id, event_id = seed(store)
    other_run, _ = seed(store)
    base = store.pending_events(run_id)[0]["payload"]
    for changed in ({"event_id": f"{run_id}-post-run-2"}, {"object_id": other_run}):
        with store.transaction() as conn:
            conn.execute(
                "UPDATE events SET payload = ?, delivered = 0 WHERE event_id = ?",
                (json.dumps({**base, **changed}), event_id),
            )
        res = deliver(store, root, post_run_config({"cmd": "n"}), run_id=run_id)
        assert [r.status for r in res] == ["abandoned"]
    assert fake_gateway.calls == []
    run = store.get_run(run_id)
    assert run is not None and "hook_failed" in run.warnings


def test_an_oserror_from_one_entry_does_not_stop_the_pass(
    store: Store, root: Path, fake_gateway: FakeGateway
) -> None:
    fake_gateway.script(SH, OSError(28, "no space"), ok(), ok())
    _, first = seed(store)
    _, second = seed(store)
    res = deliver(store, root, post_run_config({"cmd": "n"}))
    assert sorted(r.status for r in res) == ["delivered", "retry"]
    assert {event_row(store, first)[0], event_row(store, second)[0]} == {1}


def test_an_invalid_stored_entry_is_a_failed_attempt_not_a_silent_skip(
    store: Store, root: Path, fake_gateway: FakeGateway
) -> None:
    _, event_id = seed(store, flag_hooks=("garbage-no-equals",))
    assert deliver(store, root, None)[0].status == "retry"
    assert fake_gateway.calls == []
    assert event_row(store, event_id)[1] == 0


def test_a_sync_entry_reruns_on_each_pass_and_async_starts_only_on_the_ending_pass(
    store: Store, root: Path, fake_gateway: FakeGateway
) -> None:
    fake_gateway.script(SH, ok(), ok(returncode=1), ok(), ok(returncode=1), ok(), ok(returncode=1),
                        ok(), ok(returncode=1), ok(), ok(returncode=1))  # fmt: skip
    _, event_id = seed(store)
    config = post_run_config({"cmd": "good"}, {"cmd": "bad"}, {"cmd": "bg", "async": True})
    statuses = [deliver(store, root, config)[0].status for _ in range(5)]
    assert statuses == ["retry"] * 4 + ["abandoned"]
    sync_runs = [c for c in fake_gateway.calls if not c.detached]
    async_runs = [c for c in fake_gateway.calls if c.detached]
    assert len(sync_runs) == 10  # the succeeding entry too, on every pass
    assert len(async_runs) == 1  # only on the pass that ended the event


# -- real processes ---------------------------------------------------------------------


def real(store: Store, root: Path, config: dict[str, Any], **kw: Any) -> list[hooks.DeliveryResult]:
    return hooks.deliver_pending(
        store,
        AuditWriter(root),
        LogWriter(root),
        config,
        base_env={"PATH": os.environ.get("PATH", "")},
        shell=(PY, "-c"),
        **kw,
    )


def test_a_hung_hook_loses_its_whole_process_group_at_the_timeout(
    store: Store, root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hooks, "TERM_GRACE_S", 0.3)
    pids = tmp_path / "pids"
    code = f"""
import os, signal, subprocess, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
open({str(pids)!r}, "w").write(f"{{os.getpid()}} {{g.pid}}")
time.sleep(60)
"""
    _, event_id = seed(store)
    started = time.monotonic()
    results = real(store, root, post_run_config({"cmd": code, "timeout": "1s"}))
    assert results == [hooks.DeliveryResult(event_id, "retry")]
    assert time.monotonic() - started < 15
    leader, member = (int(x) for x in pids.read_text().split())
    for pid in (leader, member):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and Path(f"/proc/{pid}/stat").exists():
            stat = Path(f"/proc/{pid}/stat").read_text()
            if stat[stat.rindex(")") + 2] == "Z":
                break
            time.sleep(0.02)
        assert not Path(f"/proc/{pid}/stat").exists() or (
            Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()[0] == "Z"
        )
    assert event_row(store, event_id)[:2] == (1, 0)  # still pending: redelivered later
    assert [r["code"] for r in log_records(root)] == ["timeout"]


def test_the_payload_reaches_a_real_hook_on_stdin_and_the_env_carries_only_ids(
    store: Store, root: Path, tmp_path: Path
) -> None:
    out = tmp_path / "got.json"
    code = (
        "import json, os, sys\n"
        f"open({str(out)!r}, 'w').write(json.dumps({{'stdin': json.load(sys.stdin),"
        " 'env': {k: v for k, v in os.environ.items() if k.startswith('REVIEW_RUN_')}}))\n"
    )
    run_id, event_id = seed(store)
    assert real(store, root, post_run_config({"cmd": code}))[0].status == "delivered"
    got = json.loads(out.read_text())
    assert got["stdin"]["event_id"] == event_id and got["stdin"]["object_id"] == run_id
    assert got["env"] == {
        "REVIEW_RUN_HOOK": "post-run",
        "REVIEW_RUN_ID": run_id,
        "REVIEW_RUN_OBJECT_TYPE": "run",
        "REVIEW_RUN_EVENT_ID": event_id,
    }


def test_two_concurrent_deliverers_run_the_hook_once(root: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    code = appender(out) + "; import time; time.sleep(1.5)"
    with Store.open(root) as s:
        _, event_id = seed(s)
    outcomes: list[list[hooks.DeliveryResult]] = []

    def worker() -> None:
        with Store.open(root) as mine:
            outcomes.append(real(mine, root, post_run_config({"cmd": code, "timeout": "30s"})))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert out.read_text().splitlines() == [event_id]
    flat = sorted(r.status for rs in outcomes for r in rs)
    assert flat.count("delivered") == 1


def test_a_sigkill_between_the_hook_and_the_mark_redelivers_the_same_event(
    store: Store, root: Path, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    _, event_id = seed(store)
    proc = subprocess.run(
        [sys.executable, str(WORKER), str(root), str(out), "crash"],
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
    )
    assert proc.returncode == -9, proc.stderr
    assert out.read_text().splitlines() == [event_id]
    assert event_row(store, event_id)[:2] == (1, 0)
    assert len(audit_lines(root)) == 2  # the audit step finished before the crash
    results = real(store, root, post_run_config({"cmd": appender(out)}))
    assert results == [hooks.DeliveryResult(event_id, "delivered")]
    assert out.read_text().splitlines() == [event_id, event_id]  # at least once, same id
    assert len(audit_lines(root)) == 2  # never appended twice
