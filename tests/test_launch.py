"""Launch: the argv, result files, the mapping table, the wait and the parked teardown (#18)."""

# ruff: noqa: E501

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from review_run import launch as lc
from review_run import settings as rs
from review_run.errors import ReviewRunError
from review_run.gateway import (
    CallTimeout,
    CompletedRun,
    GatewayError,
    OutputTooLarge,
    ToolMissing,
)
from tests.fake_gateway import Call, FakeGateway

AG = "ag-aaaaaaaa"
CS = "cs-aaaaaaaa"
RUN = "rr-aaaaaaaa"
PRX = "github.com/o/r#1"
REPO = "github.com/o/r"


def agent_doc(**kw: Any) -> dict[str, Any]:
    return {
        "agent_id": AG,
        "csession_id": CS,
        "status": "live",
        "run": 1,
        "kind": "review",
        "labels": ["owner=review-run", f"run={RUN}", f"pr={PRX}", f"repo={REPO}"],
        **kw,
    }


def result_doc(
    outcome: str | None = None, phase: str | None = None, exit_code: int | None = None, **kw: Any
) -> dict[str, Any]:
    final = outcome is not None
    return {
        "agent": agent_doc(**kw),
        "phase": phase,
        "outcome": outcome,
        "end_reason": "normal" if final else None,
        "exit_code": exit_code,
        "run": {"run": 1} if final else None,
        "first_working_at": None,
        "timed_out": False,
        "warnings": [],
    }


def envelope_doc(code: str = "failed", **kw: Any) -> dict[str, Any]:
    return {"error": {"code": code, "message": "text that is never copied", **kw}}


def parsed(exit_code: int, doc: dict[str, Any] | None) -> lc.SpawnResult | None:
    return lc.parse_result(exit_code, doc)


# -- the argv ------------------------------------------------------------------------------------


def _settings(tmp_path: Path, **config: object) -> rs.RunSettings:
    from review_run.config import load_config

    path = tmp_path / "c.yaml"
    path.write_text("".join(f"{k}: {v}\n" for k, v in config.items()), encoding="utf-8")
    path.chmod(0o600)
    resolved = load_config(path).resolve(REPO, {})
    profile, _ = rs.effective_profile(resolved.values)
    return rs.build_settings(
        resolved,
        plugin_dirs=("/p/bundled",),
        plugin_dirs_hash="a" * 64,
        profile=profile,
    )


def test_launch_argv_has_exactly_the_flags_of_the_table(tmp_path: Path) -> None:
    s = _settings(tmp_path, model="haiku", timeout="30m", max_turns=7, nits="true")
    run_dir = tmp_path / "runs" / RUN
    argv = lc.launch_argv(
        s,
        repo=REPO,
        pr=PRX,
        run_id=RUN,
        run_dir=run_dir,
        input_paths=[run_dir / "inputs" / "f0001.patch", run_dir / "inputs" / "f0002.patch"],
        labels=["team=x"],
        actor="human:alice",
        decision_ids=["-d1"],
    )
    assert argv[:2] == ["agent-spawn", "launch"]
    flags = [a for a in argv[2:] if a.startswith("--")]
    assert set(flags) == {
        "--mode", "--kind", "--no-implicit-done", "--cwd", "--repo", "--skill", "--plugin-dir",
        "--permission-profile", "--sandbox", "--setting-sources", "--env-allowlist-only",
        "--model", "--timeout", "--max-turns", "--no-promote", "--label", "--result-file",
        "--json", "--actor", "--decision-id=-d1", "--prompt-file", "--input-file",
    }  # fmt: skip
    pairs = {argv[i]: argv[i + 1] for i in range(2, len(argv) - 1) if argv[i].startswith("--")}
    assert pairs["--kind"] == "review" and pairs["--cwd"] == "session-dir"
    assert pairs["--result-file"] == str(run_dir / "launch.json")
    assert pairs["--prompt-file"] == str(run_dir / "prompt.md")
    assert pairs["--permission-profile"] == str(run_dir / "permission-profile.json")
    assert (pairs["--model"], pairs["--timeout"], pairs["--max-turns"]) == ("haiku", "30m", "7")
    assert argv.count("--input-file") == 2
    assert [argv[i + 1] for i, a in enumerate(argv) if a == "--label"] == [
        "owner=review-run", f"run={RUN}", f"pr={PRX}", f"repo={REPO}", "team=x",
    ]  # fmt: skip


def test_the_argv_never_has_wait_ephemeral_worktree_or_a_prompt(tmp_path: Path) -> None:
    argv = lc.launch_argv(
        _settings(tmp_path), repo=REPO, pr=PRX, run_id=RUN, run_dir=tmp_path, input_paths=[]
    )
    for banned in (
        "--wait",
        "--wait-timeout",
        "--ephemeral",
        "--worktree",
        "--prompt",
        "--dry-run",
    ):
        assert banned not in argv
    assert all(a.startswith("--") or not a.startswith("-") for a in argv)


def test_a_relative_launch_path_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        lc.launch_argv(
            _settings(tmp_path), repo=REPO, pr=PRX, run_id=RUN, run_dir="runs/x", input_paths=[]
        )


def test_wait_argv_and_timeout(tmp_path: Path) -> None:
    assert lc.wait_argv(AG, tmp_path) == [
        "agent-spawn", "wait", AG, "--result-file", str(tmp_path / "wait.json"), "--json",
    ]  # fmt: skip
    with pytest.raises(ValueError):
        lc.wait_argv("--evil", tmp_path)
    assert lc.wait_timeout_seconds(_settings(tmp_path)) == 120 * 60 + lc.WAIT_MARGIN_SECONDS
    assert (
        lc.wait_timeout_seconds(_settings(tmp_path, timeout="30m")) == 1800 + lc.WAIT_MARGIN_SECONDS
    )
    assert lc.wait_timeout_seconds(_settings(tmp_path, timeout="30d")) == (
        lc.UNLIMITED_SESSION_SECONDS + lc.WAIT_MARGIN_SECONDS
    )


# -- result files --------------------------------------------------------------------------------


def write(path: Path, data: bytes | str) -> Path:
    path.write_bytes(data if isinstance(data, bytes) else data.encode())
    return path


def test_a_result_file_is_read(tmp_path: Path) -> None:
    doc = result_doc()
    assert lc.read_result_file(write(tmp_path / "launch.json", json.dumps(doc))) == doc


@pytest.mark.parametrize(
    "content",
    [
        pytest.param('{"agent": {"agent_id": "ag-aaaaaaaa"', id="truncated"),
        pytest.param("", id="empty"),
        pytest.param("[1]", id="array"),
        pytest.param('{"a": 1, "a": 2}', id="duplicate-key"),
        pytest.param('{"a": NaN}', id="nan"),
        pytest.param('{"a": 1e999}', id="infinity"),
        pytest.param(b'{"a": "\xff"}', id="not-utf8"),
        pytest.param(b"\xef\xbb\xbf{}", id="bom"),
        pytest.param("{}" + " " * (lc.RESULT_MAX_BYTES + 1), id="over-one-mib"),
    ],
)
def test_a_result_file_that_cannot_be_used_counts_as_absent(
    tmp_path: Path, content: bytes | str
) -> None:
    assert lc.read_result_file(write(tmp_path / "launch.json", content)) is None


def test_a_result_file_of_exactly_one_mib_is_read(tmp_path: Path) -> None:
    body = b"{}" + b" " * (lc.RESULT_MAX_BYTES - 2)
    assert lc.read_result_file(write(tmp_path / "x.json", body)) == {}


def test_a_symlink_a_fifo_a_directory_and_a_missing_file_are_absent(tmp_path: Path) -> None:
    target = write(tmp_path / "real.json", "{}")
    (tmp_path / "link.json").symlink_to(target)
    os.mkfifo(tmp_path / "fifo.json")
    (tmp_path / "dir.json").mkdir()
    for name in ("link.json", "fifo.json", "dir.json", "missing.json"):
        assert lc.read_result_file(tmp_path / name) is None


def test_stdout_wins_and_the_file_is_the_fallback(tmp_path: Path) -> None:
    file = write(tmp_path / "launch.json", json.dumps(result_doc()))
    assert lc.interpret(0, "{not json", file) is not None
    assert lc.interpret(0, "{not json", tmp_path / "none.json") is None
    other = envelope_doc("launch_failed")
    got = lc.interpret(12, json.dumps(other), file)
    assert got is not None and got.envelope is not None
    assert lc.interpret_file(file) is not None
    write(file, '{"agent":')
    assert lc.interpret_file(file) is None


# -- parsing -------------------------------------------------------------------------------------


def test_a_started_run_parses() -> None:
    got = parsed(0, result_doc())
    assert got is not None and not got.final
    assert (got.agent_id, got.csession_id, got.agent_run) == (AG, CS, 1)


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda d: d["agent"].update(agent_id="ag-short"), id="agent-id"),
        pytest.param(lambda d: d["agent"].update(csession_id="x"), id="csession-id"),
        pytest.param(lambda d: d.update(phase="elsewhere"), id="phase"),
        pytest.param(lambda d: d.update(outcome="exploded"), id="outcome"),
        pytest.param(lambda d: d.update(agent="no"), id="agent-not-an-object"),
        pytest.param(
            lambda d: d.update(run={"run": 0}, phase="session", outcome="done"), id="run-0"
        ),
        pytest.param(
            lambda d: d.update(run={"run": True}, phase="session", outcome="done"), id="run-bool"
        ),
        pytest.param(lambda d: d.update(run=5), id="run-not-an-object"),
    ],
)
def test_a_result_off_the_closed_shape_is_absent(mutate: Any) -> None:
    doc = result_doc()
    mutate(doc)
    assert parsed(0, doc) is None


def test_a_final_run_whose_exit_code_differs_from_the_process_is_absent() -> None:
    assert parsed(0, result_doc("done", "session", 14)) is None
    assert parsed(14, result_doc("done", "session", 14)) is not None


def test_an_envelope_keeps_only_closed_words() -> None:
    doc = envelope_doc(
        "failed",
        reason="Not A Word",
        tool="agent-spawn",
        lower_code="tmux_missing",
        agent_id="ag-bbbbbbbb",
        details={"retryable": True, "cause": "interrupted", "hint": "free text"},
        lower_error={"message": "free text"},
    )
    got = parsed(12, doc)
    assert got is not None and got.envelope is not None
    env = got.envelope
    assert (env.code, env.reason, env.tool, env.lower_code) == (
        "failed",
        None,
        "agent-spawn",
        "tmux_missing",
    )
    assert (env.agent_id, env.retryable, env.cause) == ("ag-bbbbbbbb", True, "interrupted")
    assert "free text" not in repr(got)


def test_an_envelope_without_a_code_is_absent() -> None:
    assert parsed(12, {"error": {"message": "x"}}) is None
    assert parsed(12, {"error": "x"}) is None
    assert parsed(12, {"something": 1}) is None
    assert parsed(12, None) is None


# -- the mapping table ---------------------------------------------------------------------------

SESSION_ROWS = [
    (0, "done", "finalize", None, None),
    (13, "needs-input", "escalate", "needs_input", 13),
    (13, "blocked", "escalate", "blocked", 13),
    (14, "exited-unreported", "fail", "unreported", 14),
    (15, "killed", "fail", "killed", 15),
    (16, "timeout", "fail", "timeout", 16),
    (17, "lost", "fail", "lost", 17),
    (18, "failed", "fail", "session_failed", 18),
]


@pytest.mark.parametrize(("code", "outcome", "kind", "reason", "exit_code"), SESSION_ROWS)
def test_every_session_row_maps_for_launch_and_wait(
    code: int, outcome: str, kind: str, reason: str | None, exit_code: int | None
) -> None:
    result = parsed(code, result_doc(outcome, "session", code))
    for disposition in (lc.map_launch(result, code), lc.map_wait(result, code)):
        assert (disposition.kind, disposition.reason, disposition.exit_code) == (
            kind,
            reason,
            exit_code,
        )
        assert disposition.agent == result


@pytest.mark.parametrize(
    ("code", "outcome"),
    [
        (0, "failed"),
        (13, "done"),
        (11, "done"),
        (12, "done"),
        (22, "done"),
        (19, "failed"),
        (1, "done"),
    ],
)
def test_a_session_outcome_off_the_table_is_internal(code: int, outcome: str) -> None:
    result = parsed(code, result_doc(outcome, "session", code))
    assert (lc.map_wait(result, code).kind, lc.map_wait(result, code).reason) == (
        "fail",
        "internal",
    )
    assert lc.map_launch(result, code).reason == "internal"


@pytest.mark.parametrize(
    ("outcome", "code", "lower", "end_reason", "tool"),
    [
        ("not-started", 12, "not_started", "normal", "claude-session"),
        ("vetoed", 11, "vetoed", "normal", "agent-spawn"),
        ("escalated", 13, "escalated", "normal", "agent-spawn"),
    ],
)
def test_a_run_ended_in_its_launch_phase_is_launch_failed(
    outcome: str, code: int, lower: str, end_reason: str, tool: str
) -> None:
    result = parsed(code, result_doc(outcome, "launch", code))
    for disposition in (lc.map_launch(result, code), lc.map_wait(result, code)):
        assert (disposition.kind, disposition.reason, disposition.exit_code) == (
            "fail",
            "launch_failed",
            12,
        )
        assert disposition.failure == {
            "tool": tool, "lower_code": lower, "lower_reason": end_reason,
            "retryable": False, "retry_after_s": None, "errors": [],
        }  # fmt: skip
        assert disposition.teardown_agent == AG


@pytest.mark.parametrize("code", [2, 10, 11, 12, 13, 20, 21])
def test_an_envelope_at_launch_is_launch_failed_with_closed_codes(code: int) -> None:
    result = parsed(
        code,
        envelope_doc(
            "failed",
            tool="termctl",
            lower_code="tmux_missing",
            reason="third_party_cwd",
            details={"retryable": True},
        ),  # fmt: skip
    )
    d = lc.map_launch(result, code)
    assert (d.kind, d.reason, d.exit_code) == ("fail", "launch_failed", 12)
    assert d.failure is not None
    assert (
        d.failure["lower_code"] == "tmux_missing" and d.failure["lower_reason"] == "third_party_cwd"
    )
    assert (
        d.failure["tool"] is None and d.failure["retryable"] is True
    )  # termctl is not a failure tool
    assert d.teardown_agent is None


def test_an_envelope_with_an_agent_id_names_the_agent_to_tear_down() -> None:
    d = lc.map_launch(parsed(12, envelope_doc("launch_failed", agent_id=AG)), 12)
    assert d.teardown_agent == AG
    assert d.failure is not None and d.failure["tool"] == "agent-spawn"
    assert d.failure["lower_code"] == "launch_failed"


@pytest.mark.parametrize("code", [130, 143])
def test_an_interrupted_launcher_is_canceled_with_its_own_code(code: int) -> None:
    for result in (
        None,
        parsed(code, envelope_doc("launch_failed", details={"cause": "interrupted"})),
    ):
        d = lc.map_launch(result, code)
        assert (d.kind, d.reason, d.exit_code) == ("fail", "canceled", code)


@pytest.mark.parametrize("code", [1, 22, 12, 0])
def test_launch_exit_one_or_unparsable_output_is_internal(code: int) -> None:
    assert lc.map_launch(None, code).reason == "internal"
    assert lc.map_launch(None, code).exit_code == 12
    if code in (0, 1, 22):
        assert lc.map_launch(parsed(code, envelope_doc("internal")), code).reason == "internal"


def test_a_started_run_without_a_session_is_internal() -> None:
    assert lc.map_launch(parsed(0, result_doc(csession_id=None)), 0).reason == "internal"
    assert lc.map_launch(parsed(0, result_doc(run=None)), 0).reason == "internal"
    assert lc.map_launch(parsed(0, result_doc()), 0).kind == "started"


def test_wait_exit_10_and_a_torn_down_agent_are_agent_gone() -> None:
    gone = lc.map_wait(parsed(10, envelope_doc("not_found")), 10)
    assert (gone.kind, gone.reason, gone.exit_code) == ("agent_gone", "agent_gone", 12)
    torn = lc.map_wait(parsed(0, result_doc("done", "session", 0, status="torn_down")), 0)
    assert (torn.kind, torn.reason) == ("agent_gone", "agent_gone")


def test_wait_exit_22_is_internal() -> None:
    assert lc.map_wait(parsed(22, envelope_doc("internal")), 22).reason == "internal"
    timed = result_doc()
    timed["timed_out"] = True
    assert lc.map_wait(parsed(22, timed), 22).reason == "internal"


@pytest.mark.parametrize("code", [1, 2, 11, 12, 13, 20, 21])
def test_any_other_wait_envelope_or_garbage_is_a_retry(code: int) -> None:
    assert lc.map_wait(parsed(code, envelope_doc("failed")), code).kind == "retry"
    assert lc.map_wait(None, code).kind == "retry"


@pytest.mark.parametrize("code", [130, 143])
def test_a_waiter_ended_by_a_signal_is_interrupted(code: int) -> None:
    d = lc.map_wait(None, code)
    assert (d.kind, d.exit_code) == ("interrupted", code)


# -- the wait ------------------------------------------------------------------------------------


def _wait_fake(
    tmp_path: Path, *results: CompletedRun | Exception
) -> tuple[FakeGateway, list[float]]:
    fake = FakeGateway()
    fake.script("agent-spawn", *results)  # type: ignore[arg-type]
    return fake, []


def ok(code: int, doc: dict[str, Any] | None, text: str | None = None) -> CompletedRun:
    out = text if text is not None else ("" if doc is None else json.dumps(doc))
    return CompletedRun(("agent-spawn",), code, out, "")


def test_a_result_ends_the_wait_at_once(tmp_path: Path, fake_gateway: FakeGateway) -> None:
    fake_gateway.script("agent-spawn", ok(0, result_doc("done", "session", 0)))
    sleeps: list[float] = []
    d = lc.wait_session(AG, tmp_path, timeout=5, sleep=sleeps.append)
    assert d.kind == "finalize" and sleeps == []
    call = fake_gateway.calls[0]
    assert call.argv == (
        "agent-spawn",
        "wait",
        AG,
        "--result-file",
        str(tmp_path / "wait.json"),
        "--json",
    )
    assert call.timeout == 5


def test_wait_retries_three_times_two_seconds_apart_then_fails_lower_failed(
    tmp_path: Path, fake_gateway: FakeGateway
) -> None:
    fake_gateway.script(
        "agent-spawn",
        ok(12, envelope_doc("failed", lower_code="no_run", details={"retryable": True})),
    )
    sleeps: list[float] = []
    with pytest.raises(ReviewRunError) as info:
        lc.wait_session(AG, tmp_path, timeout=5, sleep=sleeps.append)
    err = info.value
    assert (err.code, err.reason, err.tool, err.lower_code, err.exit_code) == (
        "failed",
        "lower_failed",
        "agent-spawn",
        "no_run",
        12,
    )
    assert err.details == {"retryable": True}
    assert len(fake_gateway.calls) == 3 and sleeps == [2.0, 2.0]
    assert "text that is never copied" not in json.dumps(err.envelope())


def test_wait_recovers_when_a_later_attempt_answers(
    tmp_path: Path, fake_gateway: FakeGateway
) -> None:
    fake_gateway.script(
        "agent-spawn",
        ok(12, envelope_doc()),
        ok(0, None, "garbage"),
        ok(15, result_doc("killed", "session", 15)),
    )
    d = lc.wait_session(AG, tmp_path, timeout=5, sleep=lambda _s: None)
    assert (d.kind, d.reason) == ("fail", "killed") and len(fake_gateway.calls) == 3


@pytest.mark.parametrize(
    ("failure", "lower"),
    [(CallTimeout("t"), "timeout"), (OutputTooLarge("big"), "output_too_large")],
)
def test_a_wait_that_timed_out_or_overflowed_fails_at_once(
    tmp_path: Path, fake_gateway: FakeGateway, failure: Exception, lower: str
) -> None:
    fake_gateway.script("agent-spawn", failure)  # type: ignore[arg-type]
    with pytest.raises(ReviewRunError) as info:
        lc.wait_session(AG, tmp_path, timeout=5, sleep=lambda _s: None)
    assert info.value.lower_code == lower and len(fake_gateway.calls) == 1


def test_a_failed_wait_call_is_an_attempt(tmp_path: Path, fake_gateway: FakeGateway) -> None:
    fake_gateway.script("agent-spawn", GatewayError("x"))  # type: ignore[arg-type]
    with pytest.raises(ReviewRunError):
        lc.wait_session(AG, tmp_path, timeout=5, sleep=lambda _s: None)
    assert len(fake_gateway.calls) == 3


@pytest.mark.parametrize(
    "kw",
    [{"agent_id": "ag-bbbbbbbb"}, {"csession_id": "cs-bbbbbbbb"}],
    ids=["other-agent", "other-session"],
)
def test_a_wait_result_that_is_not_the_stored_session_is_unusable(
    tmp_path: Path, fake_gateway: FakeGateway, kw: dict[str, Any]
) -> None:
    fake_gateway.script("agent-spawn", ok(15, result_doc("killed", "session", 15, **kw)))
    with pytest.raises(ReviewRunError) as info:
        lc.wait_session(AG, tmp_path, timeout=5, sleep=lambda _s: None, csession_id=CS)
    assert info.value.lower_code == "invalid_output" and len(fake_gateway.calls) == 3


def test_a_wait_result_of_another_run_number_is_unusable(
    tmp_path: Path, fake_gateway: FakeGateway
) -> None:
    doc = result_doc("killed", "session", 15)
    doc["run"] = {"run": 2}
    fake_gateway.script("agent-spawn", ok(15, doc))
    with pytest.raises(ReviewRunError):
        lc.wait_session(AG, tmp_path, timeout=5, sleep=lambda _s: None, awaited_run=1)
    assert len(fake_gateway.calls) == 3


def test_an_internal_result_carries_its_agent_for_the_teardown() -> None:
    result = parsed(0, result_doc("failed", "session", 0))
    d = lc.map_wait(result, 0)
    assert (d.reason, d.agent) == ("internal", result) and "internal" in lc.TEARDOWN_REASONS
    started = parsed(0, result_doc(csession_id=None))
    assert lc.map_launch(started, 0).agent == started


def test_an_error_envelope_in_a_result_file_maps_like_a_launch_envelope(tmp_path: Path) -> None:
    write(tmp_path / "launch.json", json.dumps(envelope_doc("launch_failed", agent_id=AG)))
    found = lc.interpret_file(tmp_path / "launch.json")
    assert found is not None
    d = lc.map_launch(found, found.exit_code)
    assert (d.kind, d.reason, d.teardown_agent) == ("fail", "launch_failed", AG)


def test_wait_with_agent_spawn_missing_fails_at_once(
    tmp_path: Path, fake_gateway: FakeGateway
) -> None:
    fake_gateway.script("agent-spawn", ToolMissing("gone"))  # type: ignore[arg-type]
    with pytest.raises(ReviewRunError) as info:
        lc.wait_session(AG, tmp_path, timeout=5, sleep=lambda _s: None)
    assert info.value.lower_code == "tool_missing" and len(fake_gateway.calls) == 1


def test_a_stale_wait_file_never_stands_in_for_the_call(
    tmp_path: Path, fake_gateway: FakeGateway
) -> None:
    write(tmp_path / "wait.json", json.dumps(result_doc("done", "session", 0)))
    fake_gateway.script("agent-spawn", ok(12, None, "not json"))
    with pytest.raises(ReviewRunError):
        lc.wait_session(AG, tmp_path, timeout=5, sleep=lambda _s: None)
    assert not (tmp_path / "wait.json").exists()


def test_wait_falls_back_to_the_result_file(tmp_path: Path, fake_gateway: FakeGateway) -> None:
    def answer(call: Call) -> CompletedRun:
        write(tmp_path / "wait.json", json.dumps(result_doc("killed", "session", 15)))
        return CompletedRun(call.argv, 15, "{broken", "")

    fake_gateway.script("agent-spawn", answer)
    d = lc.wait_session(AG, tmp_path, timeout=5, sleep=lambda _s: None)
    assert (d.kind, d.reason) == ("fail", "killed")


# -- the ownership check and the parked teardown --------------------------------------------------


def show_ok(**kw: Any) -> CompletedRun:
    return CompletedRun(("agent-spawn",), 0, json.dumps({"agent": agent_doc(**kw)}), "")


def test_check_parked_verdicts(fake_gateway: FakeGateway) -> None:
    fake_gateway.script("agent-spawn", show_ok(status="parked"))
    verdict = lc.check_parked(AG, run_id=RUN, pr=PRX, repo=REPO, csession_id=CS)
    assert verdict == "owned" and [c.argv[1] for c in fake_gateway.calls] == ["show"]


def teardown(fake: FakeGateway, **kw: Any) -> lc.Teardown:
    return lc.teardown_parked(
        AG, run_id=RUN, pr=PRX, repo=REPO, csession_id=kw.pop("cs", None), **kw
    )


def test_a_parked_agent_of_this_run_is_torn_down_without_force(fake_gateway: FakeGateway) -> None:
    fake_gateway.script("agent-spawn", show_ok(status="parked"), ok(0, {"agent": agent_doc()}))
    assert teardown(fake_gateway, cs=CS, actor="auto:review-run") == "done"
    show, down = fake_gateway.calls
    assert show.argv == ("agent-spawn", "show", AG, "--json")
    assert down.argv == ("agent-spawn", "teardown", AG, "--json", "--actor", "auto:review-run")
    assert "--force" not in down.argv
    assert down.timeout == lc.TEARDOWN_TIMEOUT


@pytest.mark.parametrize(
    "kw",
    [
        {"labels": ["owner=review-run", "run=rr-bbbbbbbb", f"pr={PRX}", f"repo={REPO}"]},
        {"labels": ["owner=someone", f"run={RUN}", f"pr={PRX}", f"repo={REPO}"]},
        {"labels": [f"run={RUN}"]},
        {"kind": "implement"},
        {"csession_id": "cs-bbbbbbbb"},
    ],
    ids=["other-run", "other-owner", "missing-labels", "other-kind", "other-session"],
)
def test_a_look_alike_agent_is_never_torn_down(
    fake_gateway: FakeGateway, kw: dict[str, Any]
) -> None:
    fake_gateway.script("agent-spawn", show_ok(status="parked", **kw))
    assert teardown(fake_gateway, cs=CS) == "mismatch"
    assert [c.argv[1] for c in fake_gateway.calls] == ["show"]


def test_a_live_agent_is_never_torn_down(fake_gateway: FakeGateway) -> None:
    fake_gateway.script("agent-spawn", show_ok(status="live"))
    assert teardown(fake_gateway) == "live" and len(fake_gateway.calls) == 1


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        ([ok(10, envelope_doc("not_found"))], "gone"),
        ([show_ok(status="torn_down")], "gone"),
        ([ok(12, None, "garbage")], "failed"),
        ([CallTimeout("t")], "failed"),
        ([show_ok(status="parked"), ok(20, envelope_doc("refused", reason="live"))], "refused"),
        ([show_ok(status="parked"), ok(12, envelope_doc("failed"))], "failed"),
        ([show_ok(status="parked"), ok(10, envelope_doc("not_found"))], "gone"),
        ([show_ok(status="parked"), CallTimeout("t")], "failed"),
    ],
)
def test_teardown_results(fake_gateway: FakeGateway, script: list[Any], expected: str) -> None:
    fake_gateway.script("agent-spawn", *script)
    assert teardown(fake_gateway) == expected
