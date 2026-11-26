"""Meta derivation (issue #21): end head, coverage, code run, bytes of review.json."""

# ruff: noqa: E501

from __future__ import annotations

import copy
import json
import os
import re
import time
from collections.abc import Callable
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


def test_production_seams_are_complete() -> None:
    from review_run import session_check

    seams = meta.production_seams()
    assert seams.derive is meta.derive and seams.check_session is session_check.check_session
    assert seams.complete


# -- round 1 review fixes (R1-R15) ---------------------------------------------------------------------

SCHEMAS = Path(__file__).parent.parent / "schemas"


def private_file(path: Path, data: bytes | str, mode: int = 0o600) -> None:
    raw = data.encode() if isinstance(data, str) else data
    path.write_bytes(raw)
    path.chmod(mode)


def run_with(world: World, mutate: Callable[[Path], None], text: str | None = None) -> Any:
    """A derive run whose run directory is changed after the session ends, before derive."""

    def check(ctx: fin.SessionContext) -> fin.SessionCheck:
        mutate(ctx.run_dir)
        return ok_check(ctx)

    world.fb.use("agent-spawn", "outcome", outcome="done", artifact_text=text or artifact_text())
    return world.run(seams=fin.Seams(check, derive=meta.derive))


def test_a_deleted_manifest_ends_internal(world: World) -> None:  # R1
    run = run_with(world, lambda d: (d / "manifest.json").unlink())
    assert (run.state, run.reason) == ("failed", "internal")


def test_a_malformed_pr_json_ends_internal(world: World) -> None:  # R1
    run = run_with(world, lambda d: private_file(d / "pr.json", "[]"))
    assert (run.state, run.reason) == ("failed", "internal")


def test_a_missing_pr_json_ends_internal(world: World) -> None:  # R1
    run = run_with(world, lambda d: (d / "pr.json").unlink())
    assert (run.state, run.reason) == ("failed", "internal")


# R2: read_run_file hardening


def test_read_run_file_missing_is_none(tmp_path: Path) -> None:
    assert meta.read_run_file(tmp_path, "x.json", 10) is None


def test_read_run_file_reads_up_to_the_limit(tmp_path: Path) -> None:
    private_file(tmp_path / "x.json", b"a" * 10)
    assert meta.read_run_file(tmp_path, "x.json", 10) == b"a" * 10


def test_read_run_file_over_the_limit_is_internal(tmp_path: Path) -> None:
    private_file(tmp_path / "x.json", b"a" * 11)
    with pytest.raises(ReviewRunError) as exc:
        meta.read_run_file(tmp_path, "x.json", 10)
    assert exc.value.code == "internal"


def test_read_run_file_refuses_a_symlink(tmp_path: Path) -> None:
    private_file(tmp_path / "real", b"{}")
    (tmp_path / "x.json").symlink_to(tmp_path / "real")
    with pytest.raises(ReviewRunError):
        meta.read_run_file(tmp_path, "x.json", 10)


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o660])
def test_read_run_file_refuses_group_or_other_access(tmp_path: Path, mode: int) -> None:
    private_file(tmp_path / "x.json", b"{}", mode)
    with pytest.raises(ReviewRunError):
        meta.read_run_file(tmp_path, "x.json", 10)


def test_read_run_file_refuses_a_fifo_without_hanging(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "x.json", 0o600)
    start = time.monotonic()
    with pytest.raises(ReviewRunError):
        meta.read_run_file(tmp_path, "x.json", 10)
    assert time.monotonic() - start < 5


def test_read_run_file_refuses_a_directory(tmp_path: Path) -> None:
    (tmp_path / "x.json").mkdir(mode=0o700)
    with pytest.raises(ReviewRunError):
        meta.read_run_file(tmp_path, "x.json", 10)


# R3, R11: code-run.json


def good_code_run_file(d: Path) -> None:
    private_file(d / "code-run.json", json.dumps(code_run(**OK_RUN)))


def test_a_present_code_run_file_is_in_the_meta(world: World) -> None:  # R3
    run = run_with(world, good_code_run_file)
    assert run.state == "done"
    m = stored(world, run)["meta"]["review_run"]
    assert m["code_run"] == code_run(**OK_RUN)
    assert run.summary is not None and run.summary["code_run_status"] == "ok"


@pytest.mark.parametrize(
    "content",
    ["not json", "[]", json.dumps(code_run(status="weird")), json.dumps(code_run(**{**OK_RUN, "bytes": None}))],
)  # fmt: skip
def test_a_bad_code_run_file_ends_internal(world: World, content: str) -> None:  # R3
    run = run_with(world, lambda d: private_file(d / "code-run.json", content))
    assert (run.state, run.reason) == ("failed", "internal")


RUN_PR_CODE = "repos:\n  github.com/o/r:\n    run_pr_code:\n      command: [x]\n"


def test_a_missing_code_run_file_is_not_configured_when_unset(world: World) -> None:  # R11
    run = run_with(world, lambda d: None)
    assert run.state == "done"
    assert stored(world, run)["meta"]["review_run"]["code_run"]["status"] == "not_configured"


def test_a_missing_code_run_file_is_internal_when_the_run_configured_it(
    world: World,
) -> None:  # R11
    world.config_text += RUN_PR_CODE
    run = run_with(world, lambda d: (d / "code-run.json").unlink(missing_ok=True))
    assert (run.state, run.reason) == ("failed", "internal")


def test_a_code_run_file_with_run_pr_code_configured_is_used(world: World) -> None:  # R11
    world.config_text += RUN_PR_CODE
    run = run_with(world, good_code_run_file)
    assert run.state == "done"
    assert stored(world, run)["meta"]["review_run"]["code_run"]["status"] == "ok"


# R3, R8, R14: skill_version


def test_skill_version_is_for_the_default_skill_only() -> None:
    assert meta.skill_version("review-run:review-pr") == "0.0.0"
    for other in ("review-run:other", "review-run:", "review-run", "other:review-pr", ""):
        assert meta.skill_version(other) is None


# R4: determinism


def test_derive_twice_on_the_same_inputs_gives_the_same_bytes(world: World) -> None:
    seen: list[fin.Processed] = []

    def twice(ctx: fin.ArtifactContext, validated: art.Validated) -> fin.Processed:
        first, second = meta.derive(ctx, validated), meta.derive(ctx, validated)
        seen.extend([first, second])
        return first

    world.fb.use("agent-spawn", "outcome", outcome="done", artifact_text=artifact_text())
    run = world.run(seams=fin.Seams(ok_check, derive=twice))
    assert run.state == "done" and len(seen) == 2
    assert seen[0].review == seen[1].review and seen[0].summary == seen[1].summary


# R5, R7: code_run_of against the schema

CODE_RUN_VALUES: dict[str, list[Any]] = {
    "status": ["ok", "failed", "skipped", "not_configured"],
    "reason": [None, "wtctl_missing"],
    "exit_code": [None, 0, 1],
    "signal": [None, "SIGKILL"],
    "timed_out": [False, True],
    "bytes": [None, 0, 5],
    "duration_ms": [None, 3],
    "sandbox": [None, "required"],
    "output_sha256": [None, "d" * 64],
}


def schema_validator() -> Any:
    from jsonschema import Draft202012Validator

    from tests.conftest import _registry

    schema = json.loads((SCHEMAS / "defs.json").read_text())
    return Draft202012Validator(
        {**schema["$defs"]["code_run"], "$defs": schema["$defs"]}, registry=_registry()
    )


def test_code_run_of_accepts_exactly_what_the_schema_accepts() -> None:  # R5, R7
    import itertools

    validator = schema_validator()
    keys = list(CODE_RUN_VALUES)
    checked = 0
    for combo in itertools.product(*(CODE_RUN_VALUES[k] for k in keys)):
        doc = {**dict(zip(keys, combo, strict=True)), "truncated": False}
        valid = validator.is_valid(doc)
        try:
            meta.code_run_of(doc)
            accepted = True
        except ReviewRunError:
            accepted = False
        # meta is stricter than the schema only for the status cases the schema leaves open
        # (ok needs exit 0 and no signal, failed needs a failure); never looser.
        assert not accepted or valid, doc
        checked += 1
    assert checked > 1000


@pytest.mark.parametrize("status", ["skipped", "not_configured"])
@pytest.mark.parametrize("key", ["bytes", "duration_ms"])
def test_a_code_run_that_did_not_run_has_null_counts(status: str, key: str) -> None:  # R5
    doc = code_run(status=status, reason="wtctl_missing" if status == "skipped" else None)
    meta.code_run_of(doc)
    with pytest.raises(ReviewRunError):
        meta.code_run_of({**doc, key: 5})


CODE_RUN_VARIANTS = [
    code_run(),
    code_run(status="skipped", reason="sandbox_failed"),
    code_run(**OK_RUN),
    code_run(**{**OK_RUN, "status": "failed", "exit_code": 2, "truncated": True}),
]


@pytest.mark.parametrize("variant", CODE_RUN_VARIANTS)
@pytest.mark.parametrize("partial", [False, True])
@pytest.mark.parametrize("claim_text", ["plain", "é中 \U0001f600 \ue000"])
def test_build_meta_validates_against_the_schema(
    world: World, variant: dict[str, Any], partial: bool, claim_text: str
) -> None:  # R7
    captured: list[Any] = []

    def spy(ctx: fin.ArtifactContext, validated: art.Validated) -> fin.Processed:
        captured.append((ctx, validated))
        return meta.derive(ctx, validated)

    text = artifact_text(
        meta={
            "verdict": "comment", "risk": "low", "risk_reasons": [claim_text],
            "summary": claim_text, "coverage": {"reviewed": [] if partial else ["f0001"]},
        }
    )  # fmt: skip
    world.fb.use("agent-spawn", "outcome", outcome="done", artifact_text=text)
    run = world.run(seams=fin.Seams(ok_check, derive=spy))
    assert run.state == "done"
    ctx, validated = captured[0]
    manifest = meta.load_manifest(ctx.run_dir)
    m = meta.build_meta(
        ctx, validated, manifest=manifest, pr_facts=meta.load_pr_facts(ctx.run_dir),
        code_run=meta.code_run_of(variant), head_at_end=None, head_moved=None,
    )  # fmt: skip
    validate_against("meta.json", m)


# R6: the claim filter is no looser than the schema patterns


def test_forbidden_in_claim_is_no_looser_than_the_schema_patterns() -> None:
    schema = json.loads((SCHEMAS / "meta.json").read_text())
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if isinstance(node.get("pattern"), str) and "\\u2060-\\u206f" in node["pattern"]:
                found.append(node["pattern"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(schema)
    strict = [re.compile(p) for p in found if "\\u001f" in p]
    lenient = [re.compile(p) for p in found if "\\u0008" in p]
    assert strict and lenient
    # every assigned block of the BMP and the planes below 0x30000, the tags and the private planes
    for cp in (*range(0x30000), *range(0xE0000, 0xE1000), 0xF0000, 0x10FFFF):
        ch = chr(cp)
        if not art.forbidden_in_claim(ch, allow_lf=False):
            assert all(p.fullmatch(ch) for p in strict), hex(cp)
        if not art.forbidden_in_claim(ch, allow_lf=True):
            assert all(p.fullmatch(ch) for p in lenient), hex(cp)


# R12: normalized_count


def test_normalized_count_is_the_sum_of_the_entry_counts_the_truncated_one_included() -> None:
    m = {
        "verdict": "comment", "risk": "low", "coverage": {"status": "full", "skipped": []},
        "code_run": {"status": "not_configured", "reason": None}, "head_moved": False,
        "severity_counts": {},
        "normalized": [
            {"rule": "drop_null", "pointer": "/findings/0", "count": 2},
            {"rule": "truncated", "pointer": "", "count": 3},
        ],
    }  # fmt: skip
    assert meta.summary_of(m)["normalized_count"] == 5


# R13: tools_unexpected


@pytest.mark.parametrize("bad", [-1, True, 1.5, "2", None])
def test_session_of_refuses_a_bad_tools_unexpected_when_checked(world: World, bad: Any) -> None:
    plan = world.plan()
    session = fin.SessionCheck(skill_checked=True, tools_checked=True, tools_unexpected=bad)
    ctx = SimpleNamespace(session=session)
    with pytest.raises(ReviewRunError):
        meta.session_of(plan.settings, ctx)  # type: ignore[arg-type]


def test_session_of_counts_zero_when_the_tools_were_not_checked(world: World) -> None:
    plan = world.plan()
    session = fin.SessionCheck(skill_checked=True, tools_checked=False, tools_unexpected=None)
    got = meta.session_of(plan.settings, SimpleNamespace(session=session))  # type: ignore[arg-type]
    assert got["tools_unexpected"] == 0 and got["tools_checked"] is False


# R15: the manifest read cap covers the writer's worst case


def test_manifest_limit_covers_the_writers_worst_case() -> None:
    from review_run.manifest import MAX_FILES_IN_PR

    ceiling = 8 << 20  # the default hard_ceiling_bytes
    # a path appears twice in its diff header, so its JSON-escaped bytes (6 per BMP control or
    # non-ASCII character, 12 per astral one) cost at most 3 per byte of the diff
    worst = 3 * ceiling + MAX_FILES_IN_PR * 1024
    assert meta.manifest_limit(ceiling) >= worst
    assert meta.manifest_limit(ceiling) >= meta.MAX_FILE_BYTES
    big = 100 << 20
    assert meta.manifest_limit(big) >= 3 * big


def test_a_manifest_at_the_limit_is_read_and_one_byte_more_is_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = json.dumps([entry(1, "provided")])
    padded = body + " " * 100
    monkeypatch.setattr(meta, "manifest_limit", lambda ceiling: len(padded))
    private_file(tmp_path / "manifest.json", padded)
    assert len(meta.load_manifest(tmp_path, 1).entries) == 1
    private_file(tmp_path / "manifest.json", padded + " ")
    with pytest.raises(ReviewRunError):
        meta.load_manifest(tmp_path, 1)
