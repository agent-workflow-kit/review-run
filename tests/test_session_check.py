"""Skill and tool checks of finalize step 5 (issue #22), end to end against the fake siblings
and as units over hostile init objects."""

# ruff: noqa: E501

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from review_run import gateway
from review_run import session_check as sc
from review_run.errors import ReviewRunError
from review_run.finalize import FinalizeFailure
from tests.fakebin._sessions import DEFAULT_TOOLS
from tests.run_helpers import World
from tests.test_meta import artifact_text

SKILL = "review-run:review-pr"


def run_with(world: World, scenario: str | None = None, **params: Any) -> Any:
    world.fb.use("agent-spawn", "outcome", outcome="done", artifact_text=artifact_text())
    if scenario:
        world.fb.use("claude-session", scenario, **params)
    return world.run()


# -- end to end with the production seams -------------------------------------------------------


def test_a_good_session_ends_done_with_review_json(world: World) -> None:
    run = run_with(world)
    assert (run.state, run.reason, run.exit_code) == ("done", None, 0)
    assert run.artifact_path and Path(run.artifact_path).is_file()
    meta = json.loads(Path(run.artifact_path).read_bytes())["meta"]["review_run"]
    assert meta["skill_checked"] is True
    assert meta["session"]["tools_checked"] is True and meta["session"]["tools_unexpected"] == 0
    assert "tools_unexpected" not in run.warnings and "skill_unchecked" not in run.warnings
    (call,) = world.fb.calls("claude-session", "status")
    assert call.argv[0:4] == ["status", run.csession_id, "--run", "1"]
    assert "--json" in call.argv


@pytest.mark.parametrize(
    "scenario",
    ["init_skill_missing", "init_missing_file", "init_no_path", "init_absent", "init_garbage"],
)
def test_a_skill_that_cannot_be_shown_loaded_is_skill_missing(world: World, scenario: str) -> None:
    run = run_with(world, scenario)
    assert (run.state, run.reason, run.exit_code) == ("failed", "skill_missing", 22)
    assert run.artifact_path is None


def test_an_init_object_after_other_lines_is_found(tmp_path: Path) -> None:
    lines = [{"type": "assistant", "n": i} for i in range(5)]
    assert sc.read_init(write(tmp_path / "late", *lines, INIT)) == INIT


def test_a_lookalike_skill_is_skill_missing(world: World) -> None:
    run = run_with(world, "init_ok", skills=["review-run:review-pr​", "review-run:review‐pr"])
    assert run.reason == "skill_missing"


def test_extra_tools_warn_with_a_count_and_never_a_name(world: World) -> None:
    run = run_with(world, "init_extra_tools", extra=("WebFetch", "MARKER-TOOL"))
    assert run.state == "done" and "tools_unexpected" in run.warnings
    data = Path(run.artifact_path).read_bytes()
    meta = json.loads(data)["meta"]["review_run"]["session"]
    assert meta["tools_unexpected"] == 2 and b"MARKER-TOOL" not in data
    root = world.root
    for f in root.rglob("*"):
        if f.is_file() and f.suffix in (".jsonl", ".json", ".log"):
            assert b"MARKER-TOOL" not in f.read_bytes(), f


def test_a_failing_status_ends_internal_without_its_text(world: World) -> None:
    run = run_with(world, "status_error")
    assert (run.state, run.reason, run.exit_code) == ("failed", "internal", 12)
    assert run.failure["tool"] == "claude-session"
    assert run.failure["lower_code"] == "failed"
    assert "fake failure" not in json.dumps(run.failure)
    assert "fake failure" not in json.dumps(world.only_run().failure)


# -- units over hostile init objects ------------------------------------------------------------


def write(path: Path, *lines: Any) -> str:
    path.write_text("".join((x if isinstance(x, str) else json.dumps(x)) + "\n" for x in lines))
    return str(path)


INIT = {"type": "system", "subtype": "init", "skills": [SKILL], "tools": list(DEFAULT_TOOLS)}


def test_read_init_refuses_links_and_non_files(tmp_path: Path) -> None:
    real = write(tmp_path / "r.json", INIT)
    os.symlink(real, tmp_path / "l.json")
    assert sc.read_init(real) == INIT
    assert sc.read_init(str(tmp_path / "l.json")) is None
    assert sc.read_init(str(tmp_path)) is None
    for bad in (None, 5, "", "relative.json", "/a\x00b", ["/x"]):
        assert sc.read_init(bad) is None


def test_read_init_stops_at_the_first_init_and_at_garbage(tmp_path: Path) -> None:
    other = dict(INIT, skills=["x:y"])
    assert sc.read_init(write(tmp_path / "a", INIT, other)) == INIT
    assert sc.read_init(write(tmp_path / "b", "{bad", INIT)) is None
    assert (
        sc.read_init(write(tmp_path / "c", '{"type":"system","subtype":"init","a":1,"a":2}'))
        is None
    )
    assert sc.read_init(write(tmp_path / "d", '{"type":"system","subtype":"init","x":NaN}')) is None
    assert sc.read_init(write(tmp_path / "e", "[1]", INIT)) is None  # step 6 wants an object
    (tmp_path / "f").write_bytes(b"\xff\xfe\n")
    assert sc.read_init(str(tmp_path / "f")) is None


def test_read_init_honors_the_one_mebibyte_bound(tmp_path: Path) -> None:
    pad = {"type": "assistant", "text": "x" * 1000}
    lines = [pad] * 1100  # more than 1 MiB before the init object
    assert sc.read_init(write(tmp_path / "big", *lines, INIT)) is None
    one = write(tmp_path / "huge", {**INIT, "skills": ["a"] * 300000})
    assert sc.read_init(one) is None  # the init line itself passes the bound


def test_read_init_accepts_a_last_line_without_newline(tmp_path: Path) -> None:
    p = tmp_path / "n"
    p.write_text(json.dumps(INIT))
    assert sc.read_init(str(p)) == INIT


@pytest.mark.parametrize(
    "skills",
    [None, "review-run:review-pr", [], [None], [["review-run:review-pr"]], [{"a": 1}],
     ["Review-Run:Review-Pr"], ["review-run:review-pr "], ["review-runа:review-pr"],
     ["review-run:review-pр"], ["review-run:review-pr​"], ["../review-run:review-pr"]],
)  # fmt: skip
def test_has_skill_is_exact(skills: Any) -> None:
    assert sc.has_skill({"skills": skills}, SKILL) is False


def test_has_skill_matches_among_duplicates_and_noise() -> None:
    assert sc.has_skill({"skills": [1, None, SKILL, SKILL]}, SKILL) is True
    assert sc.has_skill({}, SKILL) is False


def test_unexpected_tools_counts_distinct_names() -> None:
    base = list(DEFAULT_TOOLS)
    assert sc.unexpected_tools({"tools": base}, subagents=False) == 0
    assert sc.unexpected_tools({"tools": base + base}, subagents=False) == 0
    assert (
        sc.unexpected_tools({"tools": base + ["WebFetch", "WebFetch", "mcp__x"]}, subagents=False)
        == 2
    )
    assert (
        sc.unexpected_tools({"tools": base + ["bash", "READ"]}, subagents=False) == 2
    )  # case variants
    assert sc.unexpected_tools({"tools": base + [None, 3, {"a": 1}, ["x"]]}, subagents=False) == 4
    assert sc.unexpected_tools({"tools": base + ["Read​", "Rеad"]}, subagents=False) == 2


def test_task_and_agent_are_one_name_allowed_only_with_subagents() -> None:
    base = list(DEFAULT_TOOLS)
    assert sc.unexpected_tools({"tools": base + ["Task", "Agent"]}, subagents=False) == 1
    assert sc.unexpected_tools({"tools": base + ["Agent"]}, subagents=True) == 0
    assert sc.unexpected_tools({"tools": base + ["Task", "Agent"]}, subagents=True) == 0


def test_a_huge_tool_list_is_counted_name_by_name() -> None:
    tools = [f"T{i}" for i in range(50000)]
    assert sc.unexpected_tools({"tools": tools}, subagents=False) == 50000


def test_non_string_entries_count_one_each_and_repeated_names_one() -> None:
    assert sc.unexpected_tools({"tools": [{}, {}, 3]}, subagents=False) == 3
    assert sc.unexpected_tools({"tools": ["X", "X", "X"]}, subagents=False) == 1


@pytest.mark.parametrize("tools", [None, "Read", {"Read": 1}, 5])
def test_tools_that_are_not_a_list_are_not_compared(tools: Any) -> None:
    assert sc.unexpected_tools({"tools": tools}, subagents=False) is None


def status(tmp: Path, mode: str = "headless", **over: Any) -> dict[str, Any]:
    path = None if mode == "interactive" else write(tmp / "res.json", INIT)
    run = {"run": 1, "mode": mode, "result_path": path, **over}
    return {"csession_id": "cs-aaaaaaaa", "run": run}


def evaluate(doc: dict[str, Any], **over: Any) -> Any:
    kw: dict[str, Any] = dict(
        csession_id="cs-aaaaaaaa", awaited_run=1, mode="headless", skill=SKILL,
        profile_kind="bundled", subagents=False,
    )  # fmt: skip
    return sc.evaluate(doc, **{**kw, **over})


def test_interactive_is_unchecked_with_both_warnings(tmp_path: Path) -> None:
    out = evaluate(status(tmp_path, "interactive"), mode="interactive")
    assert out.warnings == ("skill_unchecked", "tools_unchecked")
    assert (out.skill_checked, out.tools_checked, out.tools_unexpected) == (False, False, None)


def test_a_custom_profile_checks_the_skill_but_not_the_tools(tmp_path: Path) -> None:
    out = evaluate(status(tmp_path), profile_kind="custom")
    assert (out.skill_checked, out.tools_checked, out.warnings) == (
        True,
        False,
        ("tools_unchecked",),
    )


def test_the_variant_profile_compares_with_task_allowed(tmp_path: Path) -> None:
    doc = status(tmp_path)
    write(tmp_path / "res.json", dict(INIT, tools=[*DEFAULT_TOOLS, "Task"]))
    out = evaluate(doc, profile_kind="variant", subagents=True)
    assert (out.tools_checked, out.tools_unexpected, out.warnings) == (True, 0, ())


@pytest.mark.parametrize(
    "over",
    [{"run": 2}, {"run": True}, {"run": "1"}, {"mode": "interactive"}, {"mode": None}],
)
def test_a_status_about_another_run_or_mode_is_internal(
    tmp_path: Path, over: dict[str, Any]
) -> None:
    with pytest.raises(FinalizeFailure) as info:
        evaluate(status(tmp_path, **over))
    assert info.value.reason == "internal" and info.value.failure["tool"] == "claude-session"


def test_a_status_of_another_session_or_shape_is_internal(tmp_path: Path) -> None:
    doc = status(tmp_path)
    for bad in ({**doc, "csession_id": "cs-bbbbbbbb"}, {"csession_id": "cs-aaaaaaaa"},
                {**doc, "run": []}):  # fmt: skip
        with pytest.raises(FinalizeFailure) as info:
            evaluate(bad)
        assert info.value.failure["tool"] == "claude-session"


def test_headless_without_a_result_path_is_skill_missing(tmp_path: Path) -> None:
    with pytest.raises(FinalizeFailure) as info:
        evaluate(status(tmp_path, result_path=None))
    assert info.value.reason == "skill_missing" and info.value.exit_code == 22


# -- strict step-6 parsing of every line of the init search ---------------------------------------

DEEP = "[" * 40 + "]" * 40
BAD_LINES = [
    "",
    "   ",
    "\x0b",
    "\x0c",
    '{"a":' + DEEP + "}",
    '{"a":' + "1" * 21 + "}",
    '{"a":0.' + "1" * 70 + "}",
    '{"a":"\\ud800"}',
]


@pytest.mark.parametrize("bad", BAD_LINES)
def test_a_line_that_fails_step_6_before_the_init_ends_the_search(tmp_path: Path, bad: str) -> None:
    assert sc.read_init(write(tmp_path / "x", bad, INIT)) is None


@pytest.mark.parametrize("bad", BAD_LINES[4:])
def test_an_init_line_that_fails_step_6_is_not_an_init(tmp_path: Path, bad: str) -> None:
    line = bad[:-1] + ',"type":"system","subtype":"init"}'
    assert sc.read_init(write(tmp_path / "y", line)) is None


def test_garbage_after_the_init_line_is_never_read(tmp_path: Path) -> None:
    assert sc.read_init(write(tmp_path / "z", INIT, "{bad", "")) == INIT


# -- check_session with a fake gateway ------------------------------------------------------------


class Ctx:
    def __init__(self, *, mode: str = "headless", csid: Any = "cs-aaaaaaaa", kind: str = "bundled",
                 subagents: bool = False, skill: Any = SKILL, awaited: Any = 1) -> None:  # fmt: skip
        self.run = SimpleNamespace(csession_id=csid, awaited_run=awaited, mode=mode)
        self.settings = SimpleNamespace(
            profile_kind=kind,
            value=lambda name: {"skill": skill, "subagents": subagents}[name],
        )


def stub(monkeypatch: pytest.MonkeyPatch, reply: Any) -> list[list[str]]:
    seen: list[list[str]] = []

    def fake(argv: list[str], *, timeout: float) -> Any:
        seen.append(argv)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(gateway, "run", fake)
    return seen


def done(stdout: str, code: int = 0) -> gateway.CompletedRun:
    return gateway.CompletedRun(("claude-session",), code, stdout, "")


def test_check_session_interactive_is_unchecked(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = {"csession_id": "cs-aaaaaaaa", "run": {"run": 1, "mode": "interactive"}}
    seen = stub(monkeypatch, done(json.dumps(doc)))
    out = sc.check_session(cast(Any, Ctx(mode="interactive")))
    assert out.warnings == ("skill_unchecked", "tools_unchecked") and out.skill_checked is False
    assert seen == [["claude-session", "status", "cs-aaaaaaaa", "--run", "1", "--json"]]


def test_check_session_custom_profile_with_subagents(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    doc = status(tmp_path)
    stub(monkeypatch, done(json.dumps(doc)))
    out = sc.check_session(cast(Any, Ctx(kind="custom", subagents=True)))
    assert (out.skill_checked, out.tools_checked, out.warnings) == (
        True,
        False,
        ("tools_unchecked",),
    )


@pytest.mark.parametrize(
    "kw",
    [{"csid": "cs-x"}, {"csid": None}, {"awaited": 0}, {"awaited": True}, {"mode": "weird"},
     {"skill": "nocolon"}, {"skill": None}],
)  # fmt: skip
def test_check_session_invalid_session_facts_are_internal(
    monkeypatch: pytest.MonkeyPatch, kw: dict[str, Any]
) -> None:
    seen = stub(monkeypatch, done("{}"))
    with pytest.raises(ReviewRunError):
        sc.check_session(cast(Any, Ctx(**kw)))
    assert seen == []


def test_check_session_gateway_error_is_internal_with_its_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub(monkeypatch, gateway.CallTimeout("claude-session"))
    with pytest.raises(FinalizeFailure) as info:
        sc.check_session(cast(Any, Ctx()))
    assert info.value.reason == "internal"
    assert info.value.failure["tool"] == "claude-session"
    assert info.value.failure["lower_code"] == "timeout"


def test_check_session_non_json_status_is_invalid_output(monkeypatch: pytest.MonkeyPatch) -> None:
    stub(monkeypatch, done("SECRET not json"))
    with pytest.raises(FinalizeFailure) as info:
        sc.check_session(cast(Any, Ctx()))
    assert info.value.reason == "internal"
    assert info.value.failure["lower_code"] == "invalid_output"
    assert "SECRET" not in repr(info.value.failure)


def test_check_session_non_object_status_is_invalid_output(monkeypatch: pytest.MonkeyPatch) -> None:
    stub(monkeypatch, done("[1]"))
    with pytest.raises(FinalizeFailure) as info:
        sc.check_session(cast(Any, Ctx()))
    assert info.value.failure["lower_code"] == "invalid_output"


def test_check_session_non_zero_exit_keeps_the_envelope_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub(monkeypatch, done(json.dumps({"error": {"code": "not_found"}}), 10))
    with pytest.raises(FinalizeFailure) as info:
        sc.check_session(cast(Any, Ctx()))
    assert info.value.failure["lower_code"] == "not_found"


@pytest.mark.parametrize("tools", [None, "Read", {"Read": 1}, 5])
def test_evaluate_tools_missing_or_not_a_list_is_tools_unchecked(
    tmp_path: Path, tools: Any
) -> None:
    doc = status(tmp_path)
    init = {k: v for k, v in INIT.items() if k != "tools"}
    write(tmp_path / "res.json", init if tools is None else dict(init, tools=tools))
    out = evaluate(doc)
    assert (out.skill_checked, out.tools_checked, out.tools_unexpected, out.warnings) == (
        True, False, None, ("tools_unchecked",),
    )  # fmt: skip


def test_evaluate_a_pinned_tool_absent_from_the_list_is_not_flagged(tmp_path: Path) -> None:
    doc = status(tmp_path)
    write(tmp_path / "res.json", dict(INIT, tools=["Read"]))
    out = evaluate(doc)
    assert (out.tools_checked, out.tools_unexpected, out.warnings) == (True, 0, ())
