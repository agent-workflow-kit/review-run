"""``reconcile`` and the recovery shared with ``show`` and ``wait`` (issue #29)."""

# ruff: noqa: E501

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
from typing import Any

import pytest

from review_run import procid
from review_run import recovery as recovery_mod
from review_run.reconcile import ReconcileOptions, reconcile
from review_run.store import RECONCILE_LEASE, Run, Store, default_state_root, lease_name
from tests.conftest import validate_against
from tests.helpers import Result, run_cli
from tests.run_helpers import PR_URL, World
from tests.test_cancel import dead_pid_identity, live_run_without_agent
from tests.test_meta import artifact_text
from tests.test_outcome import FakeClock, live, poke, started, stored


@pytest.fixture
def cli(world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("XDG_STATE_HOME", str(world.tmp / "xdg-state"))
    world.config()
    world.fb.use("agent-spawn", "outcome", outcome="done", artifact_text=artifact_text())
    clock = FakeClock()
    monkeypatch.setattr("review_run.cli._CLOCK", clock.clock())

    def call(*argv: str) -> Result:
        return run_cli(capsys, "--config", str(world.tmp / "config.yaml"), *argv)

    call.clock = clock  # type: ignore[attr-defined]
    return call


def rec(cli, *flags: str) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    res = cli("--json", "reconcile", *flags)
    assert res.code == 0, res.err
    doc = res.envelope()
    validate_against("cli/reconcile.json", doc)
    return doc


def kinds(doc: dict[str, Any]) -> list[tuple[str | None, str]]:
    return [(c["run_id"], c["kind"]) for c in doc["changes"]]


def audit_records() -> list[dict[str, Any]]:
    path = default_state_root() / "audit.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def calls(world: World, command: str) -> list[Any]:
    return world.fb.calls("agent-spawn", command)


def escalated(cli, world: World) -> str:  # type: ignore[no-untyped-def]
    world.fb.use("agent-spawn", "outcome", outcome="needs-input")
    res = cli("--json", "run", PR_URL)
    assert res.code == 13, res.out
    return str(res.envelope()["run_id"])


def old(days: int = 30) -> str:
    return (dt.datetime.now(dt.UTC) - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def sleeper() -> tuple[subprocess.Popen[bytes], procid.ProcIdentity]:
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    ident = procid.identity_of(proc.pid)
    assert ident is not None
    return proc, ident


# -- the command -----------------------------------------------------------------------------------


def test_an_empty_state_reports_nothing_and_exits_0(cli) -> None:  # type: ignore[no-untyped-def]
    doc = rec(cli)
    assert doc == {
        "schema_version": 1,
        "dry_run": False,
        "already_running": False,
        "changes": [],
        "warnings": [],
        "stuck_teardowns": 0,
        "stuck_audit": 0,
    }


def test_human_output_and_quiet(cli) -> None:  # type: ignore[no-untyped-def]
    assert "reconcile: changed 0" in cli("reconcile").out
    assert cli("--quiet", "reconcile").out == ""


def test_a_usage_error_exits_2(cli) -> None:  # type: ignore[no-untyped-def]
    assert cli("reconcile", "--bogus").code == 2


def test_reconcile_finalizes_a_run_whose_agent_ended_once(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    run_id = started(cli, world)
    doc = rec(cli)
    assert kinds(doc)[0] == (run_id, "finalized")
    assert doc["changes"][0]["details"]["state"] == "done"
    run = stored(run_id)
    assert (run.state, run.teardown) == ("done", "done")
    finals = [r for r in audit_records() if r["action"] == "finalize"]
    assert len(finals) == 1 and finals[0]["inputs"]["finalized_by"] == "reconcile"
    again = rec(cli)  # idempotent
    assert again["changes"] == []
    assert len([r for r in audit_records() if r["action"] == "finalize"]) == 1


def test_a_live_agent_is_left_alone(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    run_id = live(cli)
    assert rec(cli)["changes"] == []
    assert stored(run_id).state == "running"


def test_dry_run_reports_and_changes_nothing(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    run_id = started(cli, world)
    before = len(world.fb.calls("agent-spawn", "teardown"))
    doc = rec(cli, "--dry-run")
    assert doc["dry_run"] is True and kinds(doc) == [(run_id, "finalized")]
    assert stored(run_id).state == "running"
    assert len(world.fb.calls("agent-spawn", "teardown")) == before
    with Store.open(default_state_root()) as store:
        assert store.lease_info(RECONCILE_LEASE) is None
    assert kinds(rec(cli)) == [(run_id, "finalized")]  # the real pass does it


def test_dry_run_plans_runner_lost_and_agent_gone(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    lost = live_run_without_agent(world)
    doc = rec(cli, "--dry-run")
    assert (lost, "runner_lost") in kinds(doc)
    assert stored(lost).state == "running"


def test_a_second_concurrent_reconcile_reports_already_running(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    run_id = started(cli, world)
    proc, ident = sleeper()
    try:
        with Store.open(default_state_root()) as store:
            assert store.acquire_lease(RECONCILE_LEASE, holder=ident) is not None
        doc = rec(cli)
        assert doc["already_running"] is True and doc["changes"] == []
        assert stored(run_id).state == "running"
    finally:
        proc.kill()
        proc.wait()
    doc = rec(cli)  # the holder is gone: its lease is taken over
    assert doc["already_running"] is False and kinds(doc) == [(run_id, "finalized")]
    with Store.open(default_state_root()) as store:
        assert store.lease_info(RECONCILE_LEASE) is None  # released at the end


def test_a_run_whose_lease_is_held_by_a_live_process_is_skipped(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    run_id = started(cli, world)
    proc, ident = sleeper()
    try:
        with Store.open(default_state_root()) as store:
            assert store.acquire_lease(lease_name(run_id), holder=ident) is not None
        assert rec(cli)["changes"] == []
        assert stored(run_id).state == "running"
    finally:
        proc.kill()
        proc.wait()
    assert kinds(rec(cli)) == [(run_id, "finalized")]


# -- the runner check ------------------------------------------------------------------------------


def test_a_run_with_a_dead_runner_and_no_agent_ends_runner_lost(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    run_id = live_run_without_agent(world)
    doc = rec(cli)
    assert kinds(doc) == [(run_id, "runner_lost")]
    run = stored(run_id)
    assert (run.state, run.reason, run.exit_code, run.phase) == ("failed", "runner_lost", 12, None)
    assert rec(cli)["changes"] == []
    records = [r for r in audit_records() if r["action"] == "finalize"]
    assert records[0]["inputs"] == {"cause": "runner_lost", "finalized_by": "reconcile"}


def test_a_live_runner_is_never_ended(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    run_id = live_run_without_agent(world)
    proc, ident = sleeper()
    try:
        with Store.open(default_state_root()) as store:
            row = store.get_run(run_id)
            assert row is not None
            store.update_fields(run_id, row.key, runner_pid=ident.pid, runner_start=ident.start)
        assert rec(cli)["changes"] == []
        assert stored(run_id).state == "running"
    finally:
        proc.kill()
        proc.wait()


def test_an_unreadable_proc_means_unknown_and_nothing_is_ended(
    world: World, cli, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    run_id = live_run_without_agent(world)
    monkeypatch.setattr(procid, "_boot_id", lambda: None)
    assert rec(cli)["changes"] == []
    assert stored(run_id).state == "running"


def test_a_live_launcher_keeps_the_run(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    run_id = live_run_without_agent(world)
    proc, ident = sleeper()
    try:
        with Store.open(default_state_root()) as store:
            row = store.get_run(run_id)
            assert row is not None
            store.update_fields(
                run_id, row.key, launcher_pid=ident.pid, launcher_start=ident.start,
                launch_started_at=old(1),
            )  # fmt: skip
        assert rec(cli)["changes"] == []
    finally:
        proc.kill()
        proc.wait()


def test_a_launcher_within_its_window_keeps_the_run_then_the_window_ends(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    run_id = live_run_without_agent(world)
    now = dt.datetime.now(dt.UTC)
    with Store.open(default_state_root()) as store:
        row = store.get_run(run_id)
        assert row is not None
        store.advance_phase(run_id, row.key, "session", launcher_pid=dead_pid_identity()[0],
                            launch_started_at=(now - dt.timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%S.000Z"))  # fmt: skip
    assert rec(cli)["changes"] == []  # a PID with no start: unknown for 120 s
    later = ReconcileOptions(now=lambda: now + dt.timedelta(seconds=200))
    result = reconcile(default_state_root(), later)
    # the window is over, but a launch began and no launch.json is usable: label recovery is #30
    assert result.changes == [] and stored(run_id).state == "running"


def test_a_launch_that_began_with_no_trace_is_left_for_label_recovery(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    run_id = live_run_without_agent(world)
    with Store.open(default_state_root()) as store:
        row = store.get_run(run_id)
        assert row is not None
        store.advance_phase(run_id, row.key, "session", launch_started_at=old(1))
    assert rec(cli)["changes"] == []  # TODO(#30)
    assert stored(run_id).state == "running"


def test_a_launch_json_with_an_agent_is_adopted_and_finalized(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    run_id = started(cli, world)
    run = stored(run_id)
    agent = run.agent_id
    poke(run_id, agent_id=None, csession_id=None, awaited_run=None, launch_started_at=old(1))
    assert (default_state_root() / "runs" / run_id / "launch.json").exists()
    doc = rec(cli)
    assert kinds(doc) == [(run_id, "agent_adopted"), (run_id, "finalized")]
    assert doc["changes"][0]["details"] == {"agent_id": agent}
    assert stored(run_id).state == "done"


def test_a_launch_json_naming_a_look_alike_agent_ends_agent_gone_and_is_never_touched(
    world: World, cli
) -> None:  # type: ignore[no-untyped-def]
    run_id = started(cli, world)
    agent = stored(run_id).agent_id or ""
    poke(run_id, agent_id=None, csession_id=None, awaited_run=None, launch_started_at=old(1))
    state = world.fb.state()
    state["agents"][agent]["settings"]["labels"] = ["owner=someone-else"]
    (world.fb.dir / "state.json").write_text(json.dumps(state))
    doc = rec(cli)
    assert kinds(doc) == [(run_id, "agent_gone")]
    run = stored(run_id)
    assert (run.state, run.reason) == ("failed", "agent_gone")
    assert "agent_mismatch" in run.warnings and run.agent_id is None
    assert calls(world, "teardown") == [] and calls(world, "stop") == []


def test_a_launch_json_error_envelope_ends_the_run_as_run_would(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    run_id = live_run_without_agent(world)
    run_dir = default_state_root() / "runs" / run_id
    with Store.open(default_state_root()) as store:
        row = store.get_run(run_id)
        assert row is not None
        store.advance_phase(run_id, row.key, "session", launch_started_at=old(1))
    (run_dir / "launch.json").write_text(
        json.dumps(
            {
                "error": {
                    "code": "failed",
                    "message": "x",
                    "tool": "termctl",
                    "lower_code": "tmux_missing",
                }
            }
        )
    )
    doc = rec(cli)
    assert kinds(doc) == [(run_id, "finalized")]
    assert stored(run_id).reason == "launch_failed"


# -- agent gone, expiry ----------------------------------------------------------------------------


def test_a_torn_down_agent_ends_the_running_run_agent_gone(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    run_id = live(cli)
    agent = stored(run_id).agent_id or ""
    world.fb.run("agent-spawn", "stop", agent, "--json")
    world.fb.run("agent-spawn", "teardown", agent, "--json")
    doc = rec(cli)
    assert kinds(doc) == [(run_id, "agent_gone")]
    assert stored(run_id).reason == "agent_gone"


def test_an_escalated_run_older_than_the_ttl_expires_and_its_agent_is_torn_down(
    world: World, cli
) -> None:  # type: ignore[no-untyped-def]
    run_id = escalated(cli, world)
    agent = stored(run_id).agent_id or ""
    assert rec(cli)["changes"] == []  # younger than 7 days
    poke(run_id, escalated_at=old(8))
    doc = rec(cli)
    assert kinds(doc) == [(run_id, "expired")]
    run = stored(run_id)
    assert (run.state, run.reason, run.exit_code, run.teardown) == ("failed", "expired", 12, "done")
    assert world.fb.state()["agents"][agent]["status"] == "torn_down"
    posts = [r for r in audit_records() if r["action"] == "finalize"]
    assert posts[-1]["inputs"]["cause"] == "expired"
    assert rec(cli)["changes"] == []


def test_an_expired_looking_run_with_a_live_agent_is_skipped(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    run_id = escalated(cli, world)
    agent = stored(run_id).agent_id or ""
    poke(run_id, escalated_at=old(30))
    world.fb.use("agent-spawn", "resume_outcome", outcome="done", never_ends=True)
    resumed = world.fb.run("agent-spawn", "resume", agent, "--json")  # a human answers it
    assert resumed.returncode == 0, resumed.stderr
    assert rec(cli)["changes"] == []
    assert stored(run_id).state == "escalated"
    assert calls(world, "stop") == [] and calls(world, "teardown") == []


def test_an_escalated_run_with_a_torn_down_agent_ends_agent_gone(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    run_id = escalated(cli, world)
    world.fb.run("agent-spawn", "teardown", stored(run_id).agent_id or "", "--json")
    assert kinds(rec(cli)) == [(run_id, "agent_gone")]
    assert stored(run_id).reason == "agent_gone"


def test_an_escalated_run_whose_agent_finished_a_newer_run_is_finalized_not_expired(
    world: World, cli
) -> None:  # type: ignore[no-untyped-def]
    run_id = escalated(cli, world)
    agent = stored(run_id).agent_id or ""
    poke(run_id, escalated_at=old(30))
    world.fb.use("agent-spawn", "resume_outcome", outcome="done", artifact_text=artifact_text())
    resumed = world.fb.run("agent-spawn", "resume", agent, "--wait", "--json")
    assert resumed.returncode in (0, 13), resumed.stderr
    doc = rec(cli)
    assert kinds(doc)[0] == (run_id, "finalized")
    assert stored(run_id).reason != "expired"


def test_show_expires_an_escalated_run_but_no_refresh_does_not(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    run_id = escalated(cli, world)
    poke(run_id, escalated_at=old(30))
    assert cli("--json", "show", run_id, "--no-refresh").envelope()["state"] == "escalated"
    shown = cli("--json", "show", run_id).envelope()
    assert (shown["state"], shown["reason"]) == ("failed", "expired")


def test_show_without_a_loadable_config_does_not_expire(
    world: World, cli, capsys: pytest.CaptureFixture[str]
) -> None:  # type: ignore[no-untyped-def]
    run_id = escalated(cli, world)
    poke(run_id, escalated_at=old(30))
    res = run_cli(capsys, "--config", str(world.tmp / "missing.yaml"), "--json", "show", run_id)
    assert res.code == 0 and res.envelope()["state"] == "escalated"


# -- show and wait use the runner check ------------------------------------------------------------


def test_show_ends_a_run_with_a_dead_runner_and_wait_exits_12(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    run_id = live_run_without_agent(world)
    res = cli("--json", "wait", run_id, "--timeout", "30s")
    assert res.code == 12
    obj = res.envelope()
    assert (obj["state"], obj["reason"]) == ("failed", "runner_lost")
    assert cli.clock.sleeps == []  # type: ignore[attr-defined]


def test_wait_exits_12_tool_missing_when_agent_spawn_is_missing(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    run_id = live(cli)
    world.router.missing.add("agent-spawn")
    res = cli("--json", "wait", run_id)
    assert res.code == 12
    assert res.envelope()["error"]["reason"] == "tool_missing"
    assert stored(run_id).state == "running"
    shown = cli("show", run_id)
    assert shown.code == 0 and "not refreshed (failed)" in shown.err


# -- a recorded cancellation -----------------------------------------------------------------------


def request_cancel(run_id: str) -> None:
    with Store.open(default_state_root()) as store:
        row = store.get_run(run_id)
        assert row is not None
        assert store.request_cancel(run_id, row.key, actor="human:alice", exit_code=143)


def test_a_recorded_cancellation_ends_canceled_once_the_agent_is_parked(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    run_id = started(cli, world)
    request_cancel(run_id)
    doc = rec(cli)
    assert kinds(doc) == [(run_id, "canceled")]
    run = stored(run_id)
    assert (run.state, run.reason, run.exit_code, run.teardown) == (
        "failed",
        "canceled",
        143,
        "done",
    )
    assert run.artifact_path is None


def test_a_recorded_cancellation_never_ends_a_run_with_a_live_agent(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    run_id = live(cli)
    request_cancel(run_id)
    assert rec(cli)["changes"] == []
    assert stored(run_id).state == "running"
    assert calls(world, "stop") == []  # reconcile never stops an agent


def test_a_recorded_cancellation_on_an_escalated_run_ends_canceled(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    run_id = escalated(cli, world)
    request_cancel(run_id)
    assert kinds(rec(cli)) == [(run_id, "canceled")]
    assert stored(run_id).teardown == "done"


def test_a_recorded_cancellation_with_no_agent_ends_canceled(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    run_id = live_run_without_agent(world)
    request_cancel(run_id)
    assert kinds(rec(cli)) == [(run_id, "canceled")]


# -- a resume that died ----------------------------------------------------------------------------


def moved_by_a_dying_resume(run_id: str) -> Run:
    with Store.open(default_state_root()) as store:
        row = store.get_run(run_id)
        assert row is not None and row.awaited_run is not None
        lease = store.acquire_lease(lease_name(run_id))
        assert lease is not None
        assert store.begin_resume(run_id, row.key, lease=lease, awaited_run=row.awaited_run + 1)
        store.release_lease(lease)
    return stored(run_id)


def test_a_dying_resume_before_the_launcher_is_reverted_exactly(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    run_id = escalated(cli, world)
    before = stored(run_id)
    moved = moved_by_a_dying_resume(run_id)
    poke(run_id, launch_started_at=old(1))
    assert moved.state == "running" and moved.escalation_at is not None
    (default_state_root() / "runs" / run_id / "prompt.md").write_text("the raw answer")
    doc = rec(cli)
    assert kinds(doc) == [(run_id, "resume_reverted")]
    after = stored(run_id)
    assert (after.state, after.reason, after.exit_code, after.escalated_at, after.awaited_run) == (
        before.state, before.reason, before.exit_code, before.escalated_at, before.awaited_run
    )  # fmt: skip
    assert after.escalation_at is None
    assert not (default_state_root() / "runs" / run_id / "prompt.md").exists()
    assert rec(cli)["changes"] == []


def test_a_dying_resume_within_the_launch_window_is_left_alone(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    run_id = escalated(cli, world)
    moved_by_a_dying_resume(run_id)
    recent = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    poke(run_id, launch_started_at=recent)
    assert rec(cli)["changes"] == []
    assert stored(run_id).state == "running"


def test_a_dying_resume_whose_agent_did_start_the_run_is_adopted_and_finalized(
    world: World, cli
) -> None:  # type: ignore[no-untyped-def]
    run_id = escalated(cli, world)
    agent = stored(run_id).agent_id or ""
    moved_by_a_dying_resume(run_id)
    poke(run_id, launch_started_at=old(1))
    world.fb.use("agent-spawn", "resume_outcome", outcome="done", artifact_text=artifact_text())
    resumed = world.fb.run("agent-spawn", "resume", agent, "--wait", "--json")
    assert resumed.returncode in (0, 13), resumed.stderr
    doc = rec(cli)
    assert kinds(doc)[0] == (run_id, "agent_adopted")
    run = stored(run_id)
    assert run.state != "running" and run.escalation_at is None


# -- teardown retry and the stuck counters ----------------------------------------------------------


def break_teardown(world: World) -> None:
    world.fb.use("agent-spawn", "teardown_failed")


def fix_teardown(world: World) -> None:
    world.fb.scenario["agent-spawn"].pop("teardown", None)
    world.fb._write()  # noqa: SLF001


def test_a_pending_teardown_is_retried_and_counted_when_it_keeps_failing(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    break_teardown(world)
    run_id = started(cli, world)
    assert cli("--json", "show", run_id).envelope()["teardown"] == "failed"
    agent = stored(run_id).agent_id or ""
    for n in range(1, 5):
        doc = rec(cli)
        assert kinds(doc) == [(run_id, "teardown_retried")] and doc["stuck_teardowns"] == 0, n
    doc = rec(cli)  # the 5th failed pass
    assert doc["stuck_teardowns"] == 1 and doc["warnings"] == ["teardown_stuck"]
    fix_teardown(world)
    doc = rec(cli)
    assert doc["changes"][0]["details"] == {"teardown": "done"}
    assert doc["stuck_teardowns"] == 0 and doc["warnings"] == []
    assert world.fb.state()["agents"][agent]["status"] == "torn_down"
    assert rec(cli)["changes"] == []


def test_a_look_alike_agent_is_never_torn_down_by_a_retry(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    run_id = started(cli, world)
    cli("--json", "show", run_id)
    agent = stored(run_id).agent_id or ""
    poke(run_id, teardown="pending")
    state = world.fb.state()
    state["agents"][agent]["status"] = "parked"
    state["agents"][agent]["torn_down_at"] = None
    state["agents"][agent]["settings"]["labels"] = ["owner=someone-else"]
    (world.fb.dir / "state.json").write_text(json.dumps(state))
    n = len(calls(world, "teardown"))
    doc = rec(cli)
    assert kinds(doc) == [(run_id, "teardown_retried")]
    assert "agent_mismatch" in stored(run_id).warnings and stored(run_id).teardown == "failed"
    assert len(calls(world, "teardown")) == n
    assert rec(cli)["changes"] == []  # and never retried again


# -- the outbox ------------------------------------------------------------------------------------


def test_deferred_hook_events_are_delivered_by_reconcile(
    world: World, cli, capsys: pytest.CaptureFixture[str]
) -> None:  # type: ignore[no-untyped-def]
    out = world.tmp / "hook.jsonl"
    code = f"import sys;open({str(out)!r},'a').write(sys.stdin.read()+'\\n')"
    world.config_text += f"hooks:\n  post-run:\n    - cmd: {json.dumps(code)}\n"
    world.config()
    run_id = started(cli, world)
    bad = world.tmp / "missing.yaml"  # the finalizer cannot load its config: delivery is deferred
    assert run_cli(capsys, "--config", str(bad), "show", run_id).code == 0
    assert stored(run_id).state == "done" and not out.exists()
    doc = rec(cli, "--dry-run")
    assert (run_id, "hook_redelivered") in kinds(doc)
    from review_run import reconcile as rc

    result = rc.reconcile(
        default_state_root(),
        ReconcileOptions(config_path=world.tmp / "config.yaml", shell=(sys.executable, "-c")),
    )
    assert "hook_redelivered" in [c.kind for c in result.changes]
    assert len(out.read_text().splitlines()) == 1
    assert rec(cli)["changes"] == []
    assert len(out.read_text().splitlines()) == 1  # at least once, here exactly once


def test_a_config_that_does_not_load_defers_delivery_but_completes_audit(
    world: World, cli, capsys: pytest.CaptureFixture[str]
) -> None:  # type: ignore[no-untyped-def]
    run_id = started(cli, world)
    bad = world.tmp / "missing.yaml"
    run_cli(capsys, "--config", str(bad), "show", run_id)
    with Store.open(default_state_root()) as store:
        assert [e["delivered"] for e in store.pending_events(run_id)] == [False]
    res = run_cli(capsys, "--config", str(bad), "--json", "reconcile")
    assert res.code == 0
    with Store.open(default_state_root()) as store:
        events = store.pending_events(run_id)
    assert [(e["delivered"], e["audit_pending"]) for e in events] == [(False, False)]


def test_audit_that_cannot_be_written_is_counted_stuck_after_five_passes(
    world: World, cli, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    run_id = started(cli, world)
    assert cli("show", run_id).code == 0  # finalized, audit and hooks done
    import review_run.reconcile as rc

    def boom(*_a: Any, **_k: Any) -> Any:
        raise OSError("audit.jsonl is not writable")

    # the finalize pass itself must succeed: make only the reconcile outbox pass fail
    monkeypatch.setattr(rc, "flush_outbox", boom)
    monkeypatch.setattr(rc.hooks_mod, "deliver_pending", lambda *a, **k: [])
    with Store.open(default_state_root()) as store:
        store._conn.execute("UPDATE events SET audit_pending = 1 WHERE run_id = ?", (run_id,))  # noqa: SLF001
    docs = [rec(cli) for _ in range(5)]
    assert [d["stuck_audit"] for d in docs] == [0, 0, 0, 0, 1]
    assert docs[-1]["warnings"] == ["audit_stuck"]
    monkeypatch.undo()
    final = rec(cli)
    assert final["stuck_audit"] == 0 and final["warnings"] == []


# -- stale files -----------------------------------------------------------------------------------


def test_stale_prompt_files_of_a_terminal_run_are_removed_by_exact_name(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    run_id = started(cli, world)
    rec(cli)
    run_dir = default_state_root() / "runs" / run_id
    (run_dir / "prompt.md").write_text("x")
    (run_dir / "inputs").mkdir()
    (run_dir / "inputs" / "f0001.patch").write_text("x")
    (run_dir / "notes.txt").write_text("keep")
    doc = rec(cli)
    assert doc["changes"] == [
        {"run_id": run_id, "kind": "cleaned", "details": {"what": "prompt_files"}}
    ]
    assert not (run_dir / "prompt.md").exists() and not (run_dir / "inputs").exists()
    assert (run_dir / "notes.txt").exists() and (run_dir / "settings.json").exists()


def test_prompt_files_of_a_run_with_a_live_runner_or_lease_are_kept(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    run_id = live(cli)
    run_dir = default_state_root() / "runs" / run_id
    (run_dir / "prompt.md").write_text("x")
    proc, ident = sleeper()
    try:
        with Store.open(default_state_root()) as store:
            row = store.get_run(run_id)
            assert row is not None
            store.update_fields(run_id, row.key, runner_pid=ident.pid, runner_start=ident.start)
        assert rec(cli)["changes"] == []
        assert (run_dir / "prompt.md").exists()
    finally:
        proc.kill()
        proc.wait()
    # a lease holder (a resume in flight) keeps its prompt even when the runner is gone
    holder, hident = sleeper()
    try:
        with Store.open(default_state_root()) as store:
            assert store.acquire_lease(lease_name(run_id), holder=hident) is not None
        assert rec(cli)["changes"] == []
        assert (run_dir / "prompt.md").exists()
    finally:
        holder.kill()
        holder.wait()
    assert kinds(rec(cli)) == [(run_id, "cleaned")]


def test_leftover_dry_run_scratch_directories_are_removed_when_old(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    tmp = default_state_root() / "tmp"
    tmp.mkdir(parents=True, mode=0o700, exist_ok=True)
    stale = tmp / ("dry-" + "a" * 16)
    fresh = tmp / ("dry-" + "b" * 16)
    other = tmp / "dry-notahexname"
    for d in (stale, fresh, other):
        d.mkdir(mode=0o700)
        (d / "x").write_text("x")
    os.utime(stale, (1, 1))
    os.utime(other, (1, 1))
    doc = rec(cli)
    assert doc["changes"] == [
        {"run_id": None, "kind": "cleaned", "details": {"what": "dry_run_scratch"}}
    ]
    assert not stale.exists() and fresh.exists() and other.exists()


# -- isolation -------------------------------------------------------------------------------------


def test_one_corrupt_row_does_not_stop_the_others(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    bad = started(cli, world)
    good = started(cli, world)
    poke(bad, pr="not a pr")
    doc = rec(cli)
    assert (good, "finalized") in kinds(doc)
    assert stored(bad).state == "running"
    log = (default_state_root() / "log.jsonl").read_text()
    assert bad in log and "not a pr" not in log and "not a pr" not in json.dumps(doc)


def test_a_run_with_a_missing_settings_file_is_skipped_not_fatal(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    broken = live_run_without_agent(world)
    (default_state_root() / "runs" / broken / "settings.json").unlink()
    good = started(cli, world)
    doc = rec(cli)
    assert (good, "finalized") in kinds(doc)
    assert stored(broken).state == "running"


def test_the_output_has_no_untrusted_text(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    run_id = started(cli, world)
    poke(run_id, failure='{"x": "SECRET-TEXT"}', summary='{"title": "SECRET-TEXT"}')
    text = cli("--json", "reconcile").out
    assert "SECRET-TEXT" not in text


def test_the_runner_check_helper_is_importable_for_show_and_wait() -> None:
    assert callable(recovery_mod.recover_run) and callable(recovery_mod.retry_teardown)


def test_concurrent_reconciles_and_recoveries_change_a_run_exactly_once(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    import threading

    runs = [started(cli, world) for _ in range(2)]
    errors: list[BaseException] = []
    barrier = threading.Barrier(4)

    def full() -> None:
        try:
            barrier.wait()
            reconcile(default_state_root(), ReconcileOptions())
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    def single() -> None:
        try:
            barrier.wait()
            recovery_mod.recover_run(
                default_state_root(), runs[0], recovery_mod.RecoverOptions(by="wait")
            )
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=f) for f in (full, full, single, single)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert errors == []
    rec(cli)  # whatever a skipped lease left is finished by one more pass
    assert [stored(r).state for r in runs] == ["done", "done"]
    finals = [r["target"]["run_id"] for r in audit_records() if r["action"] == "finalize"]
    assert sorted(finals) == sorted(runs)  # one finalize record per run
    with Store.open(default_state_root()) as store:
        assert [e["run_id"] for e in store.pending_events()] == []
