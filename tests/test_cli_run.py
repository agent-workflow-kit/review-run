"""``review-run run`` end to end through the CLI, against the fake siblings (#17, #18)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.conftest import validate_against
from tests.helpers import Result, run_cli
from tests.run_helpers import PR_URL, World, routed, world  # noqa: F401


@pytest.fixture
def cli(world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("XDG_STATE_HOME", str(world.tmp / "xdg-state"))
    world.config()  # writes config.yaml

    def call(*argv: str) -> Result:
        return run_cli(capsys, "--config", str(world.tmp / "config.yaml"), *argv)

    return call


def test_an_escalated_run_prints_the_run_object_and_exits_13(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    world.fb.use("agent-spawn", "outcome", outcome="needs-input")
    res = cli("--json", "run", PR_URL, "--actor", "human:alice")
    assert res.code == 13
    doc = res.envelope()
    validate_against("cli/run.json", doc)
    assert (doc["state"], doc["reason"], doc["exit_code"]) == ("escalated", "needs_input", 13)


@pytest.mark.parametrize(
    ("outcome", "code"),
    [("exited-unreported", 14), ("killed", 15), ("timeout", 16), ("lost", 17), ("failed", 18)],
)
def test_a_session_failure_exits_with_the_sessions_code(
    world: World, cli, outcome: str, code: int
) -> None:  # type: ignore[no-untyped-def]
    world.fb.use("agent-spawn", "outcome", outcome=outcome)
    res = cli("--json", "run", PR_URL)
    assert res.code == code
    validate_against("cli/run.json", res.envelope())


def test_a_launch_failure_exits_12_with_the_run_object(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    world.fb.use("agent-spawn", "launch_error")
    res = cli("--json", "run", PR_URL)
    assert res.code == 12
    doc = res.envelope()
    validate_against("cli/run.json", doc)
    assert (doc["reason"], doc["failure"]["lower_code"]) == ("launch_failed", "launch_failed")


def test_human_output_is_the_sanitized_run_summary(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    world.fb.use("agent-spawn", "outcome", outcome="killed")
    res = cli("run", PR_URL)
    assert res.code == 15 and "killed" in res.out and "rr-" in res.out


def test_no_wait_prints_the_run_id_and_exits_0(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    res = cli("run", PR_URL, "--no-wait")
    assert res.code == 0
    assert res.out.startswith("rr-") and res.out.count("\n") == 1
    as_json = cli("--json", "run", PR_URL, "--no-wait")
    assert as_json.code == 0
    doc = as_json.envelope()
    validate_against("cli/run.json", doc)
    assert (doc["state"], doc["phase"]) == ("running", "session") and doc["agent_id"]


def test_a_done_session_waits_for_finalize_and_exits_1_until_it_exists(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    res = cli("--json", "run", PR_URL)
    assert res.code == 1 and res.envelope()["error"]["code"] == "internal"
    shown = cli("--json", "list", "--state", "running")
    assert len(json.loads(shown.out)["runs"]) == 1  # left for show, wait and reconcile


def test_a_waiter_ended_by_a_signal_prints_nothing(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    world.fb.script(
        "agent-spawn",
        "wait",
        {"error": {"code": "internal", "message": "x"}, "exit": 130, "schema": None},
    )
    res = cli("--json", "run", PR_URL)
    assert (res.code, res.out) == (130, "")


def test_a_wait_that_never_answers_is_exit_12_lower_failed(
    world: World, cli, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    from review_run import launch

    monkeypatch.setattr(launch, "WAIT_RETRY_SECONDS", 0.0)
    world.fb.use("agent-spawn", "wait_error")
    res = cli("--json", "run", PR_URL)
    assert res.code == 12
    err = res.envelope()["error"]
    validate_against("cli/error.json", res.envelope())
    assert (err["reason"], err["tool"]) == ("lower_failed", "agent-spawn")


def test_a_bad_hook_is_a_usage_error_before_any_sibling_runs(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    res = cli("--json", "run", PR_URL, "--hook", "post-run=")
    assert res.code == 2
    assert world.router.calls == [] and not Path(world.tmp / "xdg-state").exists()
    bad = cli("--json", "run", PR_URL, "--hook", "nope=true")
    assert bad.code == 2 and world.router.calls == []


def test_dry_run_is_still_a_stub_owned_by_15(world: World, cli) -> None:  # type: ignore[no-untyped-def]
    res = cli("--json", "run", PR_URL, "--dry-run")
    assert res.code == 1 and res.envelope()["error"]["code"] == "internal"
    assert all(
        "--dry-run" in c.argv for c in world.fb.calls("agent-spawn", "launch")
    )  # step 2 only
