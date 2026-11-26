"""Finalize (issue #19): classify, copy and verify, the terminal transaction, teardown, recovery.

The checks other issues own (``Seams``: skill and tool checks, validation and meta) are stood in
for by small functions here; everything else is the real pipeline against the fake siblings, the
real store, audit trail, outbox and hooks.
"""

# ruff: noqa: E501

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from review_run import finalize as fin
from review_run import hooks, runner, runs
from review_run import launch as lc
from review_run.errors import ReviewRunError
from review_run.store import Expect, Store, lease_name
from tests.conftest import validate_against
from tests.run_helpers import PY, World

MARKER = "UNTRUSTED-MARKER-9c1d"
SUMMARY: dict[str, Any] = {
    "verdict": "comment",
    "severity_counts": {k: 0 for k in ("blocker", "major", "minor", "nit", "info", "unspecified")},
    "risk": "low",
    "coverage_status": "full",
    "code_run_status": "not_configured",
    "code_run_reason": None,
    "skipped_files": 0,
    "normalized_count": 0,
    "head_moved": False,
}
REVIEW = b'{"schema_version": 1, "event": "comment"}\n'


def ok_check(ctx: fin.SessionContext) -> fin.SessionCheck:
    return fin.SessionCheck(
        warnings=("skill_unchecked",), skill_checked=False, tools_checked=False, tools_unexpected=0
    )


def ok_process(ctx: fin.ArtifactContext) -> fin.Processed:
    assert hashlib.sha256(ctx.original).hexdigest() == ctx.original_sha256
    return fin.Processed(REVIEW, dict(SUMMARY), warnings=("tools_unchecked",))


SEAMS = fin.Seams(ok_check, ok_process)


def audit_records(root: Path) -> list[dict[str, Any]]:
    path = root / "audit.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def agent_calls(world: World, command: str) -> list[Any]:
    return world.fb.calls("agent-spawn", command)


def run_dir(world: World) -> Path:
    return world.root / "runs" / world.only_run().run_id


def object_of(world: World) -> dict[str, Any]:
    obj = runs.run_object(world.only_run())
    validate_against("cli/run.json", obj)
    return obj


def frozen_path(world: World) -> Path:
    (call,) = agent_calls(world, "wait")
    doc = json.loads(call.stdout)
    return Path(doc["run"]["report"]["artifact"]["path"])


def parked_running(world: World, **hooks_kw: Any) -> str:
    """A run left ``running`` in phase ``session`` whose agent has ended (parked)."""
    run = world.run(no_wait=True, **hooks_kw)
    assert (run.state, run.phase) == ("running", "session") and run.agent_id
    ended = world.fb.run("agent-spawn", "wait", run.agent_id, "--json")
    assert ended.returncode in (0, 13, 14, 15, 16, 17, 18)
    return run.run_id


def agent_finalize(world: World, **kw: Any) -> fin.AgentFinalize:
    kw.setdefault("finalized_by", "show")
    kw.setdefault("seams", SEAMS)
    kw.setdefault("shell", (PY, "-c"))
    return fin.AgentFinalize(**kw)


# -- the whole pipeline from run ------------------------------------------------------------------


def test_a_done_session_is_finalized_by_run_with_everything_stored(world: World) -> None:
    out = world.tmp / "post.json"
    cmd = f"import sys;open({str(out)!r},'w').write(sys.stdin.read())"
    run = world.run(seams=SEAMS, flag_hooks=(f"post-run={cmd}",))
    assert (run.state, run.reason, run.exit_code, run.phase) == ("done", None, 0, None)
    assert run.finished_at is not None and run.teardown == "done" and run.finalized_run == 1
    base = world.root / "runs" / run.run_id
    original = base / "original.json"
    frozen = frozen_path(world)
    assert original.read_bytes() == frozen.read_bytes()
    assert stat.S_IMODE(original.stat().st_mode) == 0o600
    review = base / "review.json"
    assert review.read_bytes() == REVIEW and stat.S_IMODE(review.stat().st_mode) == 0o600
    assert run.artifact_path == str(review) and run.artifact_size == len(REVIEW)
    assert run.artifact_sha256 == hashlib.sha256(REVIEW).hexdigest()
    assert run.original_sha256 == hashlib.sha256(original.read_bytes()).hexdigest()
    assert run.summary == SUMMARY
    assert sorted(run.warnings) == ["skill_unchecked", "tools_unchecked"]
    (call,) = agent_calls(world, "teardown")
    assert "--force" not in call.argv and call.argv[0] == "teardown"
    assert not [p for p in base.iterdir() if p.name.endswith(".tmp")]
    assert not (base / "prompt.md").exists() and not (base / "inputs").exists()
    obj = object_of(world)
    assert obj["state"] == "done" and obj["artifact"]["path"] == str(review)
    # the audit pair is closed by the same attempt, the event is delivered once
    records = audit_records(world.root)
    assert [r["action"] for r in records] == ["run", "teardown", "run"]
    assert records[0]["attempt_id"] == records[2]["attempt_id"]
    assert records[2]["result"] == {"status": "done", "reason": None, "exit_code": 0}
    assert records[2]["event_id"] == f"{run.run_id}-post-run-1"
    payload = json.loads(out.read_text())
    assert (payload["state"], payload["exit_code"], payload["verdict"]) == ("done", 0, "comment")
    assert (payload["tools_checked"], payload["tools_unexpected"]) == (False, 0)
    with world.store() as store:
        assert store.pending_events(run.run_id) == []


def test_a_report_resume_run_above_the_awaited_one_is_finalized_and_recorded(
    world: World,
) -> None:
    world.fb.use("agent-spawn", "unreported_recovered", then="done")
    run = world.run(seams=SEAMS)
    assert run.state == "done" and run.awaited_run == 1 and run.finalized_run == 2


# -- steps 1 to 3 -----------------------------------------------------------------------------------


def test_an_implicit_done_fails_the_run_without_copying_anything(world: World) -> None:
    world.fb.use("agent-spawn", "implicit_done")
    run = world.run(seams=SEAMS)
    assert (run.state, run.reason, run.exit_code) == ("failed", "implicit_done", 22)
    assert not (run_dir(world) / "original.json").exists()
    assert run.teardown == "done" and len(agent_calls(world, "teardown")) == 1
    object_of(world)


def test_a_done_report_without_an_artifact_is_artifact_missing(world: World) -> None:
    world.fb.use("agent-spawn", "no_artifact")
    run = world.run(seams=SEAMS)
    assert (run.state, run.reason, run.exit_code) == ("failed", "artifact_missing", 22)
    assert run.teardown == "done"


@pytest.mark.parametrize(
    "artifact",
    [
        {"name": "result.md", "path": "/x/result.md", "sha256": "0" * 64, "size": 1},
        {"name": "review.json", "path": "relative/review.json", "sha256": "0" * 64, "size": 1},
        {"name": "review.json", "path": "/x/other.json", "sha256": "0" * 64, "size": 1},
    ],
)
def test_a_bad_artifact_object_is_artifact_missing_and_nothing_is_opened(
    world: World, artifact: dict[str, Any]
) -> None:
    world.fb.use("agent-spawn", "outcome", outcome="done", artifact=artifact)
    run = world.run(seams=SEAMS)
    assert (run.state, run.reason) == ("failed", "artifact_missing")
    assert not (run_dir(world) / "original.json").exists()


def test_a_size_or_hash_that_differs_is_artifact_mismatch_and_nothing_is_kept(
    world: World,
) -> None:
    world.fb.use("agent-spawn", "artifact_mismatch")
    run = world.run(seams=SEAMS)
    assert (run.state, run.reason, run.exit_code) == ("failed", "artifact_mismatch", 22)
    base = run_dir(world)
    assert not (base / "original.json").exists() and not (base / "review.json").exists()
    assert not [p for p in base.iterdir() if p.name.endswith(".tmp")]
    assert run.original_sha256 is None and run.teardown == "done"


def test_a_frozen_file_swapped_for_a_link_before_the_copy_is_artifact_mismatch(
    world: World,
) -> None:
    secret = world.tmp / "secret.json"
    secret.write_text('{"secret": true}\n')

    world.fb.use("agent-spawn", "outcome", outcome="done")
    run = world.run(no_wait=True)
    for path in world.fb.dir.rglob("review.json"):
        path.unlink()
        path.symlink_to(secret)
    world.fb.run("agent-spawn", "wait", run.agent_id or "", "--json")
    for path in world.fb.dir.rglob("review.json"):
        # `wait` froze a new copy after the swap; replace it with a link again
        path.unlink()
        path.symlink_to(secret)
    done = fin.finalize_agent(world.root, run.run_id, agent_finalize(world))
    assert done is not None
    assert (done.state, done.reason) == ("failed", "artifact_mismatch")
    assert not (world.root / "runs" / run.run_id / "original.json").exists()
    assert "secret" not in json.dumps(runs.run_object(done))


# -- copy_artifact, hostile files -------------------------------------------------------------------


def ref_for(path: Path, data: bytes, *, name: str = "review.json", **over: Any) -> lc.ArtifactRef:
    fields: dict[str, Any] = {
        "name": name,
        "path": str(path),
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
    }
    fields.update(over)
    return lc.ArtifactRef(**fields)


@pytest.fixture
def dirs(tmp_path: Path) -> tuple[Path, Path]:
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    return src, dst


def test_copy_artifact_copies_verifies_and_makes_the_copy_private(dirs: tuple[Path, Path]) -> None:
    src, dst = dirs
    data = b'{"a": "\xc3\xa9"}\n'
    path = src / "review.json"
    path.write_bytes(data)
    path.chmod(0o644)
    got, sha = fin.copy_artifact(ref_for(path, data), dst, max_bytes=1024)
    assert got == data and sha == hashlib.sha256(data).hexdigest()
    assert (dst / "original.json").read_bytes() == data
    assert stat.S_IMODE((dst / "original.json").stat().st_mode) == 0o600
    assert [p.name for p in dst.iterdir()] == ["original.json"]


def test_copy_artifact_replaces_a_stale_copy_atomically(dirs: tuple[Path, Path]) -> None:
    src, dst = dirs
    (dst / "original.json").write_bytes(b"stale")
    path = src / "review.json"
    path.write_bytes(b"{}")
    fin.copy_artifact(ref_for(path, b"{}"), dst, max_bytes=1024)
    assert (dst / "original.json").read_bytes() == b"{}"


def test_copy_artifact_refuses_a_symlink(dirs: tuple[Path, Path]) -> None:
    src, dst = dirs
    (src / "real.json").write_bytes(b"{}")
    (src / "review.json").symlink_to(src / "real.json")
    with pytest.raises(fin.FinalizeFailure) as info:
        fin.copy_artifact(ref_for(src / "review.json", b"{}"), dst, max_bytes=1024)
    assert info.value.reason == "artifact_mismatch" and list(dst.iterdir()) == []


def test_copy_artifact_refuses_a_fifo_without_blocking(dirs: tuple[Path, Path]) -> None:
    src, dst = dirs
    os.mkfifo(src / "review.json")
    with pytest.raises(fin.FinalizeFailure) as info:
        fin.copy_artifact(ref_for(src / "review.json", b"{}"), dst, max_bytes=1024)
    assert info.value.reason == "artifact_mismatch" and list(dst.iterdir()) == []


def test_copy_artifact_refuses_a_directory(dirs: tuple[Path, Path]) -> None:
    src, dst = dirs
    (src / "review.json").mkdir()
    with pytest.raises(fin.FinalizeFailure) as info:
        fin.copy_artifact(ref_for(src / "review.json", b"{}", size=0), dst, max_bytes=1024)
    assert info.value.reason == "artifact_mismatch"


def test_copy_artifact_refuses_a_hard_linked_file(dirs: tuple[Path, Path], tmp_path: Path) -> None:
    src, dst = dirs
    other = tmp_path / "elsewhere.json"
    other.write_bytes(b"{}")
    os.link(other, src / "review.json")
    with pytest.raises(fin.FinalizeFailure) as info:
        fin.copy_artifact(ref_for(src / "review.json", b"{}"), dst, max_bytes=1024)
    assert info.value.reason == "artifact_mismatch"


def test_copy_artifact_missing_file(dirs: tuple[Path, Path]) -> None:
    src, dst = dirs
    with pytest.raises(fin.FinalizeFailure) as info:
        fin.copy_artifact(ref_for(src / "review.json", b"{}"), dst, max_bytes=1024)
    assert info.value.reason == "artifact_missing"


@pytest.mark.parametrize("delta", [1, -1])
def test_copy_artifact_size_that_differs_either_way(dirs: tuple[Path, Path], delta: int) -> None:
    src, dst = dirs
    data = b'{"k": 1}\n'
    (src / "review.json").write_bytes(data)
    ref = ref_for(src / "review.json", data, size=len(data) + delta)
    with pytest.raises(fin.FinalizeFailure) as info:
        fin.copy_artifact(ref, dst, max_bytes=1024)
    assert info.value.reason == "artifact_mismatch" and list(dst.iterdir()) == []


def test_copy_artifact_hash_that_differs(dirs: tuple[Path, Path]) -> None:
    src, dst = dirs
    (src / "review.json").write_bytes(b"{ }")
    ref = ref_for(src / "review.json", b"{}", size=3)
    with pytest.raises(fin.FinalizeFailure) as info:
        fin.copy_artifact(ref, dst, max_bytes=1024)
    assert info.value.reason == "artifact_mismatch" and list(dst.iterdir()) == []


def test_copy_artifact_reads_at_most_the_reported_size_plus_one(
    dirs: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file that grew after fstat is stopped at size + 1 bytes and is a mismatch."""
    src, dst = dirs
    data = b"x" * 100
    (src / "review.json").write_bytes(data)
    real_read = os.read
    served: list[int] = []

    def growing(fd: int, n: int) -> bytes:
        chunk = real_read(fd, n)
        if chunk:
            chunk += b"+" * 50  # the file keeps growing under the reader
        served.append(len(chunk[:n]))
        return chunk[:n]

    monkeypatch.setattr(os, "read", growing)
    with pytest.raises(fin.FinalizeFailure) as info:
        fin.copy_artifact(ref_for(src / "review.json", data), dst, max_bytes=1024)
    assert info.value.reason == "artifact_mismatch" and sum(served) <= len(data) + 1
    assert list(dst.iterdir()) == []


def test_copy_artifact_over_the_cap_is_artifact_invalid_and_nothing_is_read(
    dirs: tuple[Path, Path],
) -> None:
    src, dst = dirs
    data = b"x" * 100
    (src / "review.json").write_bytes(data)
    with pytest.raises(fin.FinalizeFailure) as info:
        fin.copy_artifact(ref_for(src / "review.json", data), dst, max_bytes=99)
    assert info.value.reason == "artifact_invalid"
    assert info.value.failure["errors"] == [{"pointer": "", "kind": "length"}]
    assert list(dst.iterdir()) == []


def test_copy_artifact_name_and_basename_must_be_review_json(dirs: tuple[Path, Path]) -> None:
    src, dst = dirs
    (src / "other.json").write_bytes(b"{}")
    for ref in (
        ref_for(src / "other.json", b"{}"),
        ref_for(src / "other.json", b"{}", name="result.md"),
    ):
        with pytest.raises(fin.FinalizeFailure) as info:
            fin.copy_artifact(ref, dst, max_bytes=1024)
        assert info.value.reason == "artifact_missing"


def test_copy_artifact_destination_errors_are_not_artifact_failures(
    dirs: tuple[Path, Path],
) -> None:
    src, dst = dirs
    (src / "review.json").write_bytes(b"{}")
    with pytest.raises(OSError):
        fin.copy_artifact(ref_for(src / "review.json", b"{}"), dst / "missing-dir", max_bytes=1024)


# -- seams and failures -----------------------------------------------------------------------------


def test_incomplete_seams_change_nothing(world: World) -> None:
    with pytest.raises(ReviewRunError) as info:
        world.run(seams=fin.Seams(check_session=ok_check))
    assert info.value.code == "internal"
    run = world.only_run()
    assert (run.state, run.phase) == ("running", "session")
    assert not (run_dir(world) / "original.json").exists() and agent_calls(world, "teardown") == []


def test_a_failure_from_a_seam_ends_the_run_with_its_closed_reason_and_keeps_the_copy(
    world: World,
) -> None:
    def bad(ctx: fin.ArtifactContext) -> fin.Processed:
        raise fin.FinalizeFailure(
            "artifact_invalid", errors=[{"pointer": "/meta/summary", "kind": "length"}]
        )

    run = world.run(seams=fin.Seams(ok_check, bad))
    assert (run.state, run.reason, run.exit_code) == ("failed", "artifact_invalid", 22)
    assert run.failure is not None and run.failure["errors"] == [
        {"pointer": "/meta/summary", "kind": "length"}
    ]
    base = run_dir(world)
    assert (base / "original.json").exists() and not (base / "review.json").exists()
    assert run.original_sha256 == hashlib.sha256((base / "original.json").read_bytes()).hexdigest()
    assert run.teardown == "done" and run.artifact_path is None
    object_of(world)


def test_a_skill_check_failure_is_skill_missing_22(world: World) -> None:
    def missing(ctx: fin.SessionContext) -> fin.SessionCheck:
        raise fin.FinalizeFailure("skill_missing")

    run = world.run(seams=fin.Seams(missing, ok_process))
    assert (run.state, run.reason, run.exit_code) == ("failed", "skill_missing", 22)
    assert (run_dir(world) / "original.json").exists()


def test_an_unexpected_exception_ends_the_run_internal_once_without_its_text(
    world: World,
) -> None:
    def boom(ctx: fin.ArtifactContext) -> fin.Processed:
        raise RuntimeError(MARKER)

    run = world.run(seams=fin.Seams(ok_check, boom))
    assert (run.state, run.reason, run.exit_code) == ("failed", "internal", 12)
    assert run.teardown == "done"
    blob = json.dumps(runs.run_object(run)) + (world.root / "audit.jsonl").read_text()
    log = world.root / "log.jsonl"
    if log.exists():
        blob += log.read_text()
    assert MARKER not in blob
    assert len(agent_calls(world, "teardown")) == 1


@pytest.mark.parametrize(
    "bad",
    [
        fin.Processed(b"\xc3\xa9", dict(SUMMARY)),
        fin.Processed(REVIEW, {"verdict": "comment"}),
        fin.Processed(REVIEW, dict(SUMMARY), warnings=("made_up",)),
    ],
)
def test_a_malformed_product_of_a_seam_is_internal_not_done(
    world: World, bad: fin.Processed
) -> None:
    run = world.run(seams=fin.Seams(ok_check, lambda ctx: bad))
    assert (run.state, run.reason) == ("failed", "internal")
    assert not (run_dir(world) / "review.json").exists()


def test_a_seam_error_after_the_lease_was_lost_does_not_end_the_run(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def lose(ctx: fin.ArtifactContext) -> fin.Processed:
        raise RuntimeError("anything")

    original = runner.LeaseKeeper

    class Lost(original):  # a keeper whose lease was taken over while the seam ran
        def start(self) -> None:
            super().start()
            self.lost.set()

    monkeypatch.setattr(runner, "LeaseKeeper", Lost)
    with pytest.raises(ReviewRunError):
        world.run(seams=fin.Seams(ok_check, lose))
    assert world.only_run().state == "running"  # not ended by a process that lost its lease


# -- cancellation and the compare-and-set races -------------------------------------------------------


def test_a_recorded_cancellation_ends_the_run_canceled_and_nothing_is_copied(world: World) -> None:
    run_id = parked_running(world)
    with Store.open(world.root) as store:
        run = store.get_run(run_id)
        assert run is not None
        assert store.request_cancel(run_id, run.key, actor="human:alice", exit_code=143)
    done = fin.finalize_agent(world.root, run_id, agent_finalize(world))
    assert done is not None
    assert (done.state, done.reason, done.exit_code) == ("failed", "canceled", 143)
    assert not (world.root / "runs" / run_id / "original.json").exists()
    assert done.teardown == "done" and done.failure is None and done.artifact_path is None


def test_a_cancellation_recorded_after_the_row_was_read_wins_the_compare_and_set(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = parked_running(world)
    real = lc.parked_verdict

    fired: list[bool] = []

    def cancel_then_verdict(*args: Any, **kw: Any) -> str:
        if not fired:
            fired.append(True)
            with Store.open(world.root) as other:
                row = other.get_run(run_id)
                assert row is not None
                assert other.request_cancel(run_id, row.key, actor="human:bob", exit_code=130)
        return real(*args, **kw)

    monkeypatch.setattr(lc, "parked_verdict", cancel_then_verdict)
    done = fin.finalize_agent(world.root, run_id, agent_finalize(world))
    assert done is not None
    assert (done.state, done.reason, done.exit_code) == ("failed", "canceled", 130)
    assert not (world.root / "runs" / run_id / "original.json").exists()


def test_a_second_finalizer_is_a_no_op(world: World) -> None:
    run_id = parked_running(world)
    first = fin.finalize_agent(world.root, run_id, agent_finalize(world))
    assert first is not None and first.state == "done"
    calls = len(agent_calls(world, "teardown"))
    second = fin.finalize_agent(world.root, run_id, agent_finalize(world, finalized_by="wait"))
    assert second == first
    assert len(agent_calls(world, "teardown")) == calls == 1
    assert [r["action"] for r in audit_records(world.root)].count("finalize") == 1
    with Store.open(world.root) as store:
        assert len(store.pending_events()) == 0


def test_a_finalizer_that_finds_the_lease_held_does_not_wait_and_changes_nothing(
    world: World,
) -> None:
    run_id = parked_running(world)
    with Store.open(world.root) as store:
        lease = store.acquire_lease(lease_name(run_id))
        assert lease is not None
        done = fin.finalize_agent(world.root, run_id, agent_finalize(world))
        assert done is not None and (done.state, done.phase) == ("running", "session")
        store.release_lease(lease)
    assert agent_calls(world, "teardown") == []


def test_only_one_of_two_begin_finalize_calls_wins(world: World) -> None:
    run_id = parked_running(world)
    with Store.open(world.root) as store:
        lease = store.acquire_lease(lease_name(run_id))
        assert lease is not None
        run = store.get_run(run_id)
        assert run is not None
        assert store.begin_finalize(run_id, run.key, lease=lease)
        assert not store.begin_finalize(run_id, run.key, lease=lease)  # the key moved
        store.release_lease(lease)


# -- the finalizer of show, wait and reconcile --------------------------------------------------------


def test_finalize_agent_finishes_a_run_left_running_and_audits_a_finalize_record(
    world: World,
) -> None:
    out = world.tmp / "post.json"
    cmd = f"import sys;open({str(out)!r},'w').write(sys.stdin.read())"
    run_id = parked_running(world, flag_hooks=(f"post-run={cmd}",))
    done = fin.finalize_agent(world.root, run_id, agent_finalize(world, finalized_by="reconcile"))
    assert done is not None and (done.state, done.exit_code, done.teardown) == ("done", 0, "done")
    assert done.finalized_run == 1 and (world.root / "runs" / run_id / "review.json").exists()
    records = audit_records(world.root)
    assert [r["action"] for r in records] == ["run", "teardown", "finalize"]
    last = records[-1]
    assert last["actor"] == "auto:review-run"
    assert last["inputs"] == {"cause": "finalized", "finalized_by": "reconcile"}
    assert last["result"] == {"status": "done", "reason": None, "exit_code": 0}
    assert (
        last["event_id"] == f"{run_id}-post-run-1"
        and last["attempt_id"] != records[0]["attempt_id"]
    )
    payload = json.loads(out.read_text())
    assert payload["actor"] == "human:alice" and payload["state"] == "done"
    with Store.open(world.root) as store:
        assert store.lease_info(lease_name(run_id)) is None  # released


def test_finalize_agent_waits_for_the_agent_to_end(world: World) -> None:
    run = world.run(no_wait=True)
    done = fin.finalize_agent(world.root, run.run_id, agent_finalize(world))
    assert done is not None and (done.state, done.phase) == ("running", "session")
    assert agent_calls(world, "teardown") == []


def test_finalize_agent_ignores_runs_that_are_not_finalizable(world: World) -> None:
    run_id = parked_running(world)
    assert fin.finalize_agent(world.root, "rr-aaaaaaaa", agent_finalize(world)) is None
    first = fin.finalize_agent(world.root, run_id, agent_finalize(world))
    assert first is not None and first.state == "done"


def test_finalize_agent_escalates_a_needs_input_session_and_keeps_its_agent(world: World) -> None:
    world.fb.use("agent-spawn", "outcome", outcome="needs-input")
    run_id = parked_running(world)
    done = fin.finalize_agent(world.root, run_id, agent_finalize(world))
    assert done is not None
    assert (done.state, done.reason, done.exit_code) == ("escalated", "needs_input", 13)
    assert done.escalated_run == 1 and agent_calls(world, "teardown") == []
    # an escalated run that was not resumed stays escalated: a second look changes nothing
    again = fin.finalize_agent(world.root, run_id, agent_finalize(world))
    assert again == done


def test_finalize_agent_fails_a_killed_session_and_tears_the_agent_down(world: World) -> None:
    world.fb.use("agent-spawn", "outcome", outcome="killed")
    run_id = parked_running(world)
    done = fin.finalize_agent(world.root, run_id, agent_finalize(world))
    assert done is not None and (done.state, done.reason, done.exit_code) == (
        "failed",
        "killed",
        15,
    )
    assert done.teardown == "done" and len(agent_calls(world, "teardown")) == 1


def test_finalize_agent_fails_a_torn_down_agent_as_agent_gone(world: World) -> None:
    run_id = parked_running(world)
    run = world.only_run()
    assert run.agent_id
    world.fb.run("agent-spawn", "teardown", run.agent_id, "--json")
    done = fin.finalize_agent(world.root, run_id, agent_finalize(world))
    assert done is not None and (done.state, done.reason, done.exit_code) == (
        "failed",
        "agent_gone",
        12,
    )
    assert len(agent_calls(world, "teardown")) == 1  # the test's own call only


def test_a_look_alike_agent_is_never_torn_down_and_its_artifact_never_copied(
    world: World,
) -> None:
    run_id = parked_running(world)
    run = world.only_run()
    assert run.agent_id
    real = lc.parked_verdict
    # the agent now carries another run's labels
    world.fb.seed_state({})
    state = world.fb.state()
    for rec in state["agents"].values():
        rec["settings"]["labels"] = [
            x.replace(run_id, "rr-zzzzzzzz") for x in rec["settings"]["labels"]
        ]
    (world.fb.dir / "state.json").write_text(json.dumps(state))
    assert real is not None
    done = fin.finalize_agent(world.root, run_id, agent_finalize(world))
    assert done is not None and (done.state, done.reason) == ("failed", "agent_gone")
    assert "agent_mismatch" in done.warnings
    assert agent_calls(world, "teardown") == []
    assert not (world.root / "runs" / run_id / "original.json").exists()


# -- crash windows ----------------------------------------------------------------------------------


class Crash(BaseException):
    """A process death between two steps (BaseException: no handler of finalize may swallow it)."""


def test_a_finalizer_killed_after_the_copy_is_repeated_from_the_files(world: World) -> None:
    run_id = parked_running(world)

    def crash(ctx: fin.ArtifactContext) -> fin.Processed:
        raise Crash

    with pytest.raises(Crash):
        fin.finalize_agent(
            world.root, run_id, agent_finalize(world, seams=fin.Seams(ok_check, crash))
        )
    with Store.open(world.root) as store:
        row = store.get_run(run_id)
        assert row is not None and (row.state, row.phase) == ("running", "finalize")
        assert store.lease_info(lease_name(run_id)) is None  # released by the finally
    assert (world.root / "runs" / run_id / "original.json").exists()
    assert agent_calls(world, "teardown") == []
    done = fin.finalize_agent(world.root, run_id, agent_finalize(world))
    assert done is not None and (done.state, done.teardown) == ("done", "done")
    assert len(agent_calls(world, "teardown")) == 1


def test_a_finalizer_killed_after_the_terminal_transaction_leaves_teardown_and_hook_pending(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = world.tmp / "post.json"
    cmd = f"import sys;open({str(out)!r},'w').write(sys.stdin.read())"
    run_id = parked_running(world, flag_hooks=(f"post-run={cmd}",))

    def die(self: Any, *args: Any, **kw: Any) -> None:
        raise Crash

    from review_run.ending import Ender

    monkeypatch.setattr(Ender, "finish_teardown", die)
    with pytest.raises(Crash):
        fin.finalize_agent(world.root, run_id, agent_finalize(world))
    with Store.open(world.root) as store:
        row = store.get_run(run_id)
        assert row is not None and (row.state, row.teardown) == ("done", "pending")
        events = store.pending_events(run_id)
        assert [e["event_id"] for e in events] == [f"{run_id}-post-run-1"]
        assert (
            events[0]["audit"][0]["action"] == "finalize"
            or events[0]["audit"][0]["action"] == "run"
        )
        # a later pass (reconcile, #29) delivers the event; the terminal state is never redone
        from review_run.audit import AuditWriter, LogWriter

        hooks.deliver_pending(
            store, AuditWriter(world.root), LogWriter(world.root), None,
            run_id=run_id, shell=(PY, "-c"),
        )  # fmt: skip
        assert store.pending_events(run_id) == []
    assert json.loads(out.read_text())["state"] == "done"
    again = fin.finalize_agent(world.root, run_id, agent_finalize(world))
    assert again is not None and again.state == "done" and again.teardown == "pending"


def test_terminal_runs_never_change_again(world: World) -> None:
    run = world.run(seams=SEAMS)
    with Store.open(world.root) as store:
        with pytest.raises(ValueError):
            store.update_fields(run.run_id, Expect("done", None, 1), summary={})


# -- result parsing ----------------------------------------------------------------------------------


def test_the_report_of_a_wait_result_is_parsed_into_its_closed_shape() -> None:
    doc = {
        "agent": {
            "agent_id": "ag-aaaaaaaa",
            "csession_id": "cs-aaaaaaaa",
            "status": "parked",
            "run": 1,
        },
        "phase": "session",
        "outcome": "done",
        "end_reason": "normal",
        "exit_code": 0,
        "run": {
            "run": 1,
            "report": {
                "status": "done",
                "implicit": False,
                "summary": MARKER,
                "reason": MARKER,
                "artifact": {
                    "name": "review.json",
                    "path": "/a/review.json",
                    "sha256": "a" * 64,
                    "size": 3,
                },
                "reported_at": "2026-01-01T00:00:00Z",
            },
        },
    }
    result = lc.parse_result(0, doc)
    assert result is not None and result.report is not None
    assert result.report.status == "done" and result.report.implicit is False
    assert result.report.artifact == lc.ArtifactRef("review.json", "/a/review.json", "a" * 64, 3)
    assert MARKER not in repr(result)


@pytest.mark.parametrize(
    "artifact",
    [
        {"name": "review.json", "path": "/x/review.json", "sha256": "ZZ", "size": 1},
        {"name": "review.json", "path": "/x/review.json", "sha256": "0" * 64, "size": -1},
        {"name": "review.json", "path": "/x/review.json", "sha256": "0" * 64, "size": True},
        {"name": "review.json", "path": "/x/re\x00view.json", "sha256": "0" * 64, "size": 1},
        {"name": "review.json", "path": "/" + "a" * 5000, "sha256": "0" * 64, "size": 1},
        {"name": "", "path": "/x/review.json", "sha256": "0" * 64, "size": 1},
        {"name": "review.json", "path": "/x/review.json", "sha256": "0" * 64},
    ],
)
def test_an_artifact_object_that_is_not_of_the_closed_shape_is_no_artifact(
    artifact: dict[str, Any],
) -> None:
    report = lc.parse_report({"status": "done", "implicit": False, "artifact": artifact})
    assert report is not None and report.artifact is None


@pytest.mark.parametrize(
    "report", [None, 3, {"status": "done"}, {"status": "x", "implicit": False}]
)
def test_a_report_that_is_not_of_the_closed_shape_counts_as_no_report(report: Any) -> None:
    assert lc.parse_report(report) is None


def test_two_finalizers_racing_finish_the_run_once(world: World) -> None:
    import threading

    run_id = parked_running(world)
    results: list[Any] = []
    gate = threading.Barrier(2)

    def go(by: str) -> None:
        gate.wait()
        results.append(
            fin.finalize_agent(world.root, run_id, agent_finalize(world, finalized_by=by))
        )

    threads = [threading.Thread(target=go, args=(by,)) for by in ("show", "wait")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert len(results) == 2 and all(r is not None for r in results)
    assert world.only_run().state == "done"
    assert len(agent_calls(world, "teardown")) == 1
    assert [r["action"] for r in audit_records(world.root)].count("finalize") == 1
    with Store.open(world.root) as store:
        assert store.pending_events() == []
