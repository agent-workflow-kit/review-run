"""``cancel`` and the recorded-cancellation rule (issue #26)."""

# ruff: noqa: E501

from __future__ import annotations

import json
import subprocess
import sys
import threading
from typing import Any

import pytest

from review_run import cancel as cancel_mod
from review_run import prefetch, procid
from review_run import waiting as waiting_mod
from review_run.runs import run_object
from review_run.store import Expect, Run, Store, default_state_root, lease_name
from tests.conftest import validate_against
from tests.helpers import Result, run_cli
from tests.run_helpers import PR_URL, World
from tests.test_outcome import FakeClock, live, started, stored


@pytest.fixture
def cli(world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("XDG_STATE_HOME", str(world.tmp / "xdg-state"))
    world.config()
    clock = FakeClock()
    monkeypatch.setattr("review_run.cli._CLOCK", clock.clock())

    def call(*argv: str) -> Result:
        return run_cli(capsys, "--config", str(world.tmp / "config.yaml"), *argv)

    call.clock = clock  # type: ignore[attr-defined]
    return call


def audit_records() -> list[dict[str, Any]]:
    path = default_state_root() / "audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text().splitlines()]


def cancel_records() -> list[dict[str, Any]]:
    return [r for r in audit_records() if r["action"] == "cancel"]


def calls(world: World, command: str) -> list[Any]:
    return world.fb.calls("agent-spawn", command)


def escalated(cli, world: World) -> str:  # type: ignore[no-untyped-def]
    world.fb.use("agent-spawn", "outcome", outcome="needs-input")
    res = cli("--json", "run", PR_URL)
    assert res.code == 13, res.out
    return str(res.envelope()["run_id"])


def agent_status(world: World, agent_id: str) -> str:
    return str(world.fb.state()["agents"][agent_id]["status"])


# -- running ---------------------------------------------------------------------------------------


def test_cancel_of_a_running_run_stops_ends_canceled_and_tears_down(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    run_id = live(cli)
    agent = stored(run_id).agent_id or ""
    res = cli("--json", "cancel", run_id, "--actor", "human:alice")
    assert res.code == 0, res.err
    doc = res.envelope()
    validate_against("cli/cancel.json", doc)
    assert (doc["changed"], doc["action"], doc["dry_run"]) == (True, "stop", False)
    run = doc["run"]
    assert (run["state"], run["reason"], run["exit_code"]) == ("failed", "canceled", 143)
    assert run["cancel_requested_at"] and run["teardown"] == "done"
    assert (run["failure"]["tool"], run["failure"]["lower_code"]) == (None, None)
    assert len(calls(world, "stop")) == 1
    argv = calls(world, "stop")[0].argv
    assert (
        argv[argv.index("--grace") + 1] == "10s"
        and argv[argv.index("--actor") + 1] == "human:alice"
    )
    assert agent_status(world, agent) == "torn_down"
    row = stored(run_id)
    assert (row.cancel_actor, row.cancel_exit_code) == ("human:alice", 143)


def test_the_audit_pair_is_closed_by_the_terminal_transaction_and_never_vetoed(
    world: World, cli
) -> None:  # type: ignore[no-untyped-def]
    run_id = live(cli)
    cli("cancel", run_id, "--actor", "human:alice", "--decision-id", "d1")
    pair = cancel_records()
    assert [r["result"]["status"] for r in pair] == ["attempted", "failed"]
    assert pair[0]["attempt_id"] == pair[1]["attempt_id"]
    assert pair[1]["result"] == {"status": "failed", "reason": "canceled", "exit_code": 143}
    assert pair[0]["actor"] == "human:alice" and pair[0]["decision_id"] == ["d1"]
    assert pair[1]["inputs"]["state"] == "running" and pair[1]["inputs"]["agent_id"]
    assert not any(r["result"]["status"] == "vetoed" for r in pair)
    assert len(cancel_records()) == 2  # delivered once


def test_a_non_tty_cancel_is_attributed_to_the_auto_actor(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    run_id = live(cli)
    cli("cancel", run_id)
    assert stored(run_id).cancel_actor == "auto:unknown"
    assert cancel_records()[0]["actor"] == "auto:unknown"


def test_a_bad_actor_is_a_usage_error_and_changes_nothing(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    run_id = live(cli)
    res = cli("--json", "cancel", run_id, "--actor", "bob")
    assert res.code == 2
    assert stored(run_id).state == "running" and cancel_records() == []


def test_post_run_hook_fires_once_for_a_cancel(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    run_id = live(cli)
    cli("cancel", run_id, "--actor", "human:alice")
    with Store.open(default_state_root()) as store:
        events = store.pending_events(run_id)
    assert events == []  # delivered
    log = (default_state_root() / "log.jsonl").read_text()
    assert "canceled" in log


def test_cancel_is_idempotent(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    run_id = live(cli)
    first = cli("--json", "cancel", run_id).envelope()
    before = len(world.fb.all_calls())
    again = cli("--json", "cancel", run_id)
    doc = again.envelope()
    assert again.code == 0 and (doc["changed"], doc["action"]) == (False, "none")
    assert doc["run"] == first["run"] and len(world.fb.all_calls()) == before


def test_cancel_of_a_terminal_run_changes_nothing(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    world.fb.use("agent-spawn", "outcome", outcome="killed")
    run_id = str(cli("--json", "run", PR_URL).envelope()["run_id"])
    before = stored(run_id)
    res = cli("--json", "cancel", run_id)
    doc = res.envelope()
    validate_against("cli/cancel.json", doc)
    assert res.code == 0 and (doc["changed"], doc["action"]) == (False, "none")
    assert doc["run"]["reason"] == "killed" and stored(run_id) == before
    assert calls(world, "stop") == []


def test_unknown_run_is_10_and_ambiguous_ref_is_usage(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    assert cli("--json", "cancel", "rr-aaaaaaaa").code == 10
    assert cli("--json", "cancel", "rr-a").code == 2


def test_human_output_names_the_action_and_whether_it_changed(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    run_id = live(cli)
    res = cli("cancel", run_id)
    assert res.code == 0 and "action: stop" in res.out and "changed: yes" in res.out


# -- escalated -------------------------------------------------------------------------------------


def test_cancel_of_an_escalated_run_tears_the_parked_agent_down_without_a_stop(
    world: World, cli
) -> None:  # type: ignore[no-untyped-def]
    run_id = escalated(cli, world)
    agent = stored(run_id).agent_id or ""
    doc = cli("--json", "cancel", run_id).envelope()
    validate_against("cli/cancel.json", doc)
    assert (doc["changed"], doc["action"]) == (True, "none")
    assert (doc["run"]["state"], doc["run"]["reason"]) == ("failed", "canceled")
    assert calls(world, "stop") == [] and agent_status(world, agent) == "torn_down"
    assert stored(run_id).escalated_run is not None


def test_cancel_of_an_escalated_run_a_human_answers_stops_it_first(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    run_id = escalated(cli, world)
    agent = stored(run_id).agent_id or ""
    world.fb.use("agent-spawn", "never_ends")
    resumed = world.fb.run("agent-spawn", "resume", agent, "--kind", "review", "--json")
    assert resumed.returncode in (0, 13), resumed.stdout
    doc = cli("--json", "cancel", run_id).envelope()
    assert (doc["changed"], doc["action"]) == (True, "stop")
    assert (doc["run"]["state"], doc["run"]["reason"]) == ("failed", "canceled")
    assert agent_status(world, agent) == "torn_down"


def test_an_escalated_run_whose_agent_finished_a_newer_run_is_finalized_not_canceled(
    world: World, cli
) -> None:  # type: ignore[no-untyped-def]
    from tests.test_meta import artifact_text

    run_id = escalated(cli, world)
    agent = stored(run_id).agent_id or ""
    world.fb.configure(
        "sessions", resume_plan={"outcome": "done", "artifact_text": artifact_text()}
    )
    resumed = world.fb.run("agent-spawn", "resume", agent, "--kind", "review", "--json")
    assert resumed.returncode in (0, 13), resumed.stdout
    ended = world.fb.run("agent-spawn", "wait", agent, "--json")
    assert ended.returncode == 0, ended.stdout
    res = cli("--json", "cancel", run_id)
    doc = res.envelope()
    assert res.code == 0, res.out
    assert (doc["changed"], doc["action"]) == (False, "none")
    assert (
        doc["run"]["state"],
        doc["run"]["cancel_requested_at"],
        doc["run"]["reason"],
        doc["run"]["failure"],
    ) == ("done", None, None, None)
    assert calls(world, "stop") == []
    finalize = [r for r in audit_records() if r["action"] == "finalize"]
    assert [r["inputs"]["cause"] for r in finalize] == ["external_resume"]


def test_cancel_of_an_escalated_run_with_a_gone_agent_still_ends_it_canceled(
    world: World, cli
) -> None:  # type: ignore[no-untyped-def]
    run_id = escalated(cli, world)
    world.fb.run("agent-spawn", "teardown", stored(run_id).agent_id or "", "--json")
    doc = cli("--json", "cancel", run_id).envelope()
    assert (doc["run"]["state"], doc["run"]["reason"], doc["changed"]) == (
        "failed",
        "canceled",
        True,
    )
    assert calls(world, "stop") == [] and len(calls(world, "teardown")) == 1  # only the test's own


# -- agent-spawn failures --------------------------------------------------------------------------


def test_stop_exit_12_exits_12_and_the_cancellation_stays_recorded(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    run_id = live(cli)
    world.fb.script(
        "agent-spawn",
        "stop",
        {"error": {"code": "failed", "message": "x"}, "exit": 12, "schema": None},
    )
    res = cli("--json", "cancel", run_id)
    assert res.code == 12
    err = res.envelope()["error"]
    assert (err["code"], err["reason"], err["tool"], err["lower_code"]) == (
        "failed",
        "lower_failed",
        "agent-spawn",
        "failed",
    )
    row = stored(run_id)
    assert row.state == "running" and row.cancel_requested_at is not None
    pair = cancel_records()
    assert [r["result"]["status"] for r in pair] == ["attempted", "failed"]
    assert pair[1]["result"]["exit_code"] == 12
    # the next finalizer ends it canceled once the agent has ended
    world.fb.script("agent-spawn", "stop")  # default behavior again
    assert cli("--json", "cancel", run_id).envelope()["run"]["reason"] == "canceled"


def test_stop_exit_20_exits_12_with_its_closed_code(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    run_id = live(cli)
    world.fb.script(
        "agent-spawn",
        "stop",
        {"error": {"code": "refused", "message": "x"}, "exit": 20, "schema": None},
    )
    res = cli("--json", "cancel", run_id)
    assert res.code == 12 and res.envelope()["error"]["lower_code"] == "refused"
    assert stored(run_id).state == "running"


def test_stop_exit_10_continues_and_ends_canceled(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    run_id = live(cli)
    world.fb.script(
        "agent-spawn",
        "stop",
        {"error": {"code": "not_found", "message": "x"}, "exit": 10, "schema": None},
    )
    doc = cli("--json", "cancel", run_id).envelope()
    assert (doc["run"]["state"], doc["run"]["reason"], doc["action"]) == (
        "failed",
        "canceled",
        "stop",
    )


def test_a_failing_show_is_exit_12_and_nothing_is_stopped(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    run_id = live(cli)
    world.fb.script(
        "agent-spawn",
        "show",
        {"error": {"code": "failed", "message": "x"}, "exit": 12, "schema": None},
    )
    res = cli("--json", "cancel", run_id)
    assert res.code in (1, 12), res.out
    assert stored(run_id).state == "running" and calls(world, "stop") == []


def test_a_refused_teardown_leaves_the_run_canceled_with_a_warning(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    run_id = live(cli)
    world.fb.script(
        "agent-spawn",
        "teardown",
        {"error": {"code": "refused", "message": "x"}, "exit": 20, "schema": None},
    )
    res = cli("--json", "cancel", run_id)
    doc = res.envelope()
    assert res.code == 0
    assert (doc["run"]["state"], doc["run"]["reason"]) == ("failed", "canceled")
    assert doc["run"]["teardown"] == "failed" and "teardown_refused" in doc["warnings"]
    validate_against("cli/cancel.json", doc)


def test_a_look_alike_agent_is_never_stopped_or_torn_down(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    run_id = live(cli)
    agent = stored(run_id).agent_id or ""
    state = world.fb.state()
    labels = state["agents"][agent]["settings"]["labels"]
    state["agents"][agent]["settings"]["labels"] = [
        x for x in labels if not x.startswith("run=")
    ] + ["run=rr-bbbbbbbb"]
    world.fb.seed_state(state)
    doc = cli("--json", "cancel", run_id).envelope()  # the refresh finds the look-alike first
    assert (doc["run"]["reason"], doc["changed"]) == ("agent_gone", False)
    assert "agent_mismatch" in doc["warnings"]
    assert calls(world, "stop") == [] and calls(world, "teardown") == []
    assert agent_status(world, agent) == "live"


def test_a_look_alike_agent_under_a_recorded_cancellation_ends_canceled_untouched(
    world: World, cli
) -> None:  # type: ignore[no-untyped-def]
    run_id = live(cli)
    agent = stored(run_id).agent_id or ""
    state = world.fb.state()
    state["agents"][agent]["settings"]["labels"] = ["owner=review-run", "run=rr-bbbbbbbb"]
    world.fb.seed_state(state)
    with Store.open(default_state_root()) as store:
        row = store.get_run(run_id)
        assert row is not None
        assert store.request_cancel(run_id, row.key, actor="human:alice", exit_code=143)
    doc = cli("--json", "cancel", run_id).envelope()
    assert doc["run"]["reason"] == "canceled"  # the refresh of step 2 ended it (changed: false)
    assert "agent_mismatch" in doc["warnings"]
    assert calls(world, "stop") == [] and calls(world, "teardown") == []
    assert agent_status(world, agent) == "live"


# -- races -----------------------------------------------------------------------------------------


def test_cancel_loses_to_finalize_and_reports_the_stored_row(
    world: World, cli, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    run_id = live(cli)
    root = default_state_root()
    real = cancel_mod.refresh_run

    def finalize_first(*a: Any, **k: Any) -> Any:
        out = real(*a, **k)
        with Store.open(root) as other:
            lease = other.acquire_lease(lease_name(run_id))
            assert lease is not None
            row = other.get_run(run_id)
            assert row is not None and other.begin_finalize(run_id, row.key, lease=lease)
            other.release_lease(lease)
        return out

    monkeypatch.setattr(cancel_mod, "refresh_run", finalize_first)

    def end_it(_seconds: float) -> None:  # the finalizer finishes while cancel waits
        with Store.open(root) as other:
            lease = other.acquire_lease(lease_name(run_id))
            row = other.get_run(run_id)
            assert lease is not None and row is not None
            if row.state == "running":
                other.end_run(
                    run_id, row.key, lease=lease, state="failed", reason="internal", exit_code=12
                )
            other.release_lease(lease)

    cli.clock.sleep = end_it  # type: ignore[attr-defined]
    monkeypatch.setattr(
        "review_run.cli._CLOCK", waiting_mod.Clock(sleep=end_it, monotonic=cli.clock.monotonic)
    )  # type: ignore[attr-defined]
    doc = cli("--json", "cancel", run_id).envelope()
    assert (doc["changed"], doc["action"]) == (False, "none")
    assert doc["run"]["reason"] == "internal" and doc["run"]["cancel_requested_at"] is None
    assert calls(world, "stop") == []


def test_a_finalize_that_never_ends_is_wait_timeout_12(
    world: World, cli, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    run_id = live(cli)
    root = default_state_root()
    with Store.open(root) as other:
        lease = other.acquire_lease(lease_name(run_id))
        row = other.get_run(run_id)
        assert lease is not None and row is not None
        assert other.begin_finalize(run_id, row.key, lease=lease)
        res = cli("--json", "cancel", run_id)
        other.release_lease(lease)
    assert res.code == 12 and res.envelope()["error"]["reason"] == "wait_timeout"
    assert stored(run_id).cancel_requested_at is None
    assert cli.clock.now == pytest.approx(100.0 + 120.0, abs=3)  # type: ignore[attr-defined]


def test_a_cancel_recorded_first_makes_the_finalizer_end_canceled_without_the_artifact(
    world: World, cli
) -> None:  # type: ignore[no-untyped-def]
    from tests.test_meta import artifact_text

    world.fb.use("agent-spawn", "outcome", outcome="done", artifact_text=artifact_text())
    run_id = started(cli, world)  # the agent finished with a valid review
    with Store.open(default_state_root()) as store:
        row = store.get_run(run_id)
        assert row is not None
        assert store.request_cancel(run_id, row.key, actor="human:alice", exit_code=143)
        assert store.get_run(run_id).state == "running"  # type: ignore[union-attr]
    doc = cli("--json", "show", run_id).envelope()
    assert (doc["state"], doc["reason"], doc["exit_code"]) == ("failed", "canceled", 143)
    assert doc["artifact"] is None and doc["summary"] is None
    assert not (default_state_root() / "runs" / run_id / "review.json").exists()
    assert doc["teardown"] == "done"


def test_a_runner_killed_after_recording_the_cancellation_is_ended_canceled_by_show(
    world: World, cli
) -> None:  # type: ignore[no-untyped-def]
    run_id = live(cli)
    agent = stored(run_id).agent_id or ""
    with Store.open(default_state_root()) as store:
        row = store.get_run(run_id)
        assert row is not None
        assert store.request_cancel(run_id, row.key, actor="human:alice", exit_code=143)
    world.fb.run("agent-spawn", "stop", agent, "--json")  # the agent ended; the runner is gone
    doc = cli("--json", "show", run_id).envelope()
    assert (doc["state"], doc["reason"], doc["exit_code"]) == ("failed", "canceled", 143)
    assert agent_status(world, agent) == "torn_down"


def test_a_cancel_that_died_after_recording_is_finished_by_the_next_cancel(
    world: World, cli
) -> None:  # type: ignore[no-untyped-def]
    run_id = live(cli)
    with Store.open(default_state_root()) as store:
        row = store.get_run(run_id)
        assert row is not None
        assert store.request_cancel(run_id, row.key, actor="human:alice", exit_code=143)
    doc = cli("--json", "cancel", run_id, "--actor", "human:bob").envelope()
    assert (doc["changed"], doc["action"]) == (True, "stop")
    assert doc["run"]["reason"] == "canceled"
    assert stored(run_id).cancel_actor == "human:alice"  # the first recording stands


def test_a_live_lease_holder_is_waited_for_then_the_row_is_printed_as_it_is(
    world: World, cli
) -> None:  # type: ignore[no-untyped-def]
    run_id = live(cli)
    with Store.open(default_state_root()) as other:
        lease = other.acquire_lease(lease_name(run_id))
        assert lease is not None
        res = cli("--json", "cancel", run_id)
        other.release_lease(lease)
    doc = res.envelope()
    validate_against("cli/cancel.json", doc)
    assert res.code == 0 and (doc["changed"], doc["action"]) == (True, "stop")
    assert (doc["run"]["state"], doc["run"]["phase"]) == ("running", "session")
    assert doc["run"]["cancel_requested_at"] is not None
    assert cli.clock.now >= 100.0 + 120.0  # type: ignore[attr-defined]
    row = stored(run_id)
    assert row.cancel_requested_at is not None and row.state == "running"
    # the next finalizer ends it canceled (the agent was stopped)
    assert cli("--json", "show", run_id).envelope()["reason"] == "canceled"


def test_a_lost_record_race_returns_to_the_fresh_row(
    world: World, cli, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    run_id = live(cli)
    real = Store.request_cancel
    seen: list[Expect] = []

    def moved(self: Store, rid: str, expect: Expect, **kw: Any) -> bool:
        if not seen:
            seen.append(expect)
            row = self.get_run(rid)
            assert row is not None
            self.update_fields(rid, row.key, awaited_run=(row.awaited_run or 0) + 1)
        return real(self, rid, expect, **kw)

    monkeypatch.setattr(Store, "request_cancel", moved)
    doc = cli("--json", "cancel", run_id).envelope()
    assert len(seen) == 1 and doc["run"]["reason"] == "canceled" and doc["changed"] is True


# -- dry run ---------------------------------------------------------------------------------------


def test_dry_run_reports_the_action_and_changes_nothing(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    run_id = live(cli)
    before = stored(run_id)
    res = cli("--json", "cancel", run_id, "--dry-run")
    doc = res.envelope()
    validate_against("cli/cancel.json", doc)
    assert res.code == 0 and (doc["changed"], doc["action"], doc["dry_run"]) == (
        False,
        "stop",
        True,
    )
    assert stored(run_id) == before and calls(world, "stop") == [] and cancel_records() == []
    assert doc["run"]["state"] == "running"


def test_dry_run_of_a_terminal_and_of_an_escalated_run(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    run_id = escalated(cli, world)
    doc = cli("--json", "cancel", run_id, "--dry-run").envelope()
    assert (doc["action"], doc["changed"], doc["run"]["state"]) == ("none", False, "escalated")
    cli("cancel", run_id)
    doc = cli("--json", "cancel", run_id, "--dry-run").envelope()
    assert (doc["action"], doc["dry_run"], doc["run"]["state"]) == ("none", True, "failed")


# -- no agent: the runner and the launcher -----------------------------------------------------------


def dead_pid_identity() -> tuple[int, str]:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid, "gone:0"


def sleeper() -> tuple[subprocess.Popen[bytes], procid.ProcIdentity]:
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    threading.Thread(target=proc.wait, daemon=True).start()  # reaped as soon as it ends
    ident = procid.identity_of(proc.pid)
    assert ident is not None
    return proc, ident


def prefetch_run(world: World, **fields: Any) -> Run:
    """A run left in phase ``prefetch`` with no agent (as if its runner died)."""
    world.fb.use("agent-spawn", "never_ends")
    run_id = live_run_without_agent(world)
    with Store.open(default_state_root()) as store:
        row = store.get_run(run_id)
        assert row is not None
        if fields:
            store.update_fields(run_id, row.key, **fields)
        out = store.get_run(run_id)
    assert out is not None
    return out


def live_run_without_agent(world: World) -> str:
    plan = world.plan()
    with Store.open(default_state_root()) as store:
        from review_run.store import NewRun

        run_id = store.reserve_id()
        facts = plan.facts
        store.create_run(
            NewRun(
                run_id=run_id,
                pr=facts.pr,
                repo=facts.repo,
                head_sha=facts.head_sha,
                mode=plan.mode,
                skill=plan.skill,
                runner=procid.ProcIdentity(*dead_pid_identity()),
            )
        )
        from review_run.settings import write_run_files

        write_run_files(store.run_dir(run_id), plan.settings, plan.profile)
    return run_id


def test_cancel_of_a_prefetch_run_with_a_dead_runner_ends_it_canceled(
    world: World, cli, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("XDG_STATE_HOME", str(world.tmp / "xdg-state"))
    run_id = live_run_without_agent(world)
    (default_state_root() / "runs" / run_id / "prompt.md").write_text("x")
    doc = cli("--json", "cancel", run_id).envelope()
    assert (doc["changed"], doc["action"]) == (True, "none")
    assert (doc["run"]["state"], doc["run"]["reason"], doc["run"]["teardown"]) == (
        "failed",
        "canceled",
        None,
    )
    assert not (default_state_root() / "runs" / run_id / "prompt.md").exists()
    assert calls(world, "stop") == []


def test_cancel_signals_a_live_runner_by_its_exact_pid_and_waits_for_it(
    world: World, cli, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    run_id = live_run_without_agent(world)
    proc, ident = sleeper()
    with Store.open(default_state_root()) as store:
        row = store.get_run(run_id)
        assert row is not None
        store.update_fields(run_id, row.key, runner_pid=ident.pid, runner_start=ident.start)
    sent: list[int] = []

    def kill(pid: int) -> None:
        sent.append(pid)
        proc.terminate()

    monkeypatch.setattr(cancel_mod, "_kill", kill)
    monkeypatch.setattr(
        cancel_mod.CancelOptions.__dataclass_fields__["kill"], "default", kill, raising=False
    )
    result = cancel_mod.cancel_run(
        default_state_root(),
        run_id,
        cancel_mod.CancelOptions(
            caller=_caller(),
            clock=cli.clock.clock(),
            kill=kill,
            config_path=world.tmp / "config.yaml",  # type: ignore[attr-defined]
        ),
    )
    assert sent == [ident.pid]
    assert (result.action, result.changed, result.run.state, result.run.reason) == (
        "signal_runner",
        True,
        "failed",
        "canceled",
    )
    assert proc.poll() is not None


def test_a_runner_alive_after_the_wait_is_not_ended(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    run_id = live_run_without_agent(world)
    proc, ident = sleeper()
    try:
        with Store.open(default_state_root()) as store:
            row = store.get_run(run_id)
            assert row is not None
            store.update_fields(run_id, row.key, runner_pid=ident.pid, runner_start=ident.start)
            lease = store.acquire_lease(lease_name(run_id), holder=ident)  # the runner's lease
            assert lease is not None
        result = cancel_mod.cancel_run(
            default_state_root(),
            run_id,
            cancel_mod.CancelOptions(
                caller=_caller(),
                clock=cli.clock.clock(),
                kill=lambda _pid: None,
                config_path=world.tmp / "config.yaml",  # type: ignore[attr-defined]
            ),
        )
        assert result.action == "signal_runner" and result.changed is True
        assert (result.run.state, result.run.phase) == ("running", "prefetch")
        assert result.run.cancel_requested_at is not None
        assert cli.clock.now >= 100.0 + 120.0  # type: ignore[attr-defined]
    finally:
        proc.terminate()


def test_a_run_is_never_signaled_through_a_reused_pid(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    run_id = live_run_without_agent(world)
    proc, ident = sleeper()
    try:
        with Store.open(default_state_root()) as store:
            row = store.get_run(run_id)
            assert row is not None
            store.update_fields(
                run_id, row.key, runner_pid=ident.pid, runner_start=ident.start + "9"
            )
        sent: list[int] = []
        result = cancel_mod.cancel_run(
            default_state_root(),
            run_id,
            cancel_mod.CancelOptions(caller=_caller(), clock=cli.clock.clock(), kill=sent.append),  # type: ignore[attr-defined]
        )
        assert sent == [] and result.run.reason == "canceled"
    finally:
        proc.terminate()


def test_a_live_launcher_is_signaled_and_a_launch_json_agent_is_adopted_and_stopped(
    world: World, cli
) -> None:  # type: ignore[no-untyped-def]
    run_id = live(cli)
    agent = stored(run_id).agent_id or ""
    csession = stored(run_id).csession_id or ""
    proc, ident = sleeper()
    with Store.open(default_state_root()) as store:
        row = store.get_run(run_id)
        assert row is not None
        # as if the runner died right after the launcher started the session: no agent stored
        store.update_fields(
            run_id,
            row.key,
            launcher_pid=ident.pid,
            launcher_start=ident.start,
            runner_pid=dead_pid_identity()[0],
            runner_start="gone:0",
        )
        conn = store._conn  # noqa: SLF001 - test-only: forget the stored agent
        conn.execute(
            "UPDATE runs SET agent_id = NULL, csession_id = NULL WHERE run_id = ?", (run_id,)
        )
    launch_doc = {
        "agent": {"agent_id": agent, "csession_id": csession, "status": "live", "run": 1},
        "phase": "session",
        "outcome": None,
        "run": None,
    }
    sent: list[int] = []

    def kill(pid: int) -> None:
        sent.append(pid)
        proc.terminate()
        (default_state_root() / "runs" / run_id / "launch.json").write_text(json.dumps(launch_doc))

    result = cancel_mod.cancel_run(
        default_state_root(),
        run_id,
        cancel_mod.CancelOptions(
            caller=_caller(),
            clock=cli.clock.clock(),
            kill=kill,
            config_path=world.tmp / "config.yaml",
        ),  # type: ignore[attr-defined]
    )
    assert sent == [ident.pid] and result.action == "signal_launcher"
    assert (result.run.state, result.run.reason) == ("failed", "canceled")
    assert len(calls(world, "stop")) == 1 and agent_status(world, agent) == "torn_down"


def _caller():  # type: ignore[no-untyped-def]
    from tests.run_helpers import CALLER

    return CALLER


# -- the persist_prefetch gap (#12) and the runner ----------------------------------------------------


def test_persist_prefetch_is_refused_while_a_cancellation_is_recorded(world: World) -> None:
    run_id = live_run_without_agent(world)
    plan = world.plan()
    with Store.open(default_state_root()) as store:
        row = store.get_run(run_id)
        assert row is not None
        assert store.request_cancel(run_id, row.key, actor="human:alice", exit_code=143)
        fetched = type(
            "F", (), {"draft_id": "d", "anchor": type("A", (), {"to_dict": lambda self: {}})()}
        )()
        assert prefetch.persist_prefetch(store, run_id, row.key, plan.facts, fetched) is False  # type: ignore[arg-type]
        after = store.get_run(run_id)
        assert after is not None and after.draft_id is None


def test_a_run_whose_cancellation_lands_during_prefetch_ends_canceled_and_never_launches(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = prefetch.persist_prefetch

    def cancel_then_persist(store: Store, run_id: str, expect: Expect, *a: Any, **k: Any) -> bool:
        assert store.request_cancel(run_id, expect, actor="human:alice", exit_code=143)
        return real(store, run_id, expect, *a, **k)

    monkeypatch.setattr("review_run.runner.persist_prefetch", cancel_then_persist)
    run = world.run()
    assert (run.state, run.reason, run.exit_code) == ("failed", "canceled", 143)
    assert world.fb.calls("agent-spawn", "launch") == []
    assert run_object(run)["failure"]["tool"] is None


def test_a_cancellation_recorded_while_the_session_runs_is_honored_by_the_runner(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The blocking ``run`` sees its agent end (stopped by ``cancel``) and ends ``canceled``."""
    from review_run import launch as lc

    real = lc.wait_session

    def cancel_during_wait(agent_id: str, *a: Any, **k: Any) -> Any:
        with Store.open(world.root) as other:
            row = other.get_run(world.only_run().run_id)
            assert row is not None
            assert other.request_cancel(row.run_id, row.key, actor="human:alice", exit_code=143)
        world.fb.run("agent-spawn", "stop", agent_id, "--json")
        return real(agent_id, *a, **k)

    monkeypatch.setattr("review_run.runner.lc.wait_session", cancel_during_wait)
    run = world.run()
    assert (run.state, run.reason, run.exit_code, run.teardown) == (
        "failed",
        "canceled",
        143,
        "done",
    )
    assert not (world.root / "runs" / run.run_id / "review.json").exists()


def test_cancel_never_signals_its_own_process(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    run_id = live_run_without_agent(world)
    with Store.open(default_state_root()) as store:
        row = store.get_run(run_id)
        assert row is not None
        me = procid.current()
        store.update_fields(run_id, row.key, runner_pid=me.pid, runner_start=me.start)
    sent: list[int] = []
    result = cancel_mod.cancel_run(
        default_state_root(),
        run_id,
        cancel_mod.CancelOptions(caller=_caller(), clock=cli.clock.clock(), kill=sent.append),  # type: ignore[attr-defined]
    )
    assert sent == [] and result.run.reason == "canceled"


def test_envelopes_and_logs_carry_no_untrusted_text(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    run_id = live(cli)
    world.fb.script(
        "agent-spawn",
        "stop",
        {"error": {"code": "failed", "message": "SECRET-TEXT"}, "exit": 12, "schema": None},
    )
    res = cli("--json", "cancel", run_id)
    assert "SECRET-TEXT" not in res.out + res.err
    for name in ("audit.jsonl", "log.jsonl"):
        path = default_state_root() / name
        assert not path.exists() or "SECRET-TEXT" not in path.read_text()
