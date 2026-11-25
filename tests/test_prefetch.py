"""Pre-fetch (issue #12): resolve step, anchor, head consistency and failure mapping.

Most tests run the real fake siblings through the real gateway (``routed``); the table-driven and
hostile-output tests script the recording ``FakeGateway``.
"""

# ruff: noqa: E501
from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from review_run import gateway as gw
from review_run import prefetch as pf
from review_run.errors import ReviewRunError
from review_run.gateway import CompletedRun, GatewayError
from review_run.store import Expect, NewRun, Store, lease_name
from tests.fake_gateway import Call, FakeGateway
from tests.fakebin import _forgectl as fg
from tests.fakes import FakeBin

PR = "github.com/o/r#1"
CEILING = 8 * 1024 * 1024
MARKER = "PLANTED-MARKER-7f3a"
PINNED = Path(__file__).resolve().parent / "pinned_schemas"


class _Routed:
    """Send ``forgectl``/``pr-comment`` to the fake wrappers (the proc guard refuses bare names)."""

    def __init__(self, fb: FakeBin) -> None:
        self.fb = fb
        self.calls: list[tuple[str, ...]] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        stdin: str | None = None,
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
    ) -> CompletedRun:
        assert env is None, "pre-fetch must not build an environment"
        self.calls.append(tuple(argv))
        return gw.SystemGateway().run(
            [self.fb.argv0(argv[0]), *argv[1:]],
            timeout=timeout,
            stdin=stdin,
            env=self.fb.env(),
            cwd=str(self.fb.dir),
        )


@pytest.fixture
def routed(fakebin: FakeBin) -> Iterator[_Routed]:
    router = _Routed(fakebin)
    previous = gw.set_gateway(router)
    try:
        yield router
    finally:
        gw.set_gateway(previous)


def fetch(pr: str = PR, ceiling: int = CEILING) -> pf.Prefetch:
    return pf.prefetch(pr, hard_ceiling_bytes=ceiling)


def failure_of(pr: str = PR, ceiling: int = CEILING) -> pf.PrefetchFailure:
    with pytest.raises(pf.PrefetchFailure) as info:
        fetch(pr, ceiling)
    return info.value


def err(code: str, exit_code: int, **extra: Any) -> dict[str, Any]:
    return {"error": {"code": code, "message": "m", **extra}, "exit": exit_code}


# --------------------------------------------------------------------------------------------
# resolve step
# --------------------------------------------------------------------------------------------


def test_resolve_returns_the_canonical_facts(fakebin: FakeBin, routed: _Routed) -> None:
    facts = pf.resolve_pr("https://github.com/o/r/pull/1")
    assert (facts.pr, facts.repo, facts.state, facts.head_sha) == (
        PR,
        "github.com/o/r",
        "open",
        fg.HEAD,
    )
    assert facts.run_facts() == {
        "draft": False,
        "is_bot": False,
        "is_fork": False,
        "base_is_default": True,
    }
    assert routed.calls == [("forgectl", "show", "https://github.com/o/r/pull/1", "--json")]


@pytest.mark.parametrize("scenario", ["merged", "closed"])
def test_resolve_refuses_a_pr_that_is_not_open(
    fakebin: FakeBin, routed: _Routed, scenario: str
) -> None:
    fakebin.use("forgectl", scenario)
    with pytest.raises(ReviewRunError) as info:
        pf.resolve_pr(PR)
    env = info.value.envelope()["error"]
    assert info.value.exit_code == 20
    assert (env["code"], env["reason"], env["details"]) == (
        "refused",
        "not_open",
        {"state": scenario},
    )


def test_resolve_not_found_is_exit_10(fakebin: FakeBin, routed: _Routed) -> None:
    fakebin.use("forgectl", "not_found")
    with pytest.raises(ReviewRunError) as info:
        pf.resolve_pr(PR)
    env = info.value.envelope()["error"]
    assert info.value.exit_code == 10
    assert (env["code"], env["lower_code"], env["tool"]) == ("not_found", "not_found", "forgectl")


def test_resolve_unknown_host_is_exit_20(fakebin: FakeBin, routed: _Routed) -> None:
    fakebin.use("forgectl", "unknown_host")
    with pytest.raises(ReviewRunError) as info:
        pf.resolve_pr(PR)
    assert info.value.exit_code == 20
    assert info.value.envelope()["error"]["reason"] == "unknown_host"


def test_resolve_forgectl_exit_2_is_usage_with_its_code(fakebin: FakeBin, routed: _Routed) -> None:
    fakebin.script("forgectl", "show", err("invalid_pr", 2))
    with pytest.raises(ReviewRunError) as info:
        pf.resolve_pr(PR)
    env = info.value.envelope()["error"]
    assert info.value.exit_code == 2
    assert (env["code"], env["lower_code"]) == ("usage", "invalid_pr")


def test_resolve_rate_limited_is_lower_failed_with_retry_after(
    fakebin: FakeBin, routed: _Routed
) -> None:
    fakebin.use("forgectl", "rate_limited")
    with pytest.raises(ReviewRunError) as info:
        pf.resolve_pr(PR)
    env = info.value.envelope()["error"]
    assert info.value.exit_code == 12
    assert (env["code"], env["reason"], env["tool"], env["lower_code"]) == (
        "failed", "lower_failed", "forgectl", "rate_limited",
    )  # fmt: skip
    assert env["details"] == {"retry_after": 30}


def test_resolve_timeout_is_lower_failed(fakebin: FakeBin, routed: _Routed) -> None:
    fakebin.use("forgectl", "hang", command="show")

    class Quick(_Routed):
        def run(self, argv: Sequence[str], *, timeout: float, **kw: Any) -> CompletedRun:
            return super().run(argv, timeout=0.5, **kw)

    previous = gw.set_gateway(Quick(fakebin))
    try:
        with pytest.raises(ReviewRunError) as info:
            pf.resolve_pr(PR)
    finally:
        gw.set_gateway(previous)
    env = info.value.envelope()["error"]
    assert (info.value.exit_code, env["reason"], env["lower_code"]) == (
        12,
        "lower_failed",
        "timeout",
    )


@pytest.mark.parametrize(
    "arg",
    ["", "-h", "--json", "github.com/o/r", "github.com/o/r#0", "github.com/o/r#1\n", " github.com/o/r#1",
     "http://github.com/o/r/pull/1", "git․hub.com/o/r#1", "h" * 3000 + "/o#1", "a b/c#1", "x\0/y#1"],
)  # fmt: skip
def test_pr_argument_shape_is_usage_and_never_starts_a_process(
    fake_gateway: FakeGateway, arg: str
) -> None:
    with pytest.raises(ReviewRunError) as info:
        pf.resolve_pr(arg)
    assert info.value.exit_code == 2 and fake_gateway.calls == []
    assert arg not in info.value.message or arg == ""


@pytest.mark.parametrize("arg", [PR, "https://github.com/o/r/pull/1", "gitlab.com/g/s/p#12"])
def test_pr_argument_accepts_both_forms(arg: str) -> None:
    assert pf.check_pr_argument(arg) == arg


# --------------------------------------------------------------------------------------------
# anchor and the happy path
# --------------------------------------------------------------------------------------------


def test_prefetch_reads_in_order_with_the_canonical_pr(fakebin: FakeBin, routed: _Routed) -> None:
    result = fetch()
    assert routed.calls == [
        ("pr-comment", "draft", "new", PR, "--json"),
        ("forgectl", "files", PR, "--json"),
        ("forgectl", "diff", PR, "--json"),
        ("forgectl", "ci", PR, "--json"),
    ]
    assert result.attempts == 1 and result.pr == PR
    assert result.anchor == pf.Anchor(fg.HEAD, "main", fg.BASE, fg.MERGE_BASE)
    assert len(result.draft_id) == 26
    assert [f.path for f in result.files] == ["src/app.py"]
    assert result.diff.startswith("diff --git") and result.ci.state == "passing"
    assert result.ci.checks == (pf.CiCheck("build", "passing"),)


def test_null_merge_base_is_accepted(fakebin: FakeBin, routed: _Routed) -> None:
    fakebin.use("pr-comment", "merge_base_null")
    result = fetch()
    assert result.anchor.merge_base_sha is None and result.diff


def test_a_calls_use_the_gateway_timeout_and_no_env(fake_gateway: FakeGateway) -> None:
    script_all_ok(fake_gateway)
    fetch()
    assert {c.timeout for c in fake_gateway.calls} == {60.0}
    assert all(c.env is None and c.stdin is None for c in fake_gateway.calls)


# --------------------------------------------------------------------------------------------
# head consistency
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("on", ["files", "diff", "ci"])
def test_one_head_flip_retries_the_whole_prefetch_and_succeeds(
    fakebin: FakeBin, routed: _Routed, on: str
) -> None:
    fakebin.use("forgectl", "head_flip", on=on)
    result = fetch()
    assert result.attempts == 2 and result.anchor.head_sha == fg.HEAD
    drafts = [c for c in routed.calls if c[0] == "pr-comment"]
    assert len(drafts) == 2  # the retry starts from draft new
    assert len(routed.calls) == {"files": 1 + 1 + 4, "diff": 3 + 4, "ci": 4 + 4}[on]


@pytest.mark.parametrize("on", ["files", "diff", "ci"])
def test_two_head_flips_end_stale_head_without_partial_state(
    fakebin: FakeBin, routed: _Routed, on: str
) -> None:
    fakebin.use("forgectl", "head_flip_always", on=on)
    failure = failure_of()
    assert failure.reason == "stale_head" and failure.exit_code == 12 and failure.retryable
    assert len([c for c in routed.calls if c[0] == "pr-comment"]) == 2  # exactly one retry


def test_the_anchor_is_re_read_on_the_retry(fakebin: FakeBin, routed: _Routed) -> None:
    """The head moved before the retry: the new anchor is the one every read is compared with."""
    fakebin.script("pr-comment", "draft new", {"merge": {"anchor": {"head_sha": fg.NEW_HEAD}}}, {})
    fakebin.script("forgectl", "files", {})  # reports HEAD: mismatches the first anchor only
    fakebin.use("pr-comment", "head", sha=fg.HEAD)
    result = fetch()
    assert result.attempts == 2 and result.anchor.head_sha == fg.HEAD


def test_pr_comment_stale_head_passes_through_without_a_retry(
    fakebin: FakeBin, routed: _Routed
) -> None:
    fakebin.use("pr-comment", "stale_head")
    failure = failure_of()
    assert (failure.reason, failure.tool, failure.lower_code) == (
        "stale_head",
        "pr-comment",
        "stale_head",
    )
    assert len(routed.calls) == 1


# --------------------------------------------------------------------------------------------
# size failures
# --------------------------------------------------------------------------------------------


def test_files_incomplete_is_too_large_and_reads_no_diff(fakebin: FakeBin, routed: _Routed) -> None:
    fakebin.use("forgectl", "files_incomplete")
    failure = failure_of()
    assert failure.reason == "too_large" and not failure.retryable
    assert [c[1] for c in routed.calls if c[0] == "forgectl"] == ["files"]


def test_diff_too_large_refusal_is_too_large(fakebin: FakeBin, routed: _Routed) -> None:
    fakebin.use("forgectl", "too_large")
    failure = failure_of()
    assert (failure.reason, failure.lower_code, failure.lower_reason) == (
        "too_large",
        "too_large",
        "too_large",
    )
    assert [c[1] for c in routed.calls if c[0] == "forgectl"] == ["files", "diff"]


def test_diff_above_the_hard_ceiling_is_too_large(fakebin: FakeBin, routed: _Routed) -> None:
    size = len(fg.DIFF_TEXT.encode())
    assert fetch(ceiling=size).diff == fg.DIFF_TEXT  # at the ceiling: fine
    failure = failure_of(ceiling=size - 1)
    assert failure.reason == "too_large" and failure.lower_code == "hard_ceiling"


def test_the_ceiling_counts_bytes_not_characters(fake_gateway: FakeGateway) -> None:
    script_all_ok(fake_gateway, diff="é" * 10)  # 10 characters, 20 bytes
    assert fetch(ceiling=20).diff
    script_all_ok(fake_gateway, diff="é" * 10)
    assert failure_of(ceiling=19).reason == "too_large"


# --------------------------------------------------------------------------------------------
# failure mapping
# --------------------------------------------------------------------------------------------


def test_pr_closed_during_prefetch_is_prefetch_failed_with_the_siblings_code(
    fakebin: FakeBin, routed: _Routed
) -> None:
    fakebin.script(
        "pr-comment",
        "draft new",
        err("refused", 20, reason="not_open", details={"state": "merged"}),
    )
    failure = failure_of()
    assert (failure.reason, failure.tool, failure.lower_code, failure.lower_reason) == (
        "prefetch_failed", "pr-comment", "not_open", "not_open",
    )  # fmt: skip
    assert failure.failure() == {
        "tool": "pr-comment", "lower_code": "not_open", "lower_reason": "not_open",
        "retryable": False, "retry_after_s": None, "errors": [],
    }  # fmt: skip


def test_not_found_during_prefetch_is_prefetch_failed(fakebin: FakeBin, routed: _Routed) -> None:
    fakebin.script("forgectl", "ci", err("not_found", 10))
    failure = failure_of()
    assert (failure.reason, failure.tool, failure.lower_code) == (
        "prefetch_failed",
        "forgectl",
        "not_found",
    )


def test_rate_limited_is_retryable_with_retry_after(fakebin: FakeBin, routed: _Routed) -> None:
    fakebin.script("forgectl", "files", err("rate_limited", 12, retry_after=2.2))
    failure = failure_of()
    assert failure.retryable and failure.retry_after_s == 3 and failure.lower_code == "rate_limited"


FORGECTL_CODES = sorted(
    json.loads((PINNED / "forgectl/schemas/defs.json").read_text())["$defs"]["error_code"]["enum"]
)
FORGECTL_REASONS = sorted(
    json.loads((PINNED / "forgectl/schemas/defs.json").read_text())["$defs"]["refusal_reason"][
        "enum"
    ]
)
PR_COMMENT_CODES = sorted(
    json.loads((PINNED / "pr-comment/schemas/defs.json").read_text())["$defs"]["error_code"]["enum"]
)
EXITS = [1, 2, 10, 11, 12, 13, 20, 21, 99]


def envelope_run(code: str, exit_code: int, **extra: Any) -> CompletedRun:
    body = {"error": {"code": code, "message": f"{MARKER} text", **extra}}
    return CompletedRun(("x",), exit_code, json.dumps(body), f"{MARKER} stderr")


def show_ok() -> str:
    return json.dumps(fg_default_show())


def fg_default_show() -> dict[str, Any]:
    return fg.default_pr("github.com", "o/r", 1, "github")


@pytest.mark.parametrize("exit_code", EXITS)
@pytest.mark.parametrize("code", FORGECTL_CODES)
def test_resolve_mapping_table_covers_every_forgectl_code_and_exit(
    fake_gateway: FakeGateway, code: str, exit_code: int
) -> None:
    fake_gateway.script("forgectl", envelope_run(code, exit_code))
    with pytest.raises(ReviewRunError) as info:
        pf.resolve_pr(PR)
    e = info.value
    env = e.envelope()["error"]
    if exit_code == 10:
        assert (e.exit_code, env["code"], env["lower_code"]) == (10, "not_found", "not_found")
    elif exit_code == 2:
        assert (e.exit_code, env["code"], env["lower_code"]) == (2, "usage", code)
    else:  # exit 20 without a reason is "any other failure" too
        assert (e.exit_code, env["code"], env["reason"], env["tool"], env["lower_code"]) == (
            12, "failed", "lower_failed", "forgectl", code,
        )  # fmt: skip
    assert MARKER not in json.dumps(env) and MARKER not in str(e) and MARKER not in e.human()


@pytest.mark.parametrize("reason", FORGECTL_REASONS)
def test_resolve_mapping_table_covers_every_refusal_reason(
    fake_gateway: FakeGateway, reason: str
) -> None:
    fake_gateway.script("forgectl", envelope_run("refused", 20, reason=reason))
    with pytest.raises(ReviewRunError) as info:
        pf.resolve_pr(PR)
    env = info.value.envelope()["error"]
    if reason in ("unknown_host", "not_open"):
        assert (info.value.exit_code, env["code"], env["reason"]) == (20, "refused", reason)
    else:
        assert (info.value.exit_code, env["reason"], env["lower_code"]) == (
            12,
            "lower_failed",
            reason,
        )


GATEWAY_ERRORS = [
    (gw.CallTimeout("t"), "timeout"),
    (gw.ToolMissing("t"), "tool_missing"),
    (gw.OutputTooLarge("t"), "output_too_large"),
    (gw.GatewayError("t"), "failed"),
]


@pytest.mark.parametrize(("exc", "lower"), GATEWAY_ERRORS)
def test_resolve_maps_every_gateway_failure(
    fake_gateway: FakeGateway, exc: Exception, lower: str
) -> None:
    fake_gateway.script("forgectl", exc)  # type: ignore[arg-type]
    with pytest.raises(ReviewRunError) as info:
        pf.resolve_pr(PR)
    env = info.value.envelope()["error"]
    assert (info.value.exit_code, env["reason"], env["tool"], env["lower_code"]) == (
        12, "lower_failed", "forgectl", lower,
    )  # fmt: skip


def script_all_ok(fake: FakeGateway, **diff_over: Any) -> None:
    """Scripted, valid answers for the four reads of one attempt (the fake's defaults)."""
    pr = fg_default_show()
    doc = {
        "schema_version": 1,
        "draft_id": "01" + "A" * 24,
        "pr": PR,
        "anchor": {"head_sha": fg.HEAD, "base_ref": "main", "base_sha": fg.BASE, "merge_base_sha": None},
        "event": "comment", "body": "", "findings": [],
    }  # fmt: skip
    fake.scripts.clear()
    fake.script("pr-comment", CompletedRun(("x",), 0, json.dumps(doc), ""))
    docs = {
        "files": fg.default_files(pr),
        "diff": {**fg.default_diff(pr), "diff": diff_over.get("diff", fg.DIFF_TEXT)},
        "ci": fg.default_ci(pr),
    }

    def by_command(call: Any) -> CompletedRun:
        return CompletedRun(call.argv, 0, json.dumps(docs[call.argv[1]]), "")

    fake.scripts["forgectl"] = [by_command]


def override(
    fake: FakeGateway, command: str, reply: Callable[[], CompletedRun | GatewayError]
) -> None:
    """Make ``command`` (``draft`` is pr-comment's) answer ``reply()``; the others keep their script."""
    tool = "pr-comment" if command == "draft" else "forgectl"
    base = fake.scripts[tool][0]

    def run(call: Call) -> CompletedRun:
        if call.argv[1] == command:
            answer = reply()
            if isinstance(answer, GatewayError):
                raise answer
            return answer
        assert not isinstance(base, GatewayError)
        return base(call) if callable(base) else base

    fake.scripts[tool] = [run]


@pytest.mark.parametrize("exit_code", EXITS)
@pytest.mark.parametrize("code", sorted(set(FORGECTL_CODES) | set(PR_COMMENT_CODES)))
@pytest.mark.parametrize(("tool", "command"), [("pr-comment", "draft"), ("forgectl", "files"),
                                               ("forgectl", "diff"), ("forgectl", "ci")])  # fmt: skip
def test_prefetch_mapping_table_covers_every_code_exit_and_read(
    fake_gateway: FakeGateway, tool: str, command: str, code: str, exit_code: int
) -> None:
    script_all_ok(fake_gateway)
    override(fake_gateway, command, lambda: envelope_run(code, exit_code, retry_after=4))
    failure = failure_of()
    assert (failure.reason, failure.tool, failure.exit_code) == ("prefetch_failed", tool, 12)
    assert failure.lower_code == code and failure.lower_reason is None
    assert failure.retryable == (code in ("rate_limited", "network_error"))
    assert failure.retry_after_s == 4
    assert MARKER not in json.dumps(failure.failure()) and MARKER not in str(failure)


@pytest.mark.parametrize(
    "reason", sorted(set(FORGECTL_REASONS) | {"auto_approve_disabled", "plan_changed"})
)
@pytest.mark.parametrize(("tool", "command"), [("pr-comment", "draft"), ("forgectl", "files"),
                                               ("forgectl", "diff"), ("forgectl", "ci")])  # fmt: skip
def test_prefetch_mapping_table_covers_every_refusal_reason(
    fake_gateway: FakeGateway, tool: str, command: str, reason: str
) -> None:
    script_all_ok(fake_gateway)
    override(fake_gateway, command, lambda: envelope_run("refused", 20, reason=reason))
    failure = failure_of()
    expect = {"stale_head": "stale_head", "too_large": "too_large"}.get(reason, "prefetch_failed")
    assert (failure.reason, failure.lower_code, failure.tool) == (expect, reason, tool)
    assert failure.lower_reason == reason


@pytest.mark.parametrize(("exc", "lower"), GATEWAY_ERRORS)
@pytest.mark.parametrize("command", ["draft", "files", "diff", "ci"])
def test_prefetch_maps_every_gateway_failure_on_every_read(
    fake_gateway: FakeGateway, command: str, exc: gw.GatewayError, lower: str
) -> None:
    script_all_ok(fake_gateway)
    tool = "pr-comment" if command == "draft" else "forgectl"
    override(fake_gateway, command, lambda: exc)
    failure = failure_of()
    assert (failure.reason, failure.tool, failure.lower_code, failure.retryable) == (
        "prefetch_failed", tool, lower, False,
    )  # fmt: skip
    assert failure.exit_code == 12


# --------------------------------------------------------------------------------------------
# hostile output
# --------------------------------------------------------------------------------------------

DEEP = "[" * 200_000 + "]" * 200_000
BAD_DOCS = [
    "", "not json", "null", "[]", "42", '"s"', "{", DEEP, '{"a": NaN}', '{"a": Infinity}',
    '{"a": 1, "a": 2}', "{}", "9" * 6000,
]  # fmt: skip


@pytest.mark.parametrize("raw", BAD_DOCS)
@pytest.mark.parametrize("command", ["draft", "files", "diff", "ci"])
def test_garbage_from_any_read_is_prefetch_failed_invalid_output(
    fake_gateway: FakeGateway, command: str, raw: str
) -> None:
    script_all_ok(fake_gateway)
    tool = "pr-comment" if command == "draft" else "forgectl"
    override(fake_gateway, command, lambda: CompletedRun(("x",), 0, raw, ""))
    failure = failure_of()
    assert (failure.reason, failure.tool, failure.lower_code) == (
        "prefetch_failed",
        tool,
        "invalid_output",
    )


@pytest.mark.parametrize("raw", BAD_DOCS)
def test_garbage_from_show_is_lower_failed(fake_gateway: FakeGateway, raw: str) -> None:
    fake_gateway.script("forgectl", CompletedRun(("x",), 0, raw, ""))
    with pytest.raises(ReviewRunError) as info:
        pf.resolve_pr(PR)
    env = info.value.envelope()["error"]
    assert (info.value.exit_code, env["reason"], env["lower_code"]) == (
        12,
        "lower_failed",
        "invalid_output",
    )


def mutate(doc: dict[str, Any], path: str, value: Any) -> dict[str, Any]:
    out = json.loads(json.dumps(doc))
    node = out
    *parents, last = path.split(".")
    for p in parents:
        node = node[p]
    if value is KeyError:
        del node[last]
    else:
        node[last] = value
    return out


SHOW_MUTATIONS = [
    ("pr", 5), ("pr", "github.com/o/r#01"), ("pr", "github.com/other#1"), ("repo", None), ("number", True),
    ("number", 0), ("number", "1"), ("state", "OPEN"), ("state", None), ("title", None), ("title", 5),
    ("body", 5), ("labels", "x"), ("labels", [1]), ("draft", "no"), ("draft", None), ("author", None),
    ("author.is_bot", "no"), ("author.login", 5), ("head", []), ("head.sha", "abc"), ("head.sha", "A" * 40),
    ("head.ref", ""), ("head.ref", "a\nb"), ("head.ref", "a‮b"), ("head.is_fork", 1), ("base.ref", None),
    ("base.is_default", "yes"), ("head", KeyError), ("base", KeyError), ("author", KeyError),
]  # fmt: skip


@pytest.mark.parametrize(("path", "value"), SHOW_MUTATIONS)
def test_show_with_a_wrong_shape_is_invalid_output(
    fake_gateway: FakeGateway, path: str, value: Any
) -> None:
    doc = mutate(fg_default_show(), path, value)
    fake_gateway.script("forgectl", CompletedRun(("x",), 0, json.dumps(doc), ""))
    with pytest.raises(ReviewRunError) as info:
        pf.resolve_pr(PR)
    assert info.value.envelope()["error"]["lower_code"] == "invalid_output"


def test_show_extra_fields_and_hostile_text_are_tolerated_and_kept_raw(
    fake_gateway: FakeGateway,
) -> None:
    doc = fg_default_show()
    doc.update(surprise={"x": [1, 2]}, title="a‮b" + "T" * 100_000, labels=["l" * 5000] * 500)
    doc["body"] = None
    fake_gateway.script("forgectl", CompletedRun(("x",), 0, json.dumps(doc), ""))
    facts = pf.resolve_pr(PR)
    assert facts.title.startswith("a‮b") and facts.body is None and len(facts.labels) == 500


def test_untrusted_text_never_reaches_argv_or_env(fake_gateway: FakeGateway) -> None:
    doc = fg_default_show()
    doc.update(title=MARKER, body=MARKER, labels=[MARKER])
    doc["head"]["ref"] = MARKER
    fake_gateway.script("forgectl", CompletedRun(("x",), 0, json.dumps(doc), ""))
    facts = pf.resolve_pr(PR)
    assert MARKER in facts.title
    script_all_ok(fake_gateway, diff=f"diff --git a/{MARKER} b/{MARKER}\n")
    fetch(facts.pr)
    fake_gateway.assert_no_marker(MARKER)


DRAFT_MUTATIONS = [
    ("schema_version", 2), ("schema_version", True), ("schema_version", None), ("draft_id", "short"),
    ("draft_id", "i" * 26), ("draft_id", 5), ("pr", "github.com/o/r#2"), ("pr", None), ("anchor", None),
    ("anchor.head_sha", None), ("anchor.head_sha", "ABC"), ("anchor.base_ref", ""), ("anchor.base_ref", "a b"),
    ("anchor.base_ref", "x" * 1025), ("anchor.base_sha", "zz"), ("anchor.merge_base_sha", 7),
    ("anchor.base_sha", KeyError),
]  # fmt: skip


@pytest.mark.parametrize(("path", "value"), DRAFT_MUTATIONS)
def test_draft_new_with_a_wrong_shape_is_prefetch_failed(
    fake_gateway: FakeGateway, path: str, value: Any
) -> None:
    script_all_ok(fake_gateway)
    good = json.loads(fake_gateway.scripts["pr-comment"][0].stdout)  # type: ignore[union-attr]
    bad = mutate(good, path, value)
    fake_gateway.scripts["pr-comment"] = [CompletedRun(("x",), 0, json.dumps(bad), "")]
    failure = failure_of()
    want = "draft_pr_mismatch" if path == "pr" and isinstance(value, str) else "invalid_output"
    assert (failure.reason, failure.tool, failure.lower_code) == (
        "prefetch_failed",
        "pr-comment",
        want,
    )


FILES_MUTATIONS = [
    ("files", None), ("files", [1]), ("files", [{"path": "a"}]), ("files_complete", "yes"),
    ("files_complete", KeyError), ("files.0.path", ""), ("files.0.path", "a\nb"), ("files.0.status", "weird"),
    ("files.0.additions", -1), ("files.0.additions", True), ("files.0.previous_path", 5),
    ("changed_files", "3"), ("pr", "github.com/o/r#2"), ("head_sha", "short"), ("head_sha", None),
]  # fmt: skip


def deep_mutate(doc: dict[str, Any], path: str, value: Any) -> dict[str, Any]:
    out = json.loads(json.dumps(doc))
    node: Any = out
    *parents, last = path.split(".")
    for p in parents:
        node = node[int(p)] if isinstance(node, list) else node[p]
    key: Any = int(last) if isinstance(node, list) else last
    if value is KeyError:
        del node[key]
    else:
        node[key] = value
    return out


@pytest.mark.parametrize(("path", "value"), FILES_MUTATIONS)
def test_files_with_a_wrong_shape_is_prefetch_failed(
    fake_gateway: FakeGateway, path: str, value: Any
) -> None:
    script_all_ok(fake_gateway)
    good = fg.default_files(fg_default_show())
    bad = deep_mutate(good, path, value)
    override(fake_gateway, "files", lambda: CompletedRun(("x",), 0, json.dumps(bad), ""))
    failure = failure_of()
    assert (failure.reason, failure.lower_code) == ("prefetch_failed", "invalid_output")


CI_MUTATIONS = [
    ("ci", None), ("ci.state", "great"), ("ci.checks", "x"), ("ci.checks", [{"name": 1, "state": "passing"}]),
    ("ci.checks", [{"name": "a", "state": "weird"}]), ("ci.complete", None), ("ci.for_current_head", "t"),
    ("pr", "github.com/o/r#2"), ("head_sha", 5),
]  # fmt: skip


@pytest.mark.parametrize(("path", "value"), CI_MUTATIONS)
def test_ci_with_a_wrong_shape_is_prefetch_failed(
    fake_gateway: FakeGateway, path: str, value: Any
) -> None:
    script_all_ok(fake_gateway)
    bad = deep_mutate(fg.default_ci(fg_default_show()), path, value)
    override(fake_gateway, "ci", lambda: CompletedRun(("x",), 0, json.dumps(bad), ""))
    assert failure_of().lower_code == "invalid_output"


def test_a_diff_that_is_not_text_is_invalid_output(fake_gateway: FakeGateway) -> None:
    script_all_ok(fake_gateway)
    bad = {**fg.default_diff(fg_default_show()), "diff": ["x"]}
    override(fake_gateway, "diff", lambda: CompletedRun(("x",), 0, json.dumps(bad), ""))
    assert failure_of().lower_code == "invalid_output"


def test_extra_fields_in_every_read_are_tolerated(fake_gateway: FakeGateway) -> None:
    script_all_ok(fake_gateway)
    result = fetch()
    assert result.attempts == 1  # baseline; the fakes' documents carry many unused fields


@pytest.mark.parametrize("bad", [True, False, "5", -1, float("nan"), float("inf"), None, [3]])
def test_a_bad_retry_after_is_dropped(fake_gateway: FakeGateway, bad: Any) -> None:
    body = json.dumps({"error": {"code": "rate_limited", "message": "m", "retry_after": bad}})
    fake_gateway.script("forgectl", CompletedRun(("x",), 12, body, ""))
    with pytest.raises(ReviewRunError) as info:
        pf.resolve_pr(PR)
    assert info.value.envelope()["error"].get("details") is None


def test_a_huge_retry_after_is_clamped(fake_gateway: FakeGateway) -> None:
    body = '{"error": {"code": "rate_limited", "message": "m", "retry_after": 1e300}}'
    fake_gateway.script("forgectl", CompletedRun(("x",), 12, body, ""))
    with pytest.raises(ReviewRunError) as info:
        pf.resolve_pr(PR)
    assert info.value.envelope()["error"]["details"] == {"retry_after": 2**31 - 1}


@pytest.mark.parametrize("code", ["Bad Code", "x" * 65, "", "../x", "a\nb", 5, None, ["a"]])
def test_a_hostile_error_code_never_becomes_a_lower_code(
    fake_gateway: FakeGateway, code: Any
) -> None:
    body = json.dumps({"error": {"code": code, "message": MARKER, "reason": code}})
    fake_gateway.script("forgectl", CompletedRun(("x",), 12, body, ""))
    with pytest.raises(ReviewRunError) as info:
        pf.resolve_pr(PR)
    env = info.value.envelope()["error"]
    assert env["lower_code"] == "failed" and MARKER not in json.dumps(env)


# --------------------------------------------------------------------------------------------
# persistence
# --------------------------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    with Store.open(tmp_path / "state") as s:
        yield s


def make_run(store: Store) -> str:
    run_id = store.reserve_id()
    store.create_run(
        NewRun(
            run_id=run_id,
            pr=PR,
            repo="github.com/o/r",
            head_sha=fg.HEAD,
            mode="headless",
            skill="review-run:review-pr",
        )  # fmt: skip
    )
    return run_id


def sample_prefetch(fakebin: FakeBin, routed: _Routed) -> tuple[pf.PrFacts, pf.Prefetch]:
    return pf.resolve_pr(PR), fetch()


def test_persist_writes_pr_json_and_the_anchor(
    store: Store, fakebin: FakeBin, routed: _Routed
) -> None:
    facts, result = sample_prefetch(fakebin, routed)
    run_id = make_run(store)
    assert pf.persist_prefetch(store, run_id, Expect("running", "prefetch", None), facts, result)
    path = store.run_dir(run_id) / "pr.json"
    assert json.loads(path.read_text()) == {
        "base_is_default": True,
        "draft": False,
        "is_bot": False,
        "is_fork": False,
    }
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    row = store.get_run(run_id)
    assert row is not None and row.draft_id == result.draft_id
    assert row.anchor == result.anchor.to_dict()


def test_persist_loses_to_a_moved_row_and_leaves_the_row_alone(
    store: Store, fakebin: FakeBin, routed: _Routed
) -> None:
    facts, result = sample_prefetch(fakebin, routed)
    run_id = make_run(store)
    assert not pf.persist_prefetch(store, run_id, Expect("running", "session", None), facts, result)
    row = store.get_run(run_id)
    assert row is not None and row.draft_id is None and row.anchor is None


def test_a_failed_prefetch_leaves_no_state(store: Store, fakebin: FakeBin, routed: _Routed) -> None:
    run_id = make_run(store)
    fakebin.use("forgectl", "head_flip_always", on="ci")
    assert failure_of().reason == "stale_head"
    row = store.get_run(run_id)
    assert row is not None and row.draft_id is None and row.anchor is None
    assert not (store.run_dir(run_id) / "pr.json").exists()


# --------------------------------------------------------------------------------------------
# --previous
# --------------------------------------------------------------------------------------------

REVIEW = {"event": "comment", "body": "ok", "findings": []}


def done_run(store: Store, data: bytes | None = None, *, size: int | None = None) -> str:
    run_id = make_run(store)
    data = json.dumps(REVIEW).encode() if data is None else data
    (store.run_dir(run_id) / "review.json").write_bytes(data)
    lease = store.acquire_lease(lease_name(run_id))
    assert lease is not None
    assert store.begin_finalize(run_id, Expect("running", "prefetch", None), lease=lease)
    assert store.end_run(
        run_id, Expect("running", "finalize", None), lease=lease, state="done", reason=None, exit_code=0,
        fields={"artifact_sha256": hashlib.sha256(json.dumps(REVIEW).encode()).hexdigest(),
                "artifact_size": len(json.dumps(REVIEW).encode()) if size is None else size},
    )  # fmt: skip
    return run_id


def previous_failure(store: Store, run_id: str, max_bytes: int = 1 << 20) -> pf.PrefetchFailure:
    with pytest.raises(pf.PrefetchFailure) as info:
        pf.load_previous(store, run_id, max_bytes=max_bytes)
    assert info.value.reason == "prefetch_failed" and info.value.tool is None
    return info.value


def test_previous_is_read_and_verified(store: Store) -> None:
    run_id = done_run(store)
    prev = pf.load_previous(store, run_id, max_bytes=1 << 20)
    assert prev.review == REVIEW and prev.run_id == run_id


def test_previous_tampered_after_storing_is_modified(store: Store) -> None:
    run_id = done_run(store)
    (store.run_dir(run_id) / "review.json").write_bytes(b'{"event": "comment", "body": "evil"}')
    assert previous_failure(store, run_id).lower_code == "previous_modified"


def test_previous_symlink_is_never_followed(store: Store, tmp_path: Path) -> None:
    run_id = done_run(store)
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps(REVIEW))
    path = store.run_dir(run_id) / "review.json"
    path.unlink()
    os.symlink(target, path)
    assert previous_failure(store, run_id).lower_code == "previous_modified"


def test_previous_size_mismatch_and_over_cap(store: Store) -> None:
    assert previous_failure(store, done_run(store, size=3)).lower_code == "previous_modified"
    assert previous_failure(store, done_run(store), max_bytes=5).lower_code == "previous_modified"


def test_previous_missing_cases(store: Store) -> None:
    unfinished = make_run(store)
    assert previous_failure(store, unfinished).lower_code == "previous_missing"
    assert previous_failure(store, "rr-aaaaaaaa").lower_code == "previous_missing"
    run_id = done_run(store)
    (store.run_dir(run_id) / "review.json").unlink()
    assert previous_failure(store, run_id).lower_code == "previous_missing"


@pytest.mark.parametrize(
    "data", [b"[1]", b"not json", b'{"a":1,"a":2}', b"\xff\xfe", b'{"a": NaN}', b"[" * 100000]
)
def test_previous_that_is_not_one_object_is_invalid(store: Store, data: bytes) -> None:
    run_id = make_run(store)
    (store.run_dir(run_id) / "review.json").write_bytes(data)
    lease = store.acquire_lease(lease_name(run_id))
    assert lease is not None
    assert store.begin_finalize(run_id, Expect("running", "prefetch", None), lease=lease)
    assert store.end_run(
        run_id, Expect("running", "finalize", None), lease=lease, state="done", reason=None, exit_code=0,
        fields={"artifact_sha256": hashlib.sha256(data).hexdigest(), "artifact_size": len(data)},
    )  # fmt: skip
    assert previous_failure(store, run_id).lower_code == "previous_invalid"
