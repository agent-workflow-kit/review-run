"""Review round of ``reconcile`` (issue #29): the deviations and gaps found in review, one test each."""

# ruff: noqa: E501, F401, F811

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import shutil
import stat
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from review_run import liveness, procid
from review_run import reconcile as rc
from review_run import recovery as recovery_mod
from review_run.finalize import AgentFinalize
from review_run.reconcile import ReconcileOptions, reconcile
from review_run.store import Store, default_state_root, lease_name
from tests.helpers import run_cli
from tests.run_helpers import World
from tests.test_cancel import live_run_without_agent
from tests.test_meta import artifact_text
from tests.test_outcome import live, poke, started, stored
from tests.test_reconcile import (
    audit_records,
    calls,
    cli,
    escalated,
    kinds,
    moved_by_a_dying_resume,
    old,
    rec,
    request_cancel,
    sleeper,
)

ROOT = Path(__file__).resolve().parent.parent


def log_records() -> list[dict[str, Any]]:
    path = default_state_root() / "log.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def limit_records() -> list[dict[str, Any]]:
    return [
        r for r in log_records() if r.get("event") == "reconcile" and r.get("code") == "scan_limit"
    ]


# -- R1: only null disables escalation_ttl ---------------------------------------------------------


def test_a_null_ttl_disables_expiry_and_a_zero_ttl_expires_at_the_first_pass(
    world: World, cli
) -> None:  # type: ignore[no-untyped-def]
    run_id = escalated(cli, world)
    poke(run_id, escalated_at=old(400))
    world.config_text = "skill: review-run:review-pr\nescalation_ttl: null\n"
    world.config()
    assert rec(cli)["changes"] == []
    assert stored(run_id).state == "escalated"
    world.config_text = 'skill: review-run:review-pr\nescalation_ttl: "0"\n'
    world.config()
    assert kinds(rec(cli)) == [(run_id, "expired")]
    assert stored(run_id).reason == "expired"


def test_a_zero_ttl_boundary_stays_strict(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    run_id = escalated(cli, world)
    world.config_text = 'skill: review-run:review-pr\nescalation_ttl: "0"\n'
    world.config()
    now = dt.datetime.now(dt.UTC)
    at = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    poke(run_id, escalated_at=at)
    config = world.tmp / "config.yaml"
    same = reconcile(
        default_state_root(),
        ReconcileOptions(config_path=config, now=lambda: dt.datetime.fromisoformat(at)),
    )
    assert same.changes == []  # age exactly 0 is not above the TTL
    later = reconcile(
        default_state_root(),
        ReconcileOptions(
            config_path=config, now=lambda: dt.datetime.fromisoformat(at) + dt.timedelta(seconds=1)
        ),
    )
    assert [c.kind for c in later.changes] == ["expired"]


# -- R2: a change is attributed only to the call that made it --------------------------------------


def test_a_finalize_by_another_actor_just_before_is_not_attributed_to_reconcile(
    world: World, cli, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    run_id = started(cli, world)
    real = recovery_mod.finalize_agent

    def racing(root: Path, rid: str, opts: AgentFinalize) -> Any:
        real(root, rid, dataclasses.replace(opts, finalized_by="wait"))  # another actor wins
        return real(root, rid, opts)

    monkeypatch.setattr(recovery_mod, "finalize_agent", racing)
    doc = rec(cli)
    assert doc["changes"] == []
    assert stored(run_id).state == "done"
    finals = [r for r in audit_records() if r["action"] == "finalize"]
    assert [r["inputs"]["finalized_by"] for r in finals] == ["wait"]


def test_an_escalated_finalize_by_another_actor_is_not_attributed_either(
    world: World, cli, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    run_id = escalated(cli, world)
    agent = stored(run_id).agent_id or ""
    world.fb.use("agent-spawn", "resume_outcome", outcome="done", artifact_text=artifact_text())
    resumed = world.fb.run("agent-spawn", "resume", agent, "--wait", "--json")
    assert resumed.returncode in (0, 13), resumed.stderr
    real = recovery_mod.finalize_agent

    def racing(root: Path, rid: str, opts: AgentFinalize) -> Any:
        real(root, rid, dataclasses.replace(opts, finalized_by="wait"))
        return real(root, rid, opts)

    monkeypatch.setattr(recovery_mod, "finalize_agent", racing)
    assert rec(cli)["changes"] == []
    assert stored(run_id).state != "escalated"


# -- R3: scratch directories are reported only when gone --------------------------------------------


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory modes")
def test_an_undeletable_scratch_directory_is_not_reported_cleaned(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    tmp = default_state_root() / "tmp"
    tmp.mkdir(parents=True, mode=0o700, exist_ok=True)
    stale = tmp / ("dry-" + "c" * 16)
    stale.mkdir(mode=0o700)
    locked = stale / "locked"
    locked.mkdir(mode=0o700)
    (locked / "x").write_text("x")
    os.chmod(locked, 0o500)
    os.utime(stale, (1, 1))
    try:
        assert rec(cli)["changes"] == []
        assert rec(cli)["changes"] == []  # no false change at every pass
        assert stale.exists()
    finally:
        os.chmod(locked, 0o700)
        shutil.rmtree(stale, ignore_errors=True)


# -- R4: an implausible stored identity is unknown, never gone ---------------------------------------


def bare(**fields: Any) -> Any:
    from review_run.store import NewRun

    run = stored(fields.pop("run_id"))
    return dataclasses.replace(run, **fields)


@pytest.mark.parametrize("pid", ["garbage", b"\x00\x01", 2**40, 1.5, 0, -7])
def test_an_implausible_stored_pid_is_unknown(world: World, cli, pid: object) -> None:  # type: ignore[no-untyped-def]
    run = dataclasses.replace(
        stored(live_run_without_agent(world)),
        runner_pid=pid,
        runner_start="b:1",
        launcher_pid=pid,
        launcher_start="b:1",
    )
    assert liveness.runner_state(run) == "unknown"
    assert liveness.launcher_state(run) == "unknown"


@pytest.mark.parametrize("start", [b"\xff", "a\x00b", "x" * 500, "nocolon", "b:notdigits", ""])
def test_an_implausible_stored_start_is_unknown(world: World, cli, start: object) -> None:  # type: ignore[no-untyped-def]
    run = dataclasses.replace(
        stored(live_run_without_agent(world)),
        runner_pid=4242,
        runner_start=start,
        launcher_pid=4242,
        launcher_start=start,
    )
    assert liveness.runner_state(run) == "unknown"
    assert liveness.launcher_state(run) == "unknown"


def test_null_identity_fields_are_gone(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    run = dataclasses.replace(
        stored(live_run_without_agent(world)),
        runner_pid=None,
        runner_start=None,
        launcher_pid=None,
        launcher_start="b:1",
    )
    assert liveness.runner_state(run) == "gone"
    assert liveness.launcher_state(run) == "gone"


def test_a_garbage_runner_pid_with_a_live_process_is_never_ended_runner_lost(
    world: World, cli
) -> None:  # type: ignore[no-untyped-def]
    run_id = live_run_without_agent(world)
    proc, ident = sleeper()
    try:
        poke(run_id, runner_pid="garbage", runner_start=ident.start)
        assert rec(cli)["changes"] == []
        assert stored(run_id).state == "running"
        poke(
            run_id,
            runner_pid=ident.pid,
            runner_start=b"\xff\xfe",
            launcher_pid="garbage",
            launcher_start=ident.start,
        )
        assert rec(cli)["changes"] == []
        assert stored(run_id).state == "running"
    finally:
        proc.kill()
        proc.wait()


# -- R5: the launcher window ---------------------------------------------------------------------------


def window_run(world: World, started_at: object) -> Any:
    run_id = live_run_without_agent(world)
    poke(run_id, launcher_pid=None, launcher_start=None, launch_started_at=started_at)
    return stored(run_id)


def test_a_launch_time_in_the_future_counts_as_inside_the_window(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    now = dt.datetime.now(dt.UTC)
    future = (now + dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    run = window_run(world, future)
    assert liveness.launcher_in_window(run, now, launch_file_usable=False) is True


@pytest.mark.parametrize("naive_age", [5, 4000])
def test_a_naive_launch_time_never_raises(world: World, cli, naive_age: int) -> None:  # type: ignore[no-untyped-def]
    now = dt.datetime.now(dt.UTC)
    naive = (now - dt.timedelta(seconds=naive_age)).replace(tzinfo=None).isoformat()
    run = window_run(world, naive)
    assert liveness.launcher_in_window(run, now, launch_file_usable=False) is (naive_age < 120)


def test_an_unparseable_launch_time_is_inside_the_window(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    run = window_run(world, "not a time")
    assert (
        liveness.launcher_in_window(run, dt.datetime.now(dt.UTC), launch_file_usable=False) is True
    )
    assert (
        liveness.launcher_in_window(run, dt.datetime.now(dt.UTC), launch_file_usable=True) is False
    )


# -- R6: scan limits -----------------------------------------------------------------------------------


def test_a_look_alike_teardown_does_not_starve_a_newer_run(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    older = started(cli, world)
    newer = started(cli, world)
    cli("--json", "show", older)
    cli("--json", "show", newer)
    poke(older, teardown="pending", warnings='["agent_mismatch"]')
    poke(newer, teardown="pending")
    with Store.open(default_state_root()) as store:
        assert store.teardown_run_ids(1) == [newer]


def test_a_scan_that_hits_its_limit_writes_one_closed_log_record(
    world: World, cli, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    live(cli)
    live(cli)
    monkeypatch.setattr(rc, "MAX_RUNS", 2)
    rec(cli)
    assert limit_records() == []  # exactly at the limit is not over it
    monkeypatch.setattr(rc, "MAX_RUNS", 1)
    rec(cli)
    found = limit_records()
    assert found and all(r["level"] == "warn" for r in found)
    assert "live_runs" in found[0]["message"] and found[0]["count"] == 1


def test_the_prompt_sweep_reaches_old_runs_beyond_the_newest_and_logs_the_limit(
    world: World, cli, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    ids = [started(cli, world) for _ in range(3)]
    rec(cli)  # all finalized
    oldest = ids[0]
    run_dir = default_state_root() / "runs" / oldest
    (run_dir / "prompt.md").write_text("x")
    monkeypatch.setattr(rc, "MAX_SWEEP", 1)
    doc = rec(cli)
    assert doc["changes"] == [
        {"run_id": oldest, "kind": "cleaned", "details": {"what": "prompt_files"}}
    ]
    assert not (run_dir / "prompt.md").exists()
    # two leftovers and room for one: oldest first, then the next pass takes the other, with a record
    second = ids[2]
    for r in (ids[0], second):
        (default_state_root() / "runs" / r / "prompt.md").write_text("x")
    first = rec(cli)
    assert [c["run_id"] for c in first["changes"]] == [ids[0]]
    assert any("prompt_sweep" in r["message"] for r in limit_records())
    again = rec(cli)
    assert [c["run_id"] for c in again["changes"]] == [second]


# -- R7: the dry run mirrors the real pass ---------------------------------------------------------------


def pending_audit_and_hook(world: World, cli, capsys: pytest.CaptureFixture[str]) -> str:  # type: ignore[no-untyped-def]
    run_id = started(cli, world)
    bad = world.tmp / "missing.yaml"
    run_cli(capsys, "--config", str(bad), "show", run_id)  # the hooks stay undelivered
    with Store.open(default_state_root()) as store:
        store._conn.execute("UPDATE events SET audit_pending = 1 WHERE run_id = ?", (run_id,))  # noqa: SLF001
    return run_id


def test_a_dry_run_plans_both_the_audit_flush_and_the_redelivery(
    world: World, cli, capsys: pytest.CaptureFixture[str]
) -> None:  # type: ignore[no-untyped-def]
    run_id = pending_audit_and_hook(world, cli, capsys)
    assert kinds(rec(cli, "--dry-run")) == [(run_id, "audit_flushed"), (run_id, "hook_redelivered")]


def test_a_dry_run_plans_no_redelivery_when_the_config_does_not_load(
    world: World, cli, capsys: pytest.CaptureFixture[str]
) -> None:  # type: ignore[no-untyped-def]
    run_id = pending_audit_and_hook(world, cli, capsys)
    result = reconcile(
        default_state_root(), ReconcileOptions(config_path=world.tmp / "missing.yaml", dry_run=True)
    )
    assert [(c.run_id, c.kind) for c in result.changes] == [(run_id, "audit_flushed")]


@pytest.mark.parametrize("damage", ["settings", "run_dir"])
def test_a_dry_run_skips_a_run_the_real_pass_would_skip(world: World, cli, damage: str) -> None:  # type: ignore[no-untyped-def]
    broken = live_run_without_agent(world)
    run_dir = default_state_root() / "runs" / broken
    if damage == "settings":
        (run_dir / "settings.json").unlink()
    else:
        shutil.rmtree(run_dir)
    assert rec(cli, "--dry-run")["changes"] == []
    assert rec(cli)["changes"] == []
    assert stored(broken).state == "running"


def test_a_dry_run_skips_a_recorded_cancellation_it_cannot_end(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    broken = live_run_without_agent(world)
    request_cancel(broken)
    (default_state_root() / "runs" / broken / "settings.json").unlink()
    assert rec(cli, "--dry-run")["changes"] == []


# -- R8: a dry run on a missing state directory creates nothing ---------------------------------------------


def test_a_dry_run_on_a_missing_state_dir_creates_nothing(
    world: World, cli, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    fresh = world.tmp / "fresh-state"
    monkeypatch.setenv("XDG_STATE_HOME", str(fresh))
    doc = rec(cli, "--dry-run")
    assert doc["dry_run"] is True and doc["changes"] == [] and doc["already_running"] is False
    assert not fresh.exists()


# -- R9: a lost reconcile lease stops the pass ----------------------------------------------------------------


def test_a_lost_reconcile_lease_stops_the_pass(
    world: World, cli, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    run_id = started(cli, world)

    class LostKeeper:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            self.lost = threading.Event()
            self.lost.set()

        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

    monkeypatch.setattr(rc, "LeaseKeeper", LostKeeper)
    result = reconcile(default_state_root(), ReconcileOptions())
    assert result.changes == []
    assert stored(run_id).state == "running"
    assert any(r.get("code") == "lease_lost" for r in log_records())


# -- R10: a cancellation recorded between the read and the lease --------------------------------------------------


def test_a_cancellation_recorded_after_the_first_read_ends_canceled_not_runner_lost(
    world: World, cli, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    run_id = live_run_without_agent(world)
    real = recovery_mod._Recovery.row  # noqa: SLF001
    done: list[bool] = []

    def row(self: Any, rid: str) -> Any:
        if not done:
            done.append(True)
            request_cancel(run_id)
        return real(self, rid)

    monkeypatch.setattr(recovery_mod._Recovery, "row", row)  # noqa: SLF001
    assert kinds(rec(cli)) == [(run_id, "canceled")]
    run = stored(run_id)
    assert (run.reason, run.exit_code) == ("canceled", 143)


# -- R12: stored text never reaches the output -----------------------------------------------------------------------


def test_stored_text_in_details_is_replaced_by_unknown(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    run = dataclasses.replace(
        stored(live_run_without_agent(world)), state="failed", reason="SECRET-reason"
    )
    assert recovery_mod._ended(run).details == {"state": "failed", "reason": "unknown"}  # noqa: SLF001


def test_an_escalation_reason_in_a_reverted_resume_is_closed(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    run_id = escalated(cli, world)
    moved_by_a_dying_resume(run_id)
    poke(run_id, launch_started_at=old(1), escalation_reason="SECRET-reason")
    doc = rec(cli)
    assert kinds(doc) == [(run_id, "resume_reverted")]
    assert doc["changes"][0]["details"]["reason"] == "unknown"
    assert "SECRET" not in json.dumps(doc)


def test_a_stored_hook_name_in_the_outbox_is_closed(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    run_id = started(cli, world)
    cli("--json", "show", run_id)
    with Store.open(default_state_root()) as store:
        store._conn.execute(
            "UPDATE events SET hook = ?, audit_pending = 1 WHERE run_id = ?",
            ("SECRET-hook", run_id),
        )  # noqa: SLF001
    doc = rec(cli, "--dry-run")
    assert doc["changes"] and all(c["details"]["hook"] == "unknown" for c in doc["changes"])
    assert "SECRET" not in json.dumps(doc)


# -- R14: a run in phase finalize with no agent and a dead runner -----------------------------------------------------------


def test_a_finalize_phase_run_with_no_agent_and_a_dead_runner_ends_runner_lost(
    world: World, cli
) -> None:  # type: ignore[no-untyped-def]
    run_id = live_run_without_agent(world)
    poke(run_id, phase="finalize")
    assert kinds(rec(cli)) == [(run_id, "runner_lost")]
    assert stored(run_id).reason == "runner_lost"


def test_a_finalize_phase_run_with_no_agent_and_a_recorded_cancellation_ends_canceled(
    world: World, cli
) -> None:  # type: ignore[no-untyped-def]
    run_id = live_run_without_agent(world)
    request_cancel(run_id)  # a cancellation cannot be recorded once finalize began
    poke(run_id, phase="finalize")
    assert kinds(rec(cli)) == [(run_id, "canceled")]


# -- R15: acceptance paths --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("phase", ["prefetch", "code_run", "session"])
def test_a_runner_killed_in_any_phase_ends_runner_lost_at_the_next_reconcile(
    world: World, cli, phase: str
) -> None:  # type: ignore[no-untyped-def]
    run_id = live_run_without_agent(world)
    poke(run_id, phase=phase)
    assert kinds(rec(cli)) == [(run_id, "runner_lost")]
    run = stored(run_id)
    assert (run.state, run.reason, run.exit_code) == ("failed", "runner_lost", 12)


def test_wait_on_a_run_killed_in_prefetch_exits_12_runner_lost(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    run_id = live_run_without_agent(world)
    assert stored(run_id).phase == "prefetch"
    res = cli("--json", "wait", run_id, "--timeout", "30s")
    assert res.code == 12
    assert res.envelope()["reason"] == "runner_lost"


def test_expiry_with_a_refused_teardown_is_retried_by_the_next_pass(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    run_id = escalated(cli, world)
    poke(run_id, escalated_at=old(30))
    world.fb.script(
        "agent-spawn", "teardown",
        {"error": {"code": "refused", "message": "x", "reason": "live"}, "exit": 20},
    )  # fmt: skip
    first = rec(cli)  # the expiry, then the same pass retries the refused teardown (still refused)
    assert kinds(first) == [(run_id, "expired"), (run_id, "teardown_retried")]
    assert first["changes"][1]["details"] == {"teardown": "failed"}
    run = stored(run_id)
    assert (run.state, run.teardown) == ("failed", "failed") and "teardown_refused" in run.warnings
    world.fb.scenario["agent-spawn"].pop("teardown", None)
    world.fb._write()  # noqa: SLF001
    doc = rec(cli)
    assert kinds(doc) == [(run_id, "teardown_retried")]
    assert doc["changes"][0]["details"] == {"teardown": "done"}
    assert stored(run_id).state == "failed"


def test_an_external_teardown_of_an_escalated_agent_makes_show_report_agent_gone(
    world: World, cli
) -> None:  # type: ignore[no-untyped-def]
    run_id = escalated(cli, world)
    world.fb.run("agent-spawn", "teardown", stored(run_id).agent_id or "", "--json")
    shown = cli("--json", "show", run_id).envelope()
    assert (shown["state"], shown["reason"]) == ("failed", "agent_gone")


def sub_env(world: World) -> dict[str, str]:
    private = world.tmp / "sub-home"
    private.mkdir(exist_ok=True)
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(private),
        "XDG_CONFIG_HOME": str(private / "config"),
        "XDG_STATE_HOME": str(world.tmp / "xdg-state"),
        "PYTHONPATH": str(ROOT / "src"),
    }


def spawn_cli(world: World, *argv: str) -> subprocess.Popen[str]:
    config = str(world.tmp / "config.yaml")
    return subprocess.Popen(
        [sys.executable, "-m", "review_run", "--config", config, "--json", *argv],
        env=sub_env(world), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )  # fmt: skip


def finish(proc: subprocess.Popen[str]) -> tuple[int, str]:
    out, err = proc.communicate(timeout=60)
    return proc.returncode, out


def test_two_real_reconcile_processes_leave_exactly_one_winner(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    run_id = live_run_without_agent(world)
    procs = [spawn_cli(world, "reconcile") for _ in range(2)]
    results = [finish(p) for p in procs]
    assert [code for code, _ in results] == [0, 0]
    docs = [json.loads(out) for _, out in results]
    winners = [d for d in docs if (run_id, "runner_lost") in kinds(d)]
    assert len(winners) == 1
    loser = next(d for d in docs if d is not winners[0])
    assert loser["changes"] == [] or loser["already_running"] is True
    assert stored(run_id).reason == "runner_lost"
    assert len([r for r in audit_records() if r["action"] == "finalize"]) == 1


def test_concurrent_real_finalizers_have_one_winner(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    run_id = live_run_without_agent(world)
    procs = [
        spawn_cli(world, "reconcile"),
        spawn_cli(world, "show", run_id),
        spawn_cli(world, "show", run_id),
    ]
    results = [finish(p) for p in procs]
    assert [code for code, _ in results] == [0, 0, 0]
    run = stored(run_id)
    assert (run.state, run.reason) == ("failed", "runner_lost")
    assert len([r for r in audit_records() if r["action"] == "finalize"]) == 1


def test_a_resume_in_flight_holds_the_run_so_reconcile_does_not_revert_it(
    world: World, cli
) -> None:  # type: ignore[no-untyped-def]
    """A deterministic interleaving (the fakes cannot run a resume in another process): the
    resume holds the run's lease while reconcile passes; once its holder is gone the revert runs."""
    run_id = escalated(cli, world)
    moved_by_a_dying_resume(run_id)
    poke(run_id, launch_started_at=old(1))
    proc, ident = sleeper()
    try:
        with Store.open(default_state_root()) as store:
            assert store.acquire_lease(lease_name(run_id), holder=ident) is not None
        assert rec(cli)["changes"] == []
        assert stored(run_id).state == "running" and stored(run_id).escalation_at is not None
    finally:
        proc.kill()
        proc.wait()
    assert kinds(rec(cli)) == [(run_id, "resume_reverted")]


# -- dry run writes no log; the sweep is not starved ----------------------------------------------------


def test_a_dry_run_writes_nothing_to_the_log_even_when_a_scan_hits_its_limit(
    world: World, cli, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    live(cli)
    live(cli)
    log = default_state_root() / "log.jsonl"
    before = log.read_bytes() if log.exists() else None
    monkeypatch.setattr(rc, "MAX_RUNS", 1)
    rec(cli, "--dry-run")
    assert (log.read_bytes() if log.exists() else None) == before
    run_id = live(cli)
    (default_state_root() / "runs" / run_id / "settings.json").unlink()
    before = log.read_bytes() if log.exists() else None
    rec(cli, "--dry-run")
    assert (log.read_bytes() if log.exists() else None) == before


def test_undeletable_leftovers_do_not_starve_a_newer_removable_one(
    world: World, cli, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    ids = [started(cli, world) for _ in range(4)]
    rec(cli)  # all finalized
    stuck, newest = ids[:3], ids[3]
    for r in ids:
        (default_state_root() / "runs" / r / "prompt.md").write_text("x")
    real = rc.sweep_prompt_files

    def sweep(root, run_id, opts):  # type: ignore[no-untyped-def]
        if run_id in stuck:
            raise OSError("undeletable")
        return real(root, run_id, opts)

    monkeypatch.setattr(rc, "sweep_prompt_files", sweep)
    monkeypatch.setattr(rc, "MAX_SWEEP", 2)
    for _ in range(3):
        rec(cli)
    assert not (default_state_root() / "runs" / newest / "prompt.md").exists()
