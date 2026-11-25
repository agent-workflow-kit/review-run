"""The fake pr-comment: ``draft new`` and ``draft check`` (grammar, scenarios, real validation)."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from review_run.gateway import CallTimeout
from tests.fakebin import _forgectl as fg
from tests.fakes import FakeBin

PR = "github.com/acme/widgets#7"


def run(fb: FakeBin, *argv: str, stdin: str | None = None) -> tuple[int, Any, str]:
    result = fb.run("pr-comment", *argv, stdin=stdin)
    try:
        doc = json.loads(result.stdout)
    except ValueError:
        doc = None
    return result.returncode, doc, result.stderr


def new_draft(fb: FakeBin, *extra: str) -> dict[str, Any]:
    code, doc, _ = run(fb, "draft", "new", PR, "--json", *extra)
    assert code == 0, doc
    return doc


def finding(**over: Any) -> dict[str, Any]:
    base = {"id": "f1", "path": "src/app.py", "line": 2, "body": "Consider a constant."}
    return {**base, **over}


def check(fb: FakeBin, draft: Any) -> tuple[int, Any]:
    text = draft if isinstance(draft, str) else json.dumps(draft)
    code, doc, _ = run(fb, "draft", "check", "-", "--json", stdin=text)
    return code, doc


def test_draft_new_is_a_valid_skeleton(fakebin: FakeBin) -> None:
    draft = new_draft(fakebin)
    assert draft["pr"] == PR and draft["event"] == "comment" and draft["findings"] == []
    assert draft["anchor"] == {"head_sha": fg.HEAD, "base_ref": "main", "base_sha": fg.BASE,
                               "merge_base_sha": fg.MERGE_BASE}  # fmt: skip
    assert fakebin.calls("pr-comment")[0].schema == "pr-comment/cli/draft-new.json"


def test_draft_ids_are_unique_per_call(fakebin: FakeBin) -> None:
    assert new_draft(fakebin)["draft_id"] != new_draft(fakebin)["draft_id"]


def test_event_flag(fakebin: FakeBin) -> None:
    assert new_draft(fakebin, "--event", "request_changes")["event"] == "request_changes"


def test_merge_base_null_scenario(fakebin: FakeBin) -> None:
    fakebin.use("pr-comment", "merge_base_null")
    assert new_draft(fakebin)["anchor"]["merge_base_sha"] is None


def test_stale_head_scenario(fakebin: FakeBin) -> None:
    fakebin.use("pr-comment", "stale_head")
    code, doc, _ = run(fakebin, "draft", "new", PR, "--json")
    assert code == 20 and doc["error"]["reason"] == "stale_head"
    assert doc["error"]["details"] == {"head_sha": fg.NEW_HEAD}


def test_lower_failure_scenario(fakebin: FakeBin) -> None:
    fakebin.use("pr-comment", "lower_failed", exit_code=12, code="rate_limited")
    code, doc, _ = run(fakebin, "draft", "new", PR, "--json")
    assert code == 12 and doc["error"]["code"] == "rate_limited"


def test_from_copies_the_findings_and_checks_the_head(fakebin: FakeBin) -> None:
    src = new_draft(fakebin)
    src["findings"] = [finding()]
    src["body"] = "Summary"
    code, doc, _ = run(fakebin, "draft", "new", PR, "--from", "-", "--json", stdin=json.dumps(src))
    assert code == 0 and doc["findings"] == src["findings"] and doc["body"] == "Summary"
    assert doc["draft_id"] != src["draft_id"]
    src["anchor"]["head_sha"] = fg.NEW_HEAD
    code, doc, _ = run(fakebin, "draft", "new", PR, "--from", "-", "--json", stdin=json.dumps(src))
    assert code == 20 and doc["error"]["reason"] == "stale_head"


def test_from_another_pr_is_a_mismatch(fakebin: FakeBin) -> None:
    src = new_draft(fakebin)
    code, doc, _ = run(fakebin, "draft", "new", "github.com/acme/other#1", "--from", "-",
                       "--json", stdin=json.dumps(src))  # fmt: skip
    assert code == 2 and doc["error"]["code"] == "draft_pr_mismatch"


def test_check_accepts_a_good_draft(fakebin: FakeBin) -> None:
    draft = new_draft(fakebin)
    draft["findings"] = [finding(), finding(id="f2", line=None, severity="nit")]
    code, doc = check(fakebin, draft)
    assert code == 0 and doc["valid"] is True and doc["findings"] == 2 and doc["errors"] == []
    assert fakebin.calls("pr-comment", "draft")[-1].stdin == json.dumps(draft)


def mutate(path: tuple[Any, ...], value: Any) -> dict[str, Any]:
    return {"path": path, "value": value}


@pytest.mark.parametrize(
    ("edit", "pointer"),
    [
        (lambda d: d.update(schema_version=2), "/schema_version"),
        (lambda d: d.update(event="approve"), "/event"),
        (lambda d: d.pop("anchor"), ""),
        (lambda d: d.update(findings=[finding(path="")]), "/findings/0/path"),
        (lambda d: d.update(findings=[finding(line=0)]), "/findings/0/line"),
        (lambda d: d.update(findings=[finding(extra=1)]), "/findings/0"),
        (lambda d: d.update(findings=[finding(), finding()]), "/findings/1/id"),
        (lambda d: d.update(findings=[finding(start_line=2)]), "/findings/0/start_line"),
        (lambda d: d.update(findings=[finding(side="left", suggestion={"lines": ["x"]})]),
         "/findings/0/suggestion"),  # fmt: skip
        (lambda d: d.update(findings=[finding(body="a\x07b")]), "/findings/0/body"),
        (lambda d: d.update(findings=[finding(suggestion={"lines": ["```"]})]),
         "/findings/0/suggestion/lines/0"),  # fmt: skip
        (lambda d: d.update(findings=[finding(id=f"f{i}", line=i + 1) for i in range(201)]),
         "/findings"),  # fmt: skip
    ],
)
def test_check_rejects_invalid_drafts(fakebin: FakeBin, edit: Any, pointer: str) -> None:
    draft = new_draft(fakebin)
    edit(draft)
    code, doc = check(fakebin, draft)
    assert code == 2 and doc["valid"] is False
    assert pointer in {e["pointer"] for e in doc["errors"]}, doc["errors"]
    assert all("message" in e and e["message"] for e in doc["errors"])


def test_check_errors_never_echo_draft_content(fakebin: FakeBin) -> None:
    draft = new_draft(fakebin)
    draft["findings"] = [finding(body="SECRET-MARKER\x07")]
    _, doc = check(fakebin, draft)
    assert "SECRET-MARKER" not in json.dumps(doc)


def test_check_invalid_json_and_non_objects(fakebin: FakeBin) -> None:
    for text in ("{not json", "[]", "null", ""):
        code, doc = check(fakebin, text)
        assert code == 2 and doc["valid"] is False and doc["findings"] is None


def test_check_caps_the_error_list(fakebin: FakeBin) -> None:
    draft = new_draft(fakebin)
    draft["findings"] = [finding(id="same", line=0) for _ in range(120)]
    _, doc = check(fakebin, draft)
    assert len(doc["errors"]) == 101 and doc["errors"][-1]["message"] == "further errors omitted"


def test_check_forced_invalid_scenario(fakebin: FakeBin) -> None:
    fakebin.use("pr-comment", "check_forced_invalid")
    code, doc = check(fakebin, new_draft(fakebin))
    assert code == 2 and doc["errors"][0]["pointer"] == "/findings/0/path"


def test_check_reads_a_file_path(fakebin: FakeBin) -> None:
    path = fakebin.dir / "d.json"
    path.write_text(json.dumps(new_draft(fakebin)), encoding="utf-8")
    code, doc, _ = run(fakebin, "draft", "check", str(path), "--json")
    assert code == 0 and doc["valid"]
    code, doc, _ = run(fakebin, "draft", "check", str(fakebin.dir / "missing.json"), "--json")
    assert code == 2 and doc["error"]["code"] == "invalid_draft"


@pytest.mark.parametrize(
    "argv",
    [
        ["draft"],
        ["draft", "frob"],
        ["draft", "new"],
        ["draft", "new", PR, "extra"],
        ["draft", "new", PR, "--event", "approve"],
        ["draft", "new", PR, "--event"],
        ["draft", "new", PR, "--nope"],
        ["draft", "check"],
        ["draft", "check", "-", "x"],
        ["draft", "check", "-", "--expect-plan", "abc"],
        ["draft", "new", PR, "--expect-plan", "abc"],
        ["draft", "check", "-", "--actor", "robot"],
        ["draft", "check", "-", "--config", "a", "--config", "b"],
        ["draft", "check", "-", "--override-escalation"],
        ["draft", "check", "-", "--override-escalation=other"],
        ["--", "draft", "check", "-"],
        ["draft", "--", "check", "-"],
        ["draft", "new", "not a pr"],
        ["draft", "check", "-", "--verbose", "--quiet"],
        ["draft", "chec", "-"],
    ],
)
def test_the_grammar_rejects_what_the_real_tool_rejects(fakebin: FakeBin, argv: list[str]) -> None:
    code, doc, err = run(fakebin, *argv, "--json", stdin="")
    assert code == 2, argv
    assert doc["error"]["code"] in ("usage", "invalid_pr")
    assert err.startswith("pr-comment: error:")


def test_global_flags_go_anywhere_including_between_draft_and_subcommand(fakebin: FakeBin) -> None:
    assert run(fakebin, "--json", "draft", "--config", "/x", "new", PR)[0] == 0
    assert run(fakebin, "draft", "--json", "check", "-", stdin=json.dumps(new_draft(fakebin)))[0] == 0
    # the same scalar value twice is accepted
    assert run(fakebin, "draft", "new", PR, "--json", "--actor", "auto:x", "--actor", "auto:x")[0] == 0


def test_posting_commands_are_not_emulated(fakebin: FakeBin) -> None:
    assert run(fakebin, "review", PR, "--json")[0] == 99


def test_version(fakebin: FakeBin) -> None:
    assert fakebin.run("pr-comment", "--version").stdout == "pr-comment 0.39.0\n"


def test_hang_is_ended_by_the_gateway_timeout(fakebin: FakeBin) -> None:
    fakebin.use("pr-comment", "hang")
    with pytest.raises(CallTimeout):
        fakebin.run("pr-comment", "draft", "check", "-", "--json", stdin="{}", timeout=0.6)


def test_a_draft_copy_is_independent(fakebin: FakeBin) -> None:
    a = new_draft(fakebin)
    b = copy.deepcopy(a)
    b["findings"].append(finding())
    assert a["findings"] == []
