"""Outcome mapping, ``show`` refresh and ``wait`` (issue #24)."""

# ruff: noqa: E501

from __future__ import annotations

import dataclasses
import json
from typing import Any

import pytest

from review_run import outcome as oc
from review_run import waiting
from review_run.errors import ReviewRunError
from review_run.store import Run, Store, default_state_root, lease_name
from tests.conftest import validate_against
from tests.golden_harness import GOLDEN, check_golden
from tests.helpers import Result, run_cli
from tests.run_helpers import PR_URL, World
from tests.test_meta import artifact_text

# -- the mapping, one test row per golden run object -----------------------------------------------

RUN_GOLDENS = sorted(p.name for p in GOLDEN.glob("reason-*.json")) + ["show-done.json"]


def load(name: str) -> dict[str, Any]:
    data = json.loads((GOLDEN / name).read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


@pytest.mark.parametrize("name", RUN_GOLDENS)
@pytest.mark.parametrize("command", ["run", "resume", "wait"])
def test_a_run_object_exits_with_its_own_code_and_prints_the_object(
    name: str, command: str
) -> None:
    obj = load(name)
    shown = oc.render_run(obj, command=command, as_json=True)
    assert shown.code == obj["exit_code"]
    assert json.loads(shown.text) == obj and shown.text.count("\n") == 1
    human = oc.render_run(obj, command=command, as_json=False)
    assert human.code == obj["exit_code"] and obj["run_id"] in human.text


@pytest.mark.parametrize("name", RUN_GOLDENS)
def test_show_exits_0_whatever_the_outcome(name: str) -> None:
    assert oc.render_run(load(name), command="show", as_json=True).code == 0


def test_a_started_no_wait_run_prints_its_id_or_object_and_exits_0() -> None:
    obj = load("show-running.json")
    assert oc.render_run(obj, command="run", as_json=False, no_wait=True) == oc.Rendered(
        obj["run_id"] + "\n", 0
    )
    as_json = oc.render_run(obj, command="resume", as_json=True, no_wait=True)
    assert (as_json.code, json.loads(as_json.text)["state"]) == (0, "running")


@pytest.mark.parametrize(
    ("command", "no_wait"), [("run", False), ("resume", False), ("wait", True)]
)
def test_a_running_run_is_otherwise_internal(command: str, no_wait: bool) -> None:
    with pytest.raises(ReviewRunError) as info:
        oc.render_run(load("show-running.json"), command=command, as_json=True, no_wait=no_wait)
    assert info.value.exit_code == 1


def test_an_unknown_command_is_refused() -> None:
    with pytest.raises(ValueError):
        oc.render_run(load("show-done.json"), command="list", as_json=True)


# -- wait_run with a fake clock --------------------------------------------------------------------


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def clock(self) -> waiting.Clock:
        return waiting.Clock(sleep=self.sleep, monotonic=self.monotonic)


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


def started(cli, world: World) -> str:  # type: ignore[no-untyped-def]
    """``run --no-wait`` and let the agent end: a run left ``running`` for a finalizer."""
    res = cli("--json", "run", PR_URL, "--no-wait")
    assert res.code == 0, res.err
    obj = res.envelope()
    ended = world.fb.run("agent-spawn", "wait", obj["agent_id"], "--json")
    assert ended.returncode in (0, 13, 14, 15, 16, 17, 18)
    return str(obj["run_id"])


def spawn_shows(world: World) -> int:
    return len(world.fb.calls("agent-spawn", "show"))


def live(cli) -> str:  # type: ignore[no-untyped-def]
    """``run --no-wait``: the agent has not ended."""
    res = cli("--json", "run", PR_URL, "--no-wait")
    assert res.code == 0, res.err
    return str(res.envelope()["run_id"])


def stored(run_id: str) -> Run:
    with Store.open(default_state_root()) as store:
        run = store.get_run(run_id)
    assert run is not None
    return run


def as_state(run: Run, state: str) -> Run:
    if state == "running":
        return run
    return dataclasses.replace(run, state="failed", reason="internal", exit_code=12, phase=None)


def test_wait_polls_every_2s_and_stops_when_terminal(cli) -> None:  # type: ignore[no-untyped-def]
    run_id = live(cli)
    base = stored(run_id)
    rows = iter(["running", "running", "running", "failed"])
    clock = FakeClock()
    obj = waiting.wait_run(
        default_state_root(),
        run_id,
        timeout=None,
        refresh=lambda _rid: as_state(base, next(rows)),
        clock=clock.clock(),
    )
    assert (obj["state"], obj["reason"]) == ("failed", "internal")
    assert clock.sleeps == [2.0, 2.0, 2.0]


def test_wait_timeout_is_exact_and_the_last_poll_is_at_the_deadline(cli) -> None:  # type: ignore[no-untyped-def]
    run_id = live(cli)
    base = stored(run_id)
    clock = FakeClock()
    polls: list[float] = []

    def refresh(_rid: str) -> Run:
        polls.append(clock.now)
        return base

    with pytest.raises(ReviewRunError) as info:
        waiting.wait_run(
            default_state_root(), run_id, timeout=5.0, refresh=refresh, clock=clock.clock()
        )
    assert clock.sleeps == [2.0, 2.0, 1.0] and polls == [100.0, 102.0, 104.0, 105.0]
    err = info.value
    assert (err.exit_code, err.reason, err.details) == (12, "wait_timeout", {"retryable": True})
    validate_against("cli/error.json", err.envelope())


def test_a_tiny_timeout_sleeps_only_the_remainder(cli) -> None:  # type: ignore[no-untyped-def]
    run_id = live(cli)
    base = stored(run_id)
    clock = FakeClock()
    with pytest.raises(ReviewRunError):
        waiting.wait_run(
            default_state_root(),
            run_id,
            timeout=0.25,
            refresh=lambda _rid: base,
            clock=clock.clock(),
        )
    assert clock.sleeps == [0.25]


def test_a_run_pruned_while_waiting_is_not_found(cli) -> None:  # type: ignore[no-untyped-def]
    run_id = live(cli)
    with pytest.raises(ReviewRunError) as info:
        waiting.wait_run(default_state_root(), run_id, timeout=None, refresh=lambda _rid: None)
    assert info.value.exit_code == 10


# -- show refresh ----------------------------------------------------------------------------------


def test_show_refresh_finalizes_a_run_whose_agent_ended(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    world.fb.use("agent-spawn", "outcome", outcome="done", artifact_text=artifact_text())
    run_id = started(cli, world)
    before = cli("--json", "show", "--no-refresh", run_id).envelope()
    assert before["state"] == "running" and spawn_shows(world) == 0
    res = cli("--json", "show", run_id)
    assert res.code == 0, res.err
    doc = res.envelope()
    validate_against("cli/show.json", doc)
    assert (doc["state"], doc["exit_code"]) == ("done", 0)
    records = [
        json.loads(x) for x in (default_state_root() / "audit.jsonl").read_text().splitlines()
    ]
    finalize = [r for r in records if r["action"] == "finalize"]
    assert [r["inputs"]["finalized_by"] for r in finalize] == ["show"]
    again = cli("--json", "show", run_id)  # idempotent: a terminal run needs no sibling call
    calls = spawn_shows(world)
    assert again.envelope() == doc
    assert cli("--json", "show", run_id).code == 0 and spawn_shows(world) == calls


def test_show_refresh_reports_a_session_failure_in_the_object_not_the_exit(  # type: ignore[no-untyped-def]
    world: World, cli
) -> None:
    world.fb.use("agent-spawn", "outcome", outcome="killed")
    run_id = started(cli, world)
    res = cli("--json", "show", run_id)
    doc = res.envelope()
    assert res.code == 0 and (doc["reason"], doc["exit_code"]) == ("killed", 15)


def test_show_leaves_a_live_agent_running_and_never_waits(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    run_id = live(cli)
    res = cli("--json", "show", run_id)
    assert res.code == 0 and res.envelope()["state"] == "running"
    assert cli.clock.sleeps == []  # type: ignore[attr-defined]


def test_show_of_a_gone_agent_ends_the_run_agent_gone(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    run_id = started(cli, world)
    world.fb.run("agent-spawn", "teardown", stored(run_id).agent_id or "", "--json")
    doc = cli("--json", "show", run_id).envelope()
    assert (doc["state"], doc["reason"], doc["exit_code"]) == ("failed", "agent_gone", 12)


def test_show_with_a_lease_held_elsewhere_changes_nothing(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    run_id = started(cli, world)
    with Store.open(default_state_root()) as other:
        lease = other.acquire_lease(lease_name(run_id))
        assert lease is not None
        res = cli("--json", "show", run_id)
        other.release_lease(lease)
    assert res.code == 0 and res.envelope()["state"] == "running"
    assert cli.clock.sleeps == []  # type: ignore[attr-defined]
    assert cli("--json", "show", run_id).envelope()["state"] == "failed"  # the lease is free now


def test_show_with_a_corrupt_settings_file_prints_the_stored_run_and_says_so(  # type: ignore[no-untyped-def]
    world: World, cli
) -> None:
    run_id = started(cli, world)
    (default_state_root() / "runs" / run_id / "settings.json").write_text("{not json")
    res = cli("--json", "show", run_id)
    assert res.code == 0 and res.envelope()["state"] == "running"
    assert "not refreshed (internal)" in res.err
    assert stored(run_id).state == "running"  # left unchanged


def test_show_no_refresh_never_calls_a_sibling(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    run_id = started(cli, world)
    cli("--json", "show", "--no-refresh", run_id)
    assert spawn_shows(world) == 0 and stored(run_id).state == "running"


def test_show_of_an_unknown_or_ambiguous_run(cli) -> None:  # type: ignore[no-untyped-def]
    assert cli("--json", "show", "rr-zzzzzzzz").code == 10
    assert cli("--json", "show", "rr-zz").code == 2


# -- wait through the CLI --------------------------------------------------------------------------


def test_wait_finalizes_and_exits_with_the_runs_code(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    world.fb.use("agent-spawn", "outcome", outcome="done", artifact_text=artifact_text())
    run_id = started(cli, world)
    res = cli("--json", "wait", run_id)
    assert res.code == 0, res.err
    doc = res.envelope()
    validate_against("cli/run.json", doc)
    assert doc["state"] == "done" and cli.clock.sleeps == []  # type: ignore[attr-defined]
    records = [
        json.loads(x) for x in (default_state_root() / "audit.jsonl").read_text().splitlines()
    ]
    assert [r["inputs"]["finalized_by"] for r in records if r["action"] == "finalize"] == ["wait"]


def test_wait_exits_22_for_an_invalid_artifact_and_18_for_a_failed_session(  # type: ignore[no-untyped-def]
    world: World, cli
) -> None:
    run_id = started(cli, world)  # the fake's default artifact has no meta
    res = cli("--json", "wait", run_id)
    assert res.code == 22 and res.envelope()["reason"] == "artifact_invalid"


def test_wait_exits_with_the_session_codes(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    world.fb.use("agent-spawn", "outcome", outcome="timeout")
    run_id = started(cli, world)
    assert cli("--json", "wait", run_id).code == 16


def test_wait_reports_an_escalated_run_at_once(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    world.fb.use("agent-spawn", "outcome", outcome="needs-input")
    res = cli("--json", "run", PR_URL, "--actor", "human:alice")
    run_id = res.envelope()["run_id"]
    waited = cli("--json", "wait", run_id)
    assert waited.code == 13 and waited.envelope()["state"] == "escalated"
    assert cli.clock.sleeps == []  # type: ignore[attr-defined]


def test_wait_on_a_terminal_run_prints_it_without_a_sibling_call(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    world.fb.use("agent-spawn", "outcome", outcome="killed")
    run_id = cli("--json", "run", PR_URL).envelope()["run_id"]
    before = spawn_shows(world)
    res = cli("--json", "wait", run_id)
    assert res.code == 15 and spawn_shows(world) == before
    human = cli("wait", run_id)
    assert human.code == 15 and run_id in human.out


def test_wait_timeout_prints_the_envelope_and_leaves_the_run(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    world.fb.use("agent-spawn", "never_ends")
    run_id = live(cli)
    res = cli("--json", "wait", run_id, "--timeout", "5s")
    assert res.code == 12
    err = res.envelope()
    validate_against("cli/error.json", err)
    assert err["error"]["reason"] == "wait_timeout" and err["error"]["details"] == {
        "retryable": True
    }
    assert sum(cli.clock.sleeps) == 5.0 and max(cli.clock.sleeps) <= 2.0  # type: ignore[attr-defined]
    check_golden("envelope-wait_timeout-retryable.json", "cli/error.json", err)
    assert stored(run_id).state == "running"


def test_wait_with_a_corrupt_settings_file_is_internal_and_unchanged(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    run_id = started(cli, world)
    (default_state_root() / "runs" / run_id / "settings.json").write_text("{not json")
    res = cli("--json", "wait", run_id, "--timeout", "5s")
    assert res.code == 1 and res.envelope()["error"]["code"] == "internal"
    assert stored(run_id).state == "running" and cli.clock.sleeps == []  # type: ignore[attr-defined]


def test_wait_on_an_unknown_run_and_a_bad_timeout(cli) -> None:  # type: ignore[no-untyped-def]
    assert cli("--json", "wait", "rr-zzzzzzzz").code == 10
    assert cli("--json", "wait", "rr-zzzzzzzz", "--timeout", "0").code == 2


def test_two_waiters_finalize_exactly_once(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    run_id = started(cli, world)
    first = cli("--json", "wait", run_id)
    second = cli("--json", "wait", run_id)
    assert first.code == second.code == 22
    assert len(world.fb.calls("agent-spawn", "teardown")) == 1
    records = [
        json.loads(x) for x in (default_state_root() / "audit.jsonl").read_text().splitlines()
    ]
    assert len([r for r in records if r["action"] == "finalize"]) == 1
