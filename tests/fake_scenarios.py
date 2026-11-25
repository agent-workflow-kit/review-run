"""Named scenarios for the fakes: ``SCENARIOS[tool][name](**params)`` returns scenario keys.

One scenario per case per fake (issue #10). A scenario is a dict of ``command -> steps`` plus
tool-wide keys; ``FakeBin.use`` merges it into the test's scenario (the format is described in
``tests/fakebin/_common.py``).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from tests.fakebin import _forgectl as fg

Scenario = dict[str, Any]
SCENARIOS: dict[str, dict[str, Callable[..., Scenario]]] = {}


def scenario(tool: str) -> Callable[[Callable[..., Scenario]], Callable[..., Scenario]]:
    def register(fn: Callable[..., Scenario]) -> Callable[..., Scenario]:
        SCENARIOS.setdefault(tool, {})[fn.__name__] = fn
        return fn

    return register


def _err(exit_code: int, code: str, message: str = "fake failure", **extra: Any) -> Scenario:
    return {"error": {"code": code, "message": message, **extra}, "exit": exit_code}


# ---- forgectl -----------------------------------------------------------------------------


@scenario("forgectl")
def ok() -> Scenario:
    return {}


@scenario("forgectl")
def merged() -> Scenario:
    return {
        "pr": {
            "state": "merged",
            "closed_at": "2026-01-03T00:00:00Z",
            "merged_at": "2026-01-03T00:00:00Z",
            "merged_by": "fake-maintainer",
            "merged_head_sha": fg.HEAD,
            "merge_commit_sha": "e5" * 20,
            "mergeable": None,
        }
    }


@scenario("forgectl")
def closed() -> Scenario:
    return {"pr": {"state": "closed", "closed_at": "2026-01-03T00:00:00Z", "mergeable": None}}


@scenario("forgectl")
def draft() -> Scenario:
    return {"pr": {"draft": True}}


@scenario("forgectl")
def head_flip(on: str = "ci") -> Scenario:
    """The head moves under the reader: ``on`` reports a new head once, then it holds still."""
    moved: dict[str, Any] = {
        "head_sha": fg.NEW_HEAD,
    }
    if on == "ci":
        moved["ci"] = {"head_sha": fg.NEW_HEAD}
    return {on: [{"merge": moved}, {}]}


@scenario("forgectl")
def head_flip_always(on: str = "ci") -> Scenario:
    moved: dict[str, Any] = {"head_sha": fg.NEW_HEAD}
    if on == "ci":
        moved["ci"] = {"head_sha": fg.NEW_HEAD}
    return {on: [{"merge": moved}]}


@scenario("forgectl")
def files_incomplete() -> Scenario:
    return {"files": [{"merge": {"files_complete": False, "changed_files": 3001}}]}


@scenario("forgectl")
def too_large() -> Scenario:
    return {
        "diff": [
            _err(
                20,
                "refused",
                "the diff is too large to review whole",
                reason="too_large",
            )
        ]
    }


@scenario("forgectl")
def not_found() -> Scenario:
    return {"show": [_err(10, "not_found", "no such pull request")]}


@scenario("forgectl")
def unknown_host() -> Scenario:
    return {"hosts": []}


@scenario("forgectl")
def rate_limited() -> Scenario:
    return {"show": [_err(12, "rate_limited", "rate limited", retry_after=30)]}


@scenario("forgectl")
def ci_failing() -> Scenario:
    check = {"name": "build", "state": "failing", "required": True, "counted": True, "url": None,
             "finished_at": None}  # fmt: skip
    return {"ci": [{"merge": {"ci": {"state": "failing", "checks": [check]}}}]}


@scenario("forgectl")
def ci_none() -> Scenario:
    return {"ci": [{"json": {**_ci_none()}}]}


def _ci_none() -> dict[str, Any]:
    return {
        "pr": "github.com/o/r#1",
        "head_sha": fg.HEAD,
        "ci": {"state": "none", "head_sha": fg.HEAD, "for_current_head": True,
               "complete": True, "checks": []},
    }  # fmt: skip


@scenario("forgectl")
def hang(command: str = "show") -> Scenario:
    return {command: [{"hang": True}]}


# ---- pr-comment ---------------------------------------------------------------------------


@scenario("pr-comment")
def draft_ok() -> Scenario:
    return {}


@scenario("pr-comment")
def merge_base_null() -> Scenario:
    """``forgectl diff`` was too large: pr-comment still drafts, with ``merge_base_sha: null``."""
    return {"merge_base_sha": None}


@scenario("pr-comment")
def stale_head() -> Scenario:
    return {
        "draft new": [
            _err(20, "refused", "the head did not hold still", reason="stale_head",
                 details={"head_sha": fg.NEW_HEAD})  # fmt: skip
        ]
    }


@scenario("pr-comment")
def head(sha: str) -> Scenario:
    return {"head_sha": sha}


@scenario("pr-comment")
def check_forced_invalid() -> Scenario:
    doc = {
        "valid": False,
        "schema_version": 1,
        "draft_id": None,
        "findings": 1,
        "errors": [{"pointer": "/findings/0/path", "message": "does not match the schema",
                    "kind": None}],
    }  # fmt: skip
    return {"draft check": [{"json": doc, "exit": 2}]}


@scenario("pr-comment")
def lower_failed(exit_code: int = 12, code: str = "upstream_error") -> Scenario:
    """forgectl failed under pr-comment: its code is passed through."""
    return {
        "draft new": [_err(exit_code, code, "forgectl failed", retry_after=None, landed=False,
                           retry="same_command")]  # fmt: skip
    }


@scenario("pr-comment")
def hang(command: str = "draft check") -> Scenario:
    return {command: [{"hang": True}]}


# ---- wtctl --------------------------------------------------------------------------------


@scenario("wtctl")
def ok() -> Scenario:
    return {}


@scenario("wtctl")
def ref_not_found() -> Scenario:
    return {"create": [_err(12, "ref_not_found", "the ref is not in the repository")]}


@scenario("wtctl")
def fetch_failed() -> Scenario:
    return {"create": [_err(12, "fetch_failed", "fetch failed")]}


@scenario("wtctl")
def auth_required() -> Scenario:
    return {"create": [_err(12, "auth_required", "authentication required")]}


@scenario("wtctl")
def create_vetoed() -> Scenario:
    return {"create": [_err(11, "vetoed", "vetoed", hook="pre-create", hook_stderr="no")]}


@scenario("wtctl")
def create_escalated() -> Scenario:
    return {
        "create": [
            _err(13, "escalated", "escalated", hook="pre-create", hook_stderr="ask",
                 escalated_hooks=["pre-create"])  # fmt: skip
        ]
    }


@scenario("wtctl")
def remove_in_use() -> Scenario:
    proc = {"pid": 4242, "command": "fake-process", "path": "/tmp/x"}
    return {
        "remove": [
            _err(20, "refused", "in use", reason="in_use", processes=[proc],
                 checks={"in_use": {"status": "fail", "details": [proc]}}),
            {},
        ]
    }


@scenario("wtctl")
def remove_in_use_always() -> Scenario:
    return {"remove": [SCENARIOS["wtctl"]["remove_in_use"]()["remove"][0]]}


@scenario("wtctl")
def remove_busy() -> Scenario:
    return {"remove": [_err(12, "busy", "another wtctl holds the record's lease"), {}]}


@scenario("wtctl")
def remove_partial() -> Scenario:
    return {"remove": [_err(1, "partial", "the removal deleted part of the tree",
                       details={"remaining_path": "/tmp/fake-throwaway", "remaining": []})]}


@scenario("wtctl")
def hang(command: str = "create") -> Scenario:
    return {command: [{"hang": True}]}


# ---- agent-spawn and claude-session (run plans live in the shared "sessions" section) --------

OUTCOME_ROWS = ["done", "blocked", "needs-input", "exited-unreported", "killed", "timeout",
                "lost", "failed", "not-started", "vetoed", "escalated"]  # fmt: skip


def _plan(**plan: Any) -> Scenario:
    return {"@sessions": {"plan": plan}}


def _as_err(exit_code: int, code: str, message: str = "fake failure", **extra: Any) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, **extra}, "exit": exit_code}


@scenario("agent-spawn")
def outcome(outcome: str = "done", **plan: Any) -> Scenario:
    """Every run ends as ``outcome`` (a row of review-run's launch mapping table)."""
    return _plan(outcome=outcome, **plan)


@scenario("agent-spawn")
def unreported_recovered(then: str = "done") -> Scenario:
    """exited-unreported (14), then the automatic report-resume run ends ``then``."""
    return _plan(outcome="exited-unreported", auto_resume={"outcome": then})


@scenario("agent-spawn")
def implicit_done() -> Scenario:
    return _plan(outcome="done", implicit=True)


@scenario("agent-spawn")
def no_artifact() -> Scenario:
    return _plan(outcome="done", artifact=None)


@scenario("agent-spawn")
def artifact_mismatch(size_off: int = 1) -> Scenario:
    """The reported artifact's size and hash do not match the frozen file."""
    return _plan(outcome="done", artifact_text='{"x": 1}\n', mismatch=size_off)


@scenario("agent-spawn")
def resume_outcome(outcome: str = "done") -> Scenario:
    return {"@sessions": {"resume_plan": {"outcome": outcome}}}


@scenario("agent-spawn")
def never_ends() -> Scenario:
    """The run stays live: ``wait --timeout`` expires (exit 22)."""
    return _plan(outcome="done", never_ends=True)


@scenario("agent-spawn")
def launch_error(exit_code: int = 12, code: str = "launch_failed", **extra: Any) -> Scenario:
    return {"launch": [_as_err(exit_code, code, **extra)]}


@scenario("agent-spawn")
def tool_missing() -> Scenario:
    return {"launch": [_as_err(12, "tool_missing", "claude-session is not installed")]}  # fmt: skip


@scenario("agent-spawn")
def no_tmux() -> Scenario:
    """termctl's tmux preflight failed: agent-spawn passes termctl's code through."""
    lower = {"code": "tmux_missing", "message": "tmux is not installed"}
    return {"launch": [_as_err(12, "failed", "termctl failed", tool="termctl",
                               lower_code="tmux_missing", lower_error=lower)]}  # fmt: skip


@scenario("agent-spawn")
def launch_vetoed() -> Scenario:
    return {"launch": [_as_err(11, "vetoed", "vetoed by pre-launch", hook="pre-launch",
                               hook_stderr="no")]}  # fmt: skip


@scenario("agent-spawn")
def launch_refused(reason: str = "third_party_cwd") -> Scenario:
    return {"launch": [_as_err(20, "refused", "refused", reason=reason)]}


@scenario("agent-spawn")
def launch_ambiguous() -> Scenario:
    return {"launch": [_as_err(21, "ambiguous", "ambiguous",
                               candidates=["ag-aaaaaaaa", "ag-bbbbbbbb"])]}  # fmt: skip


@scenario("agent-spawn")
def dry_run_ok() -> Scenario:
    return {}


@scenario("agent-spawn")
def dry_run_error(exit_code: int = 12, code: str = "failed", **extra: Any) -> Scenario:
    return {"launch --dry-run": [_as_err(exit_code, code, **extra)]}


@scenario("agent-spawn")
def dry_run_no_tmux() -> Scenario:
    lower = {"code": "tmux_missing", "message": "tmux is not installed"}
    return {"launch --dry-run": [_as_err(12, "failed", "termctl failed", tool="termctl",
                                         lower_code="tmux_missing", lower_error=lower)]}  # fmt: skip


@scenario("agent-spawn")
def dry_run_would_refuse(reason: str = "third_party_cwd") -> Scenario:
    return {"launch --dry-run": [{"merge": {"would_refuse": reason}, "exit": 20}]}


@scenario("agent-spawn")
def wait_error(exit_code: int = 12, code: str = "failed", **extra: Any) -> Scenario:
    return {"wait": [_as_err(exit_code, code, **extra)]}


@scenario("agent-spawn")
def wait_not_found() -> Scenario:
    return {"wait": [_as_err(10, "not_found", "no such agent")]}


@scenario("agent-spawn")
def teardown_failed(exit_code: int = 12, code: str = "failed") -> Scenario:
    return {"teardown": [_as_err(exit_code, code)]}


@scenario("agent-spawn")
def hang(command: str = "launch") -> Scenario:
    return {command: [{"hang": True}]}


@scenario("claude-session")
def init_ok(skills: list[str] | None = None) -> Scenario:
    init: dict[str, Any] = {} if skills is None else {"skills": skills}
    return _plan(init=init)


@scenario("claude-session")
def init_skill_missing() -> Scenario:
    return _plan(init={"skills": ["some-other:skill"]})


@scenario("claude-session")
def init_extra_tools(extra: tuple[str, ...] = ("WebFetch",)) -> Scenario:
    from tests.fakebin._sessions import DEFAULT_TOOLS

    return _plan(init={"tools": [*DEFAULT_TOOLS, *extra]})


@scenario("claude-session")
def init_missing_file() -> Scenario:
    return _plan(result_path="missing")


@scenario("claude-session")
def init_no_path() -> Scenario:
    return _plan(result_path="none")


@scenario("claude-session")
def init_absent() -> Scenario:
    """A result file with no init object."""
    return _plan(result_lines=[{"type": "assistant"}, {"type": "result", "is_error": False}])


@scenario("claude-session")
def init_garbage() -> Scenario:
    return _plan(result_lines=["{not json", "[1,"])


@scenario("claude-session")
def init_late(skip: int = 5) -> Scenario:
    lines: list[Any] = [{"type": "assistant", "n": i} for i in range(skip)]
    lines.append({"type": "system", "subtype": "init", "tools": [], "skills": []})
    return _plan(result_lines=lines)


@scenario("claude-session")
def status_error(exit_code: int = 12, code: str = "failed") -> Scenario:
    return {"status": [_as_err(exit_code, code)]}


@scenario("claude-session")
def hang(command: str = "status") -> Scenario:
    return {command: [{"hang": True}]}
