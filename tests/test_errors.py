import json

import pytest

from review_run.errors import ERROR_EXIT, ExitCode, ReviewRunError
from tests.conftest import SCHEMAS, validate_against


def schema_error_codes() -> list[str]:
    defs = json.loads((SCHEMAS / "defs.json").read_text(encoding="utf-8"))
    return defs["$defs"]["error_code"]["enum"]


def test_exit_code_values_match_the_interface() -> None:
    assert {c.name: int(c) for c in ExitCode} == {
        "OK": 0,
        "INTERNAL": 1,
        "USAGE": 2,
        "NOT_FOUND": 10,
        "VETOED": 11,
        "FAILED": 12,
        "ESCALATED": 13,
        "REFUSED": 20,
        "AMBIGUOUS": 21,
        "ARTIFACT": 22,
        "SIGINT": 130,
        "SIGTERM": 143,
    }


def test_every_schema_error_code_has_an_exit_code() -> None:
    assert sorted(ERROR_EXIT) == sorted(schema_error_codes())


@pytest.mark.parametrize(
    ("code", "exit_code"),
    [
        ("usage", 2),
        ("config_invalid", 2),
        ("not_found", 10),
        ("ambiguous", 21),
        ("vetoed", 11),
        ("escalated", 13),
        ("refused", 20),
        ("failed", 12),
        ("internal", 1),
    ],
)
def test_error_code_to_exit_code(code: str, exit_code: int) -> None:
    assert ERROR_EXIT[code] == exit_code


def test_envelope_minimal_validates() -> None:
    err = ReviewRunError("internal", "boom")
    assert err.exit_code == 1
    doc = err.envelope()
    assert doc == {"error": {"code": "internal", "message": "boom"}}
    validate_against("cli/error.json", doc)


def test_envelope_carries_optional_fields_only_when_set() -> None:
    err = ReviewRunError(
        "failed",
        "tool missing",
        reason="tool_missing",
        details={"tool": "forgectl"},
        tool="forgectl",
        lower_code="not_found",
    )
    doc = err.envelope()
    assert doc["error"] == {
        "code": "failed",
        "message": "tool missing",
        "reason": "tool_missing",
        "details": {"tool": "forgectl"},
        "tool": "forgectl",
        "lower_code": "not_found",
    }
    validate_against("cli/error.json", doc)
    assert err.exit_code == 12


def test_refused_envelope_validates_and_exits_20() -> None:
    err = ReviewRunError("refused", "pr is closed", reason="not_open")
    validate_against("cli/error.json", err.envelope())
    assert err.exit_code == 20


def test_vetoed_envelope_with_hook() -> None:
    err = ReviewRunError("vetoed", "vetoed", hook="pre-run", decision_ids=["d1"])
    validate_against("cli/error.json", err.envelope())
    assert err.exit_code == 11


def test_unknown_code_is_rejected() -> None:
    with pytest.raises(ValueError):
        ReviewRunError("nope", "x")


def test_human_line_is_one_line_and_names_the_code() -> None:
    err = ReviewRunError("usage", "bad\nflag\r\x1b[31m")
    line = err.human()
    assert "\n" not in line and "\r" not in line and "\x1b" not in line
    assert line.startswith("review-run: usage: ")
