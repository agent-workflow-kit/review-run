"""Hook specs, environment, payloads, one hook call and the ``pre-run`` outcome."""

from __future__ import annotations

import json
import sys
from typing import Any

import pytest

from review_run import gateway as gw
from review_run import hooks
from review_run.audit import Caller
from review_run.errors import ReviewRunError
from review_run.gateway import CallTimeout, CompletedRun, OutputTooLarge, ToolMissing
from tests.fake_gateway import FakeGateway, ok

MARKER = "UNTRUSTED-MARKER-7f3a"
RUN = "rr-abcdefgh"
PR = "host/o/r#7"
REPO = "host/o/r"
SHA = "a" * 40
SH = "/bin/sh"


def pre_payload(**over: Any) -> dict[str, Any]:
    args: dict[str, Any] = dict(
        run_id=RUN,
        repo=REPO,
        labels=["team=core"],
        pr=PR,
        skill="review-run:review-pr",
        mode="headless",
        previous_run=None,
        head_sha=SHA,
        pr_facts={"draft": False, "is_bot": None, "is_fork": False, "base_is_default": True},
        will_run_code=False,
        code_run_sandbox=None,
        override_escalation=False,
        session_sandbox="required",
        subagents=False,
        permission_profile="bundled",
        setting_sources=["user"],
        sizes={"additions": 1, "deletions": 2, "changed_files": 3},
        actor="human:alice",
        decision_ids=[],
    )
    args.update(over)
    return hooks.pre_run_payload(**args)


# -- specs ---------------------------------------------------------------------------------


def test_config_entries_come_first_then_flag_entries_with_defaults() -> None:
    config = {"post-run": [{"cmd": "a"}, {"cmd": "b", "timeout": "2m", "async": True}]}
    specs = hooks.specs_for("post-run", config, ["post-run=c", "pre-run=zzz", "post-escalate=d"])
    assert [(s.cmd, s.timeout, s.is_async) for s in specs] == [
        ("a", 30.0, False),
        ("b", 120.0, True),
        ("c", 30.0, False),
    ]


def test_flag_entries_split_at_the_first_equals_and_keep_the_rest() -> None:
    (spec,) = hooks.specs_for("pre-run", None, ["pre-run=FOO=bar baz"])
    assert spec.cmd == "FOO=bar baz"


BAD_ENTRIES = ["", "nope", "unknown=cmd", "post-run=", "post-run=a\x00b", "post-run=a\x01b", "=x"]


@pytest.mark.parametrize("bad", BAD_ENTRIES)
def test_invalid_stored_entries_fail_closed_as_an_invalid_spec(bad: str) -> None:
    for hook in hooks.HOOK_NAMES:
        (found,) = hooks.specs_for(hook, None, [bad])
        assert found.invalid


@pytest.mark.parametrize("bad", BAD_ENTRIES)
def test_add_flag_hooks_rejects_invalid_entries_without_echoing_them(bad: str) -> None:
    with pytest.raises(ReviewRunError) as info:
        hooks.add_flag_hooks([], ["post-run=ok", bad])
    assert info.value.code == "usage"


def test_a_rejected_hook_entry_is_never_echoed() -> None:
    with pytest.raises(ReviewRunError) as info:
        hooks.add_flag_hooks([], [f"{MARKER}=cmd"])
    assert MARKER not in str(info.value) and MARKER not in repr(info.value.__dict__)


def test_an_invalid_stored_entry_vetoes_pre_run_and_runs_nothing(
    fake_gateway: FakeGateway,
) -> None:
    fake_gateway.script(SH, ok())
    result = run_pre(hooks.specs_for("pre-run", None, ["pre-run=gate", "bogus"]))
    assert result.outcome == "veto"
    assert [c.argv for c in fake_gateway.calls] == [(SH, "-c", "gate")]  # first ran, then the veto
    fake_gateway.calls.clear()
    assert run_pre(hooks.specs_for("pre-run", None, ["bogus", "pre-run=gate"])).outcome == "veto"
    assert fake_gateway.calls == []


def test_an_invalid_spec_is_an_invalid_result_and_never_runs(fake_gateway: FakeGateway) -> None:
    result = hooks.run_spec(hooks.HookSpec("", invalid=True), "post-run", {}, env={})
    assert result.code == "invalid" and not result.ok
    assert fake_gateway.calls == []


def test_entries_may_hold_newlines_and_tabs() -> None:
    (spec,) = hooks.specs_for("post-run", None, ["post-run=echo a\n\techo b"])
    assert spec.cmd == "echo a\n\techo b"


def test_a_bad_stored_value_type_fails_closed() -> None:
    got = hooks.specs_for("post-run", None, [None, 3, ["post-run=x"]])  # type: ignore[list-item]
    assert [g.invalid for g in got] == [True, True, True]


def test_add_flag_hooks_keeps_an_identical_entry_once_in_order() -> None:
    stored = hooks.add_flag_hooks(["post-run=a", "pre-run=b"], ["post-run=a", "post-run=c"])
    assert stored == ["post-run=a", "pre-run=b", "post-run=c"]
    assert hooks.add_flag_hooks([], ["post-run=1", "post-run=1"]) == ["post-run=1"]


def test_event_ids() -> None:
    assert hooks.event_id_for(RUN, "post-run", 1) == f"{RUN}-post-run-1"
    assert hooks.event_id_for(RUN, "post-escalate", 3) == f"{RUN}-post-escalate-3"
    with pytest.raises(ValueError):
        hooks.event_id_for(RUN, "pre-run", 1)
    with pytest.raises(ValueError):
        hooks.event_id_for(RUN, "post-run", 0)


# -- environment ---------------------------------------------------------------------------


def test_env_has_the_documented_variables_and_inherits_the_rest() -> None:
    env = hooks.hook_env("post-run", RUN, event_id="evt-0001", base={"PATH": "/x", "HOME": "/h"})
    assert env == {
        "PATH": "/x",
        "HOME": "/h",
        "REVIEW_RUN_HOOK": "post-run",
        "REVIEW_RUN_ID": RUN,
        "REVIEW_RUN_OBJECT_TYPE": "run",
        "REVIEW_RUN_EVENT_ID": "evt-0001",
    }


def test_pre_run_env_has_no_event_id_and_stale_values_are_replaced() -> None:
    base = {
        "REVIEW_RUN_HOOK": "stale",
        "REVIEW_RUN_ID": "stale",
        "REVIEW_RUN_OBJECT_TYPE": "stale",
        "REVIEW_RUN_EVENT_ID": "stale",
        "KEEP": "1",
    }
    env = hooks.hook_env("pre-run", RUN, base=base)
    assert "REVIEW_RUN_EVENT_ID" not in env
    assert env["REVIEW_RUN_HOOK"] == "pre-run"
    assert env["REVIEW_RUN_ID"] == RUN
    assert env["KEEP"] == "1"


def test_env_defaults_to_the_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOME_USER_TOKEN", "t")
    assert hooks.hook_env("post-run", RUN, event_id="evt-0001")["SOME_USER_TOKEN"] == "t"


def test_env_refuses_values_that_are_not_trusted_ids() -> None:
    with pytest.raises(ValueError):
        hooks.hook_env("post-run", "not a run id", event_id="e", base={})
    with pytest.raises(ValueError):
        hooks.hook_env("post-run", RUN, event_id="e\nx", base={})
    with pytest.raises(ValueError):
        hooks.hook_env("post-run", RUN, base={})  # a post hook needs its event id
    with pytest.raises(ValueError):
        hooks.hook_env("nope", RUN, base={})


# -- payloads ------------------------------------------------------------------------------


def test_pre_run_payload_is_valid_and_has_the_common_keys() -> None:
    p = pre_payload()
    assert p["tool"] == "review-run" and p["hook"] == "pre-run" and p["object_type"] == "run"
    assert p["object_id"] == RUN and p["repo"] == REPO and p["labels"] == ["team=core"]
    assert p["actor"] == "human:alice" and p["decision_ids"] == []
    assert "event_id" not in p


def test_a_payload_rejects_free_text_where_a_closed_value_belongs() -> None:
    with pytest.raises(ValueError):
        pre_payload(skill=f"bad skill {MARKER}")
    with pytest.raises(ValueError):
        pre_payload(mode=MARKER)
    with pytest.raises(ValueError):
        pre_payload(labels=[MARKER])


def test_validate_payload_rejects_unknown_keys_and_wrong_hooks() -> None:
    p = pre_payload()
    hooks.validate_payload("pre-run", p)
    with pytest.raises(ValueError):
        hooks.validate_payload("pre-run", {**p, "pr_title": MARKER})
    with pytest.raises(ValueError):
        hooks.validate_payload("post-run", p)
    with pytest.raises(ValueError):
        hooks.validate_payload("nope", p)


def done_summary() -> dict[str, Any]:
    return {
        "verdict": "comment",
        "severity_counts": dict(blocker=0, major=1, minor=0, nit=0, info=0, unspecified=0),
        "risk": "low",
        "coverage_status": "full",
        "code_run_status": "not_configured",
        "code_run_reason": None,
        "skipped_files": 0,
        "normalized_count": 0,
        "head_moved": False,
    }


def post_run(**over: Any) -> dict[str, Any]:
    args: dict[str, Any] = dict(
        run_id=RUN,
        repo=REPO,
        labels=[],
        event_id=f"{RUN}-post-run-1",
        state="done",
        reason=None,
        exit_code=0,
        pr=PR,
        agent_id="ag-abcdefgh",
        draft_id="0" * 26,
        anchor_head_sha=SHA,
        actor="auto:unknown",
        decision_ids=["d-1"],
        warnings=["hook_failed"],
        summary=done_summary(),
        session_sandbox="required",
        tools_checked=True,
        tools_unexpected=0,
    )
    args.update(over)
    return hooks.post_run_payload(**args)


def test_post_run_payload_for_done_takes_only_the_listed_summary_fields() -> None:
    p = post_run()
    assert p["verdict"] == "comment" and p["head_moved"] is False
    assert p["event_id"] == f"{RUN}-post-run-1"
    assert "skipped_files" not in p and "code_run_reason" not in p


def test_post_run_payload_for_failed_has_no_done_only_keys() -> None:
    p = post_run(
        state="failed",
        reason="canceled",
        exit_code=143,
        agent_id=None,
        draft_id=None,
        anchor_head_sha=None,
        summary=None,
    )
    assert "verdict" not in p and "tools_checked" not in p
    assert p["reason"] == "canceled" and p["agent_id"] is None


def test_post_run_payload_refuses_inconsistent_values() -> None:
    with pytest.raises(ValueError):
        post_run(state="failed", reason="free text " + MARKER, exit_code=12)
    with pytest.raises(ValueError):
        post_run(state="failed", reason="canceled", exit_code=12, draft_id=None, summary=None)
    with pytest.raises(ValueError):
        post_run(summary=None)  # done needs its summary


def test_post_escalate_payload() -> None:
    p = hooks.post_escalate_payload(
        run_id=RUN,
        repo=REPO,
        labels=[],
        event_id=f"{RUN}-post-escalate-2",
        reason="needs_input",
        pr=PR,
        agent_id="ag-abcdefgh",
        actor="auto:unknown",
        decision_ids=[],
        session_sandbox="off",
        warnings=[],
    )
    assert p["hook"] == "post-escalate" and p["reason"] == "needs_input"
    with pytest.raises(ValueError):
        hooks.post_escalate_payload(
            run_id=RUN,
            repo=REPO,
            labels=[],
            event_id=f"{RUN}-post-escalate-2",
            reason=MARKER,
            pr=PR,
            agent_id="ag-abcdefgh",
            actor="auto:unknown",
            decision_ids=[],
            session_sandbox="off",
            warnings=[],
        )


# -- one hook call -------------------------------------------------------------------------


def spec(cmd: str = "gate", timeout: float = 30.0, is_async: bool = False) -> hooks.HookSpec:
    return hooks.HookSpec(cmd, timeout, is_async)


def test_a_hook_runs_through_sh_c_with_the_payload_on_stdin_only(
    fake_gateway: FakeGateway,
) -> None:
    fake_gateway.script(SH, ok())
    payload = pre_payload(labels=[f"k={MARKER}"])
    result = hooks.run_spec(spec("my cmd"), "pre-run", payload, env={"A": "1"})
    assert result.ok and result.code == "ok"
    (call,) = fake_gateway.calls
    assert call.argv == (SH, "-c", "my cmd")
    assert json.loads(call.stdin or "") == payload
    assert call.stdin is not None and "\n" not in call.stdin
    assert call.env == {"A": "1"}
    assert call.timeout == 30.0
    assert call.term_grace == hooks.TERM_GRACE_S
    fake_gateway.assert_no_marker(MARKER)  # the payload text is in stdin, never argv or env


def test_exit_codes_and_failures_are_closed_codes(fake_gateway: FakeGateway) -> None:
    fake_gateway.script(
        SH,
        ok(returncode=3, stderr=MARKER, stdout=MARKER),
        CallTimeout("t"),
        ToolMissing("m"),
        OutputTooLarge("o"),
        gw.GatewayError("g"),
        ok(returncode=-9),
    )
    got = [hooks.run_spec(spec(), "post-run", {}, env={}) for _ in range(6)]
    assert [r.code for r in got] == [
        "exit",
        "timeout",
        "tool_missing",
        "output_too_large",
        "error",
        "exit",
    ]
    assert not any(r.ok for r in got)
    assert got[0].returncode == 3 and got[1].returncode is None
    assert MARKER not in repr(got)  # stderr and stdout text are not kept in the failure


def test_a_shell_can_be_substituted(fake_gateway: FakeGateway) -> None:
    fake_gateway.script(sys.executable, ok())
    hooks.run_spec(spec("print(1)"), "post-run", {}, env={}, shell=(sys.executable, "-c"))
    assert fake_gateway.calls[0].argv == (sys.executable, "-c", "print(1)")


def test_an_async_spec_is_started_detached(fake_gateway: FakeGateway) -> None:
    result = hooks.run_spec(spec("bg", 7.0, True), "post-run", {"a": 1}, env={"A": "1"})
    assert result.ok
    (call,) = fake_gateway.calls
    assert call.detached and call.timeout == 7.0 and call.term_grace == hooks.TERM_GRACE_S
    assert json.loads(call.stdin or "") == {"a": 1}


def test_an_async_start_failure_is_a_failure(fake_gateway: FakeGateway) -> None:
    fake_gateway.script(SH, ToolMissing("m"))
    assert hooks.run_spec(spec("bg", 7.0, True), "post-run", {}, env={}).code == "tool_missing"


# -- pre-run -------------------------------------------------------------------------------


def script_pre(fake: FakeGateway, *results: Any) -> None:
    fake.script(SH, *results)


def run_pre(specs: list[hooks.HookSpec], **kw: Any) -> hooks.PreRunResult:
    return hooks.run_pre_run(specs, pre_payload(), object_id=RUN, base_env={}, **kw)


def test_no_hooks_continue(fake_gateway: FakeGateway) -> None:
    assert run_pre([]).outcome == "continue"
    assert fake_gateway.calls == []


def test_exit_zero_continues_and_every_hook_gets_the_pre_run_env(
    fake_gateway: FakeGateway,
) -> None:
    script_pre(fake_gateway, ok())
    result = run_pre([spec("a"), spec("b")])
    assert result.outcome == "continue" and result.hook is None
    assert [c.argv[2] for c in fake_gateway.calls] == ["a", "b"]
    assert fake_gateway.calls[0].env == {
        "REVIEW_RUN_HOOK": "pre-run",
        "REVIEW_RUN_ID": RUN,
        "REVIEW_RUN_OBJECT_TYPE": "run",
    }


def test_exit_13_escalates_and_the_remaining_hooks_still_run(fake_gateway: FakeGateway) -> None:
    script_pre(fake_gateway, ok(returncode=13), ok())
    result = run_pre([spec("a"), spec("b")])
    assert result.outcome == "escalate" and result.hook == "pre-run"
    assert len(fake_gateway.calls) == 2


def test_a_veto_after_an_escalation_vetoes(fake_gateway: FakeGateway) -> None:
    script_pre(fake_gateway, ok(returncode=13), ok(returncode=1))
    assert run_pre([spec("a"), spec("b")]).outcome == "veto"


def test_a_veto_stops_the_later_hooks(fake_gateway: FakeGateway) -> None:
    script_pre(fake_gateway, ok(returncode=1), ok())
    result = run_pre([spec("a"), spec("b")])
    assert result.outcome == "veto"
    assert len(fake_gateway.calls) == 1


@pytest.mark.parametrize("returncode", [1, 2, 11, 12, 14, 127, -9, -15])
def test_any_other_exit_vetoes(fake_gateway: FakeGateway, returncode: int) -> None:
    script_pre(fake_gateway, ok(returncode=returncode))
    assert run_pre([spec()]).outcome == "veto"


@pytest.mark.parametrize(
    "failure", [CallTimeout("t"), ToolMissing("m"), OutputTooLarge("o"), gw.GatewayError("g")]
)
def test_a_timeout_or_a_hook_that_cannot_run_vetoes(
    fake_gateway: FakeGateway, failure: gw.GatewayError
) -> None:
    script_pre(fake_gateway, failure)
    assert run_pre([spec()]).outcome == "veto"


def test_the_override_turns_an_escalation_into_a_pass_but_never_a_veto(
    fake_gateway: FakeGateway,
) -> None:
    script_pre(fake_gateway, ok(returncode=13))
    passed = run_pre([spec()], override_escalation=True)
    assert passed.outcome == "continue" and passed.overridden is True
    fake_gateway.scripts.clear()
    script_pre(fake_gateway, ok(returncode=13), ok(returncode=2))
    assert run_pre([spec(), spec()], override_escalation=True).outcome == "veto"


def test_without_the_flag_a_pass_is_not_marked_overridden(fake_gateway: FakeGateway) -> None:
    script_pre(fake_gateway, ok())
    assert run_pre([spec()], override_escalation=True).overridden is False


def stdout_script(text: str, returncode: int = 0) -> Any:
    return lambda call: CompletedRun(call.argv, returncode, text, MARKER)


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        ('{"decision_id": "gate:42"}', ("gate:42",)),
        ('{"decision_id": ["a", "b"]}', ("a", "b")),
        ('  {"decision_id": "x"}\n', ("x",)),
        ('{"decision_id": "has space"}', ()),
        ('{"decision_id": "' + "x" * 129 + '"}', ()),
        ('{"decision_id": 5}', ()),
        ('{"decision_id": ["ok", 5, "bad id", "fine"]}', ("ok", "fine")),
        ("not json", ()),
        ("[1]", ()),
        ('{"other": 1}', ()),
        ("", ()),
        ('{"decision_id": "' + "a" * 70_000 + '"}', ()),
    ],
)
def test_decision_ids_are_read_from_json_stdout_by_grammar(
    fake_gateway: FakeGateway, stdout: str, expected: tuple[str, ...]
) -> None:
    script_pre(fake_gateway, stdout_script(stdout))
    result = run_pre([spec()])
    assert result.decision_ids == expected
    assert MARKER not in repr(result)  # stderr is never reported


def test_decision_ids_of_all_hooks_are_kept_deduplicated_and_capped(
    fake_gateway: FakeGateway,
) -> None:
    many = json.dumps({"decision_id": [f"id-{i}" for i in range(40)]})
    script_pre(
        fake_gateway,
        stdout_script('{"decision_id": "id-0"}', 13),
        stdout_script(many, 1),
    )
    result = run_pre([spec(), spec()])
    assert result.outcome == "veto"
    assert len(result.decision_ids) == 32
    assert result.decision_ids[0] == "id-0"
    assert len(set(result.decision_ids)) == 32


def test_the_pre_run_payload_is_validated_before_any_hook_runs(
    fake_gateway: FakeGateway,
) -> None:
    with pytest.raises(ValueError):
        hooks.run_pre_run([spec()], {"hook": "pre-run", "x": MARKER}, object_id=RUN, base_env={})
    assert fake_gateway.calls == []


# -- the human-only override ---------------------------------------------------------------


def test_the_resulting_actor_must_be_human_even_on_a_tty() -> None:
    with pytest.raises(ReviewRunError):
        hooks.require_human_override(Caller("auto:ci", "flag", True))


def test_the_override_needs_a_human_actor() -> None:
    hooks.require_human_override(Caller("human:alice", "flag", False))
    hooks.require_human_override(Caller("human:bob", "tty", True))
    for caller in (
        Caller("auto:unknown", "default", False),
        Caller("auto:ci", "flag", True),
    ):
        with pytest.raises(ReviewRunError) as info:
            hooks.require_human_override(caller)
        assert info.value.code == "usage"
        assert info.value.exit_code == 2


# -- run_spec OSError, nested stdout, ids, shared grammar, audit record -------------------


@pytest.mark.parametrize("exc", [OSError(28, "no space"), RuntimeError("can't start new thread")])
def test_an_oserror_or_runtimeerror_from_the_gateway_is_a_failed_attempt(
    fake_gateway: FakeGateway, exc: OSError | RuntimeError
) -> None:
    fake_gateway.script(SH, exc)
    assert hooks.run_spec(spec(), "post-run", {}, env={}).code == "error"
    fake_gateway.script(SH, exc)
    assert hooks.run_spec(spec("bg", 5.0, True), "post-run", {}, env={}).code == "error"


@pytest.mark.parametrize(("code", "outcome"), [(1, "veto"), (13, "escalate"), (0, "continue")])
def test_deeply_nested_stdout_is_no_decision_id_and_never_crashes(
    fake_gateway: FakeGateway, code: int, outcome: str
) -> None:
    script_pre(fake_gateway, stdout_script("[" * 60000, code))
    result = run_pre([spec()])
    assert result.outcome == outcome and result.decision_ids == ()


def test_the_decision_id_grammar_is_the_one_shared_with_the_cli() -> None:
    from review_run import cli, errors

    assert cli._DECISION_ID_RE is errors.DECISION_ID_RE  # noqa: SLF001
    assert hooks.MAX_DECISION_IDS == errors.MAX_DECISION_IDS == cli.MAX_DECISION_IDS


def test_the_event_id_of_a_payload_must_belong_to_its_run_and_hook() -> None:
    common: dict[str, Any] = dict(
        run_id=RUN, repo=REPO, labels=[], pr=PR, agent_id=None, actor="auto:unknown",
        decision_ids=[], warnings=[],
    )  # fmt: skip
    with pytest.raises(ValueError):
        hooks.post_run_payload(
            **common, event_id="rr-zzzzzzzz-post-run-1", state="failed", reason="timeout",
            exit_code=12, draft_id=None, anchor_head_sha=None,
        )  # fmt: skip
    with pytest.raises(ValueError):
        hooks.post_run_payload(
            **common, event_id=f"{RUN}-post-run-2", state="failed", reason="timeout",
            exit_code=12, draft_id=None, anchor_head_sha=None,
        )  # fmt: skip
    with pytest.raises(ValueError):
        hooks.post_escalate_payload(
            **{**common, "agent_id": "ag-abcdefgh"},
            event_id=f"{RUN}-post-run-1", reason="needs_input", session_sandbox="required",
        )  # fmt: skip
    hooks.post_escalate_payload(
        **{**common, "agent_id": "ag-abcdefgh"},
        event_id=f"{RUN}-post-escalate-3",
        reason="needs_input",
        session_sandbox="required",
    )  # fmt: skip


AUDIT_INPUTS: dict[str, Any] = {
    "head_sha": SHA,
    "config_sha256": "b" * 64,
    "skill": "review-run:review-pr",
    "previous_run": None,
    "permission_profile_sha256": "b" * 64,
    "session_sandbox": "required",
    "setting_sources": [],
    "plugin_dirs_sha256": None,
    "override_escalation": False,
}


@pytest.mark.parametrize(
    ("outcome", "status", "exit_code"), [("veto", "vetoed", 11), ("escalate", "escalated", 13)]
)
def test_pre_run_audit_record_matches_the_audit_schema(
    outcome: str, status: str, exit_code: int
) -> None:
    from tests.conftest import validate_against

    caller = Caller("human:alice", "flag", True, ("d-1",), ("team=core",))
    result = hooks.PreRunResult(outcome, "pre-run", ("d-1", "gate:2"))  # type: ignore[arg-type]
    record = hooks.pre_run_audit_record(caller, result, RUN, PR, AUDIT_INPUTS)
    validate_against("audit.json", record)
    assert record["action"] == "run"
    assert record["result"] == {"status": status, "reason": None, "exit_code": exit_code}
    assert record["target"] == {"run_id": RUN, "pr": PR}
    assert record["decision_id"] == ["d-1", "gate:2"]
    assert record["labels"] == ["team=core"]
    assert record["event_id"] is None


def test_pre_run_audit_record_refuses_a_continue_outcome() -> None:
    with pytest.raises(ValueError):
        hooks.pre_run_audit_record(
            Caller("human:a", "flag", True), hooks.PreRunResult("continue"), RUN, PR, AUDIT_INPUTS
        )
