"""Meta derivation (issue #21): end head, coverage, code run, bytes of review.json."""

# ruff: noqa: E501

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from review_run import artifact as art
from review_run import finalize as fin
from review_run import meta, runs
from review_run.errors import ReviewRunError
from review_run.manifest import Manifest
from review_run.prefetch import LowerFailure
from tests.conftest import validate_against
from tests.run_helpers import World

HEAD = "a" * 40
NEW = "c" * 40
PR = "github.com/o/r#1"


def entry(n: int, disposition: str, **kw: Any) -> dict[str, Any]:
    return {
        "file": f"f{n:04d}", "path": f"p{n}.py", "status": "modified", "old_path": None,
        "additions": 1, "deletions": 0, "bytes": 10, "inputs": [], "disposition": disposition, **kw,
    }  # fmt: skip


def manifest_of(*dispositions: str) -> Manifest:
    return Manifest.from_json([entry(i + 1, d) for i, d in enumerate(dispositions)])


# -- coverage ---------------------------------------------------------------------------------------


def test_full_coverage_when_every_provided_entry_is_reviewed() -> None:
    m = manifest_of("provided", "provided", "binary", "empty")
    assert meta.coverage_of(m, ["f0002", "f0001"]) == {
        "status": "full", "reviewed": ["f0001", "f0002"], "skipped": [], "ignored_ids": 0,
    }  # fmt: skip


def test_unlisted_provided_entry_is_not_reviewed_and_makes_it_partial() -> None:
    got = meta.coverage_of(manifest_of("provided", "provided"), ["f0002"])
    assert got["status"] == "partial" and got["reviewed"] == ["f0002"]
    assert got["skipped"] == [{"file": "f0001", "reason": "not_reviewed"}]


def test_truncated_is_always_skipped_even_when_the_session_lists_it() -> None:
    got = meta.coverage_of(manifest_of("truncated", "provided"), ["f0001", "f0002"])
    assert got["skipped"] == [{"file": "f0001", "reason": "truncated"}]
    assert got["reviewed"] == ["f0002"] and got["ignored_ids"] == 1


def test_size_and_file_caps_are_skipped_with_their_reasons_in_manifest_order() -> None:
    m = manifest_of("skipped_file_cap", "provided", "skipped_size_cap")
    got = meta.coverage_of(m, ["f0002", "f0003"])
    assert got["skipped"] == [
        {"file": "f0001", "reason": "file_cap"},
        {"file": "f0003", "reason": "size_cap"},
    ]
    assert got["ignored_ids"] == 1 and got["status"] == "partial"


def test_ids_that_are_not_provided_are_ignored_and_never_reviewed() -> None:
    m = manifest_of("provided", "binary", "empty")
    got = meta.coverage_of(m, ["f0001", "f0002", "f0003", "f9999"])
    assert got["reviewed"] == ["f0001"] and got["ignored_ids"] == 3 and got["skipped"] == []


def test_an_empty_manifest_is_full_with_nothing_reviewed() -> None:
    assert meta.coverage_of(manifest_of(), []) == {
        "status": "full", "reviewed": [], "skipped": [], "ignored_ids": 0,
    }  # fmt: skip


def test_coverage_never_exceeds_the_manifest_for_a_flood_of_claims() -> None:
    m = manifest_of("provided")
    got = meta.coverage_of(m, [f"f{i:04d}" for i in range(1, 5000)])
    assert got["reviewed"] == ["f0001"] and got["ignored_ids"] == 4998


# -- severity, claims, code run ------------------------------------------------------------------------


def test_severity_counts_include_unspecified_and_unknown() -> None:
    got = meta.severity_counts(
        [
            {"severity": "blocker"},
            {"severity": "nit"},
            {},
            {"severity": "weird"},
            {"severity": None},
        ]
    )
    assert got == {"blocker": 1, "major": 0, "minor": 0, "nit": 1, "info": 0, "unspecified": 3}


def claims(**kw: Any) -> art.Claims:
    base: dict[str, Any] = {
        "verdict": "comment", "risk": "low", "risk_reasons": ("r",), "summary": "ok", "reviewed": ("f0001",),
    }  # fmt: skip
    return art.Claims(**{**base, **kw})


@pytest.mark.parametrize(
    "bad",
    [
        {"verdict": "merge"}, {"risk": "x"}, {"risk_reasons": ("a",) * 11},
        {"risk_reasons": ("",)}, {"risk_reasons": ("a" * 201,)}, {"risk_reasons": ("a\nb",)},
        {"risk_reasons": ("a‮b",)}, {"summary": ""}, {"summary": "x" * 2001},
        {"summary": "a‮"}, {"reviewed": ("f1",)}, {"reviewed": ("f0001", "f0001")},
        {"reviewed": ("f0001", "f0002", "f0003")},
    ],
)  # fmt: skip
def test_claims_are_asserted_again(bad: dict[str, Any]) -> None:
    with pytest.raises(ReviewRunError) as exc:
        meta.assert_claims(claims(**bad), 2)
    assert "‮" not in str(exc.value)


def test_good_claims_pass_and_summary_may_hold_lf() -> None:
    meta.assert_claims(claims(summary="a\nb"), 2)


def code_run(**kw: Any) -> dict[str, Any]:
    base = meta._not_configured()
    return {**base, **kw}


OK_RUN = {
    "status": "ok",
    "exit_code": 0,
    "bytes": 5,
    "duration_ms": 3,
    "sandbox": "required",
    "output_sha256": "d" * 64,
}


@pytest.mark.parametrize(
    "doc",
    [
        code_run(),
        code_run(status="skipped", reason="wtctl_missing"),
        code_run(**OK_RUN),
        code_run(**{**OK_RUN, "status": "failed", "exit_code": 1}),
        code_run(**{**OK_RUN, "status": "failed", "exit_code": None, "signal": "SIGKILL"}),
        code_run(**{**OK_RUN, "status": "failed", "exit_code": None, "timed_out": True, "truncated": True}),
    ],
)  # fmt: skip
def test_good_code_run_objects_pass_the_schema(doc: dict[str, Any]) -> None:
    assert meta.code_run_of({**doc, "extra": 1}) == doc
    schema = json.loads((Path(__file__).parent.parent / "schemas" / "defs.json").read_text())
    from jsonschema import Draft202012Validator

    Draft202012Validator({**schema["$defs"]["code_run"], "$defs": schema["$defs"]}).validate(doc)


@pytest.mark.parametrize(
    "doc",
    [
        code_run(status="skipped"),
        code_run(status="skipped", reason="nope"),
        code_run(reason="wtctl_missing"),
        code_run(status="ok"),
        code_run(**{**OK_RUN, "exit_code": 1}),
        code_run(**{**OK_RUN, "status": "failed"}),
        code_run(**{**OK_RUN, "sandbox": "preferred_fallback"}),
        code_run(**{**OK_RUN, "output_sha256": "z"}),
        code_run(**{**OK_RUN, "signal": "sigkill", "status": "failed", "exit_code": None}),
        code_run(**{**OK_RUN, "bytes": True}),
        code_run(timed_out=True),
        code_run(exit_code=0),
        {"status": "ok"},
        [],
    ],
)  # fmt: skip
def test_bad_code_run_objects_are_internal(doc: Any) -> None:
    with pytest.raises(ReviewRunError):
        meta.code_run_of(doc)


def test_code_run_constants_match_the_schema() -> None:
    schema = json.loads((Path(__file__).parent.parent / "schemas" / "defs.json").read_text())
    props = schema["$defs"]["code_run"]["properties"]
    assert list(props) == list(meta.CODE_RUN_KEYS)
    assert [r for r in props["reason"]["enum"] if r] == list(meta.CODE_RUN_REASONS)
    assert schema["$defs"]["code_run_status"]["enum"] == list(meta.CODE_RUN_STATUSES)


# -- end head ----------------------------------------------------------------------------------------


def facts(**kw: Any) -> SimpleNamespace:
    return SimpleNamespace(**{"pr": PR, "head_sha": HEAD, **kw})


def patch_show(monkeypatch: pytest.MonkeyPatch, result: Any) -> None:
    def show(pr: str) -> Any:
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(meta, "show_facts", show)


def test_end_head_unmoved(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_show(monkeypatch, facts())
    assert meta.end_head(PR, HEAD) == (HEAD, False)


def test_end_head_moved(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_show(monkeypatch, facts(head_sha=NEW))
    assert meta.end_head(PR, HEAD) == (NEW, True)


@pytest.mark.parametrize(
    "code", ["not_found", "rate_limited", "timeout", "invalid_output", "failed"]
)
def test_end_head_failure_is_unreadable(monkeypatch: pytest.MonkeyPatch, code: str) -> None:
    patch_show(monkeypatch, LowerFailure("forgectl", 12, code))
    assert meta.end_head(PR, HEAD) == (None, None)


def test_end_head_for_another_pr_is_unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_show(monkeypatch, facts(pr="github.com/o/r#2"))
    assert meta.end_head(PR, HEAD) == (None, None)


# -- derive, with the real pipeline ------------------------------------------------------------------------

MARKER = "UNTRUSTED-MARKER-31bd"


def ok_check(ctx: fin.SessionContext) -> fin.SessionCheck:
    return fin.SessionCheck(skill_checked=True, tools_checked=True, tools_unexpected=2)


def artifact_text(**kw: Any) -> str:
    doc: dict[str, Any] = {
        "event": "comment",
        "body": "",
        "findings": [
            {"id": "F1", "path": "a.py", "line": 3, "body": "text", "severity": "major"},
            {"id": "F2", "path": "a.py", "line": 4, "body": "text"},
        ],
        "meta": {
            "verdict": "comment", "risk": "medium", "risk_reasons": ["why"],
            "summary": "Summary é中", "coverage": {"reviewed": ["f0001", "f0999"]},
        },
    }  # fmt: skip
    doc.update(kw)
    return json.dumps(doc)


def run_derive(world: World, text: str) -> Any:
    world.fb.use("agent-spawn", "outcome", outcome="done", artifact_text=text)
    return world.run(seams=fin.Seams(ok_check, derive=meta.derive))


def stored(world: World, run: Any) -> dict[str, Any]:
    return json.loads(Path(run.artifact_path).read_bytes())


def test_a_run_ends_done_with_derived_meta_and_canonical_bytes(world: World) -> None:
    run = run_derive(world, artifact_text())
    assert (run.state, run.reason) == ("done", None)
    data = Path(run.artifact_path).read_bytes()
    doc = json.loads(data)
    m = doc["meta"]["review_run"]
    assert list(doc["meta"]) == ["review_run"]
    validate_against("meta.json", m)
    assert m["run_id"] == run.run_id and m["skill"] == run.skill and m["mode"] == run.mode
    assert m["session"]["tools_checked"] is True and m["session"]["tools_unexpected"] == 2
    assert m["skill_checked"] is True and m["skill_version"] == "0.0.0"
    assert m["head_at_end"] == run.anchor["head_sha"] and m["head_moved"] is False
    assert m["severity_counts"] == {
        "blocker": 0,
        "major": 1,
        "minor": 0,
        "nit": 0,
        "info": 0,
        "unspecified": 1,
    }
    assert m["code_run"]["status"] == "not_configured" and m["previous_run"] is None
    assert m["summary"] == "Summary é中" and data.isascii() and data.endswith(b"}\n")
    assert data == (art.canonical_json(doc) + "\n").encode()  # canonical: sorted, compact, ASCII
    assert run.summary is not None and run.summary["coverage_status"] == m["coverage"]["status"]
    assert run.summary["head_moved"] is False and "head_unreadable" not in run.warnings
    assert runs.summary_of(run.summary) == run.summary


def test_coverage_in_the_stored_review_is_from_the_manifest(world: World) -> None:
    run = run_derive(world, artifact_text())
    cov = stored(world, run)["meta"]["review_run"]["coverage"]
    manifest = json.loads((world.root / "runs" / run.run_id / "manifest.json").read_text())
    provided = {e["file"] for e in manifest if e["disposition"] == "provided"}
    assert set(cov["reviewed"]) <= provided and cov["ignored_ids"] >= 1
    assert cov["status"] == ("partial" if cov["skipped"] else "full")


def test_head_moved_at_the_end_is_recorded_and_the_run_stays_done(world: World) -> None:
    # the pre-fetch reads show once; every later read reports a moved head
    world.fb.script("forgectl", "show", {}, {"merge": {"head": {"sha": NEW}}})
    run = run_derive(world, artifact_text())
    assert len(world.fb.calls("forgectl", "show")) == 2
    assert (run.state, run.reason) == ("done", None)
    m = stored(world, run)["meta"]["review_run"]
    assert m["head_at_end"] == NEW and m["head_moved"] is True
    assert run.summary is not None and run.summary["head_moved"] is True


def test_an_unreadable_end_head_is_null_with_a_warning(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_show(monkeypatch, LowerFailure("forgectl", 12, "failed"))
    run = run_derive(world, artifact_text())
    assert run.state == "done" and "head_unreadable" in run.warnings
    m = stored(world, run)["meta"]["review_run"]
    assert (m["head_at_end"], m["head_moved"]) == (None, None)
    assert run.summary is not None and run.summary["head_moved"] is None


def test_hostile_claims_never_leave_their_bounds_or_reach_the_failure(world: World) -> None:
    text = artifact_text(
        meta={
            "verdict": "comment", "risk": "low", "risk_reasons": [MARKER + "\n"],
            "summary": "x", "coverage": {"reviewed": []},
        }
    )  # fmt: skip
    run = run_derive(world, text)
    assert run.state == "failed" and run.reason == "artifact_invalid"
    assert MARKER not in json.dumps(runs.run_object(run))


def test_a_missing_manifest_is_internal_not_a_coverage_claim(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        meta, "load_manifest", lambda d: (_ for _ in ()).throw(ReviewRunError("internal", "x"))
    )
    run = run_derive(world, artifact_text())
    assert (run.state, run.reason) == ("failed", "internal")


def test_final_bytes_above_the_artifact_cap_end_artifact_invalid(world: World) -> None:
    world.config_text += "limits:\n  max_artifact_bytes: 1500\n"
    text = artifact_text(body="é" * 400)  # 800 bytes raw, 2400 with escapes
    run = run_derive(world, text)
    assert (run.state, run.reason) == ("failed", "artifact_invalid")
    assert runs.run_object(run)["failure"]["errors"] == [{"pointer": "", "kind": "length"}]
    assert not (world.root / "runs" / run.run_id / "review.json").exists()


def test_the_same_inputs_give_the_same_bytes(world: World) -> None:
    run = run_derive(world, artifact_text())
    first = Path(run.artifact_path).read_bytes()
    ctx_doc = json.loads(first)
    assert art.canonical_json(copy.deepcopy(ctx_doc)).encode() + b"\n" == first


def test_production_seams_hold_derive_and_stay_incomplete_until_22() -> None:
    seams = meta.production_seams()
    assert seams.derive is meta.derive and not seams.complete
