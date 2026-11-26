"""``run`` after preflight, against the real fake siblings (issues #17 and #18).

Every test runs the whole orchestration through the real gateway: the fakes record their exact
argv and env, and the store, audit and run directory are the real ones.
"""

# ruff: noqa: E501

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

from review_run import audit as audit_mod
from review_run import runner
from review_run import store as store_mod
from review_run.audit import Caller
from review_run.errors import ReviewRunError
from review_run.store import Store, lease_name
from tests.conftest import validate_against
from tests.fakes import FakeBin
from tests.run_helpers import PR, REPO, World

MARKER = "UNTRUSTED-MARKER-7f3a"
SESSION_ROWS = [
    ("exited-unreported", "unreported", 14),
    ("killed", "killed", 15),
    ("timeout", "timeout", 16),
    ("lost", "lost", 17),
    ("failed", "session_failed", 18),
]
LAUNCH_ROWS = [
    ("not-started", "not_started", "claude-session"),
    ("vetoed", "vetoed", "agent-spawn"),
    ("escalated", "escalated", "agent-spawn"),
]


def audit_records(root: Path) -> list[dict[str, Any]]:
    path = root / "audit.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def run_object(world: World) -> dict[str, Any]:
    from review_run import runs

    obj = runs.run_object(world.only_run())
    validate_against("cli/run.json", obj)
    return obj


def agent_calls(world: World, command: str) -> list[Any]:
    return world.fb.calls("agent-spawn", command)


def no_files_left(world: World) -> None:
    run = world.only_run()
    base = world.root / "runs" / run.run_id
    assert not (base / "prompt.md").exists() and not (base / "inputs").exists()


# -- escalation, failures and the table rows ------------------------------------------------------


@pytest.mark.parametrize(
    ("outcome", "reason"), [("needs-input", "needs_input"), ("blocked", "blocked")]
)
def test_a_session_that_needs_a_human_escalates_and_keeps_its_agent(
    world: World, outcome: str, reason: str
) -> None:
    world.fb.use("agent-spawn", "outcome", outcome=outcome)
    run = world.run()
    assert (run.state, run.reason, run.exit_code, run.phase) == ("escalated", reason, 13, None)
    assert run.agent_id and run.csession_id and run.awaited_run == 1 and run.escalated_run == 1
    assert run.escalated_at is not None and run.finished_at is None
    assert agent_calls(world, "teardown") == [] and agent_calls(world, "stop") == []
    obj = run_object(world)
    assert (obj["state"], obj["reason"], obj["exit_code"]) == ("escalated", reason, 13)
    no_files_left(world)


@pytest.mark.parametrize(("outcome", "reason", "code"), SESSION_ROWS)
def test_session_failures_end_the_run_with_the_same_exit_and_tear_the_agent_down(
    world: World, outcome: str, reason: str, code: int
) -> None:
    world.fb.use("agent-spawn", "outcome", outcome=outcome)
    run = world.run()
    assert (run.state, run.reason, run.exit_code, run.phase) == ("failed", reason, code, None)
    assert run.finished_at is not None and run.failure is None
    assert run.teardown == "done"
    (call,) = agent_calls(world, "teardown")
    assert call.exit == 0 and "--force" not in call.argv and call.argv[0] == "teardown"
    assert run_object(world)["failure"] == {
        "tool": None, "lower_code": None, "lower_reason": None,
        "retryable": False, "retry_after_s": None, "errors": [],
    }  # fmt: skip
    no_files_left(world)


@pytest.mark.parametrize(("outcome", "lower", "tool"), LAUNCH_ROWS)
def test_a_run_that_ends_in_its_launch_phase_is_launch_failed_and_torn_down(
    world: World, outcome: str, lower: str, tool: str
) -> None:
    world.fb.use("agent-spawn", "outcome", outcome=outcome)
    run = world.run()
    assert (run.state, run.reason, run.exit_code) == ("failed", "launch_failed", 12)
    assert run.failure is not None
    assert (run.failure["tool"], run.failure["lower_code"]) == (tool, lower)
    assert run.teardown == "done" and len(agent_calls(world, "teardown")) == 1
    assert agent_calls(world, "wait") == []
    run_object(world)


@pytest.mark.parametrize(
    ("scenario", "params", "lower", "tool"),
    [
        ("launch_error", {}, "launch_failed", "agent-spawn"),
        ("tool_missing", {}, "tool_missing", "agent-spawn"),
        ("no_tmux", {}, "tmux_missing", None),
        ("launch_vetoed", {}, "vetoed", "agent-spawn"),
        ("launch_refused", {"reason": "third_party_cwd"}, "refused", "agent-spawn"),
        ("launch_ambiguous", {}, "ambiguous", "agent-spawn"),
    ],
)
def test_a_launch_envelope_is_launch_failed_with_closed_codes_and_no_sibling_text(
    world: World, scenario: str, params: dict[str, Any], lower: str, tool: str | None
) -> None:
    world.fb.use("agent-spawn", scenario, **params)
    run = world.run()
    assert (run.state, run.reason, run.exit_code) == ("failed", "launch_failed", 12)
    assert run.failure is not None and run.failure["tool"] == tool
    assert lower in (run.failure["lower_code"], run.failure["lower_reason"])
    text = json.dumps(run_object(world))
    for sibling_text in (
        "fake failure",
        "vetoed by pre-launch",
        "tmux is not installed",
        "hook_stderr",
    ):
        assert sibling_text not in text
    assert agent_calls(world, "teardown") == [] and agent_calls(world, "wait") == []
    no_files_left(world)


def pinned_run_id(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(store_mod, "generate_run_id", lambda: "rr-aaaaaaaa")
    return "rr-aaaaaaaa"


def make_agent(world: World, run_id: str, *, run_label: str | None = None) -> str:
    """An agent in the fake agent-spawn, labelled like this run's (or another run's)."""
    labels = ["owner=review-run", f"run={run_label or run_id}", f"pr={PR}", f"repo={REPO}"]
    world.fb.use("agent-spawn", "outcome", outcome="not-started")
    argv = ["launch", "--kind", "review", "--mode", "headless", "--skill", "p:s", "--json"]
    for label in labels:
        argv += ["--label", label]
    result = world.fb.run("agent-spawn", *argv)
    return str(json.loads(result.stdout)["agent"]["agent_id"])


def launch_envelope_with(world: World, agent_id: str) -> None:
    world.fb.script(
        "agent-spawn",
        "launch",
        {"error": {"code": "launch_failed", "message": "x", "agent_id": agent_id}, "exit": 12},
    )


def test_a_launch_envelope_naming_an_agent_tears_the_parked_agent_down(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = pinned_run_id(monkeypatch)
    agent_id = make_agent(world, run_id)
    launch_envelope_with(world, agent_id)
    run = world.run()
    assert (run.reason, run.exit_code, run.agent_id, run.teardown) == (
        "launch_failed",
        12,
        agent_id,
        "done",
    )
    assert [c.argv[1] for c in agent_calls(world, "teardown")] == [agent_id]
    assert world.fb.state()["agents"][agent_id]["status"] == "torn_down"


def test_an_agent_that_is_not_this_runs_is_never_torn_down(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = pinned_run_id(monkeypatch)
    agent_id = make_agent(world, run_id, run_label="rr-bbbbbbbb")
    launch_envelope_with(world, agent_id)
    run = world.run()
    # the spec: an agent that fails the ownership check ends the run agent_gone, never stored
    assert (run.state, run.reason, run.exit_code) == ("failed", "agent_gone", 12)
    assert run.warnings == ["agent_mismatch"] and run.teardown is None
    assert run.agent_id is None and run.failure is None
    assert agent_calls(world, "teardown") == [] and agent_calls(world, "stop") == []
    assert world.fb.state()["agents"][agent_id]["status"] != "torn_down"
    assert [r["action"] for r in audit_records(world.root)] == ["run", "run"]


@pytest.mark.parametrize(
    ("step", "warning"),
    [
        (
            {"error": {"code": "refused", "message": "x", "reason": "live"}, "exit": 20},
            "teardown_refused",
        ),
        ({"error": {"code": "failed", "message": "x"}, "exit": 12}, "teardown_failed"),
    ],
)
def test_a_refused_or_failed_teardown_is_a_warning_and_the_run_stays_failed(
    world: World, step: dict[str, Any], warning: str
) -> None:
    world.fb.use("agent-spawn", "outcome", outcome="killed")
    world.fb.script("agent-spawn", "teardown", step)
    run = world.run()
    assert (run.state, run.reason) == ("failed", "killed")
    assert run.warnings == [warning] and run.teardown == "failed"


# -- done: the finalize seam ----------------------------------------------------------------------


def test_a_done_session_reaches_the_finalize_seam_with_its_agent_stored(world: World) -> None:
    seen: list[runner.FinalizeRequest] = []

    def finalize(request: runner.FinalizeRequest) -> Any:
        seen.append(request)
        run = request.store.get_run(request.run_id)
        assert run is not None
        assert request.store.lease_info(lease_name(request.run_id)) is not None  # still held
        return run

    run = world.run(finalize=finalize)
    (request,) = seen
    assert request.result.outcome == "done" and request.result.run_number == 1
    assert (run.state, run.phase, run.awaited_run) == ("running", "session", 1)
    assert run.agent_id and run.csession_id


def test_by_default_a_done_session_leaves_the_run_running_for_a_later_finalizer(
    world: World,
) -> None:
    with pytest.raises(ReviewRunError) as info:
        world.run()
    assert info.value.code == "internal"
    run = world.only_run()
    assert (run.state, run.phase, run.awaited_run) == ("running", "session", 1)
    assert run.agent_id is not None and run.finished_at is None
    assert agent_calls(world, "teardown") == []
    with world.store() as store:
        assert store.lease_info(lease_name(run.run_id)) is None  # released
    no_files_left(world)


# -- the wait -------------------------------------------------------------------------------------


def test_wait_exiting_12_three_times_leaves_the_run_running(world: World) -> None:
    world.fb.use("agent-spawn", "wait_error", exit_code=12, code="failed")
    with pytest.raises(ReviewRunError) as info:
        world.run()
    err = info.value
    assert (err.exit_code, err.reason, err.tool, err.code) == (
        12,
        "lower_failed",
        "agent-spawn",
        "failed",
    )
    assert len(agent_calls(world, "wait")) == 3 and world.sleeps == [2.0, 2.0]
    run = world.only_run()
    assert (run.state, run.phase, run.agent_id is not None) == ("running", "session", True)
    assert agent_calls(world, "teardown") == []


def test_wait_garbage_output_counts_as_a_failed_attempt(world: World) -> None:
    world.fb.script("agent-spawn", "wait", {"stdout_raw": "garbage", "schema": None})
    with pytest.raises(ReviewRunError) as info:
        world.run()
    assert info.value.lower_code == "invalid_output" and len(agent_calls(world, "wait")) == 3


def test_wait_for_an_unknown_agent_ends_the_run_agent_gone(world: World) -> None:
    world.fb.use("agent-spawn", "wait_not_found")
    run = world.run()
    assert (run.state, run.reason, run.exit_code) == ("failed", "agent_gone", 12)
    assert len(agent_calls(world, "wait")) == 1 and agent_calls(world, "teardown") == []


def test_a_later_wait_attempt_can_answer(world: World) -> None:
    world.fb.script(
        "agent-spawn", "wait", {"error": {"code": "failed", "message": "x"}, "exit": 12}, {}
    )
    world.fb.use("agent-spawn", "outcome", outcome="needs-input")
    run = world.run()
    assert (run.state, run.reason) == ("escalated", "needs_input") and world.sleeps == [2.0]


def test_a_waiter_ended_by_a_signal_changes_nothing(world: World) -> None:
    world.fb.script(
        "agent-spawn",
        "wait",
        {"error": {"code": "internal", "message": "x"}, "exit": 143, "schema": None},
    )
    with pytest.raises(runner.Interrupted) as info:
        world.run()
    assert info.value.exit_code == 143
    assert world.only_run().state == "running"


def test_no_wait_returns_after_the_store_with_the_session_started(world: World) -> None:
    run = world.run(no_wait=True)
    assert (run.state, run.phase, run.agent_id is not None, run.awaited_run) == (
        "running",
        "session",
        True,
        1,
    )
    assert agent_calls(world, "wait") == [] and agent_calls(world, "teardown") == []
    no_files_left(world)
    assert (
        audit_records(world.root)[-1]["result"]["status"] == "attempted"
    )  # a later finalizer closes it


# -- the launch call ------------------------------------------------------------------------------


def test_the_launch_argv_follows_the_interface_and_never_carries_untrusted_text(
    world: World,
) -> None:
    world.fb.configure("forgectl", pr={"title": MARKER, "body": MARKER, "head": {"ref": "x"}})
    world.fb.use("agent-spawn", "outcome", outcome="needs-input")
    run = world.run(request={"labels": ("team=a",), "model": "haiku", "timeout": "20m"})
    (call,) = agent_calls(world, "launch")
    argv = call.argv
    for banned in ("--wait", "--ephemeral", "--worktree", "--prompt", "--dry-run"):
        assert banned not in argv
    assert argv.count("--no-implicit-done") == 1 and argv.count("--no-promote") == 1
    assert (
        argv[argv.index("--cwd") + 1] == "session-dir"
        and argv[argv.index("--kind") + 1] == "review"
    )
    assert argv[argv.index("--mode") + 1] == "headless"
    run_dir = world.root / "runs" / run.run_id
    assert argv[argv.index("--result-file") + 1] == str(run_dir / "launch.json")
    assert argv[argv.index("--prompt-file") + 1] == str(run_dir / "prompt.md")
    assert argv[argv.index("--permission-profile") + 1] == str(run_dir / "permission-profile.json")
    assert argv[argv.index("--plugin-dir") + 1] == str(world.plugin)
    labels = [argv[i + 1] for i, a in enumerate(argv) if a == "--label"]
    assert labels == ["owner=review-run", f"run={run.run_id}", f"pr={PR}", f"repo={REPO}", "team=a"]
    assert [argv[i + 1] for i, a in enumerate(argv) if a == "--input-file"] == [
        str(run_dir / "inputs" / "f0001.patch")
    ]
    # untrusted PR text is in no argv, env or cwd of any call of any sibling
    for recorded in world.fb.all_calls():
        assert MARKER not in json.dumps([recorded.argv, recorded.env, recorded.cwd])
    assert world.router.envs[-1] is None or MARKER not in json.dumps(world.router.envs[-1])
    # and it reached the session only inside the prompt files (deleted once launch returned)
    no_files_left(world)


def test_the_launcher_and_its_start_are_stored_around_the_call(world: World) -> None:
    world.fb.use("agent-spawn", "outcome", outcome="needs-input")
    run = world.run()
    (call,) = agent_calls(world, "launch")
    assert run.launch_started_at is not None
    assert run.launcher_pid == call.pid and run.launcher_start is not None
    assert run.launcher_start and run.runner_start
    assert run.launcher_start.rsplit(":", 1)[0] == run.runner_start.rsplit(":", 1)[0]  # same boot


def test_prompt_and_input_files_exist_while_launch_runs_and_are_private(world: World) -> None:
    seen: dict[str, Any] = {}

    def probe(argv: Any) -> None:
        if argv[1:2] == ["launch"]:
            base = Path(argv[argv.index("--prompt-file") + 1]).parent
            seen["prompt"] = (base / "prompt.md").read_text()
            seen["modes"] = {
                p.name: oct(p.stat().st_mode & 0o777)
                for p in [base / "prompt.md", *(base / "inputs").iterdir()]
            }
            seen["dir"] = oct(base.stat().st_mode & 0o777)

    world.router.on_run = probe
    world.fb.use("agent-spawn", "outcome", outcome="needs-input")
    world.run()
    assert '<untrusted source="pr"' in seen["prompt"] and "rr-" in seen["prompt"]
    assert set(seen["modes"].values()) == {"0o600"} and seen["dir"] == "0o700"


def test_a_plugin_directory_changed_since_the_run_was_created_is_launch_failed(
    world: World,
) -> None:
    plan = world.plan()
    (world.plugin / "extra.txt").write_text("changed", encoding="utf-8")
    run = runner.run_review(plan, world.root, world.options())
    assert (run.state, run.reason, run.exit_code) == ("failed", "launch_failed", 12)
    assert run.failure is not None and run.failure["lower_code"] == "plugin_changed"
    assert agent_calls(world, "launch") == [] and run.agent_id is None
    assert run_object(world)["failure"]["lower_code"] == "plugin_changed"
    no_files_left(world)


def test_a_launcher_ended_by_the_gateway_leaves_the_run_for_reconcile(world: World) -> None:
    world.fb.script(
        "agent-spawn", "launch", {"stdout_raw": "x" * (8 * 1024 * 1024 + 1), "schema": None}
    )
    with pytest.raises(ReviewRunError) as info:
        world.run()
    assert (info.value.reason, info.value.lower_code, info.value.exit_code) == (
        "lower_failed",
        "output_too_large",
        12,
    )
    run = world.only_run()
    assert (run.state, run.phase, run.agent_id) == ("running", "session", None)
    assert run.launch_started_at is not None  # reconcile adopts from launch.json or by labels
    no_files_left(world)


def test_launch_exit_one_or_garbage_ends_the_run_internal(world: World) -> None:
    world.fb.script("agent-spawn", "launch", {"stdout_raw": "not json", "exit": 1, "schema": None})
    run = world.run()
    assert (run.state, run.reason, run.exit_code) == ("failed", "internal", 12)


def test_a_launcher_that_cannot_start_ends_the_run_launch_failed(world: World) -> None:
    world.router.missing.add("agent-spawn")  # not on PATH once the plan was made
    run = world.run()
    assert (run.reason, run.failure["lower_code"]) == ("launch_failed", "tool_missing")  # type: ignore[index]
    assert run.agent_id is None


def test_an_interrupted_launcher_is_canceled_with_its_code(world: World) -> None:
    world.fb.script(
        "agent-spawn",
        "launch",
        {
            "error": {
                "code": "launch_failed",
                "message": "interrupted",
                "details": {"cause": "interrupted"},
            },
            "exit": 143,
        },
    )
    run = world.run()
    assert (run.state, run.reason, run.exit_code) == ("failed", "canceled", 143)


# -- steps before the launch ----------------------------------------------------------------------


def test_a_prefetch_failure_ends_the_run_with_its_reason_and_nothing_is_launched(
    world: World,
) -> None:
    world.fb.use("forgectl", "files_incomplete")
    run = world.run()
    assert (run.state, run.reason, run.exit_code, run.phase) == ("failed", "too_large", 12, None)
    assert run.failure is not None and run.failure["lower_code"] == "files_incomplete"
    assert agent_calls(world, "launch") == [] and run.agent_id is None
    assert [r["result"]["status"] for r in audit_records(world.root)] == ["attempted", "failed"]
    no_files_left(world)


def test_a_stale_head_ends_the_run_stale_head(world: World) -> None:
    world.fb.use("pr-comment", "stale_head")
    run = world.run()
    assert (run.reason, run.exit_code) == ("stale_head", 12)
    assert run.failure is not None and run.failure["retryable"] is True


def test_the_run_files_and_the_anchor_are_stored_before_the_launch(world: World) -> None:
    seen: dict[str, Any] = {}

    def probe(argv: Any) -> None:
        if argv[1:2] == ["launch"]:
            run = world.only_run()
            base = world.root / "runs" / run.run_id
            seen["files"] = sorted(p.name for p in base.iterdir())
            seen["state"] = (run.state, run.phase, run.draft_id is not None, run.anchor is not None)
            seen["runner"] = run.runner_pid

    world.router.on_run = probe
    world.fb.use("agent-spawn", "outcome", outcome="needs-input")
    world.run()
    assert seen["state"] == ("running", "session", True, True)
    assert {
        "settings.json",
        "permission-profile.json",
        "pr.json",
        "manifest.json",
        "prompt.md",
        "inputs",
    } <= set(seen["files"])
    assert seen["runner"] is not None


def test_a_crash_between_the_run_record_and_the_launch_leaves_a_consistent_run(
    world: World,
) -> None:
    class Crash(BaseException):
        pass

    def probe(argv: Any) -> None:
        if argv[1:2] == ["launch"]:
            raise Crash

    world.router.on_run = probe
    with pytest.raises(Crash):
        world.run()
    run = world.only_run()
    assert (run.state, run.phase, run.agent_id, run.launcher_pid) == (
        "running",
        "session",
        None,
        None,
    )
    assert run.launch_started_at is not None and run.runner_pid is not None
    assert run.decision_ids == [] and run.config_sha256


def test_a_recorded_cancellation_ends_the_run_canceled_before_the_launch(world: World) -> None:
    def probe(argv: Any) -> None:
        if argv[1:2] == ["draft"] or argv[0].endswith("pr-comment"):
            with world.store() as store:
                run = store.get_run(world.only_run().run_id)
                assert run is not None
                store.request_cancel(run.run_id, run.key, actor="human:bob", exit_code=143)

    world.router.on_run = probe
    run = world.run()
    assert (run.state, run.reason, run.exit_code) == ("failed", "canceled", 143)
    assert agent_calls(world, "launch") == []


def test_a_previous_run_is_passed_as_a_fenced_input(world: World) -> None:
    world.fb.use("agent-spawn", "outcome", outcome="needs-input")
    with world.store() as store:
        from review_run.store import Expect, NewRun

        rid = store.reserve_id()
        store.create_run(NewRun(rid, PR, REPO, "a" * 40, "headless", "review-run:review-pr"))
        data = b'{"meta": {"verdict": "comment"}, "findings": []}\n'
        import hashlib

        (store.run_dir(rid) / "review.json").write_bytes(data)
        lease = store.acquire_lease(lease_name(rid))
        assert lease is not None
        store.end_run(
            rid, Expect("running", "prefetch", None), lease=lease, state="done", reason=None, exit_code=0,
            fields={"artifact_sha256": hashlib.sha256(data).hexdigest(), "artifact_size": len(data)},
        )  # fmt: skip
    seen: list[str] = []
    world.router.on_run = lambda argv: seen.extend(
        a for a in argv if a.endswith("previous-review.txt")
    )
    world.run(request={"previous": rid})
    assert len(seen) == 1


# -- the lease ------------------------------------------------------------------------------------


def test_the_run_holds_its_lease_while_it_works_and_releases_it_after(world: World) -> None:
    held: list[Any] = []

    def probe(argv: Any) -> None:
        if argv[1:2] == ["wait"]:
            with world.store() as store:
                held.append(store.lease_info(lease_name(world.only_run().run_id)))

    world.router.on_run = probe
    world.fb.use("agent-spawn", "outcome", outcome="needs-input")
    run = world.run()
    (info,) = held
    assert info is not None and info.name == lease_name(run.run_id)
    with world.store() as store:
        assert store.lease_info(lease_name(run.run_id)) is None


def test_the_keeper_renews_until_the_lease_is_taken_over(tmp_path: Path) -> None:
    root = tmp_path / "state"
    with Store.open(root) as store:
        lease = store.acquire_lease("run:rr-aaaaaaaa", ttl_seconds=60)
        assert lease is not None
        keeper = runner.LeaseKeeper(root, lease, interval=0.02)
        keeper.start()
        first = store.lease_info("run:rr-aaaaaaaa")
        assert first is not None
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            info = store.lease_info("run:rr-aaaaaaaa")
            if info is not None and info.expires_at > first.expires_at:
                break
            time.sleep(0.02)
        else:
            pytest.fail("the lease was never renewed")
        assert not keeper.lost.is_set()
        store.release_lease(lease)  # taken over (here: gone): renew_lease returns None
        assert keeper.lost.wait(5)
        keeper.stop()
        assert not keeper._thread.is_alive()  # pyright: ignore[reportPrivateUsage]


def test_a_lost_lease_stops_the_run_from_writing(world: World) -> None:
    def steal(argv: Any) -> None:
        if argv[1:2] == ["launch"]:
            with world.store() as store:
                rid = world.only_run().run_id
                store._conn.execute("DELETE FROM leases WHERE name = ?", (lease_name(rid),))  # pyright: ignore[reportPrivateUsage]

    world.router.on_run = steal
    world.fb.use("agent-spawn", "outcome", outcome="needs-input")
    with pytest.raises(ReviewRunError) as info:
        world.run()
    assert info.value.code == "internal"
    run = world.only_run()
    assert run.state == "running"  # never ended by a process that lost its lease


# -- pre-run hooks and the audit pair -------------------------------------------------------------


def test_a_pre_run_veto_exits_11_with_no_run_record_and_a_reserved_id(world: World) -> None:
    with pytest.raises(ReviewRunError) as info:
        world.run(flag_hooks=("pre-run=raise SystemExit(1)",))
    err = info.value
    assert (err.code, err.exit_code, err.hook) == ("vetoed", 11, "pre-run")
    with world.store() as store:
        assert store.list_run_ids() == ([], False)
        (reserved,) = store._conn.execute("SELECT run_id FROM ids").fetchall()  # pyright: ignore[reportPrivateUsage]
    (record,) = audit_records(world.root)
    assert record["result"] == {"status": "vetoed", "reason": None, "exit_code": 11}
    assert record["target"]["run_id"] == reserved[0]
    assert agent_calls(world, "launch") == [] and world.fb.calls("pr-comment") == []


def test_a_pre_run_escalation_exits_13_unless_a_human_overrides_it(world: World) -> None:
    hook = ("pre-run=raise SystemExit(13)",)
    with pytest.raises(ReviewRunError) as info:
        world.run(flag_hooks=hook)
    assert (info.value.code, info.value.exit_code) == ("escalated", 13)
    assert audit_records(world.root)[-1]["result"]["status"] == "escalated"
    world.fb.use("agent-spawn", "outcome", outcome="needs-input")
    run = world.run(request={"override_escalation": True, "actor": "human:alice"}, flag_hooks=hook)
    assert run.state == "escalated"


def test_the_hook_payload_has_only_trusted_values_and_hook_decision_ids_reach_the_run(
    world: World,
) -> None:
    out = world.tmp / "payload.json"
    cmd = (
        f"import json,sys;d=json.load(sys.stdin);open({str(out)!r},'w').write(json.dumps(d));"
        "print(json.dumps({'decision_id':'d-hook'}))"
    )
    world.fb.configure("forgectl", pr={"title": MARKER})
    world.fb.use("agent-spawn", "outcome", outcome="needs-input")
    run = world.run(
        flag_hooks=(f"pre-run={cmd}",), caller=Caller("human:alice", "flag", False, ("d-user",), ())
    )
    payload = json.loads(out.read_text())
    assert MARKER not in json.dumps(payload)
    assert payload["object_id"] == run.run_id or payload["object_id"].startswith("rr-")
    assert payload["pr_facts"]["draft"] is False and set(payload["sizes"]) == {
        "additions",
        "deletions",
        "changed_files",
    }
    assert payload["sizes"]["changed_files"] is not None
    assert run.decision_ids == ["d-user", "d-hook"]
    (call,) = agent_calls(world, "launch")
    assert "--decision-id=d-user" in call.argv and "--decision-id=d-hook" in call.argv
    assert run.hooks == [f"pre-run={cmd}"]


def test_the_audit_pair_and_the_outbox_event_are_written_once(world: World) -> None:
    out = world.tmp / "post.json"
    cmd = f"import sys;open({str(out)!r},'w').write(sys.stdin.read())"
    world.fb.use("agent-spawn", "outcome", outcome="killed")
    run = world.run(flag_hooks=(f"post-run={cmd}",))
    records = audit_records(world.root)
    assert [r["action"] for r in records] == ["run", "teardown", "run"]
    teardown = records.pop(1)
    assert teardown["inputs"] == {"agent_id": run.agent_id}
    assert teardown["result"] == {"status": "done"}
    assert teardown["target"] == {"run_id": run.run_id, "pr": PR}
    assert records[0]["attempt_id"] == records[1]["attempt_id"]
    assert records[0]["result"]["status"] == "attempted" and records[1]["result"] == {
        "status": "failed",
        "reason": "killed",
        "exit_code": 15,
    }
    assert records[1]["event_id"] == f"{run.run_id}-post-run-1"
    payload = json.loads(out.read_text())
    assert (payload["state"], payload["reason"], payload["exit_code"], payload["agent_id"]) == (
        "failed",
        "killed",
        15,
        run.agent_id,
    )
    with world.store() as store:
        assert store.pending_events(run.run_id) == []
    for record in records:
        audit_mod.validate_audit_record(record)


def test_a_post_escalate_event_is_delivered_with_the_escalated_run_number(world: World) -> None:
    out = world.tmp / "esc.json"
    cmd = f"import sys;open({str(out)!r},'w').write(sys.stdin.read())"
    world.fb.use("agent-spawn", "outcome", outcome="blocked")
    run = world.run(flag_hooks=(f"post-escalate={cmd}",))
    payload = json.loads(out.read_text())
    assert payload["event_id"] == f"{run.run_id}-post-escalate-1" and payload["reason"] == "blocked"


def test_the_run_stores_its_labels_hooks_actor_and_config_hash(world: World) -> None:
    world.fb.use("agent-spawn", "outcome", outcome="needs-input")
    run = world.run(request={"labels": ("team=a", "team=a")}, flag_hooks=("post-run=pass",))
    assert run.labels == ("team=a", "team=a") and run.hooks == ["post-run=pass"]
    assert run.actor == "human:alice" and run.config_sha256 and run.head_sha == "a1" * 20
    assert (run.mode, run.skill) == ("headless", "review-run:review-pr")


def test_the_run_reads_its_stored_settings_not_the_plan(world: World) -> None:
    plan = world.plan()
    base_dir = world.root / "runs"
    seen: dict[str, Any] = {}

    def probe(argv: Any) -> None:
        if argv[1:2] == ["launch"]:
            seen["settings"] = (
                base_dir / argv[argv.index("--result-file") + 1].split("/")[-2] / "settings.json"
            ).exists()

    world.router.on_run = probe
    world.fb.use("agent-spawn", "outcome", outcome="needs-input")
    runner.run_review(plan, world.root, world.options())
    assert seen["settings"] is True


def test_an_invalid_flag_hook_is_a_usage_error_before_anything_is_reserved(world: World) -> None:
    plan = world.plan()
    with pytest.raises(ReviewRunError) as info:
        runner.run_review(plan, world.root, world.options(flag_hooks=("bogus",)))
    assert info.value.exit_code == 2
    assert not (world.root / "state.db").exists() or world.fb is not None
    with world.store() as store:
        assert store._conn.execute("SELECT count(*) FROM ids").fetchone()[0] == 0  # pyright: ignore[reportPrivateUsage]


def test_run_inputs_match_the_audit_schema(world: World) -> None:
    world.fb.use("agent-spawn", "outcome", outcome="needs-input")
    run = world.run()
    first = audit_records(world.root)[0]["inputs"]
    assert first["head_sha"] == run.head_sha and first["plugin_dirs_sha256"] is None
    assert first["permission_profile_sha256"] and first["session_sandbox"] == "required"


def test_the_fake_siblings_outputs_all_validate(world: World, fakebin: FakeBin) -> None:
    world.fb.use("agent-spawn", "outcome", outcome="needs-input")
    world.run()
    fakebin.assert_valid()


# -- review round 1: lease checks, post-launch failures, cancel mapping, teardown audit ------------


def teardown_records(world: World) -> list[dict[str, Any]]:
    return [r for r in audit_records(world.root) if r["action"] == "teardown"]


def test_a_failed_teardown_attempt_is_audited_as_failed_and_no_attempt_writes_nothing(
    world: World,
) -> None:
    world.fb.use("agent-spawn", "outcome", outcome="killed")
    world.fb.script(
        "agent-spawn", "teardown", {"error": {"code": "failed", "message": "x"}, "exit": 12}
    )
    world.run()
    (record,) = teardown_records(world)
    assert record["result"] == {"status": "failed"}


def test_a_launch_envelope_naming_a_look_alike_writes_no_teardown_record(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = pinned_run_id(monkeypatch)
    agent_id = make_agent(world, run_id, run_label="rr-bbbbbbbb")
    launch_envelope_with(world, agent_id)
    world.run()
    assert teardown_records(world) == []


@pytest.mark.parametrize("error", [sqlite3.OperationalError("database is locked"), OSError("disk")])
def test_a_store_error_after_the_launch_leaves_the_run_running_and_tears_nothing_down(
    world: World, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    real = Store.update_fields

    def failing(self: Store, run_id: str, expect: Any, /, **fields: Any) -> bool:
        if "agent_id" in fields:
            raise error
        return real(self, run_id, expect, **fields)

    monkeypatch.setattr(Store, "update_fields", failing)
    with pytest.raises(ReviewRunError) as info:
        world.run()
    assert info.value.code == "internal"
    run = world.only_run()
    assert (run.state, run.phase, run.agent_id) == ("running", "session", None)
    assert agent_calls(world, "teardown") == [] and agent_calls(world, "stop") == []


def test_a_lease_taken_during_prefetch_means_the_launcher_never_starts(world: World) -> None:
    def steal(argv: Any) -> None:
        if argv[0].endswith("pr-comment") and argv[1:3] == ["draft", "new"]:
            with world.store() as store:
                rid = world.only_run().run_id
                store._conn.execute("DELETE FROM leases WHERE name = ?", (lease_name(rid),))  # pyright: ignore[reportPrivateUsage]

    world.router.on_run = steal
    with pytest.raises(ReviewRunError):
        world.run()
    run = world.only_run()
    assert run.state == "running" and run.phase == "prefetch" and run.launch_started_at is None
    assert agent_calls(world, "launch") == []


def test_a_wait_result_naming_another_agent_never_overwrites_the_stored_ids(world: World) -> None:
    def other(argv: Any) -> None:
        if argv[1:2] == ["wait"]:
            run = world.only_run()
            doc = {
                "agent": {
                    "agent_id": "ag-bbbbbbbb", "csession_id": run.csession_id, "status": "live",
                    "run": 1, "kind": "review", "labels": [],
                },
                "phase": "session", "outcome": "killed", "end_reason": "stopped", "exit_code": 15,
                "run": {"run": 1}, "first_working_at": None, "timed_out": False, "warnings": [],
            }  # fmt: skip
            world.fb.script("agent-spawn", "wait", {"json": doc, "exit": 15, "schema": None})

    world.router.on_run = other
    world.fb.use("agent-spawn", "outcome", outcome="needs-input")
    before: dict[str, Any] = {}
    with pytest.raises(ReviewRunError) as info:
        world.run()
    assert info.value.reason == "lower_failed"
    run = world.only_run()
    assert run.state == "running" and run.agent_id != "ag-bbbbbbbb" and run.agent_id is not None
    assert before == {} and agent_calls(world, "teardown") == []


def test_a_row_moved_by_another_process_is_not_torn_down_by_this_one(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.fb.use("agent-spawn", "outcome", outcome="killed")
    monkeypatch.setattr(Store, "end_run", lambda self, *a, **k: False)
    run = world.run()
    assert run.state == "running" and run.teardown is None and run.warnings == []
    assert agent_calls(world, "teardown") == [] and teardown_records(world) == []


def cancel_during_wait(world: World) -> None:
    def probe(argv: Any) -> None:
        if argv[1:2] == ["wait"]:
            with world.store() as store:
                run = world.only_run()
                assert store.request_cancel(run.run_id, run.key, actor="human:bob", exit_code=143)

    world.router.on_run = probe


@pytest.mark.parametrize("outcome", ["needs-input", "killed"])
def test_a_cancellation_recorded_during_the_wait_ends_the_run_canceled_and_tears_down(
    world: World, outcome: str
) -> None:
    cancel_during_wait(world)
    world.fb.use("agent-spawn", "outcome", outcome=outcome)
    run = world.run()
    assert (run.state, run.reason, run.exit_code, run.failure) == ("failed", "canceled", 143, None)
    assert run.teardown == "done" and len(agent_calls(world, "teardown")) == 1
    assert run.escalated_at is None


def test_an_unmapped_session_result_ends_internal_and_tears_the_agent_down(world: World) -> None:
    def odd(argv: Any) -> None:
        if argv[1:2] == ["wait"]:
            run = world.only_run()
            doc = {
                "agent": {
                    "agent_id": run.agent_id, "csession_id": run.csession_id, "status": "parked",
                    "run": 1, "kind": "review",
                    "labels": ["owner=review-run", f"run={run.run_id}", f"pr={PR}", f"repo={REPO}"],
                },
                "phase": "session", "outcome": "failed", "end_reason": "normal", "exit_code": 0,
                "run": {"run": 1}, "first_working_at": None, "timed_out": False, "warnings": [],
            }  # fmt: skip
            world.fb.script("agent-spawn", "wait", {"json": doc, "exit": 0, "schema": None})

    world.router.on_run = odd
    run = world.run()
    assert (run.state, run.reason, run.exit_code) == ("failed", "internal", 12)
    assert run.agent_id is not None and run.teardown is not None


def test_a_launcher_ended_by_the_gateway_with_an_envelope_in_launch_json_is_launch_failed(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = pinned_run_id(monkeypatch)
    agent_id = make_agent(world, run_id)

    def write_file(argv: Any) -> None:
        if argv[1:2] == ["launch"]:
            path = Path(argv[argv.index("--result-file") + 1])
            path.write_text(
                json.dumps(
                    {"error": {"code": "launch_failed", "message": "x", "agent_id": agent_id}}
                )
            )

    world.router.on_run = write_file
    world.fb.script(
        "agent-spawn", "launch", {"stdout_raw": "x" * (8 * 1024 * 1024 + 1), "schema": None}
    )
    run = world.run()
    assert (run.state, run.reason, run.exit_code) == ("failed", "launch_failed", 12)
    assert run.agent_id == agent_id and run.teardown == "done"
    assert [c.argv[1] for c in agent_calls(world, "teardown")] == [agent_id]


def test_a_repo_with_run_pr_code_is_reviewed_without_a_code_run_and_says_so_in_the_log(
    world: World,
) -> None:
    world.config_text = f"repos:\n  {REPO}:\n    run_pr_code:\n      command: [make, test]\n"
    world.fb.use("agent-spawn", "outcome", outcome="needs-input")
    run = world.run()
    assert run.state == "escalated" and run.phase is None
    lines = [json.loads(x) for x in (world.root / "log.jsonl").read_text().splitlines()]
    assert any(r["event"] == "warning" and r["code"] == "code_run_pending" for r in lines)


# -- the lease keeper -------------------------------------------------------------------------------


def test_a_transient_renew_failure_does_not_lose_the_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    calls = {"n": 0}
    real = Store.renew_lease

    def flaky(self: Store, lease: Any, **kw: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(self, lease, **kw)

    monkeypatch.setattr(Store, "renew_lease", flaky)
    with Store.open(root) as store:
        lease = store.acquire_lease("run:rr-aaaaaaaa", ttl_seconds=60)
        assert lease is not None
        keeper = runner.LeaseKeeper(root, lease, interval=0.02)
        keeper.start()
        deadline = time.monotonic() + 5
        while calls["n"] < 3 and time.monotonic() < deadline:
            time.sleep(0.02)
        keeper.stop()
        assert calls["n"] >= 3 and not keeper.lost.is_set()


def test_consecutive_renew_failures_lose_the_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"

    def broken(self: Store, lease: Any, **kw: Any) -> Any:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(Store, "renew_lease", broken)
    with Store.open(root) as store:
        lease = store.acquire_lease("run:rr-aaaaaaaa", ttl_seconds=60)
        assert lease is not None
        keeper = runner.LeaseKeeper(root, lease, interval=0.02)
        keeper.start()
        assert keeper.lost.wait(5)
        keeper.stop()
