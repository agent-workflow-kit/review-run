"""Artifact validation (issue #20): strict parse, session claims, identity, normalization and
``pr-comment draft check`` (finalize steps 6 to 10), then the pipeline with the real finalize."""

# ruff: noqa: E501

from __future__ import annotations

import copy
import json
import random
import time
import unicodedata
from pathlib import Path
from typing import Any

import pytest

from review_run import artifact as art
from review_run import finalize as fin
from review_run import runs
from review_run.sanitizer import sanitize
from tests.conftest import validate_against
from tests.fakes import FakeBin
from tests.run_helpers import World

MARKER = "UNTRUSTED-MARKER-7e3a"
ANCHOR = {"head_sha": "a" * 40, "base_ref": "main", "base_sha": "b" * 40, "merge_base_sha": None}
IDENTITY = art.Identity("01" + "A" * 24, "github.com/o/r#1", ANCHOR)
META: dict[str, Any] = {
    "verdict": "comment",
    "risk": "low",
    "risk_reasons": ["small change"],
    "summary": "Looks fine.\nOne nit.",
    "coverage": {"reviewed": ["f0001", "f0002"]},
}
FINDING: dict[str, Any] = {"id": "F1", "path": "a.py", "line": 3, "body": "text"}


def draft(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "event": "comment",
        "body": "",
        "findings": [],
        "meta": copy.deepcopy(META),
    }
    base.update(kw)
    return base


def raw(doc: object) -> bytes:
    return json.dumps(doc).encode()


def kinds(exc: pytest.ExceptionInfo[art.Invalid]) -> list[tuple[str, str]]:
    return [(e["pointer"], e["kind"]) for e in exc.value.errors]


def parse_kind(data: bytes, max_bytes: int = 1 << 23) -> str:
    with pytest.raises(art.Invalid) as exc:
        art.parse_artifact(data, max_bytes=max_bytes)
    assert len(exc.value.errors) == 1 and exc.value.errors[0]["pointer"] == ""
    return exc.value.errors[0]["kind"]


# -- step 6: strict parse --------------------------------------------------------------------------


def test_a_plain_object_parses_and_surrounding_whitespace_is_allowed() -> None:
    assert art.parse_artifact(b' \n{"a": [1, 2.5, "x", null, true]}\n ', max_bytes=100) == {
        "a": [1, 2.5, "x", None, True]
    }


@pytest.mark.parametrize(
    ("data", "kind"),
    [
        (b"\xef\xbb\xbf{}", "encoding"),
        ("{}".encode("utf-16"), "encoding"),
        (b"\xff\xfe{\x00}\x00", "encoding"),
        ('{\x00"\x00a\x00"\x00:\x001\x00}\x00'.encode("ascii"), "syntax"),
        (b'{"a": "\xff"}', "encoding"),
        (b'{"a": "\xed\xa0\x80"}', "encoding"),  # an encoded surrogate
        (b'{"a": 1, "a": 2}', "duplicate_key"),
        (b'{"a": 1, "\\u0061": 2}', "duplicate_key"),
        (b'{"x": {"a": 1, "a": 1}}', "duplicate_key"),
        (b'{"a": NaN}', "non_finite"),
        (b'{"a": Infinity}', "non_finite"),
        (b'{"a": -Infinity}', "non_finite"),
        (b'{"a": 1e999}', "non_finite"),
        (b'{"a": -1e999}', "non_finite"),
        (b'{"a": ' + b"9" * 5000 + b"}", "number"),
        (b'{"a": 0.' + b"1" * 5000 + b"}", "number"),
        (b'{"a": 1}{"b": 2}', "syntax"),
        (b'{"a": 1} x', "syntax"),
        (b"", "syntax"),
        (b"{", "syntax"),
        (b'{"a": 1,}', "syntax"),
        (b"{'a': 1}", "syntax"),
        (b'{"a": "\\ud800"}', "surrogate"),
        (b'{"\\udc00": 1}', "surrogate"),
        (b'{"a": ["\\ud83d"]}', "surrogate"),
        (b"[1, 2]", "type"),
        (b'"x"', "type"),
        (b"null", "type"),
    ],
)
def test_each_parse_failure_is_artifact_invalid_with_its_kind(data: bytes, kind: str) -> None:
    assert parse_kind(data) == kind


def test_a_valid_surrogate_pair_escape_is_accepted() -> None:
    assert art.parse_artifact(b'{"a": "\\ud83d\\ude00"}', max_bytes=100) == {"a": "\U0001f600"}


def test_the_size_cap_is_checked_first() -> None:
    assert art.parse_artifact(b'{"a":1}', max_bytes=7) == {"a": 1}
    assert parse_kind(b'{"a":1}', max_bytes=6) == "length"
    assert parse_kind(b"\xef\xbb\xbf" + b" " * 10, max_bytes=5) == "length"


def test_nesting_is_at_most_32_levels() -> None:
    def nested(levels: int) -> bytes:
        return b'{"a":' * (levels - 1) + b"[]" + b"}" * (levels - 1)

    assert len(art.parse_artifact(nested(32), max_bytes=10_000)) == 1
    assert parse_kind(nested(33), 10_000) == "depth"
    assert parse_kind(b"[" * 40 + b"]" * 40) == "type"  # top level array: not an object


def test_extreme_nesting_is_depth_not_a_crash_or_a_slow_parse() -> None:
    started = time.monotonic()
    assert parse_kind(b'{"a":' + b"[" * 3_000_000) == "depth"
    assert parse_kind(b'{"a":' + b"[" * 100_000 + b"]" * 100_000 + b"}") == "depth"
    assert parse_kind(b'{"a":' + b"[" * 900 + b"]" * 900 + b"}") == "depth"
    assert time.monotonic() - started < 10


def test_a_million_element_array_within_the_cap_parses_linearly() -> None:
    data = b'{"a":[' + b"0," * 999_999 + b"0]," + b'"b":[' + b"[]," * 5 + b"[]]}"
    started = time.monotonic()
    doc = art.parse_artifact(data, max_bytes=1 << 23)
    assert len(doc["a"]) == 1_000_000
    assert time.monotonic() - started < 10


def test_an_unterminated_string_flood_does_not_go_quadratic() -> None:
    started = time.monotonic()
    assert parse_kind(b'{"a": "' + b'\\"' * 2_000_000) == "syntax"
    assert time.monotonic() - started < 10


def test_a_pure_depth_error_never_echoes_artifact_text() -> None:
    with pytest.raises(art.Invalid) as exc:
        art.parse_artifact(f'{{"{MARKER}": 1, "{MARKER}": 2}}'.encode(), max_bytes=1000)
    assert MARKER not in repr(exc.value.errors) and MARKER not in str(exc.value)


# -- step 7: session claims --------------------------------------------------------------------------


def claims(meta: object, max_files: int = 9999) -> art.Claims:
    return art.extract_claims({"meta": meta}, max_files=max_files)


def claim_errors(meta: object, max_files: int = 9999) -> list[tuple[str, str]]:
    with pytest.raises(art.Invalid) as exc:
        claims(meta, max_files)
    return kinds(exc)


def with_meta(**kw: Any) -> dict[str, Any]:
    meta = copy.deepcopy(META)
    for key, value in kw.items():
        if value is ...:
            del meta[key]
        else:
            meta[key] = value
    return meta


def test_valid_claims_are_returned_as_given() -> None:
    got = claims({**META, "extra": [1], "coverage": {"reviewed": [], "other": 1}})
    assert got == art.Claims("comment", "low", ("small change",), "Looks fine.\nOne nit.", ())
    assert claims(with_meta(risk_reasons=[], verdict="request_changes", risk="high")).risk == "high"


def test_a_missing_meta_is_meta_missing_and_a_non_object_is_type() -> None:
    with pytest.raises(art.Invalid) as exc:
        art.extract_claims({}, max_files=10)
    assert kinds(exc) == [("/meta", "meta_missing")]
    assert claim_errors([1]) == [("/meta", "type")]
    assert claim_errors(None) == [("/meta", "type")]


@pytest.mark.parametrize("field", ["verdict", "risk", "risk_reasons", "summary", "coverage"])
def test_a_missing_field_is_meta_missing_at_its_pointer(field: str) -> None:
    assert claim_errors(with_meta(**{field: ...})) == [(f"/meta/{field}", "meta_missing")]


def test_a_missing_reviewed_is_meta_missing() -> None:
    assert claim_errors(with_meta(coverage={})) == [("/meta/coverage/reviewed", "meta_missing")]
    assert claim_errors(with_meta(coverage=[])) == [("/meta/coverage", "type")]


@pytest.mark.parametrize(
    ("meta", "expected"),
    [
        (with_meta(verdict="merge"), [("/meta/verdict", "enum")]),
        (with_meta(verdict="APPROVE"), [("/meta/verdict", "enum")]),
        (with_meta(verdict=1), [("/meta/verdict", "type")]),
        (with_meta(risk="critical"), [("/meta/risk", "enum")]),
        (with_meta(risk=None), [("/meta/risk", "type")]),
        (with_meta(risk_reasons="x"), [("/meta/risk_reasons", "type")]),
        (with_meta(risk_reasons=["x"] * 11), [("/meta/risk_reasons", "count")]),
        (with_meta(risk_reasons=["ok", ""]), [("/meta/risk_reasons/1", "length")]),
        (with_meta(risk_reasons=["x" * 201]), [("/meta/risk_reasons/0", "length")]),
        (with_meta(risk_reasons=[5]), [("/meta/risk_reasons/0", "type")]),
        (with_meta(risk_reasons=["a\nb"]), [("/meta/risk_reasons/0", "pattern")]),
        (with_meta(risk_reasons=["a\n"]), [("/meta/risk_reasons/0", "pattern")]),  # a trailing LF
        (with_meta(risk_reasons=["a\tb"]), [("/meta/risk_reasons/0", "pattern")]),
        (with_meta(summary=""), [("/meta/summary", "length")]),
        (with_meta(summary="x" * 2001), [("/meta/summary", "length")]),
        (with_meta(summary=3), [("/meta/summary", "type")]),
        (with_meta(summary="a\tb"), [("/meta/summary", "pattern")]),
        (with_meta(summary="a\rb"), [("/meta/summary", "pattern")]),
        (with_meta(summary="a\x00b"), [("/meta/summary", "pattern")]),
        (with_meta(coverage={"reviewed": "f0001"}), [("/meta/coverage/reviewed", "type")]),
        (
            with_meta(coverage={"reviewed": ["f0001", "f0001"]}),
            [("/meta/coverage/reviewed/1", "count")],
        ),
        (with_meta(coverage={"reviewed": ["f001"]}), [("/meta/coverage/reviewed/0", "pattern")]),
        (with_meta(coverage={"reviewed": ["f0001\n"]}), [("/meta/coverage/reviewed/0", "pattern")]),
        (with_meta(coverage={"reviewed": ["f١٢٣٤"]}), [("/meta/coverage/reviewed/0", "pattern")]),
        (with_meta(coverage={"reviewed": [1]}), [("/meta/coverage/reviewed/0", "type")]),
    ],
)
def test_each_violated_claim_rule_has_its_kind_and_pointer(
    meta: dict[str, Any], expected: list[tuple[str, str]]
) -> None:
    assert claim_errors(meta) == expected


def test_the_reviewed_cap_is_max_files() -> None:
    ids = [f"f{i:04d}" for i in range(5)]
    assert claims(with_meta(coverage={"reviewed": ids}), 5).reviewed == tuple(ids)
    assert claim_errors(with_meta(coverage={"reviewed": ids}), 4) == [
        ("/meta/coverage/reviewed", "count")
    ]


def test_a_huge_summary_is_only_measured() -> None:
    started = time.monotonic()
    assert claim_errors(with_meta(summary="x" * (8 << 20))) == [("/meta/summary", "length")]
    assert claim_errors(with_meta(risk_reasons=["y" * (1 << 20)])) == [
        ("/meta/risk_reasons/0", "length")
    ]
    assert time.monotonic() - started < 2


def test_every_violation_is_listed_up_to_20_and_no_value_is_echoed() -> None:
    meta = with_meta(
        verdict=MARKER, risk=MARKER, summary=MARKER + "‮", risk_reasons=[MARKER + "\n"] * 10,
        coverage={"reviewed": [MARKER] * 30},
    )  # fmt: skip
    with pytest.raises(art.Invalid) as exc:
        claims(meta)
    assert len(exc.value.errors) == 20
    assert MARKER not in repr(exc.value.errors)
    assert exc.value.errors[0] == {"pointer": "/meta/verdict", "kind": "enum"}


@pytest.mark.parametrize(
    ("char", "ok_in_summary", "ok_in_reason"),
    [
        ("\U000e0041", False, False),  # TAG LATIN CAPITAL LETTER A (Cf, astral)
        ("\U000e0001", False, False),  # LANGUAGE TAG
        ("\U000e007f", False, False),  # CANCEL TAG
        ("‮", False, False),  # right-to-left override
        ("️", False, False),  # variation selector 16
        ("\U000e0100", False, False),  # astral variation selector
        ("\U000e01ef", False, False),
        ("\U000f0000", True, True),  # plane 15 private use (Co): allowed, marked when printed
        ("", True, True),  # private use
        ("͸", True, True),  # unassigned (Cn)
        ("⁥", True, True),  # unassigned (Cn) inside the old schema's range
        ("\U0010ffff", True, True),  # noncharacter (Cn)
        ("\U0001f600", True, True),
        ("é", True, True),
        ("​", False, False),
        (" ", False, False),
        ("　", False, False),
        (" ", False, False),
        (" ", False, False),
        ("\u0085", False, False),
        ("ㅤ", False, False),
        ("⠀", False, False),
        ("͏", False, False),
        ("᠏", False, False),
        ("\n", True, False),
        ("\t", False, False),
        ("\x7f", False, False),
        (" ", True, True),
    ],
)
def test_claims_goldens_for_single_characters(
    char: str, ok_in_summary: bool, ok_in_reason: bool
) -> None:
    summary = f"a{char}b"
    assert (not _fails(with_meta(summary=summary))) == ok_in_summary
    assert (not _fails(with_meta(risk_reasons=[summary]))) == ok_in_reason


def _fails(meta: dict[str, Any]) -> bool:
    try:
        claims(meta)
    except art.Invalid:
        return True
    return False


def test_a_lone_surrogate_in_a_claim_is_refused_without_a_crash() -> None:
    assert claim_errors(with_meta(summary="a\ud800b")) == [("/meta/summary", "pattern")]
    assert claim_errors(with_meta(risk_reasons=["\udc00"])) == [("/meta/risk_reasons/0", "pattern")]


def _expected_bad(char: str, *, allow_lf: bool) -> bool:
    """The interface's rule, written independently of the sanitizer's code."""
    cp = ord(char)
    category = unicodedata.category(char)
    if char == "\n":
        return not allow_lf
    if category in ("Cc", "Cf", "Cs", "Zl", "Zp"):
        return True
    if category == "Zs":
        return char != " "
    if 0xFE00 <= cp <= 0xFE0F or 0xE0100 <= cp <= 0xE01EF:
        return True
    return cp in {
        0x34F,
        0x115F,
        0x1160,
        0x17B4,
        0x17B5,
        0x180B,
        0x180C,
        0x180D,
        0x180F,
        0x2800,
        0x3164,
        0xFFA0,
    }


def test_the_character_rule_over_every_code_point() -> None:
    for cp in range(0x110000):
        char = chr(cp)
        for allow_lf in (True, False):
            assert art.forbidden_in_claim(char, allow_lf=allow_lf) == _expected_bad(
                char, allow_lf=allow_lf
            ), hex(cp)
        # the claim rule is the sanitizer's marked set minus Co and Cn, plus TAB
        if unicodedata.category(char) not in ("Co", "Cn") and char not in "\t\n":
            assert art.forbidden_in_claim(char, allow_lf=False) == (sanitize(char) != char), hex(cp)


def test_the_session_meta_schema_agrees_on_structure() -> None:
    """The claims table and schemas/session-meta.json accept and refuse the same shapes (the
    character classes are code's job; ASCII text only here)."""
    schema = json.loads(
        (Path(__file__).parent.parent / "schemas" / "session-meta.json").read_text()
    )
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource

    defs = json.loads((Path(__file__).parent.parent / "schemas" / "defs.json").read_text())
    registry: Registry[Any] = Registry().with_resource(defs["$id"], Resource.from_contents(defs))
    validator = Draft202012Validator(schema, registry=registry)
    rng = random.Random(20)
    pool: list[Any] = [
        "comment",
        "low",
        "x",
        "",
        "f0001",
        "f0001",
        1,
        None,
        ["f0001"],
        [],
        {},
        "a" * 201,
    ]
    for _ in range(400):
        meta = copy.deepcopy(META)
        for key in list(meta):
            if rng.random() < 0.3:
                meta[key] = rng.choice(pool)
            elif rng.random() < 0.1:
                del meta[key]
        if rng.random() < 0.3:
            meta["coverage"] = {"reviewed": [rng.choice(pool) for _ in range(rng.randrange(4))]}
        if rng.random() < 0.1:
            meta["risk_reasons"] = [rng.choice(pool) for _ in range(rng.randrange(13))]
        assert validator.is_valid(meta) == (not _fails(meta)), meta


# -- step 8 and 9: identity and normalization ---------------------------------------------------------


def normalized(doc: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    doc.pop("meta", None)
    art.apply_identity(doc, IDENTITY)
    entries = art.normalize(doc)
    return doc, entries


def test_the_identity_replaces_whatever_the_session_wrote() -> None:
    doc = {
        "schema_version": 7,
        "draft_id": "x",
        "pr": MARKER,
        "anchor": {"head_sha": "c" * 40, "extra": 1},
    }
    art.apply_identity(doc, IDENTITY)
    assert doc == {
        "schema_version": 1,
        "draft_id": IDENTITY.draft_id,
        "pr": IDENTITY.pr,
        "anchor": ANCHOR,
    }


def test_the_normalization_fixtures() -> None:
    doc = draft(
        event="approve",
        findings=[
            {**FINDING, "failure_scenario": "invented", "suggestion": None, "title": None},
            {
                **FINDING,
                "id": "F2",
                "suggestion": {"lines": ["x"], "invented": 1},
                "line": None,
                "severity": None,
            },
            {**FINDING, "id": "F3", "line": None},
        ],
        unanchored=[{"junk": 1}],
        extra_top="x",
    )
    out, entries = normalized(doc)
    assert entries == [
        {"rule": "drop_unknown", "pointer": "", "count": 1},
        {"rule": "drop_unknown", "pointer": "/findings/0", "count": 1},
        {"rule": "drop_unknown", "pointer": "/findings/1/suggestion", "count": 1},
        {"rule": "drop_null", "pointer": "/findings/0", "count": 2},
        {"rule": "drop_null", "pointer": "/findings/1", "count": 1},
        {"rule": "drop_unanchored", "pointer": "", "count": 1},
        {"rule": "event_downgrade", "pointer": "", "count": 1},
    ]
    assert out["event"] == "comment" and "unanchored" not in out and "extra_top" not in out
    assert out["findings"][0] == FINDING and out["findings"][2]["line"] is None
    assert out["findings"][1]["suggestion"] == {"lines": ["x"]}
    assert MARKER not in json.dumps(entries)


def test_a_dropped_key_name_never_appears_in_the_entries() -> None:
    out, entries = normalized(draft(findings=[{**FINDING, MARKER + "/~": 1}], **{MARKER: 2}))
    assert MARKER not in json.dumps(entries) and MARKER not in json.dumps(out)


@pytest.mark.parametrize(
    ("event", "severities", "result", "applied"),
    [
        ("request_changes", [None, "minor"], "comment", True),
        ("request_changes", ["nit", "major"], "request_changes", False),
        ("request_changes", ["blocker"], "request_changes", False),
        ("request_changes", [], "comment", True),
        ("approve", ["blocker"], "comment", True),
        ("comment", ["blocker"], "comment", False),
        ("bogus", [], "bogus", False),
    ],
)
def test_event_downgrade_is_one_way(
    event: str, severities: list[Any], result: str, applied: bool
) -> None:
    findings = [{**FINDING, "id": f"F{i}", "severity": s} for i, s in enumerate(severities)]
    out, entries = normalized(draft(event=event, findings=findings))
    assert out["event"] == result
    assert [e["rule"] for e in entries if e["rule"] == "event_downgrade"] == (
        ["event_downgrade"] if applied else []
    )


def test_a_null_severity_is_dropped_before_the_downgrade_looks_at_it() -> None:
    out, entries = normalized(
        draft(event="request_changes", findings=[{**FINDING, "severity": None}])
    )
    assert out["event"] == "comment" and [e["rule"] for e in entries] == [
        "drop_null",
        "event_downgrade",
    ]


def test_findings_are_never_added_removed_reordered_or_reworded() -> None:
    findings = [{**FINDING, "id": f"F{i}", "body": f"b{i} ‮", "extra": i} for i in (3, 1, 2)]
    out, _ = normalized(draft(findings=copy.deepcopy(findings)))
    assert [(f["id"], f["body"]) for f in out["findings"]] == [
        (f["id"], f["body"]) for f in findings
    ]


def test_non_dict_findings_and_suggestions_are_left_for_draft_check() -> None:
    out, entries = normalized(draft(findings=[1, None, {**FINDING, "suggestion": [1]}]))
    assert out["findings"][:2] == [1, None] and out["findings"][2]["suggestion"] == [1]
    assert entries == []
    out, entries = normalized(draft(findings="x"))
    assert out["findings"] == "x" and entries == []


def test_failures_with_no_lossless_fix_are_not_repaired() -> None:
    bad = {
        **FINDING,
        "title": "t" * 300,
        "start_line": 9,
        "side": "left",
        "suggestion": {"lines": []},
        "body": "",
    }
    out, entries = normalized(draft(findings=[bad, copy.deepcopy(bad)]))
    assert out["findings"][0] == bad and entries == []


def test_the_entries_are_capped_at_200_with_a_truncated_entry() -> None:
    findings = [{**FINDING, "id": f"F{i}", "suggestion": None} for i in range(250)]
    out, entries = normalized(draft(findings=findings, event="approve"))
    assert len(entries) == 201
    assert entries[199] == {"rule": "drop_null", "pointer": "/findings/199", "count": 1}
    assert entries[200] == {"rule": "truncated", "pointer": "", "count": 51}
    assert all("suggestion" not in f for f in out["findings"]) and out["event"] == "comment"
    exactly = normalized(draft(findings=[{**f, "suggestion": None} for f in findings[:200]]))[1]
    assert len(exactly) == 200 and exactly[-1]["rule"] == "drop_null"


def random_draft(rng: random.Random) -> dict[str, Any]:
    values: list[Any] = [
        None,
        1,
        "x",
        [],
        {"lines": ["a"]},
        {"lines": [], "q": 1},
        "left",
        "blocker",
        "nit",
        0.5,
    ]

    def finding(i: int) -> Any:
        if rng.random() < 0.05:
            return copy.deepcopy(rng.choice(values))
        f: dict[str, Any] = {
            "id": f"F{i}",
            "path": "p",
            "line": rng.choice([None, 1, 2]),
            "body": "b",
        }
        for key in ("title", "severity", "start_line", "side", "suggestion", "zzz", "yyy"):
            if rng.random() < 0.4:
                f[key] = copy.deepcopy(rng.choice(values))
        return f

    doc: dict[str, Any] = {
        k: copy.deepcopy(rng.choice(values)) for k in ("event", "body", "q") if rng.random() < 0.8
    }
    doc["event"] = rng.choice(["approve", "comment", "request_changes", None, "x"])
    doc["findings"] = (
        [finding(i) for i in range(rng.randrange(6))]
        if rng.random() < 0.9
        else copy.deepcopy(rng.choice(values))
    )
    if rng.random() < 0.3:
        doc["unanchored"] = copy.deepcopy(rng.choice(values))
    return doc


def test_normalization_is_idempotent_and_deterministic_over_random_drafts() -> None:
    rng = random.Random(2024)
    for _ in range(300):
        doc = random_draft(rng)
        first, entries = normalized(copy.deepcopy(doc))
        before = art.canonical_json(first)
        again = copy.deepcopy(first)
        assert art.normalize(again) == []
        assert art.canonical_json(again) == before
        # the same draft with its keys in another order gives the same bytes and entries
        shuffled = json.loads(
            json.dumps(doc), object_pairs_hook=lambda p: dict(rng.sample(p, len(p)))
        )
        other, other_entries = normalized(shuffled)
        assert art.canonical_json(other) == before
        assert sorted(map(json.dumps, other_entries)) == sorted(map(json.dumps, entries))
        assert len(entries) <= 201


def test_the_known_key_sets_equal_the_pinned_review_draft_schema() -> None:
    path = Path(__file__).parent / "pinned_schemas" / "pr-comment" / "schemas" / "review-draft.json"
    schema = json.loads(path.read_text())
    finding = schema["properties"]["findings"]["items"]
    assert set(schema["properties"]) == art.DRAFT_KEYS
    assert set(finding["properties"]) == art.FINDING_KEYS
    assert set(finding["properties"]["suggestion"]["properties"]) == art.SUGGESTION_KEYS
    optional = set(finding["properties"]) - set(finding["required"])
    assert set(art.NULLABLE_FINDING_FIELDS) == optional


def test_canonical_json_is_ascii_sorted_and_compact() -> None:
    text = art.canonical_json({"b": "é\U0001f600 ", "a": [1, 2.5, None]})
    assert text == '{"a":[1,2.5,null],"b":"\\u00e9\\ud83d\\ude00\\u2028"}' and text.isascii()


# -- step 10: draft check ------------------------------------------------------------------------------


def check(fakebin: FakeBin, doc: dict[str, Any]) -> None:
    art.run_check(art.canonical_json(doc))


def check_calls(fb: FakeBin) -> list[Any]:
    return [c for c in fb.calls("pr-comment") if c.argv[:2] == ["draft", "check"]]


def valid_draft() -> dict[str, Any]:
    out, _ = normalized(draft(findings=[dict(FINDING)]))
    return out


@pytest.fixture
def pc(fakebin: FakeBin, routed: Any) -> FakeBin:
    return fakebin


def test_a_valid_draft_passes_the_check_and_stdin_carries_it(pc: FakeBin) -> None:
    check(pc, valid_draft())
    (call,) = check_calls(pc)
    assert call.argv == ["draft", "check", "-", "--json"]
    assert json.loads(call.stdin or "") == valid_draft()


def test_the_live_verification_fixtures_pass_draft_check_after_normalization(pc: FakeBin) -> None:
    doc = draft(
        event="approve",
        findings=[
            {**FINDING, "failure_scenario": "x", "suggestion": None},
            {
                **FINDING,
                "id": "F2",
                "suggestion": {"lines": ["x"], "note": "y"},
                "title": "  padded  ",
            },
        ],
    )
    out, _ = normalized(doc)
    check(pc, out)


def test_a_schema_error_is_artifact_invalid_with_kind_schema_and_no_message(pc: FakeBin) -> None:
    doc = valid_draft()
    doc["findings"][0]["path"] = ""
    with pytest.raises(art.Invalid) as exc:
        check(pc, doc)
    assert [(e["pointer"], e["kind"]) for e in exc.value.errors] == [("/findings/0/path", "schema")]
    assert all(set(e) == {"pointer", "kind"} for e in exc.value.errors)


def test_text_kinds_come_through_and_other_kinds_are_schema(pc: FakeBin) -> None:
    doc = valid_draft()
    doc["findings"][0]["body"] = "a\x01b"
    doc["findings"][0]["suggestion"] = {"lines": ["```"]}
    with pytest.raises(art.Invalid) as exc:
        check(pc, doc)
    assert sorted(e["kind"] for e in exc.value.errors) == ["control_character", "suggestion_fence"]
    pc.script(
        "pr-comment", "draft check",
        {"json": {"valid": False, "schema_version": 1, "draft_id": None, "findings": 1,
                  "errors": [{"pointer": "/a", "message": MARKER, "kind": MARKER},
                             {"pointer": "no-slash", "message": "m", "kind": None},
                             {"pointer": "/" + "é" * 500, "message": "m", "kind": None},
                             {"pointer": "/\ud800", "message": "m", "kind": "suggestion_fence"}]},
         "exit": 2, "schema": None},
    )  # fmt: skip
    with pytest.raises(art.Invalid) as exc:
        check(pc, valid_draft())
    got = exc.value.errors
    assert [e["kind"] for e in got] == ["schema", "schema", "schema", "suggestion_fence"]
    assert got[0]["pointer"] == "/a" and got[1]["pointer"] == "" and len(got[2]["pointer"]) == 200
    assert got[3]["pointer"] == "/?" and MARKER not in repr(got)
    got[3]["pointer"].encode("utf-8")


def test_only_the_first_20_errors_are_kept(pc: FakeBin) -> None:
    errors = [{"pointer": f"/findings/{i}", "message": "m", "kind": None} for i in range(30)]
    pc.script(
        "pr-comment",
        "draft check",
        {
            "json": {
                "valid": False,
                "schema_version": 1,
                "draft_id": None,
                "findings": 1,
                "errors": errors,
            },
            "exit": 2,
        },
    )
    with pytest.raises(art.Invalid) as exc:
        check(pc, valid_draft())
    assert len(exc.value.errors) == 20 and exc.value.errors[19]["pointer"] == "/findings/19"


def test_exit_12_is_check_failed_with_the_closed_code(pc: FakeBin) -> None:
    pc.script(
        "pr-comment", "draft check", {"error": {"code": "internal", "message": MARKER}, "exit": 12}
    )
    with pytest.raises(art.CheckFailed) as exc:
        check(pc, valid_draft())
    assert exc.value.lower_code == "internal" and MARKER not in str(exc.value)


@pytest.mark.parametrize(
    "step",
    [
        {
            "json": {"valid": True, "errors": [{"pointer": "", "message": "m", "kind": None}]},
            "exit": 0,
        },
        {"json": {"valid": False, "errors": []}, "exit": 2},
        {"json": {"valid": False}, "exit": 2},
        {"json": {"valid": True, "errors": []}, "exit": 2},
        {
            "json": {"valid": False, "errors": [{"pointer": "", "message": "m", "kind": None}]},
            "exit": 0,
        },
        {"json": [1], "exit": 0},
        {"json": {"no": "valid"}, "exit": 0},
        {"stdout_raw": "not json", "exit": 0},
        {"stdout_raw": '{"valid": true, "errors": [], "x": 1e999}', "exit": 0},
    ],
)
def test_output_that_is_no_verdict_is_check_failed_invalid_output(
    pc: FakeBin, step: dict[str, Any]
) -> None:
    pc.script("pr-comment", "draft check", {**step, "schema": None})
    with pytest.raises(art.CheckFailed) as exc:
        check(pc, valid_draft())
    assert exc.value.lower_code == "invalid_output"


def test_exit_2_with_an_error_envelope_is_check_failed_with_its_code(pc: FakeBin) -> None:
    pc.script("pr-comment", "draft check", {"error": {"code": "usage", "message": "m"}, "exit": 2})
    with pytest.raises(art.CheckFailed) as exc:
        check(pc, valid_draft())
    assert exc.value.lower_code == "usage"


def test_a_draft_over_the_stdin_cap_is_length_and_calls_nothing(pc: FakeBin) -> None:
    with pytest.raises(art.Invalid) as exc:
        art.run_check("x" * ((8 << 20) + 1))
    assert kinds(exc) == [("", "length")] and check_calls(pc) == []


def test_a_missing_pr_comment_is_check_failed_tool_missing(pc: FakeBin, routed: Any) -> None:
    routed.missing.add("pr-comment")
    with pytest.raises(art.CheckFailed) as exc:
        check(pc, valid_draft())
    assert exc.value.lower_code == "tool_missing"


# -- strict_json (issue #73) -------------------------------------------------------------------------


def test_strict_json_refuses_an_overflowing_float() -> None:
    from review_run.prefetch import strict_json

    for text in ("1e999", "-1e999", '{"a": 1e400}', "[1e999]"):
        with pytest.raises(ValueError):
            strict_json(text)
    assert strict_json("[1e300, 0.5]") == [1e300, 0.5]


# -- the pipeline --------------------------------------------------------------------------------------


def good_artifact(**kw: Any) -> str:
    return json.dumps(draft(findings=[dict(FINDING)], **kw))


def derive_stub(seen: list[art.Validated]) -> Any:
    def derive(ctx: fin.ArtifactContext, validated: art.Validated) -> fin.Processed:
        seen.append(validated)
        return fin.Processed(b'{"ok":1}\n', dict(_SUMMARY), ())

    return derive


_SUMMARY: dict[str, Any] = {
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


def ok_check(ctx: fin.SessionContext) -> fin.SessionCheck:
    return fin.SessionCheck(skill_checked=False, tools_checked=False, tools_unexpected=0)


def run_with(world: World, text: str, seen: list[art.Validated] | None = None) -> Any:
    world.fb.use("agent-spawn", "outcome", outcome="done", artifact_text=text)
    seams = fin.Seams(ok_check, derive=derive_stub([] if seen is None else seen))
    return world.run(seams=seams)


def test_a_valid_artifact_is_validated_and_handed_to_derive_with_the_right_identity(
    world: World,
) -> None:
    seen: list[art.Validated] = []
    wrong = good_artifact(
        schema_version=9,
        draft_id="x",
        pr="evil",
        anchor={"head_sha": "c" * 40},
        junk=1,
        unanchored=[],
    )
    run = run_with(world, wrong, seen)
    assert (run.state, run.reason, run.exit_code) == ("done", None, 0)
    (validated,) = seen
    assert run.anchor is not None and run.draft_id is not None
    assert validated.draft["anchor"] == run.anchor and validated.draft["draft_id"] == run.draft_id
    assert validated.draft["pr"] == run.pr and validated.draft["schema_version"] == 1
    assert "meta" not in validated.draft and validated.claims.verdict == "comment"
    assert [e["rule"] for e in validated.normalized] == ["drop_unknown", "drop_unanchored"]
    (check_call,) = check_calls(world.fb)
    assert json.loads(check_call.stdin or "")["anchor"] == run.anchor and "meta" not in (
        check_call.stdin or ""
    )


def failed_object(run: Any) -> dict[str, Any]:
    obj = runs.run_object(run)
    validate_against("cli/run.json", obj)
    return obj


@pytest.mark.parametrize(
    ("text", "errors"),
    [
        ("﻿" + good_artifact(), [{"pointer": "", "kind": "encoding"}]),
        ('{"a":' * 40 + "1" + "}" * 40, [{"pointer": "", "kind": "depth"}]),
        (json.dumps({"event": "comment"}), [{"pointer": "/meta", "kind": "meta_missing"}]),
        (
            good_artifact().replace('"low"', '"extreme"'),
            [{"pointer": "/meta/risk", "kind": "enum"}],
        ),
        (
            json.dumps(draft(meta={**META, "summary": "x" * (8 << 10) * 250})),
            [{"pointer": "/meta/summary", "kind": "length"}],
        ),
    ],
)
# one case per mapping; every parse and claim kind is covered by the unit tests above
def test_each_invalid_artifact_ends_artifact_invalid_with_closed_errors(
    world: World, text: str, errors: list[dict[str, str]]
) -> None:
    run = run_with(world, text)
    assert (run.state, run.reason, run.exit_code) == ("failed", "artifact_invalid", 22)
    obj = failed_object(run)
    assert obj["failure"]["errors"] == errors and obj["failure"]["tool"] is None
    assert check_calls(world.fb) == []


def test_a_draft_check_schema_error_ends_artifact_invalid_with_kind_schema(world: World) -> None:
    run = run_with(world, json.dumps(draft(findings=[{**FINDING, "path": ""}], body=MARKER)))
    assert (run.state, run.reason, run.exit_code) == ("failed", "artifact_invalid", 22)
    assert failed_object(run)["failure"]["errors"] == [
        {"pointer": "/findings/0/path", "kind": "schema"}
    ]
    assert MARKER not in json.dumps(failed_object(run))


def test_draft_check_exit_12_ends_internal_with_tool_pr_comment(world: World) -> None:
    world.fb.script(
        "pr-comment", "draft check", {"error": {"code": "internal", "message": "boom"}, "exit": 12}
    )
    run = run_with(world, good_artifact())
    assert (run.state, run.reason, run.exit_code) == ("failed", "internal", 12)
    failure = failed_object(run)["failure"]
    assert (failure["tool"], failure["lower_code"]) == ("pr-comment", "internal")


def test_default_seams_without_derive_stay_incomplete() -> None:
    assert not fin.Seams(ok_check).complete
    assert fin.Seams(ok_check, derive=derive_stub([])).complete
    assert not fin.Seams(derive=derive_stub([])).complete


def test_a_run_without_a_stored_draft_identity_is_internal() -> None:
    ctx = fin.ArtifactContext.__new__(fin.ArtifactContext)
    object.__setattr__(ctx, "run", type("R", (), {"draft_id": None, "anchor": None})())
    from review_run.errors import ReviewRunError

    with pytest.raises(ReviewRunError):
        fin.validate_step(ctx)


# -- review round 1 -------------------------------------------------------------------------------------


def scripted_check_errors(fb: FakeBin, errors: list[Any]) -> None:
    fb.script(
        "pr-comment",
        "draft check",
        {
            "json": {
                "valid": False,
                "schema_version": 1,
                "draft_id": None,
                "findings": 1,
                "errors": errors,
            },
            "exit": 2,
            "schema": None,  # these outputs are deliberately outside pr-comment's schema
        },
    )


def test_unhashable_or_non_dict_check_errors_are_schema_not_a_crash(pc: FakeBin) -> None:
    scripted_check_errors(
        pc,
        [
            {"pointer": "/a", "kind": ["suggestion_fence"]},
            {"pointer": "/b", "kind": {"x": 1}},
            [],
            "text",
            {"pointer": "/c", "kind": "control_character"},
        ],
    )
    with pytest.raises(art.Invalid) as exc:
        check(pc, valid_draft())
    assert kinds(exc) == [
        ("/a", "schema"),
        ("/b", "schema"),
        ("", "schema"),
        ("", "schema"),
        ("/c", "control_character"),
    ]


def test_check_error_pointers_are_printable_ascii_and_fit_the_failure_schema(pc: FakeBin) -> None:
    scripted_check_errors(
        pc,
        [
            {"pointer": "/é中\x01\x7f\n/x", "kind": None},
            {"pointer": "/" + "é" * 500, "kind": None},
        ],
    )
    with pytest.raises(art.Invalid) as exc:
        check(pc, valid_draft())
    first, second = exc.value.errors
    assert first["pointer"] == "/?????/x" and second["pointer"] == "/" + "?" * 199
    defs = json.loads((Path(__file__).parent.parent / "schemas" / "defs.json").read_text())
    item = defs["$defs"]["failure"]["properties"]["errors"]["items"]
    from jsonschema import Draft202012Validator

    for err in exc.value.errors:
        assert err["pointer"].isascii() and err["pointer"].isprintable()
        Draft202012Validator({**item, "$defs": defs["$defs"]}).validate(err)


def test_claim_errors_follow_the_tables_order_for_mixed_missing_and_invalid_fields() -> None:
    meta = with_meta(verdict="merge", risk=..., risk_reasons="x", summary=..., coverage=[])
    assert claim_errors(meta) == [
        ("/meta/verdict", "enum"),
        ("/meta/risk", "meta_missing"),
        ("/meta/risk_reasons", "type"),
        ("/meta/summary", "meta_missing"),
        ("/meta/coverage", "type"),
    ]


def test_the_20_cut_is_in_table_order() -> None:
    meta = with_meta(
        risk=..., summary=..., coverage={"reviewed": [MARKER] * 30}, risk_reasons=["\n"] * 10
    )
    got = claim_errors(meta)
    assert len(got) == 20
    assert got[0] == ("/meta/risk", "meta_missing")
    assert got[1] == ("/meta/risk_reasons/0", "pattern")
    assert got[11] == ("/meta/summary", "meta_missing")
    assert got[12] == ("/meta/coverage/reviewed/0", "pattern")


def script_valid_check(fb: FakeBin) -> None:
    """A passing check whatever the (huge) stdin is: the fake would apply pr-comment's text caps."""
    out = {"valid": True, "schema_version": 1, "draft_id": None, "findings": 0, "errors": []}
    fb.script("pr-comment", "draft check", {"json": out, "exit": 0, "schema": None})


def test_a_non_ascii_draft_is_sent_as_utf8_and_measured_in_bytes(pc: FakeBin) -> None:
    script_valid_check(pc)
    doc = valid_draft()
    doc["body"] = "中" * 2_400_000  # 7.2 MB as UTF-8, 14.4 MB with \u escapes
    text = art.canonical_json(doc, ensure_ascii=False)
    assert art.canonical_json({"b": "é"}) == '{"b":"\\u00e9"}'
    assert art.canonical_json({"b": "é"}, ensure_ascii=False) == '{"b":"é"}'
    art.run_check(text)
    (call,) = check_calls(pc)
    assert json.loads(call.stdin or "")["body"] == doc["body"]


def test_validate_sends_the_utf8_form_so_a_valid_non_ascii_artifact_reaches_the_check(
    pc: FakeBin,
) -> None:
    script_valid_check(pc)
    data = json.dumps(draft(body="中" * 2_400_000), ensure_ascii=False).encode()
    assert len(data) < 8 << 20
    got = art.validate(data, max_bytes=8 << 20, max_files=10, identity=IDENTITY)
    assert len(got.draft["body"]) == 2_400_000 and len(check_calls(pc)) == 1


def test_a_draft_over_the_stdin_cap_in_utf8_bytes_is_length(pc: FakeBin) -> None:
    with pytest.raises(art.Invalid) as exc:
        art.run_check("é" * ((4 << 20) + 1))  # half as many characters as bytes
    assert kinds(exc) == [("", "length")] and check_calls(pc) == []


def test_more_than_500_findings_is_schema_at_findings_before_normalization(pc: FakeBin) -> None:
    data = json.dumps(draft(findings=[{}] * 501)).encode()
    with pytest.raises(art.Invalid) as exc:
        art.validate(data, max_bytes=8 << 20, max_files=10, identity=IDENTITY)
    assert kinds(exc) == [("/findings", "schema")] and check_calls(pc) == []


def test_exactly_500_findings_still_reach_the_check(pc: FakeBin) -> None:
    script_valid_check(pc)
    findings = [{**FINDING, "id": f"F{i}", "line": i + 1} for i in range(500)]
    data = json.dumps(draft(findings=findings)).encode()
    art.validate(data, max_bytes=8 << 20, max_files=10, identity=IDENTITY)
    assert len(check_calls(pc)) == 1


def test_claim_errors_come_before_the_findings_count() -> None:
    data = json.dumps(draft(findings=[{}] * 501, meta={**META, "risk": "x"})).encode()
    with pytest.raises(art.Invalid) as exc:
        art.validate(data, max_bytes=8 << 20, max_files=10, identity=IDENTITY)
    assert kinds(exc) == [("/meta/risk", "enum")]


def test_childless_containers_are_not_walked_twice() -> None:
    started = time.monotonic()
    art.parse_artifact(b'{"a":' + b"[" + b"{}," * 500_000 + b"{}]}", max_bytes=8 << 20)
    assert time.monotonic() - started < 3


def test_the_session_meta_schema_refuses_every_bmp_character_the_code_refuses() -> None:
    import re

    schema = json.loads(
        (Path(__file__).parent.parent / "schemas" / "session-meta.json").read_text()
    )
    summary = re.compile(schema["properties"]["summary"]["pattern"])
    reason = re.compile(schema["properties"]["risk_reasons"]["items"]["pattern"])
    for cp in range(0x10000):
        if 0xD800 <= cp <= 0xDFFF:
            continue
        char = chr(cp)
        if art.forbidden_in_claim(char, allow_lf=True):
            assert summary.search(char) is None, hex(cp)
        if (
            art.forbidden_in_claim(char, allow_lf=False) and char != "\n"
        ):  # `$` accepts a trailing LF
            assert reason.search(char) is None, hex(cp)
