"""The fake claude-session: ``status`` (with init-object fixtures) and ``forget``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from review_run.gateway import CallTimeout
from tests.fakebin._sessions import DEFAULT_TOOLS
from tests.fakes import FakeBin
from tests.test_fake_agent_spawn import aid, inputs, launch, run  # noqa: F401  (fixture)


def csid_of(doc: dict[str, Any]) -> str:
    return doc["agent"]["csession_id"]


def status(fb: FakeBin, csid: str, *extra: str) -> tuple[int, Any, str]:
    return run(fb, "claude-session", "status", csid, *extra, "--json")


def result_lines(path: str) -> list[Any]:
    out: list[Any] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            out.append(line)
    return out


def finished(fb: FakeBin, inputs: Any, **launch_kw: Any) -> tuple[str, dict[str, Any]]:
    doc = launch(fb, inputs, **launch_kw)
    csid = csid_of(doc)
    run(fb, "agent-spawn", "wait", aid(doc), "--json")
    return csid, status(fb, csid)[1]


def test_status_of_a_live_run(fakebin: FakeBin, inputs: Any) -> None:
    csid = csid_of(launch(fakebin, inputs))
    code, doc, _ = status(fakebin, csid)
    assert code == 0 and doc["live"] is True and doc["state"] == "working"
    assert doc["run"]["mode"] == "headless" and doc["run"]["outcome"] is None
    assert doc["run"]["skill"] == "review-run:review-pr" and doc["run"]["result_path"] is None
    assert fakebin.calls("claude-session")[0].argv == ["status", csid, "--json"]


def test_status_of_a_finished_headless_run_has_a_result_path_with_an_init_object(
    fakebin: FakeBin, inputs: Any
) -> None:
    csid, doc = finished(fakebin, inputs)
    run_doc = doc["run"]
    assert doc["live"] is False and run_doc["state"] == "exited" and run_doc["outcome"] == "done"
    lines = result_lines(run_doc["result_path"])
    init = lines[0]
    assert init["type"] == "system" and init["subtype"] == "init"
    assert init["skills"] == ["review-run:review-pr"] and init["tools"] == DEFAULT_TOOLS
    assert status(fakebin, csid, "--run", "1")[0] == 0
    assert status(fakebin, csid, "--run", "2")[0] == 10


def test_interactive_runs_have_no_result_path(fakebin: FakeBin, inputs: Any) -> None:
    _, doc = finished(fakebin, inputs, mode="interactive")
    assert doc["run"]["mode"] == "interactive" and doc["run"]["result_path"] is None


@pytest.mark.parametrize(
    ("scenario", "params", "check"),
    [
        ("init_skill_missing", {}, lambda lines: lines[0]["skills"] == ["some-other:skill"]),
        ("init_ok", {"skills": ["a:b", "c:d"]}, lambda lines: lines[0]["skills"] == ["a:b", "c:d"]),
        ("init_extra_tools", {}, lambda lines: "WebFetch" in lines[0]["tools"]),
        ("init_absent", {}, lambda lines: all(x.get("subtype") != "init" for x in lines)),
        ("init_garbage", {}, lambda lines: lines == ["{not json", "[1,"]),
        ("init_late", {"skip": 5}, lambda lines: lines[5]["subtype"] == "init"),
    ],
)
def test_init_object_fixtures(fakebin: FakeBin, inputs: Any, scenario: str, params: Any, check: Any) -> None:
    fakebin.use("claude-session", scenario, **params)
    _, doc = finished(fakebin, inputs)
    assert check(result_lines(doc["run"]["result_path"]))


def test_missing_and_absent_result_files(fakebin: FakeBin, inputs: Any) -> None:
    fakebin.use("claude-session", "init_missing_file")
    _, doc = finished(fakebin, inputs)
    assert doc["run"]["result_path"] and not Path(doc["run"]["result_path"]).exists()
    fakebin.use("claude-session", "init_no_path")
    _, doc = finished(fakebin, inputs)
    assert doc["run"]["result_path"] is None


@pytest.mark.parametrize("outcome", ["blocked", "needs-input", "failed", "exited-unreported",
                                     "killed", "timeout", "lost"])  # fmt: skip
def test_status_reports_the_final_state_of_every_outcome(fakebin: FakeBin, inputs: Any, outcome: str) -> None:
    fakebin.use("agent-spawn", "outcome", outcome=outcome)
    _, doc = finished(fakebin, inputs)
    assert doc["run"]["outcome"] == outcome and doc["live"] is False


def test_status_unknown_session_and_usage(fakebin: FakeBin) -> None:
    assert status(fakebin, "cs-aaaaaaaa")[0] == 10
    for argv in (["status"], ["status", "cs-short"], ["status", "cs-aaaaaaaa", "--run", "0"],
                 ["status", "cs-aaaaaaaa", "--run"], ["status", "cs-aaaaaaaa", "--bogus"],
                 ["status", "cs-aaaaaaaa", "extra"], ["--json", "--run", "1", "status", "cs-aaaaaaaa"],
                 ["status", "cs-aaaaaaaa", "--verbose", "--quiet"], ["frob"]):  # fmt: skip
        code, doc, _ = run(fakebin, "claude-session", *argv, "--json")
        assert code == 2 and doc["error"]["code"] == "usage", argv


def test_forget_refuses_a_live_session_and_forgets_a_final_one(fakebin: FakeBin, inputs: Any) -> None:
    doc = launch(fakebin, inputs)
    csid = csid_of(doc)
    code, err, _ = run(fakebin, "claude-session", "forget", csid, "--json")
    assert code == 20 and err["error"]["reason"] == "live"
    assert status(fakebin, csid)[0] == 0  # still there
    run(fakebin, "agent-spawn", "wait", aid(doc), "--json")
    code, out, _ = run(fakebin, "claude-session", "forget", csid, "--actor", "auto:review-run", "--json")
    assert code == 0 and out["forgotten"][0]["csession_id"] == csid and out["skipped"] == []
    assert status(fakebin, csid)[0] == 10
    assert run(fakebin, "claude-session", "forget", csid, "--json")[0] == 10


def test_forget_dry_run_and_purge(fakebin: FakeBin, inputs: Any) -> None:
    doc = launch(fakebin, inputs)
    csid = csid_of(doc)
    run(fakebin, "agent-spawn", "wait", aid(doc), "--json")
    code, out, _ = run(fakebin, "claude-session", "forget", csid, "--dry-run", "--json")
    assert code == 0 and out["dry_run"] is True and status(fakebin, csid)[0] == 0
    code, out, _ = run(fakebin, "claude-session", "forget", csid, "--purge", "--json")
    assert code == 0 and out["forgotten"][0]["purged"] is True


def test_forget_unemulated_forms(fakebin: FakeBin) -> None:
    assert run(fakebin, "claude-session", "forget", "--older-than", "1d", "--json")[0] == 2
    assert run(fakebin, "claude-session", "exec", "--json")[0] == 99


def test_status_error_scenario_and_hang(fakebin: FakeBin, inputs: Any) -> None:
    csid = csid_of(launch(fakebin, inputs))
    fakebin.use("claude-session", "status_error")
    assert status(fakebin, csid)[0] == 12
    fakebin.use("claude-session", "hang")
    with pytest.raises(CallTimeout):
        fakebin.run("claude-session", "status", csid, "--json", timeout=0.6)


def test_version(fakebin: FakeBin) -> None:
    result = fakebin.run("claude-session", "--version")
    assert result.stdout == "claude-session 0.45.2\n"
    assert fakebin.run("claude-session", "status", "--version").stdout == "claude-session 0.45.2\n"
