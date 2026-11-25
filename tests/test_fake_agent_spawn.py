"""The fake agent-spawn and the fake claude-session it shares sessions with."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import stat
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from review_run.gateway import CallTimeout
from tests.fake_scenarios import OUTCOME_ROWS
from tests.fakebin._sessions import OUTCOMES
from tests.fakes import FakeBin

LAUNCH_EXTRA = ["--no-implicit-done", "--no-promote", "--sandbox", "required",
                "--setting-sources", "", "--env-allowlist-only", "", "--label", "owner=review-run",
                "--label", "run=rr-abcd2345", "--label", "pr=github.com/acme/widgets#7",
                "--label", "repo=github.com/acme/widgets", "--actor", "auto:review-run",
                "--decision-id", "d1", "--json"]  # fmt: skip


def run(fb: FakeBin, tool: str, *argv: str, stdin: str | None = None) -> tuple[int, Any, str]:
    result = fb.run(tool, *argv, stdin=stdin)
    try:
        doc = json.loads(result.stdout)
    except ValueError:
        doc = None
    return result.returncode, doc, result.stderr


@pytest.fixture
def inputs(tmp_path: Path) -> dict[str, Path]:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("review it", encoding="utf-8")
    patch = tmp_path / "f0001.patch"
    patch.write_text("diff", encoding="utf-8")
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    profile = tmp_path / "permission-profile.json"
    profile.write_text("{}", encoding="utf-8")
    (tmp_path / "runs").mkdir()
    return {"prompt": prompt, "patch": patch, "plugin": plugin, "profile": profile,
            "result": tmp_path / "runs" / "launch.json"}  # fmt: skip


def launch_argv(inputs: dict[str, Path], *extra: str, mode: str = "headless") -> list[str]:
    return ["launch", "--mode", mode, "--kind", "review", "--no-implicit-done", "--cwd",
            "session-dir", "--repo", "github.com/acme/widgets", "--skill", "review-run:review-pr",
            "--plugin-dir", str(inputs["plugin"]), "--prompt-file", str(inputs["prompt"]),
            "--input-file", str(inputs["patch"]), "--permission-profile", str(inputs["profile"]),
            "--sandbox", "required", "--setting-sources", "", "--env-allowlist-only", "",
            "--no-promote", "--label", "owner=review-run", "--label", "run=rr-abcd2345",
            "--result-file", str(inputs["result"]), "--json", "--actor", "auto:review-run",
            *extra]  # fmt: skip


def launch(fb: FakeBin, inputs: dict[str, Path], *extra: str, mode: str = "headless") -> dict[str, Any]:
    code, doc, err = run(fb, "agent-spawn", *launch_argv(inputs, *extra, mode=mode))
    assert code == 0, (doc, err)
    return doc


def aid(doc: dict[str, Any]) -> str:
    return doc["agent"]["agent_id"]


# ---- launch, wait and the outcome table -------------------------------------------------------


def test_launch_without_wait_returns_a_live_agent_and_no_run(fakebin: FakeBin, inputs: Any) -> None:
    doc = launch(fakebin, inputs)
    assert doc["run"] is None and doc["phase"] is None and doc["exit_code"] is None
    assert doc["agent"]["status"] == "live" and doc["agent"]["run"] == 1
    assert doc["agent"]["kind"] == "review" and doc["agent"]["mode"] == "headless"
    assert doc["agent"]["labels"] == ["owner=review-run", "run=rr-abcd2345"]
    assert json.loads(inputs["result"].read_text()) == doc


@pytest.mark.parametrize("outcome", [o for o in OUTCOME_ROWS if OUTCOMES[o][3] == "session"])
def test_wait_ends_the_run_as_the_table_says(fakebin: FakeBin, inputs: Any, outcome: str) -> None:
    fakebin.use("agent-spawn", "outcome", outcome=outcome)
    agent = aid(launch(fakebin, inputs))
    code, doc, _ = run(fakebin, "agent-spawn", "wait", agent, "--json")
    expected = OUTCOMES[outcome][0]
    assert code == expected and doc["exit_code"] == expected and doc["outcome"] == outcome
    assert doc["phase"] == "session" and doc["run"]["run"] == 1 and doc["timed_out"] is False
    assert doc["agent"]["status"] == "parked"
    report = doc["run"]["report"]
    if outcome in ("done", "blocked", "needs-input", "failed"):
        assert report["status"] == outcome
    else:
        assert report is None
    # waiting again on a parked agent returns the same outcome at once
    assert run(fakebin, "agent-spawn", "wait", agent, "--json")[0] == expected


@pytest.mark.parametrize(
    ("outcome", "exit_code"), [("vetoed", 11), ("escalated", 13), ("not-started", 12)]
)
def test_a_launch_phase_run_comes_back_from_launch_itself(
    fakebin: FakeBin, inputs: Any, outcome: str, exit_code: int
) -> None:
    fakebin.use("agent-spawn", "outcome", outcome=outcome)
    code, doc, _ = run(fakebin, "agent-spawn", *launch_argv(inputs))
    assert code == exit_code and doc["phase"] == "launch" and doc["outcome"] == outcome
    assert doc["agent"]["status"] == "parked"
    assert json.loads(inputs["result"].read_text()) == doc


def test_done_reports_a_frozen_artifact_whose_size_and_hash_are_true(fakebin: FakeBin, inputs: Any) -> None:
    fakebin.use("agent-spawn", "outcome", outcome="done", artifact_text='{"hello": "world"}\n')
    _, doc, _ = run(fakebin, "agent-spawn", "wait", aid(launch(fakebin, inputs)), "--json")
    art = doc["run"]["report"]["artifact"]
    data = Path(art["path"]).read_bytes()
    assert art["name"] == "review.json" and art["size"] == len(data)
    assert art["sha256"] == hashlib.sha256(data).hexdigest()


def test_artifact_scenarios(fakebin: FakeBin, inputs: Any) -> None:
    fakebin.use("agent-spawn", "artifact_mismatch")
    _, doc, _ = run(fakebin, "agent-spawn", "wait", aid(launch(fakebin, inputs)), "--json")
    art = doc["run"]["report"]["artifact"]
    data = Path(art["path"]).read_bytes()
    assert art["size"] != len(data) and art["sha256"] != hashlib.sha256(data).hexdigest()
    fakebin.use("agent-spawn", "no_artifact")
    _, doc, _ = run(fakebin, "agent-spawn", "wait", aid(launch(fakebin, inputs)), "--json")
    assert doc["run"]["report"]["artifact"] is None
    fakebin.use("agent-spawn", "implicit_done")
    _, doc, _ = run(fakebin, "agent-spawn", "wait", aid(launch(fakebin, inputs)), "--json")
    assert doc["run"]["report"]["implicit"] is True


def test_unreported_recovered_by_the_automatic_report_resume(fakebin: FakeBin, inputs: Any) -> None:
    fakebin.use("agent-spawn", "unreported_recovered")
    agent = aid(launch(fakebin, inputs))
    code, doc, _ = run(fakebin, "agent-spawn", "wait", agent, "--json")
    assert code == 0 and doc["run"]["run"] == 2 and doc["run"]["auto_resumed"] is True
    _, shown, _ = run(fakebin, "agent-spawn", "show", agent, "--json")
    assert [r["run"] for r in shown["runs"]] == [1, 2]
    assert shown["runs"][0]["outcome"]["outcome"] == "exited-unreported"


def test_wait_timeout_exits_22(fakebin: FakeBin, inputs: Any) -> None:
    fakebin.use("agent-spawn", "never_ends")
    agent = aid(launch(fakebin, inputs))
    code, doc, _ = run(fakebin, "agent-spawn", "wait", agent, "--timeout", "0.1s", "--json")
    assert code == 22 and doc["timed_out"] is True and doc["run"] is None


def test_launch_wait_blocks_to_the_final_outcome(fakebin: FakeBin, inputs: Any) -> None:
    fakebin.use("agent-spawn", "outcome", outcome="timeout")
    code, doc, _ = run(fakebin, "agent-spawn", *launch_argv(inputs, "--wait"))
    assert code == 16 and doc["outcome"] == "timeout"


@pytest.mark.parametrize(
    ("scenario", "params", "code", "error"),
    [
        ("tool_missing", {}, 12, "tool_missing"),
        ("no_tmux", {}, 12, "failed"),
        ("launch_vetoed", {}, 11, "vetoed"),
        ("launch_refused", {}, 20, "refused"),
        ("launch_ambiguous", {}, 21, "ambiguous"),
        ("launch_error", {"exit_code": 12, "code": "launch_failed"}, 12, "launch_failed"),
        ("launch_error", {"exit_code": 1, "code": "internal"}, 1, "internal"),
    ],
)
def test_launch_error_envelopes(
    fakebin: FakeBin, inputs: Any, scenario: str, params: dict[str, Any], code: int, error: str
) -> None:
    fakebin.use("agent-spawn", scenario, **params)
    got, doc, _ = run(fakebin, "agent-spawn", *launch_argv(inputs))
    assert got == code and doc["error"]["code"] == error
    assert json.loads(inputs["result"].read_text()) == doc  # the result file holds the envelope too
    assert fakebin.state().get("agents", {}) == {}


def test_no_tmux_envelope_names_termctl(fakebin: FakeBin, inputs: Any) -> None:
    fakebin.use("agent-spawn", "no_tmux")
    _, doc, _ = run(fakebin, "agent-spawn", *launch_argv(inputs, mode="interactive"))
    assert doc["error"]["tool"] == "termctl" and doc["error"]["lower_code"] == "tmux_missing"


# ---- dry run ----------------------------------------------------------------------------------


def test_dry_run_prints_the_plan_and_creates_nothing(fakebin: FakeBin, inputs: Any) -> None:
    code, doc, _ = run(fakebin, "agent-spawn", "launch", "--dry-run", "--mode", "interactive",
                       "--kind", "review", "--label", "owner=review-run", "--json")  # fmt: skip
    assert code == 0 and doc["dry_run"] is True and doc["would_refuse"] is None
    assert doc["agent_settings"]["labels"] == ["owner=review-run"]
    assert fakebin.state().get("agents", {}) == {}


def test_dry_run_would_refuse_exits_20_with_the_plan(fakebin: FakeBin) -> None:
    fakebin.use("agent-spawn", "dry_run_would_refuse")
    code, doc, _ = run(fakebin, "agent-spawn", "launch", "--dry-run", "--json")
    assert code == 20 and doc["would_refuse"] == "third_party_cwd"


def test_dry_run_envelopes(fakebin: FakeBin) -> None:
    fakebin.use("agent-spawn", "dry_run_no_tmux")
    code, doc, _ = run(fakebin, "agent-spawn", "launch", "--dry-run", "--json")
    assert code == 12 and doc["error"]["tool"] == "termctl"
    fakebin.use("agent-spawn", "dry_run_error", exit_code=2, code="usage")
    assert run(fakebin, "agent-spawn", "launch", "--dry-run", "--json")[0] == 2


def test_the_dry_run_and_the_real_launch_script_independently(fakebin: FakeBin, inputs: Any) -> None:
    fakebin.use("agent-spawn", "dry_run_would_refuse")
    assert run(fakebin, "agent-spawn", "launch", "--dry-run", "--json")[0] == 20
    assert run(fakebin, "agent-spawn", *launch_argv(inputs))[0] == 0


# ---- show, list, stop -------------------------------------------------------------------------


def test_show_before_and_after_the_run_ends(fakebin: FakeBin, inputs: Any) -> None:
    agent = aid(launch(fakebin, inputs))
    _, live, _ = run(fakebin, "agent-spawn", "show", agent, "--json")
    assert live["agent"]["status"] == "live" and live["supervisor"]["alive"] is True
    assert live["runs"][0]["outcome"] is None and live["session"]["state"] == "working"
    run(fakebin, "agent-spawn", "wait", agent, "--json")
    _, done, _ = run(fakebin, "agent-spawn", "show", agent, "--json")
    assert done["agent"]["status"] == "parked" and done["supervisor"]["alive"] is False
    assert done["runs"][0]["outcome"]["exit_code"] == 0 and done["session"]["report_status"] == "done"
    _, fast, _ = run(fakebin, "agent-spawn", "show", agent, "--no-liveness", "--json")
    assert fast["liveness_checked"] is False and fast["supervisor"]["alive"] is None


def test_show_unknown_agent_exits_10(fakebin: FakeBin) -> None:
    code, doc, _ = run(fakebin, "agent-spawn", "show", "ag-aaaaaaaa", "--json")
    assert code == 10 and doc["error"]["code"] == "not_found"


def test_list_filters_by_label_and_hides_torn_down(fakebin: FakeBin, inputs: Any) -> None:
    a = aid(launch(fakebin, inputs))
    b = aid(launch(fakebin, inputs, "--label", "extra=1"))
    _, doc, _ = run(fakebin, "agent-spawn", "list", "--label", "owner=review-run", "--json")
    assert {x["agent_id"] for x in doc["agents"]} == {a, b}
    _, doc, _ = run(fakebin, "agent-spawn", "list", "--label", "extra=1", "--json")
    assert [x["agent_id"] for x in doc["agents"]] == [b]
    run(fakebin, "agent-spawn", "wait", b, "--json")
    run(fakebin, "agent-spawn", "teardown", b, "--json")
    _, doc, _ = run(fakebin, "agent-spawn", "list", "--json")
    assert [x["agent_id"] for x in doc["agents"]] == [a]
    _, doc, _ = run(fakebin, "agent-spawn", "list", "--status", "torn_down", "--json")
    assert [x["agent_id"] for x in doc["agents"]] == [b]


def test_stop_ends_a_live_agent_as_killed(fakebin: FakeBin, inputs: Any) -> None:
    agent = aid(launch(fakebin, inputs))
    code, doc, _ = run(fakebin, "agent-spawn", "stop", agent, "--grace", "10s",
                       "--actor", "auto:review-run", "--json")  # fmt: skip
    assert code == 0 and doc["action"] == "session_stop" and doc["agent"]["status"] == "parked"
    assert doc["agent"]["last_outcome"] == "killed" and doc["already_parked"] is False
    code, doc, _ = run(fakebin, "agent-spawn", "stop", agent, "--json")
    assert code == 0 and doc["already_parked"] is True and doc["action"] == "none"
    assert run(fakebin, "agent-spawn", "wait", agent, "--json")[0] == 15


def test_stop_unknown_agent_exits_10(fakebin: FakeBin) -> None:
    assert run(fakebin, "agent-spawn", "stop", "ag-aaaaaaaa", "--json")[0] == 10


# ---- teardown and forget are refused for a live agent -----------------------------------------


def test_teardown_of_a_live_agent_is_refused_and_leaves_it_alone(fakebin: FakeBin, inputs: Any) -> None:
    agent = aid(launch(fakebin, inputs))
    code, doc, _ = run(fakebin, "agent-spawn", "teardown", agent, "--json")
    assert code == 20 and doc["error"]["reason"] == "live"
    assert run(fakebin, "agent-spawn", "show", agent, "--json")[1]["agent"]["status"] == "live"
    code, doc, _ = run(fakebin, "agent-spawn", "teardown", agent, "--dry-run", "--json")
    assert code == 20 and doc["would_refuse"] == "live"


def test_a_launch_phase_failure_can_be_torn_down(fakebin: FakeBin, inputs: Any) -> None:
    fakebin.use("agent-spawn", "outcome", outcome="vetoed")
    _, doc, _ = run(fakebin, "agent-spawn", *launch_argv(inputs))
    assert run(fakebin, "agent-spawn", "teardown", aid(doc), "--json")[0] == 0


def test_teardown_of_a_parked_agent_removes_it_and_forgets_its_session(fakebin: FakeBin, inputs: Any) -> None:
    agent = aid(launch(fakebin, inputs))
    csid = run(fakebin, "agent-spawn", "wait", agent, "--json")[1]["agent"]["csession_id"]
    code, doc, _ = run(fakebin, "agent-spawn", "teardown", agent, "--actor", "auto:review-run", "--json")
    assert code == 0 and doc["agent"]["status"] == "torn_down" and doc["session_forgotten"] is True
    code, doc, _ = run(fakebin, "agent-spawn", "teardown", agent, "--json")
    assert code == 10 and doc["error"]["details"] == {"status": "torn_down"}
    assert run(fakebin, "claude-session", "status", csid, "--json")[0] == 10
    code, doc, _ = run(fakebin, "agent-spawn", "teardown", agent, "--dry-run", "--json")
    assert code == 10


def test_teardown_dry_run_of_a_parked_agent_changes_nothing(fakebin: FakeBin, inputs: Any) -> None:
    agent = aid(launch(fakebin, inputs))
    run(fakebin, "agent-spawn", "wait", agent, "--json")
    code, doc, _ = run(fakebin, "agent-spawn", "teardown", agent, "--dry-run", "--json")
    assert code == 0 and doc["would_refuse"] is None
    assert run(fakebin, "agent-spawn", "show", agent, "--json")[1]["agent"]["status"] == "parked"


def test_teardown_failure_scenario(fakebin: FakeBin, inputs: Any) -> None:
    agent = aid(launch(fakebin, inputs))
    run(fakebin, "agent-spawn", "wait", agent, "--json")
    fakebin.use("agent-spawn", "teardown_failed")
    assert run(fakebin, "agent-spawn", "teardown", agent, "--json")[0] == 12


def test_human_only_flags_need_a_human_actor(fakebin: FakeBin, inputs: Any) -> None:
    agent = aid(launch(fakebin, inputs))
    run(fakebin, "agent-spawn", "wait", agent, "--json")
    assert run(fakebin, "agent-spawn", "teardown", agent, "--force", "--json")[0] == 2
    assert run(fakebin, "agent-spawn", "teardown", agent, "--force", "--actor", "auto:x", "--json")[0] == 2
    assert run(fakebin, "agent-spawn", "teardown", agent, "--force", "--actor", "human:me", "--json")[0] == 0


# ---- resume -----------------------------------------------------------------------------------


RESUME = ["--kind", "review", "--no-implicit-done", "--no-promote", "--json"]


def test_resume_of_a_live_agent_is_refused(fakebin: FakeBin, inputs: Any) -> None:
    agent = aid(launch(fakebin, inputs))
    code, doc, _ = run(fakebin, "agent-spawn", "resume", agent, *RESUME, "--prompt-file", str(inputs["prompt"]))
    assert code == 20 and doc["error"]["reason"] == "live"


def test_resume_an_escalated_agent_starts_run_two(fakebin: FakeBin, inputs: Any) -> None:
    fakebin.use("agent-spawn", "outcome", outcome="needs-input")
    agent = aid(launch(fakebin, inputs))
    assert run(fakebin, "agent-spawn", "wait", agent, "--json")[0] == 13
    fakebin.use("agent-spawn", "resume_outcome", outcome="done")
    code, doc, _ = run(fakebin, "agent-spawn", "resume", agent, *RESUME,
                       "--prompt-file", str(inputs["prompt"]), "--result-file", str(inputs["result"]))  # fmt: skip
    assert code == 0 and doc["run"] is None and doc["agent"]["run"] == 2
    assert doc["agent"]["status"] == "live"
    code, doc, _ = run(fakebin, "agent-spawn", "wait", agent, "--json")
    assert code == 0 and doc["run"]["run"] == 2


def test_resume_of_unknown_and_torn_down_agents(fakebin: FakeBin, inputs: Any) -> None:
    assert run(fakebin, "agent-spawn", "resume", "ag-aaaaaaaa", *RESUME)[0] == 10
    agent = aid(launch(fakebin, inputs))
    run(fakebin, "agent-spawn", "wait", agent, "--json")
    run(fakebin, "agent-spawn", "teardown", agent, "--json")
    assert run(fakebin, "agent-spawn", "resume", agent, *RESUME)[0] == 10


# ---- result file, signals, hangs --------------------------------------------------------------


def test_the_result_file_is_private_and_holds_exactly_stdout(fakebin: FakeBin, inputs: Any) -> None:
    agent = aid(launch(fakebin, inputs))
    result = fakebin.run("agent-spawn", "wait", agent, "--result-file", str(inputs["result"]), "--json")
    assert inputs["result"].read_text() == result.stdout
    assert stat.S_IMODE(inputs["result"].stat().st_mode) == 0o600


def test_a_usage_error_still_writes_the_result_file(fakebin: FakeBin, inputs: Any) -> None:
    result = fakebin.run("agent-spawn", "launch", "--bogus", "--result-file", str(inputs["result"]), "--json")
    assert result.returncode == 2
    assert json.loads(inputs["result"].read_text())["error"]["code"] == "usage"


def test_a_relative_result_file_is_refused(fakebin: FakeBin, inputs: Any) -> None:
    code, doc, _ = run(fakebin, "agent-spawn", "launch", "--result-file", "x.json", "--json")
    assert code == 2 and doc["error"]["code"] == "usage"


def test_launch_hang_is_ended_by_the_gateway_timeout(fakebin: FakeBin, inputs: Any) -> None:
    fakebin.use("agent-spawn", "hang")
    with pytest.raises(CallTimeout):
        fakebin.run("agent-spawn", *launch_argv(inputs), timeout=0.6)
    assert not fakebin.calls("agent-spawn")[0].finished


def test_wait_hang_is_ended_by_the_gateway_timeout(fakebin: FakeBin, inputs: Any) -> None:
    agent = aid(launch(fakebin, inputs))
    fakebin.use("agent-spawn", "hang", command="wait")
    with pytest.raises(CallTimeout):
        fakebin.run("agent-spawn", "wait", agent, "--json", timeout=0.6)


def _start_and_signal(fb: FakeBin, argv: list[str], signum: int) -> subprocess.Popen[bytes]:
    proc = subprocess.Popen(
        [fb.argv0("agent-spawn"), *argv], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=fb.env(), start_new_session=True,
    )  # fmt: skip
    deadline = time.monotonic() + 10
    while not fb.all_calls() and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(0.2)
    os.kill(proc.pid, signum)  # the PID we launched
    return proc


def test_sigterm_on_a_hanging_launch_prints_the_interrupted_envelope(fakebin: FakeBin, inputs: Any) -> None:
    fakebin.use("agent-spawn", "hang")
    proc = _start_and_signal(fakebin, launch_argv(inputs), signal.SIGTERM)
    out, _ = proc.communicate(timeout=10)
    assert proc.returncode == 143
    doc = json.loads(out)
    assert doc["error"]["code"] == "launch_failed" and doc["error"]["details"] == {"cause": "interrupted"}
    assert json.loads(inputs["result"].read_text()) == doc


def test_sigint_on_a_hanging_wait_prints_nothing(fakebin: FakeBin, inputs: Any) -> None:
    agent = aid(launch(fakebin, inputs))
    fakebin.use("agent-spawn", "hang", command="wait")
    n_before = len(fakebin.all_calls())
    proc = _start_and_signal_after(fakebin, ["wait", agent, "--json"], signal.SIGINT, n_before)
    out, _ = proc.communicate(timeout=10)
    assert proc.returncode == 130 and out == b""


def _start_and_signal_after(fb: FakeBin, argv: list[str], signum: int, n: int) -> subprocess.Popen[bytes]:
    proc = subprocess.Popen(
        [fb.argv0("agent-spawn"), *argv], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=fb.env(), start_new_session=True,
    )  # fmt: skip
    deadline = time.monotonic() + 10
    while len(fb.all_calls()) <= n and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(0.2)
    os.kill(proc.pid, signum)
    return proc


def test_the_launch_environment_reaches_the_fake(fakebin: FakeBin, inputs: Any) -> None:
    env = fakebin.env(CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH="1")
    fakebin.run("agent-spawn", *launch_argv(inputs), env=env)
    assert fakebin.calls("agent-spawn")[0].env["CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH"] == "1"


# ---- grammar ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [
        ["--bogus"],
        ["--mod", "headless"],  # abbreviation
        ["--mode", "background"],
        ["--sandbox", "maybe"],
        ["--max-turns", "0"],
        ["--timeout", "0"],
        ["--timeout", "soon"],
        ["--label", "agent=x"],
        ["--label", "kind=review"],
        ["--label", "Bad=1"],
        ["--label", "nokey"],
        ["--implicit-done"],  # with --no-implicit-done already given
        ["--promote"],  # with --no-promote
        ["--prompt", "text"],  # with --prompt-file
        ["--worktree", "relative/path"],
        ["--worktree", "new", "--cwd", "session-dir"],
        ["--branch", "b"],  # needs --worktree new
        ["--wait-timeout", "5s"],  # needs --wait
        ["--override-escalation"],  # human-only
        ["--hook", "nonsense"],
        ["--hook", "pre-teardown=true"],
        ["--actor", "robot"],
        ["--decision-id", "bad id"],
        ["--env-allowlist-only"],  # missing value
        ["--plugin-dir", "/nonexistent-plugin-dir"],
        ["--input-file", "/nonexistent-input-file"],
        ["--repo", "NOT A REPO"],
        ["extra-positional"],
        ["--json"],  # given twice
    ],
)
def test_launch_rejects_what_the_real_tool_rejects(fakebin: FakeBin, inputs: Any, extra: list[str]) -> None:
    code, doc, err = run(fakebin, "agent-spawn", *launch_argv(inputs, *extra))
    assert code == 2, extra
    assert doc["error"]["code"] == "usage" and err.startswith("agent-spawn: error:")
    assert fakebin.state().get("agents", {}) == {}


@pytest.mark.parametrize(
    "argv",
    [
        ["wait"],
        ["wait", "not-an-id"],
        ["wait", "ag-aaaaaaaa", "extra"],
        ["wait", "ag-aaaaaaaa", "--timeout", "0"],
        ["show"],
        ["show", "ag-aaaaaaaa", "--bogus"],
        ["stop", "ag-aaaaaaaa", "--grace", "0"],
        ["teardown", "ag-aaaaaaaa", "--merged-head", "abc"],
        ["teardown", "ag-aaaaaaaa", "--hook", "pre-launch=x"],
        ["list", "--status", "dead"],
        ["list", "extra"],
        ["frobnicate"],
        ["--", "list"],
        [],
    ],
)
def test_other_commands_reject_bad_argv(fakebin: FakeBin, argv: list[str]) -> None:
    code, doc, _ = run(fakebin, "agent-spawn", *argv, "--json")
    assert code == 2, argv
    assert doc["error"]["code"] == "usage"


def test_version_and_unemulated_commands(fakebin: FakeBin) -> None:
    assert fakebin.run("agent-spawn", "--version").stdout == "agent-spawn 0.38.1\n"
    assert run(fakebin, "agent-spawn", "prune", "--json")[0] == 99
    assert run(fakebin, "agent-spawn", "label", "add", "ag-aaaaaaaa", "a=b", "--json")[0] == 99
