"""Finalize, review round 1 (issue #19): the fixes R1 to R14 of the resolution list."""

# ruff: noqa: E501

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from review_run import ending, hooks
from review_run import finalize as fin
from review_run import launch as lc
from review_run.errors import ReviewRunError
from review_run.store import LeaseLostError, Store, lease_name
from tests.run_helpers import World
from tests.test_finalize import (
    MARKER,
    REVIEW,
    SEAMS,
    SUMMARY,
    agent_calls,
    agent_finalize,
    ok_check,
    ok_process,
    parked_running,
    ref_for,
    run_dir,
)


def verdict_is(monkeypatch: pytest.MonkeyPatch, verdict: str) -> None:
    monkeypatch.setattr(lc, "check_parked", lambda *a, **k: verdict)


def steal_lease(world: World, run_id: str) -> None:
    with world.store() as store, store.transaction() as conn:
        conn.execute(
            "UPDATE leases SET token = ? WHERE name = ?", ("stolen-token", lease_name(run_id))
        )


def hook_cmd(out: Path) -> str:
    return f"import sys;open({str(out)!r},'w').write(sys.stdin.read())"


# -- R1, R5: the run path ------------------------------------------------------------------------


def test_r1_a_gone_agent_ends_the_run_agent_gone_before_anything_is_copied(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    verdict_is(monkeypatch, "gone")
    run = world.run(seams=SEAMS)
    assert (run.state, run.reason, run.exit_code) == ("failed", "agent_gone", 12)
    assert "agent_mismatch" not in run.warnings
    assert run.phase is None and run.original_sha256 is None
    assert not (run_dir(world) / "original.json").exists()
    assert agent_calls(world, "teardown") == []


def test_r5_a_live_agent_leaves_the_run_unchanged(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    verdict_is(monkeypatch, "live")
    run = world.run(seams=SEAMS)
    assert (run.state, run.phase) == ("running", "session")
    assert not (run_dir(world) / "original.json").exists()


def test_r5_a_failed_show_raises_lower_failed_and_the_row_is_unchanged(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    verdict_is(monkeypatch, "failed")
    with pytest.raises(ReviewRunError) as info:
        world.run(seams=SEAMS)
    assert (info.value.reason, info.value.exit_code) == ("lower_failed", 12)
    run = world.only_run()
    assert (run.state, run.phase) == ("running", "session")


@pytest.mark.parametrize("how", ["corrupt", "missing"])
def test_r5_unreadable_settings_propagate_internal_and_leave_the_run(
    world: World, how: str
) -> None:
    run_id = parked_running(world)
    path = world.root / "runs" / run_id / "settings.json"
    if how == "corrupt":
        path.write_text("{not json")
    else:
        path.unlink()
    with pytest.raises(ReviewRunError) as info:
        fin.finalize_agent(world.root, run_id, agent_finalize(world))
    assert info.value.code == "internal"
    run = world.only_run()
    assert (run.state, run.phase) == ("running", "session")


# -- R2, R3: no unverified original.json -----------------------------------------------------------


def test_r2_artifact_invalid_length_keeps_no_original_json(tmp_path: Path) -> None:
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    data = b"x" * 100
    (src / "review.json").write_bytes(data)
    (dst / "original.json").write_bytes(b"stale")
    with pytest.raises(fin.FinalizeFailure) as info:
        fin.copy_artifact(ref_for(src / "review.json", data), dst, max_bytes=99)
    assert info.value.reason == "artifact_invalid"
    assert list(dst.iterdir()) == []


def test_r3_a_stale_original_is_removed_when_the_artifact_mismatches(tmp_path: Path) -> None:
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    (src / "review.json").write_bytes(b"{}")
    (dst / "original.json").write_bytes(b"stale")
    with pytest.raises(fin.FinalizeFailure) as info:
        fin.copy_artifact(ref_for(src / "review.json", b"{}", sha256="0" * 64), dst, max_bytes=99)
    assert info.value.reason == "artifact_mismatch"
    assert list(dst.iterdir()) == []


def test_r3_a_post_rename_read_failure_leaves_no_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    (src / "review.json").write_bytes(b"{}")

    def bad(*a: Any, **k: Any) -> bytes:
        raise fin.FinalizeFailure("artifact_mismatch")

    monkeypatch.setattr(fin, "read_verified", bad)
    with pytest.raises(fin.FinalizeFailure):
        fin.copy_artifact(ref_for(src / "review.json", b"{}"), dst, max_bytes=99)
    assert list(dst.iterdir()) == []


def test_r3_a_stale_original_then_a_mismatch_through_the_pipeline(world: World) -> None:
    world.fb.use("agent-spawn", "artifact_mismatch")
    run_id = parked_running(world)
    stale = world.root / "runs" / run_id / "original.json"
    stale.write_bytes(b"stale")
    done = fin.finalize_agent(world.root, run_id, agent_finalize(world))
    assert done is not None and done.reason == "artifact_mismatch"
    assert not stale.exists() and done.original_sha256 is None


# -- R4: the cancellation that converts a done ending ------------------------------------------------


def test_r4_a_cancellation_converting_done_marks_the_teardown_pending_first(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def cancel_in_seam(ctx: fin.ArtifactContext) -> fin.Processed:
        with world.store() as store, store.transaction() as conn:
            conn.execute(
                "UPDATE runs SET cancel_requested_at = '2026-01-01T00:00:00Z',"
                " cancel_exit_code = 143 WHERE run_id = ?",
                (ctx.run.run_id,),
            )
        return ok_process(ctx)

    seen: list[str | None] = []
    real = ending.Ender.teardown_after

    def spy(self: Any, agent_id: str, csession_id: str | None, run: Any) -> None:
        seen.append(self.row().teardown)
        real(self, agent_id, csession_id, run)

    monkeypatch.setattr(ending.Ender, "teardown_after", spy)
    run = world.run(seams=fin.Seams(ok_check, cancel_in_seam))
    assert (run.state, run.reason, run.exit_code) == ("failed", "canceled", 143)
    assert seen == ["pending"] and run.teardown == "done"


# -- R6: the lease is checked in the database before the files are replaced ---------------------------


def test_r6_a_stale_holder_never_overwrites_review_json(world: World) -> None:
    winner = b'{"winner": true}\n'
    holder: dict[str, Path] = {}

    def process(ctx: fin.ArtifactContext) -> fin.Processed:
        path = ctx.run_dir / "review.json"
        path.write_bytes(winner)  # the new holder's finished file
        steal_lease(world, ctx.run.run_id)  # the keeper's in-memory flag lags by up to 60 s
        holder["review"] = path
        return fin.Processed(REVIEW, dict(SUMMARY))

    with pytest.raises(LeaseLostError):
        world.run(seams=fin.Seams(ok_check, process))
    assert holder["review"].read_bytes() == winner
    run = world.only_run()
    assert (run.state, run.phase) == ("running", "finalize")
    assert not [p for p in holder["review"].parent.iterdir() if p.name.endswith(".tmp")]


def test_r6_the_lease_is_verified_before_original_json_is_replaced(tmp_path: Path) -> None:
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    (src / "review.json").write_bytes(b"{}")
    (dst / "original.json").write_bytes(b"winner")

    def lost() -> None:
        raise LeaseLostError()

    with pytest.raises(LeaseLostError):
        fin.copy_artifact(
            ref_for(src / "review.json", b"{}"), dst, max_bytes=99, before_commit=lost
        )
    assert [p.name for p in dst.iterdir()] == ["original.json"]
    assert (dst / "original.json").read_bytes() == b"winner"


def test_r6_re_entering_phase_finalize_verifies_the_lease_before_the_copy(world: World) -> None:
    run_id = parked_running(world)

    class Crash(BaseException):
        pass

    def crash(ctx: fin.ArtifactContext) -> fin.Processed:
        raise Crash

    with pytest.raises(Crash):
        fin.finalize_agent(
            world.root, run_id, agent_finalize(world, seams=fin.Seams(ok_check, crash))
        )
    (world.root / "runs" / run_id / "original.json").unlink()
    with world.store() as store:
        lease = store.acquire_lease(lease_name(run_id))
        assert lease is not None
        store.release_lease(lease)
        with pytest.raises(LeaseLostError):
            store.assert_holds(run_id, lease)  # released: no longer the holder


# -- R7: dot components ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path", ["/x/../review.json", "/x/./review.json", "//x/review.json", "/x//review.json"]
)
def test_r7_an_artifact_path_with_dot_or_empty_components_is_no_artifact(path: str) -> None:
    report = {
        "status": "done",
        "implicit": False,
        "artifact": {"name": "review.json", "path": path, "sha256": "0" * 64, "size": 1},
    }
    info = lc.parse_report(report)
    assert info is None or info.artifact is None


# -- R11: a malformed SessionCheck ------------------------------------------------------------------------


def test_r11_an_invalid_session_check_ends_the_run_internal_once(world: World) -> None:
    def bad_check(ctx: fin.SessionContext) -> fin.SessionCheck:
        return fin.SessionCheck(tools_checked="yes", tools_unexpected=-5)  # type: ignore[arg-type]

    run = world.run(seams=fin.Seams(bad_check, ok_process))
    assert (run.state, run.reason, run.exit_code) == ("failed", "internal", 12)
    again = fin.finalize_agent(world.root, run.run_id, agent_finalize(world))
    assert again is not None and again == world.only_run() and again.state == "failed"


def test_r11_a_payload_that_cannot_be_built_ends_the_run_internal_once(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = hooks.post_run_payload

    def boom(**kw: Any) -> Any:
        if kw["state"] == "done":
            raise ValueError(MARKER)
        return real(**kw)

    monkeypatch.setattr(hooks, "post_run_payload", boom)
    run = world.run(seams=SEAMS)
    assert (run.state, run.reason, run.exit_code) == ("failed", "internal", 12)
    assert MARKER not in json.dumps(run.failure) + (world.root / "audit.jsonl").read_text()


# -- R12: parked_verdict --------------------------------------------------------------------------------


def _doc(status: str, run_id: str = "rr-aaaaaaaa", **over: Any) -> dict[str, Any]:
    agent = {
        "status": status,
        "kind": "review",
        "csession_id": "cs-aaaaaaaa",
        "labels": [
            "owner=review-run",
            f"run={run_id}",
            "pr=github.com/o/r#1",
            "repo=github.com/o/r",
        ],
    }
    agent.update(over)
    return {"agent": agent}


@pytest.mark.parametrize(
    ("status", "verdict"),
    [
        ("parked", "owned"),
        ("tearing_down", "owned"),
        ("torn_down", "gone"),
        ("live", "live"),
        ("launching", "live"),
        ("something_new", "live"),
    ],
)
def test_r12_parked_verdict_by_status(status: str, verdict: str) -> None:
    got = lc.parked_verdict(
        _doc(status),
        run_id="rr-aaaaaaaa",
        pr="github.com/o/r#1",
        repo="github.com/o/r",
        csession_id="cs-aaaaaaaa",
    )
    assert got == verdict


def test_r12_a_tearing_down_look_alike_is_still_a_mismatch() -> None:
    got = lc.parked_verdict(
        _doc("tearing_down"),
        run_id="rr-bbbbbbbb",
        pr="github.com/o/r#1",
        repo="github.com/o/r",
        csession_id=None,
    )
    assert got == "mismatch"


# -- R13, R14: after the commit ---------------------------------------------------------------------------


def test_r13_a_prompt_file_removal_error_does_not_stop_teardown_or_delivery(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = world.tmp / "post.json"

    def boom(path: Any) -> None:
        raise PermissionError(MARKER)

    monkeypatch.setattr(ending, "remove_prompt_files", boom)
    run = world.run(seams=SEAMS, flag_hooks=(f"post-run={hook_cmd(out)}",))
    assert (run.state, run.teardown) == ("done", "done")
    assert json.loads(out.read_text())["state"] == "done"


def test_r14_the_hook_is_delivered_before_teardown_on_done(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = world.tmp / "post.json"
    seen: list[bool] = []
    real = lc.teardown_owned

    def spy(*a: Any, **k: Any) -> Any:
        seen.append(out.exists())
        return real(*a, **k)

    monkeypatch.setattr(lc, "teardown_owned", spy)
    run = world.run(seams=SEAMS, flag_hooks=(f"post-run={hook_cmd(out)}",))
    assert run.state == "done" and seen == [True]


def test_r14_the_hook_is_delivered_before_teardown_on_failure(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.fb.use("agent-spawn", "outcome", outcome="killed")
    out = world.tmp / "post.json"
    seen: list[bool] = []
    real = lc.teardown_owned

    def spy(*a: Any, **k: Any) -> Any:
        seen.append(out.exists())
        return real(*a, **k)

    monkeypatch.setattr(lc, "teardown_owned", spy)
    run = world.run(seams=SEAMS, flag_hooks=(f"post-run={hook_cmd(out)}",))
    assert run.state == "failed" and seen == [True]


def test_r14_a_failing_teardown_bookkeeping_still_delivers_the_hook(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = world.tmp / "post.json"

    def lost(self: Any, *a: Any, **k: Any) -> bool:
        raise LeaseLostError()

    monkeypatch.setattr(Store, "set_teardown", lost)
    with pytest.raises(LeaseLostError):
        world.run(seams=SEAMS, flag_hooks=(f"post-run={hook_cmd(out)}",))
    assert json.loads(out.read_text())["state"] == "done"


# -- R8, R9 -------------------------------------------------------------------------------------------------


def test_r8_ownership_is_checked_again_right_before_teardown(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    real = lc.check_parked

    def flip(*a: Any, **k: Any) -> str:
        calls.append("check")
        return real(*a, **k) if len(calls) == 1 else "mismatch"

    monkeypatch.setattr(lc, "check_parked", flip)
    run = world.run(seams=SEAMS)
    assert run.state == "done" and "agent_mismatch" in run.warnings
    assert agent_calls(world, "teardown") == [] and len(calls) == 2


def test_r9_agent_gone_finalize_record_has_cause_agent_gone(world: World) -> None:
    run_id = parked_running(world)
    run = world.only_run()
    assert run.agent_id
    world.fb.run("agent-spawn", "teardown", run.agent_id, "--json")
    done = fin.finalize_agent(world.root, run_id, agent_finalize(world))
    assert done is not None and done.reason == "agent_gone"
    records = [json.loads(x) for x in (world.root / "audit.jsonl").read_text().splitlines()]
    (rec,) = [r for r in records if r["action"] == "finalize"]
    assert rec["inputs"]["cause"] == "agent_gone"


_ = hashlib
