"""The run object, the reasons table, ``list`` and ``show`` (issue #11)."""

from __future__ import annotations

import datetime as dt
import json
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from review_run import runs
from review_run.errors import ERROR_EXIT, ReviewRunError
from review_run.store import Expect, NewRun, Store, default_state_root, lease_name
from tests.conftest import validate_against
from tests.golden_harness import check_golden
from tests.helpers import run_cli

HEAD = "a" * 40
OTHER_HEAD = "b" * 40
SHA256 = "c" * 64
ORIGINAL = "d" * 64
DRAFT_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
IDS = [f"rr-{c * 8}" for c in "abcdefghijklmnopqrstuvwxyz"]

SUMMARY = {
    "verdict": "comment",
    "severity_counts": {
        "blocker": 0,
        "major": 1,
        "minor": 2,
        "nit": 0,
        "info": 0,
        "unspecified": 0,
    },
    "risk": "medium",
    "coverage_status": "full",
    "code_run_status": "skipped",
    "code_run_reason": "sandbox_unavailable",
    "skipped_files": 0,
    "normalized_count": 1,
    "head_moved": False,
}
ANCHOR = {"head_sha": HEAD, "base_ref": "main", "base_sha": "e" * 40, "merge_base_sha": None}


class Clock:
    def __init__(self) -> None:
        self.t = dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=dt.UTC)

    def __call__(self) -> dt.datetime:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += dt.timedelta(seconds=seconds)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(clock: Clock) -> Iterator[Store]:
    """A store at the default state root, so the CLI under test reads the same database."""
    ids = iter(IDS)
    with Store.open(default_state_root(), now=clock, id_generator=lambda: next(ids)) as s:
        yield s


Maker = Callable[..., str]


@pytest.fixture
def make(store: Store, clock: Clock) -> Maker:
    def make_run(
        *,
        end: str | None = None,
        reason: str | None = None,
        exit_code: int | None = None,
        pr: str = "host/o/r#1",
        repo: str = "host/o/r",
        head: str = HEAD,
        labels: tuple[str, ...] = (),
        fields: dict[str, Any] | None = None,
        cancel: int | None = None,
    ) -> str:
        clock.advance(1)
        run_id = store.reserve_id()
        run = store.create_run(
            NewRun(
                run_id=run_id,
                pr=pr,
                repo=repo,
                head_sha=head,
                mode="headless",
                skill="review-run:review-pr",
                labels=labels,
            )
        )
        if cancel is not None:
            assert store.request_cancel(run_id, run.key, actor="human:me", exit_code=cancel)
            fresh = store.get_run(run_id)
            assert fresh is not None
            run = fresh
        if end is not None:
            lease = store.acquire_lease(lease_name(run_id))
            assert lease is not None
            assert store.end_run(
                run_id,
                run.key,
                lease=lease,
                state=end,
                reason=reason,
                exit_code=exit_code,
                fields=fields,
            )
        return run_id

    return make_run


def done_fields() -> dict[str, Any]:
    return {
        "artifact_path": "/state/runs/rr-aaaaaaaa/review.json",
        "artifact_sha256": SHA256,
        "artifact_size": 1234,
        "original_sha256": ORIGINAL,
        "summary": SUMMARY,
        "draft_id": DRAFT_ID,
        "anchor": ANCHOR,
        "agent_id": "ag-aaaaaaaa",
        "csession_id": "cs-aaaaaaaa",
        "teardown": "done",
    }


def failed_fields(**failure: Any) -> dict[str, Any]:
    base = {
        "tool": None,
        "lower_code": None,
        "lower_reason": None,
        "retryable": False,
        "retry_after_s": None,
        "errors": [],
    }
    return {"failure": {**base, **failure}}


# ------------------------------------------------------------------ the reasons table

# The Reasons table of interface.md, written out independently of the module under test.
EXPECTED_RUN_REASONS = {
    "prefetch_failed": ("failed", 12),
    "stale_head": ("failed", 12),
    "too_large": ("failed", 12),
    "launch_failed": ("failed", 12),
    "canceled": ("failed", 143),
    "session_failed": ("failed", 18),
    "unreported": ("failed", 14),
    "killed": ("failed", 15),
    "timeout": ("failed", 16),
    "lost": ("failed", 17),
    "runner_lost": ("failed", 12),
    "agent_gone": ("failed", 12),
    "expired": ("failed", 12),
    "implicit_done": ("failed", 22),
    "artifact_missing": ("failed", 22),
    "artifact_mismatch": ("failed", 22),
    "skill_missing": ("failed", 22),
    "artifact_invalid": ("failed", 22),
    "internal": ("failed", 12),
    "needs_input": ("escalated", 13),
    "blocked": ("escalated", 13),
}
EXPECTED_RETRYABLE = {"stale_head", "runner_lost"}
EXPECTED_NO_RUN = {
    "not_open": ("refused", 20),
    "unknown_host": ("refused", 20),
    "live": ("refused", 20),
    "not_escalated": ("refused", 20),
    "lower_refused": ("refused", 20),
    "tool_missing": ("failed", 12),
    "tool_too_old": ("failed", 12),
    "no_tmux": ("failed", 12),
    "schema_newer": ("failed", 12),
    "state_unsafe": ("failed", 12),
    "plugin_changed": ("failed", 12),
    "lower_failed": ("failed", 12),
    "wait_timeout": ("failed", 12),
    "previous_mismatch": ("usage", 2),
    "previous_not_done": ("usage", 2),
    "skill_not_found": ("usage", 2),
    "label_reserved": ("usage", 2),
    "too_many_labels": ("usage", 2),
    "pr_too_long": ("usage", 2),
}
EXPECTED_NO_REASON = {
    "usage": 2,
    "config_invalid": 2,
    "not_found": 10,
    "ambiguous": 21,
    "vetoed": 11,
    "escalated": 13,
    "internal": 1,
}


def test_the_reasons_table_is_the_interface_table() -> None:
    assert {r: (i.state, i.exits[0]) for r, i in runs.REASONS.items()} == EXPECTED_RUN_REASONS
    assert runs.NO_RUN_REASONS == EXPECTED_NO_RUN
    assert runs.NO_REASON_CODES == EXPECTED_NO_REASON


def test_the_table_matches_the_run_schema_enums() -> None:
    defs = json.loads((Path(__file__).parents[1] / "schemas" / "defs.json").read_text())["$defs"]
    failed = set(defs["failed_reason"]["enum"])
    escalated = set(defs["escalated_reason"]["enum"])
    assert failed | escalated == set(runs.REASONS)
    assert set(defs["error_reason"]["enum"]) == set(runs.NO_RUN_REASONS)
    assert set(defs["error_code"]["enum"]) == set(ERROR_EXIT)


@pytest.mark.parametrize("reason", sorted(EXPECTED_RUN_REASONS))
def test_every_run_reason_has_a_golden(
    reason: str, make: Maker, capsys: pytest.CaptureFixture[str]
) -> None:
    state, exit_code = EXPECTED_RUN_REASONS[reason]
    if state == "escalated":
        fields: dict[str, Any] = {"agent_id": "ag-aaaaaaaa", "csession_id": "cs-aaaaaaaa"}
        run_id = make(end="escalated", reason=reason, exit_code=exit_code, fields=fields)
    else:
        cancel = exit_code if reason == "canceled" else None
        run_id = make(
            end="failed",
            reason=reason,
            exit_code=exit_code,
            fields=failed_fields(),
            cancel=cancel,
        )
    res = run_cli(capsys, "--json", "show", run_id)
    assert res.code == 0  # show exits 0 whenever it printed the run object
    doc = res.envelope()
    assert doc["state"] == state
    assert doc["reason"] == reason
    assert doc["exit_code"] == exit_code
    assert (
        doc["failure"] is None
        if state == "escalated"
        else doc["failure"]["retryable"] == (reason in EXPECTED_RETRYABLE)
    )
    check_golden(f"reason-{reason}.json", "cli/show.json", {**doc, "run_id": "rr-aaaaaaaa"})


@pytest.mark.parametrize("reason", sorted(EXPECTED_NO_RUN))
def test_every_no_run_reason_has_an_envelope_golden(reason: str) -> None:
    code, exit_code = EXPECTED_NO_RUN[reason]
    err = ReviewRunError(code, "closed message", reason=reason)
    assert err.exit_code == exit_code
    check_golden(f"envelope-{reason}.json", "cli/error.json", err.envelope())


@pytest.mark.parametrize("code", sorted(EXPECTED_NO_REASON))
def test_every_no_reason_code_has_an_envelope_golden(code: str) -> None:
    extra = {"hook": "pre-run", "decision_ids": ["d1"]} if code in ("vetoed", "escalated") else {}
    err = ReviewRunError(code, "closed message", **extra)  # type: ignore[arg-type]
    assert err.exit_code == EXPECTED_NO_REASON[code]
    check_golden(f"envelope-code-{code}.json", "cli/error.json", err.envelope())


@pytest.mark.parametrize(
    ("reason", "lower_code", "lower_flag", "expected"),
    [
        ("prefetch_failed", "rate_limited", False, True),
        ("prefetch_failed", "network_error", False, True),
        ("prefetch_failed", "not_found", True, False),
        ("prefetch_failed", None, False, False),
        ("launch_failed", "x", True, True),
        ("launch_failed", "x", False, False),
        ("stale_head", None, False, True),
        ("runner_lost", None, False, True),
        ("too_large", "rate_limited", True, False),
        ("timeout", None, True, False),
        ("needs_input", None, True, False),
        ("not_a_reason", "rate_limited", True, False),
    ],
)
def test_retryable_follows_the_table(
    reason: str, lower_code: str | None, lower_flag: bool, expected: bool
) -> None:
    assert runs.is_retryable(reason, lower_code, lower_flag) is expected


def test_failure_carries_the_siblings_closed_codes(
    make: Maker, capsys: pytest.CaptureFixture[str]
) -> None:
    errors = [{"pointer": "/findings/0/line", "kind": "range"}]
    run_id = make(
        end="failed",
        reason="prefetch_failed",
        exit_code=12,
        fields=failed_fields(
            tool="forgectl",
            lower_code="rate_limited",
            lower_reason="slow_down",
            retry_after_s=30,
            errors=errors,
        ),
    )
    doc = run_cli(capsys, "--json", "show", run_id).envelope()
    check_golden("failure-lower-codes.json", "cli/show.json", doc)
    assert doc["failure"] == {
        "tool": "forgectl",
        "lower_code": "rate_limited",
        "lower_reason": "slow_down",
        "retryable": True,
        "retry_after_s": 30,
        "errors": errors,
    }


# ------------------------------------------------------------------ list and show: shapes


def test_list_json_on_an_empty_store(capsys: pytest.CaptureFixture[str]) -> None:
    res = run_cli(capsys, "--json", "list")
    assert res.code == 0
    check_golden("list-empty.json", "cli/list.json", res.envelope())


def test_list_human_on_an_empty_store(capsys: pytest.CaptureFixture[str]) -> None:
    res = run_cli(capsys, "list")
    assert (res.code, res.out) == (0, "")


def test_show_of_an_unknown_run_exits_10_with_the_envelope(
    capsys: pytest.CaptureFixture[str],
) -> None:
    res = run_cli(capsys, "--json", "show", "rr-aaaaaaaa")
    assert res.code == 10
    check_golden("show-unknown.json", "cli/error.json", res.envelope())


def test_show_on_an_empty_store_creates_nothing_else(capsys: pytest.CaptureFixture[str]) -> None:
    run_cli(capsys, "--json", "list")
    root = default_state_root()
    assert sorted(p.name for p in root.iterdir() if not p.name.startswith("state.db")) == ["runs"]


def test_show_of_a_canceled_run_exits_0_with_the_run_object(
    make: Maker, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = make(
        end="failed", reason="canceled", exit_code=143, fields=failed_fields(), cancel=143
    )
    res = run_cli(capsys, "--json", "show", run_id)
    assert res.code == 0
    doc = res.envelope()
    assert (doc["state"], doc["reason"], doc["exit_code"]) == ("failed", "canceled", 143)
    assert doc["cancel_requested_at"] is not None
    check_golden("show-canceled.json", "cli/show.json", doc)


def test_a_sigint_canceled_run_keeps_exit_130(
    make: Maker, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = make(
        end="failed", reason="canceled", exit_code=130, fields=failed_fields(), cancel=130
    )
    assert run_cli(capsys, "--json", "show", run_id).envelope()["exit_code"] == 130


def test_show_of_a_done_run(make: Maker, capsys: pytest.CaptureFixture[str]) -> None:
    run_id = make(end="done", exit_code=0, fields=done_fields(), labels=("team=a", "team=a"))
    doc = run_cli(capsys, "--json", "show", run_id).envelope()
    check_golden("show-done.json", "cli/show.json", doc)
    assert doc["labels"] == ["team=a", "team=a"]  # a multiset, in stored order


def test_show_of_a_running_run(make: Maker, capsys: pytest.CaptureFixture[str]) -> None:
    run_id = make()
    doc = run_cli(capsys, "--json", "show", "--no-refresh", run_id).envelope()
    check_golden("show-running.json", "cli/show.json", doc)
    assert (doc["phase"], doc["exit_code"], doc["finished_at"]) == ("prefetch", None, None)


def test_list_json_golden_and_timestamps(make: Maker, capsys: pytest.CaptureFixture[str]) -> None:
    make(end="done", exit_code=0, fields=done_fields())
    make()
    doc = run_cli(capsys, "--json", "list").envelope()
    assert [r["run_id"] for r in doc["runs"]] == ["rr-bbbbbbbb", "rr-aaaaaaaa"]
    for run in doc["runs"]:
        assert run["created_at"].endswith("Z")
    check_golden("list-two.json", "cli/list.json", doc)


def test_show_prefix_resolution(make: Maker, capsys: pytest.CaptureFixture[str]) -> None:
    make()
    make()
    assert run_cli(capsys, "--json", "show", "aaaa").code == 0
    assert run_cli(capsys, "--json", "show", "rr-aaaa").code == 0
    short = run_cli(capsys, "--json", "show", "rr-aaa")
    assert short.code == 2
    assert short.envelope()["error"]["code"] == "usage"
    assert run_cli(capsys, "--json", "show", "rr-zzzz").code == 10
    assert run_cli(capsys, "--json", "show", "rr-AAAA").code == 2


def test_show_ambiguous_prefix_exits_21(
    store: Store, capsys: pytest.CaptureFixture[str], clock: Clock
) -> None:
    for run_id in ("rr-abcdaaaa", "rr-abcdbbbb"):
        store._conn.execute("INSERT INTO ids VALUES (?, 'x')", (run_id,))
        store.create_run(
            NewRun(run_id, "host/o/r#1", "host/o/r", HEAD, "headless", "review-run:review-pr")
        )
    res = run_cli(capsys, "--json", "show", "abcd")
    assert res.code == 21
    validate_against("cli/error.json", res.envelope())
    assert res.envelope()["error"]["details"]["matches"] == ["rr-abcdaaaa", "rr-abcdbbbb"]


# ------------------------------------------------------------------ list filters


@pytest.fixture
def several(make: Maker) -> dict[str, str]:
    return {
        "a": make(pr="host/o/r#1", head=HEAD, labels=("team=x", "env=prod")),
        "b": make(
            end="done",
            exit_code=0,
            fields=done_fields() | {"anchor": ANCHOR | {"head_sha": OTHER_HEAD}},
            pr="host/o/r#2",
            head=HEAD,
        ),
        "c": make(
            end="failed",
            reason="timeout",
            exit_code=16,
            fields=failed_fields(),
            pr="host/o/other#3",
            repo="host/o/other",
            head="f" * 40,
            labels=("team=y",),
        ),
        "d": make(
            end="escalated",
            reason="blocked",
            exit_code=13,
            fields={"agent_id": "ag-aaaaaaaa"},
            labels=("env=prod",),
        ),
    }


def ids_of(capsys: pytest.CaptureFixture[str], *argv: str) -> list[str]:
    res = run_cli(capsys, "--json", "list", *argv)
    assert res.code == 0, res.err
    return [r["run_id"] for r in res.envelope()["runs"]]


def test_filter_by_state(several: dict[str, str], capsys: pytest.CaptureFixture[str]) -> None:
    assert ids_of(capsys, "--state", "done") == [several["b"]]
    assert set(ids_of(capsys, "--state", "done", "--state", "failed")) == {
        several["b"],
        several["c"],
    }
    assert ids_of(capsys, "--state", "escalated") == [several["d"]]


def test_filter_by_pr_and_repo_is_exact(
    several: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert ids_of(capsys, "--pr", "host/o/r#2") == [several["b"]]
    assert ids_of(capsys, "--pr", "host/o/r#") == []
    assert ids_of(capsys, "--pr", "host/o/%") == []
    assert set(ids_of(capsys, "--repo", "host/o/r")) == {several[k] for k in "abd"}
    assert ids_of(capsys, "--repo", "host/o/other") == [several["c"]]
    assert ids_of(capsys, "--repo", "host/o") == []


def test_filter_by_label(several: dict[str, str], capsys: pytest.CaptureFixture[str]) -> None:
    assert ids_of(capsys, "--label", "team=x") == [several["a"]]
    assert set(ids_of(capsys, "--label", "team")) == {several["a"], several["c"]}
    assert set(ids_of(capsys, "--label", "env=prod")) == {several["a"], several["d"]}
    assert ids_of(capsys, "--label", "env=prod", "--label", "team=x") == [several["a"]]
    assert ids_of(capsys, "--label", "team=") == []
    assert ids_of(capsys, "--label", "tea") == []


def test_filter_head_matches_resolve_head_or_anchor_head(
    several: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    # b's resolve-time head is HEAD, its anchor head differs.
    assert set(ids_of(capsys, "--head", HEAD)) == {several["a"], several["b"], several["d"]}
    assert ids_of(capsys, "--head", OTHER_HEAD) == [several["b"]]
    assert ids_of(capsys, "--head", "f" * 40) == [several["c"]]
    assert ids_of(capsys, "--head", HEAD[:10]) == []


def test_filters_are_anded(several: dict[str, str], capsys: pytest.CaptureFixture[str]) -> None:
    assert ids_of(capsys, "--head", HEAD, "--state", "done", "--repo", "host/o/r") == [several["b"]]
    assert ids_of(capsys, "--head", HEAD, "--state", "failed") == []


def test_since_duration_and_rfc3339(
    several: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    # Runs were created at 12:00:01 .. 12:00:04 on 2026-01-01.
    assert ids_of(capsys, "--since", "1s") == []  # "now" is far later than the runs
    assert len(ids_of(capsys, "--since", "36500d")) == 4
    assert ids_of(capsys, "--since", "2026-01-01T12:00:03Z") == [several["d"], several["c"]]
    assert ids_of(capsys, "--since", "2026-01-01T13:00:03+01:00") == [several["d"], several["c"]]
    assert ids_of(capsys, "--since", "2026-01-01T12:00:04.001Z") == []
    assert len(ids_of(capsys, "--since", "2026-01-01T12:00:01Z")) == 4
    assert len(ids_of(capsys, "--since", "0")) == 0


@pytest.mark.parametrize("bad", ["yesterday", "10", "-1h", "1.5h", "2026-01-01", "1w", ""])
def test_since_grammar_errors_exit_2(bad: str, capsys: pytest.CaptureFixture[str]) -> None:
    res = run_cli(capsys, "--json", "list", "--since", bad)
    assert res.code == 2
    assert res.envelope()["error"]["code"] == "usage"


@pytest.mark.parametrize("bad", ["0", "-1", "x", "1.5", "9999999999999999999"])
def test_limit_grammar_errors_exit_2(bad: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert run_cli(capsys, "--json", "list", "--limit", bad).code == 2


def test_limit_truncated_and_ordering(make: Maker, capsys: pytest.CaptureFixture[str]) -> None:
    created = [make() for _ in range(5)]
    doc = run_cli(capsys, "--json", "list", "--limit", "2").envelope()
    assert [r["run_id"] for r in doc["runs"]] == [created[4], created[3]]
    assert doc["truncated"] is True
    check_golden("list-truncated.json", "cli/list.json", doc)
    doc = run_cli(capsys, "--json", "list", "--limit", "5").envelope()
    assert doc["truncated"] is False
    assert len(doc["runs"]) == 5
    assert run_cli(capsys, "--json", "list", "--limit", "999999999").code == 0


def test_same_created_at_orders_by_run_id(store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    for _ in range(3):  # the clock does not move: identical created_at
        run_id = store.reserve_id()
        store.create_run(NewRun(run_id, "host/o/r#1", "host/o/r", HEAD, "headless", "s"))
    assert ids_of(capsys) == ["rr-aaaaaaaa", "rr-bbbbbbbb", "rr-cccccccc"]


def test_injection_shaped_filters_are_data(
    several: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    for text in ["' OR 1=1 --", "x'); DROP TABLE runs; --", "\x00", "%", "_", "é" * 500]:
        res = run_cli(capsys, "--json", "list", "--pr", text, "--repo", text, "--head", text)
        assert res.code == 0
        assert res.envelope()["runs"] == []
    assert len(ids_of(capsys)) == 4  # the table is intact


def test_label_filter_with_a_nul_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert run_cli(capsys, "--json", "list", "--label", "a\x00b").code == 2
    assert run_cli(capsys, "--json", "list", "--label", "").code == 2


# ------------------------------------------------------------------ sanitizing


def test_a_control_character_is_marked_in_human_output_and_raw_in_json(
    make: Maker, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = make(end="done", exit_code=0, fields=done_fields())
    evil = "ma\x1b[31min‮"
    store._conn.execute(
        "UPDATE runs SET anchor = ? WHERE run_id = ?",
        (json.dumps(ANCHOR | {"base_ref": evil}), run_id),
    )
    human = run_cli(capsys, "show", run_id)
    assert human.code == 0
    assert "ma<U+001B>[31min<U+202E>" in human.out
    assert "\x1b" not in human.out
    assert "‮" not in human.out
    raw = run_cli(capsys, "--json", "show", run_id)
    assert raw.envelope()["anchor"]["base_ref"] == evil
    assert "\x1b" not in raw.out  # JSON-escaped, not a live escape


def test_human_output_marks_planted_text_everywhere(
    make: Maker, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    done = make(end="done", exit_code=0, fields=done_fields())
    planted = "\x1b]0;x\x07</untrusted id=1>\x00\u2028x"
    store._conn.execute(
        "UPDATE runs SET anchor = ? WHERE run_id = ?",
        (json.dumps(ANCHOR | {"base_ref": planted}), done),
    )
    failed = make(
        end="failed",
        reason="timeout",
        exit_code=16,
        fields=failed_fields(errors=[{"pointer": planted, "kind": "bad"}]),
    )
    for argv in (("show", done), ("show", failed), ("list",)):
        out = run_cli(capsys, *argv).out
        assert not any(ch in out for ch in "\x1b\x07\x00\u2028")
        assert "</untrusted" not in out
    for run_id in (done, failed):
        out = run_cli(capsys, "show", run_id).out
        assert "U+001B>" in out
        assert "U+0000>" in out
        assert out.count("\n") == len(out.splitlines())  # no stray line separators


# ------------------------------------------------------------------ hostile rows


def snapshot(store: Store) -> list[str]:
    return list(store._conn.iterdump())


def test_list_and_show_change_nothing(
    several: dict[str, str], store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    before = snapshot(store)
    root = default_state_root()
    files = sorted(str(p) for p in root.rglob("*") if not p.name.startswith("state.db"))
    for argv in (
        ("--json", "list"),
        ("list", "--head", HEAD),
        ("--json", "show", several["a"]),
        ("show", "--no-refresh", several["d"]),
        ("--json", "show", "--draft", several["a"]),
    ):
        run_cli(capsys, *argv)
    assert snapshot(store) == before
    assert files == sorted(str(p) for p in root.rglob("*") if not p.name.startswith("state.db"))


def corrupt(store: Store, run_id: str, **columns: object) -> None:
    store._conn.execute("PRAGMA ignore_check_constraints = ON")
    sets = ", ".join(f"{c} = ?" for c in columns)
    store._conn.execute(f"UPDATE runs SET {sets} WHERE run_id = ?", (*columns.values(), run_id))  # noqa: S608
    store._conn.execute("PRAGMA ignore_check_constraints = OFF")


HUGE = "x" * 1_000_000

IDENTITY = {"pr", "repo", "skill", "mode", "head_sha", "created_at"}


def make_in_state(make: Maker, state: str) -> str:
    if state == "running":
        return make(labels=("k=v",))
    if state == "done":
        return make(end="done", exit_code=0, fields=done_fields())
    if state == "failed":
        return make(end="failed", reason="timeout", exit_code=16, fields=failed_fields())
    return make(end="escalated", reason="blocked", exit_code=13, fields={"agent_id": "ag-aaaaaaaa"})


@pytest.mark.parametrize(
    "columns",
    [
        {"state": "exploded", "reason": "x"},
        {"reason": "not-in-the-table", "exit_code": 99},
        {"reason": 7, "exit_code": "twelve"},
        {"exit_code": 3.5},
        {"anchor": "{not json"},
        {"anchor": "[1, 2]"},
        {"anchor": json.dumps({"head_sha": 5})},
        {"summary": "{not json"},
        {"summary": json.dumps({"verdict": "approve!"})},
        {"summary": json.dumps([HUGE[:100]])},
        {"failure": "nope"},
        {"failure": json.dumps({"errors": "no", "tool": "rm -rf", "lower_code": "A B"})},
        {"failure": json.dumps({"errors": [{"pointer": HUGE, "kind": "x"}] * 100})},
        {"warnings": "[1, 2"},
        {"warnings": json.dumps(["unknown", "teardown_failed", "teardown_failed", HUGE])},
        {"pr": HUGE, "repo": HUGE, "skill": HUGE, "mode": HUGE, "head_sha": HUGE},
        {"agent_id": HUGE, "csession_id": HUGE, "draft_id": HUGE, "previous_run": HUGE},
        {"created_at": HUGE, "finished_at": HUGE, "escalated_at": HUGE},
        {"created_at": 5, "cancel_requested_at": "<script>"},
        {"artifact_path": HUGE, "artifact_sha256": HUGE, "artifact_size": -4},
        {"teardown": "weird", "phase": "elsewhere"},
        {"hooks": "{", "decision_ids": "{"},
        {"state": "failed", "phase": None, "reason": "canceled", "cancel_exit_code": 7},
    ],
)
@pytest.mark.parametrize("state", ["running", "done", "failed", "escalated"])
def test_a_corrupt_row_never_crashes_and_stays_bounded(
    columns: dict[str, object],
    state: str,
    make: Maker,
    store: Store,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_id = make_in_state(make, state)
    if "state" in columns:
        columns = {"phase": None, **columns}
    corrupt(store, run_id, **columns)
    store._conn.execute(
        "INSERT INTO labels VALUES (?, 99, ?, ?)", (run_id, HUGE[:3000], HUGE[:3000])
    )
    for _ in range(80):
        store._conn.execute(
            "INSERT INTO labels VALUES (?, ?, 'a', 'b')",
            (run_id, 100 + _),
        )
    corrupt_identity = bool(IDENTITY & set(columns))
    for argv in (("--json", "show", run_id), ("show", run_id), ("--json", "list"), ("list",)):
        res = run_cli(capsys, *argv)
        assert res.code == (1 if corrupt_identity and argv[-1] != "list" else 0), (argv, res.err)
        assert len(res.out) < 200_000
        assert len(res.err) < 1000
        if argv[0] == "--json" and res.code == 0:
            doc = json.loads(res.out)
            if "runs" in doc:
                assert bool(doc["runs"]) != corrupt_identity
                if corrupt_identity:
                    continue
            obj = doc["runs"][0] if "runs" in doc else doc
            assert len(obj["labels"]) <= 60
            assert obj["state"] in runs.STATES
            if obj["state"] == "failed":
                assert obj["reason"] in runs.REASONS
                assert obj["exit_code"] in runs.REASONS[obj["reason"]].exits
            for warning in obj["warnings"]:
                assert warning in runs.WARNINGS


def test_a_corrupt_row_does_not_hide_the_others(
    make: Maker, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    good = make()
    bad = make()
    corrupt(store, bad, anchor="{", summary="{", failure="{", warnings="{")
    assert ids_of(capsys) == [bad, good]


def test_unknown_reason_is_reported_as_internal(
    make: Maker, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = make(end="failed", reason="timeout", exit_code=16, fields=failed_fields())
    corrupt(store, run_id, reason="mystery", exit_code=99)
    doc = run_cli(capsys, "--json", "show", run_id).envelope()
    assert (doc["reason"], doc["exit_code"]) == ("internal", 12)
    validate_against("cli/show.json", doc)


def test_head_filter_survives_a_malformed_anchor(
    make: Maker, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = make(head=OTHER_HEAD)
    corrupt(store, run_id, anchor="{broken")
    make()
    assert ids_of(capsys, "--head", OTHER_HEAD) == [run_id]


def test_a_label_over_the_grammar_is_dropped_from_the_run_object(
    make: Maker, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = make(labels=("k=v",))
    store._conn.execute("UPDATE labels SET value = ?", ("v" * 100_000,))
    labels = run_cli(capsys, "--json", "show", run_id).envelope()["labels"]
    assert labels == []


def test_a_hostile_db_that_is_not_a_database_is_an_internal_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = default_state_root()
    root.mkdir(parents=True, mode=0o700)
    (root / "state.db").write_bytes(b"this is not sqlite" * 100)
    os.chmod(root / "state.db", 0o600)
    res = run_cli(capsys, "--json", "list")
    assert res.code == 1
    assert res.envelope()["error"]["code"] == "internal"


def test_a_symlinked_state_root_is_state_unsafe(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = default_state_root()
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    root.parent.mkdir(parents=True, exist_ok=True)
    root.symlink_to(real)
    res = run_cli(capsys, "--json", "list")
    assert res.code == 12
    assert res.envelope()["error"]["reason"] == "state_unsafe"


# ------------------------------------------------------------------ show --draft


def test_draft_of_a_run_without_one_exits_10_with_the_envelope(
    make: Maker, capsys: pytest.CaptureFixture[str]
) -> None:
    for run_id in (
        make(),
        make(end="failed", reason="timeout", exit_code=16, fields=failed_fields()),
    ):
        res = run_cli(capsys, "--json", "show", "--draft", run_id)
        assert res.code == 10
        validate_against("cli/error.json", res.envelope())
        assert res.envelope()["error"]["code"] == "not_found"
        assert res.out.count("\n") == 1


def test_draft_of_a_done_run_with_a_missing_file_exits_10(
    make: Maker, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = make(end="done", exit_code=0, fields=done_fields())
    assert run_cli(capsys, "--json", "show", "--draft", run_id).code == 10


def test_draft_prints_the_stored_bytes_and_a_stderr_notice(
    make: Maker, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = make(end="done", exit_code=0, fields=done_fields())
    body = '{"schema_version":1,"title":"\\u001b[31m evil"}'  # no trailing newline, as stored
    (store.run_dir(run_id) / "review.json").write_text(body)
    for argv in (("show", "--draft", run_id), ("--json", "show", "--draft", run_id)):
        res = run_cli(capsys, *argv)
        assert res.code == 0
        assert res.out == body
        assert "as stored" in res.err
        assert res.err.count("\n") == 1


def test_draft_of_a_done_row_that_show_degrades_exits_10(
    make: Maker, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = make(end="done", exit_code=0, fields=done_fields())
    (store.run_dir(run_id) / "review.json").write_text('{"schema_version":1}')
    assert run_cli(capsys, "show", "--draft", run_id).code == 0
    corrupt(store, run_id, draft_id=None)
    for argv in (("show", "--draft", run_id), ("--json", "show", "--draft", run_id)):
        res = run_cli(capsys, *argv)
        assert res.code == 10
        assert "schema_version" not in res.out


def test_draft_refuses_a_symlink_a_directory_and_a_huge_file(
    make: Maker, store: Store, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = make(end="done", exit_code=0, fields=done_fields())
    path = store.run_dir(run_id) / "review.json"
    secret = tmp_path / "secret.json"
    secret.write_text('{"secret": true}')
    path.symlink_to(secret)
    res = run_cli(capsys, "--json", "show", "--draft", run_id)
    assert res.code == 10
    assert "secret" not in res.out
    path.unlink()
    path.mkdir()
    assert run_cli(capsys, "--json", "show", "--draft", run_id).code == 10
    path.rmdir()
    with path.open("wb") as handle:
        handle.truncate(runs.MAX_DRAFT_BYTES + 1)
    assert run_cli(capsys, "--json", "show", "--draft", run_id).code == 10


def test_draft_of_an_unknown_or_short_ref(capsys: pytest.CaptureFixture[str]) -> None:
    assert run_cli(capsys, "--json", "show", "--draft", "rr-zzzzzzzz").code == 10
    assert run_cli(capsys, "--json", "show", "--draft", "rr-a").code == 2


def test_the_state_of_a_running_run_is_not_a_draft(
    make: Maker, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = make()
    (store.run_dir(run_id) / "review.json").write_text("{}")  # a half-written leftover
    assert run_cli(capsys, "--json", "show", "--draft", run_id).code == 10


# ------------------------------------------------------------------ the module itself


def test_since_cutoff() -> None:
    now = dt.datetime(2026, 1, 2, 0, 0, 0, tzinfo=dt.UTC)
    assert runs.since_cutoff(dt.timedelta(hours=1), now) == "2026-01-01T23:00:00.000Z"
    assert runs.since_cutoff(None, now) is None
    stamp = dt.datetime(2026, 1, 1, 1, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=1)))
    assert runs.since_cutoff(stamp, now) == "2026-01-01T00:00:00.000Z"
    assert runs.since_cutoff(dt.timedelta(days=36500 * 100), now) == "0000-00-00T00:00:00.000Z"


def test_expect_key_is_unchanged_by_reads(make: Maker, store: Store) -> None:
    run_id = make()
    before = store.get_run(run_id)
    assert before is not None
    assert before.key == Expect("running", "prefetch", None)
    runs.show_run(store, run_id)
    assert store.get_run(run_id) == before


# ------------------------------------------------------------------ R1: schema-valid output


def test_pattern_constants_are_the_schema_patterns() -> None:
    defs = json.loads((Path(__file__).parent.parent / "schemas" / "defs.json").read_text())
    table = defs["$defs"]
    for name, rx in runs.SCHEMA_PATTERNS.items():
        assert rx.pattern == table[name]["pattern"], name


CORRUPT_IDENTITY = [
    {"pr": "not a pr"},
    {"pr": "host/o/r#1\n"},
    {"pr": "host/o/r#0"},
    {"repo": "Host/O/R"},
    {"repo": "a" * 5000},
    {"skill": "bad skill"},
    {"skill": "x" * 129},
    {"mode": "weird"},
    {"head_sha": "A" * 40},
    {"head_sha": "a" * 39},
    {"head_sha": "a" * 40 + "\n"},
    {"created_at": "yesterday"},
    {"created_at": "2026-01-01T00:00:00Z\n"},
]


@pytest.mark.parametrize("columns", CORRUPT_IDENTITY)
def test_a_row_without_a_valid_identity_is_corrupt(
    columns: dict[str, object], make: Maker, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    good = make()
    bad = make()
    corrupt(store, bad, **columns)
    shown = run_cli(capsys, "--json", "show", bad)
    assert shown.code == 1
    validate_against("cli/error.json", shown.envelope())
    assert shown.envelope()["error"]["code"] == "internal"
    for value in columns.values():
        if isinstance(value, str) and len(value) > 3:
            assert value.strip() not in shown.out + shown.err
    listed = run_cli(capsys, "--json", "list")
    assert listed.code == 0
    doc = listed.envelope()
    validate_against("cli/list.json", doc)
    assert [r["run_id"] for r in doc["runs"]] == [good]
    assert listed.err.count("\n") == 1
    assert "1" in listed.err
    human = run_cli(capsys, "list")
    assert human.code == 0
    assert human.err.count("\n") == 1


def test_a_clean_list_prints_no_notice(make: Maker, capsys: pytest.CaptureFixture[str]) -> None:
    make()
    assert run_cli(capsys, "--json", "list").err == ""


HOSTILE_OPTIONAL = [
    {"agent_id": "nope"},
    {"agent_id": "ag-AAAAAAAA"},
    {"csession_id": "cs-1"},
    {"draft_id": "lowercase"},
    {"previous_run": "rr-short"},
    {"previous_run": "rr-aaaaaaaa\n"},
    {"anchor": json.dumps({**ANCHOR, "head_sha": "zz"})},
    {"anchor": json.dumps({**ANCHOR, "base_sha": "zz"})},
    {"anchor": json.dumps({**ANCHOR, "merge_base_sha": 5})},
    {"anchor": json.dumps({**ANCHOR, "base_ref": 5})},
    {"anchor": json.dumps({**ANCHOR, "base_ref": "m" * 900})},
    {"escalated_at": "later", "cancel_requested_at": "soon", "finished_at": "never"},
    {"artifact_path": "relative/review.json"},
    {"artifact_path": ""},
    {"artifact_sha256": "G" * 64},
    {"artifact_sha256": SHA256 + "\n"},
    {"original_sha256": "short"},
    {"artifact_size": -1},
    {"summary": json.dumps({**SUMMARY, "verdict": "meh"})},
    {"summary": json.dumps({**SUMMARY, "code_run_reason": "bogus"})},
    {"summary": json.dumps({**SUMMARY, "severity_counts": {"blocker": "1"}})},
    {"failure": json.dumps({"lower_code": "Bad Code", "lower_reason": "9x", "retry_after_s": -3})},
    {"failure": json.dumps({"errors": [{"pointer": "/a", "kind": "Bad"}, 5, {"pointer": 1}]})},
    {"teardown": "weird", "phase": "elsewhere", "warnings": json.dumps(["nope"])},
    {"exit_code": 99, "reason": "nonsense"},
    {"state": "exploded", "reason": "nonsense", "exit_code": 12},
]


@pytest.mark.parametrize("columns", HOSTILE_OPTIONAL)
@pytest.mark.parametrize("state", ["running", "done", "failed", "escalated"])
def test_hostile_rows_always_validate_against_the_schema(
    columns: dict[str, object],
    state: str,
    make: Maker,
    store: Store,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_id = make_in_state(make, state)
    if "state" in columns:
        columns = {"phase": None, **columns}
    corrupt(store, run_id, **columns)
    store._conn.execute("INSERT INTO labels VALUES (?, 98, 'UP', 'ok')", (run_id,))
    store._conn.execute("INSERT INTO labels VALUES (?, 99, 'k', 'sp ace')", (run_id,))
    shown = run_cli(capsys, "--json", "show", run_id)
    assert shown.code == 0, shown.err
    obj = shown.envelope()
    validate_against("cli/show.json", obj)
    assert all(label not in ("UP=ok", "k=sp ace") for label in obj["labels"])
    listed = run_cli(capsys, "--json", "list")
    assert listed.code == 0
    validate_against("cli/list.json", listed.envelope())
    assert listed.envelope()["runs"] == [obj]


def shown_object(capsys: pytest.CaptureFixture[str], run_id: str) -> dict[str, Any]:
    res = run_cli(capsys, "--json", "show", run_id)
    assert res.code == 0, res.err
    obj = res.envelope()
    validate_against("cli/show.json", obj)
    return obj


@pytest.mark.parametrize(
    "columns",
    [
        {"artifact_path": None},
        {"artifact_path": "relative/review.json"},
        {"draft_id": None},
        {"anchor": None},
        {"summary": None},
        {"summary": "{"},
        {"finished_at": None},
    ],
)
def test_a_done_row_missing_a_required_field_degrades_to_failed_internal(
    columns: dict[str, object],
    make: Maker,
    store: Store,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_id = make(end="done", exit_code=0, fields=done_fields())
    corrupt(store, run_id, **columns)
    obj = shown_object(capsys, run_id)
    assert (obj["state"], obj["reason"], obj["exit_code"]) == ("failed", "internal", 12)
    assert obj["artifact"] is None
    assert obj["summary"] is None
    assert obj["finished_at"] is not None


def test_an_escalated_row_without_an_agent_or_time_degrades(
    make: Maker, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    for columns in ({"agent_id": None}, {"escalated_at": None}):
        run_id = make_in_state(make, "escalated")
        corrupt(store, run_id, **columns)
        obj = shown_object(capsys, run_id)
        assert (obj["state"], obj["reason"], obj["exit_code"]) == ("failed", "internal", 12)
        assert obj["finished_at"] == obj["created_at"]


def test_a_failed_row_without_finished_at_uses_created_at(
    make: Maker, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = make_in_state(make, "failed")
    corrupt(store, run_id, finished_at=None)
    obj = shown_object(capsys, run_id)
    assert obj["finished_at"] == obj["created_at"]


def test_a_canceled_row_without_cancel_time_uses_finished_at_then_created_at(
    make: Maker, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = make(end="failed", reason="canceled", exit_code=143, fields=failed_fields())
    corrupt(store, run_id, cancel_requested_at=None)
    obj = shown_object(capsys, run_id)
    assert obj["cancel_requested_at"] == obj["finished_at"]
    corrupt(store, run_id, cancel_requested_at=None, finished_at=None)
    obj = shown_object(capsys, run_id)
    assert obj["cancel_requested_at"] == obj["created_at"]


def test_a_done_row_with_a_cancel_time_drops_it(
    make: Maker, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = make(end="done", exit_code=0, fields=done_fields())
    corrupt(store, run_id, cancel_requested_at="2026-01-01T00:00:00.000Z")
    obj = shown_object(capsys, run_id)
    assert obj["state"] == "done"
    assert obj["cancel_requested_at"] is None


def test_the_run_object_raises_for_a_corrupt_row(make: Maker, store: Store) -> None:
    run_id = make()
    corrupt(store, run_id, pr="nope")
    run = store.get_run(run_id, tolerant=True)
    assert run is not None
    with pytest.raises(runs.CorruptRunError):
        runs.run_object(run)


def test_human_list_prints_a_dash_for_a_missing_pr(make: Maker, store: Store) -> None:
    run_id = make()
    run = store.get_run(run_id)
    assert run is not None
    obj = runs.run_object(run)
    obj["pr"] = None
    assert " None" not in runs.human_list([obj], False)


# ------------------------------------------------------------------ R2: tolerant reads


@pytest.mark.parametrize("column", ["warnings", "failure", "anchor", "summary"])
@pytest.mark.parametrize("payload", ["[" * 200_000, "[" * 3_000_000, '{"a":' * 100_000])
def test_deeply_nested_json_columns_do_not_crash(
    column: str,
    payload: str,
    make: Maker,
    store: Store,
    capsys: pytest.CaptureFixture[str],
) -> None:
    good = make()
    bad = make_in_state(make, "done")
    corrupt(store, bad, **{column: payload})
    assert run_cli(capsys, "--json", "show", bad).code == 0
    assert set(ids_of(capsys)) == {good, bad}


def test_invalid_utf8_in_text_columns_does_not_crash(
    make: Maker, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    good = make()
    bad = make()
    bad_pr = make()
    store._conn.execute("UPDATE runs SET pr = CAST(x'ff41fe' AS TEXT) WHERE run_id = ?", (bad_pr,))
    store._conn.execute("PRAGMA foreign_keys = OFF")
    store._conn.execute("PRAGMA ignore_check_constraints = ON")
    store._conn.execute(
        "UPDATE runs SET run_id = CAST(x'72722dff' AS TEXT) WHERE run_id = ?", (bad,)
    )
    store._conn.execute("PRAGMA ignore_check_constraints = OFF")
    listed = run_cli(capsys, "--json", "list")
    assert listed.code == 0
    doc = listed.envelope()
    validate_against("cli/list.json", doc)
    assert [r["run_id"] for r in doc["runs"]] == [good]
    assert run_cli(capsys, "--json", "show", bad_pr).code == 1
    assert run_cli(capsys, "--json", "show", good).code == 0
    assert run_cli(capsys, "--json", "list", "--pr", "host/o/r#1").code == 0


# ------------------------------------------------------------------ R3: draft path safety


def test_draft_refuses_a_symlinked_run_directory(
    make: Maker, store: Store, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = make(end="done", exit_code=0, fields=done_fields())
    real = tmp_path / "elsewhere"
    real.mkdir()
    (real / "review.json").write_text('{"secret": true}')
    target = store.runs_dir() / run_id
    if target.exists():
        target.rmdir()
    target.symlink_to(real)
    res = run_cli(capsys, "--json", "show", "--draft", run_id)
    assert res.code == 12
    assert res.envelope()["error"]["reason"] == "state_unsafe"
    assert "secret" not in res.out


def test_draft_refuses_a_run_id_that_is_not_a_run_id(
    make: Maker, store: Store, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = make(end="done", exit_code=0, fields=done_fields())
    evil = f"{run_id}/../../.."
    (store.runs_dir() / run_id).mkdir(exist_ok=True)
    (store.root / "review.json").write_text('{"secret": true}')
    store._conn.execute("PRAGMA foreign_keys = OFF")
    store._conn.execute("PRAGMA ignore_check_constraints = ON")
    store._conn.execute("UPDATE runs SET run_id = ? WHERE run_id = ?", (evil, run_id))
    store._conn.execute("PRAGMA ignore_check_constraints = OFF")
    res = run_cli(capsys, "--json", "show", "--draft", run_id[:7])
    assert res.code == 10
    assert "secret" not in res.out


# ------------------------------------------------------------------ R4, R5


def test_many_label_filters_are_deduplicated_and_capped(
    make: Maker, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = make(labels=("k=v",))
    same = [arg for _ in range(1200) for arg in ("--label", "k=v")]
    assert ids_of(capsys, *same) == [run_id]
    distinct = [arg for i in range(51) for arg in ("--label", f"k{i}=v")]
    res = run_cli(capsys, "--json", "list", *distinct)
    assert res.code == 2
    assert res.envelope()["error"]["code"] == "usage"
    many = [arg for i in range(1200) for arg in ("--label", f"k{i}=v")]
    assert run_cli(capsys, "--json", "list", *many).code == 2
    fifty = [arg for i in range(50) for arg in ("--label", f"k{i}=v")]
    assert run_cli(capsys, "--json", "list", *fifty).code == 0


def test_since_cutoff_rounds_up_to_the_next_millisecond() -> None:
    now = dt.datetime(2026, 1, 2, 0, 0, 0, tzinfo=dt.UTC)
    sub = dt.datetime(2026, 1, 1, 0, 0, 0, 1, tzinfo=dt.UTC)
    assert runs.since_cutoff(sub, now) == "2026-01-01T00:00:00.001Z"
    exact = dt.datetime(2026, 1, 1, 0, 0, 0, 5000, tzinfo=dt.UTC)
    assert runs.since_cutoff(exact, now) == "2026-01-01T00:00:00.005Z"
    now_us = dt.datetime(2026, 1, 2, 0, 0, 0, 1500, tzinfo=dt.UTC)
    assert runs.since_cutoff(dt.timedelta(hours=1), now_us) == "2026-01-01T23:00:00.002Z"
