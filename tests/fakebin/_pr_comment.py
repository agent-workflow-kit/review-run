"""Fake ``pr-comment`` (pinned 0.39.0): ``draft new`` and ``draft check``.

Grammar and rules follow pr-comment's ``docs/interface.md`` (Commands, Global flags, Exit codes,
Error envelope). ``draft check`` really validates the draft it reads against the pinned
``review-draft.json`` plus the consistency rules that need no host, so a draft review-run sends
is judged, not rubber-stamped; a scenario can force any other answer.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any

from tests.fakebin import _schemas
from tests.fakebin._common import (
    Call,
    CommandSpec,
    Grammar,
    Out,
    Parsed,
    State,
    Usage,
    actor_check,
    apply_step,
    decision_check,
    error_out,
    flag,
    next_step,
    run_tool,
    scenario,
)
from tests.fakebin._forgectl import BASE, HEAD, MERGE_BASE, parse_pr

TOOL = "pr-comment"
INLINE_CAP = 200
MAX_DRAFT_BYTES = 16 * 1024 * 1024
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

_GLOBAL = {
    "--json": flag(),
    "--config": flag(takes=True),
    "--dry-run": flag(),
    "--yes": flag(),
    "--expect-plan": flag(takes=True),
    "--override-escalation": flag(takes=True, repeat=True, choices=("pr-comment", "forgectl")),
    "--actor": flag(takes=True, check=actor_check),
    "--decision-id": flag(takes=True, repeat=True, check=decision_check),
    "--hook": flag(takes=True, repeat=True),
    "--verbose": flag(),
    "--quiet": flag(),
    "--version": flag(),
    "--help": flag(),
}
GRAMMAR = Grammar(
    tool=TOOL,
    global_flags=_GLOBAL,
    commands={
        ("draft", "new"): CommandSpec(
            flags={
                "--event": flag(takes=True, choices=("comment", "request_changes")),
                "--from": flag(takes=True),
            },
            min_args=1,
            max_args=1,
        ),
        ("draft", "check"): CommandSpec(min_args=1, max_args=1),
    },
    unimplemented=frozenset(
        (c,) for c in "review comment reply approve resolve unresolve export validate".split()
    ),
    duplicates="same_ok",
)


def make_draft_id(counter: int) -> str:
    digest = hashlib.sha256(f"draft-{counter}".encode()).digest()
    return "01" + "".join(_CROCKFORD[b % 32] for b in digest[:24])


def _pointer(path: Any) -> str:
    return "".join("/" + str(p).replace("~", "~0").replace("/", "~1") for p in path)


def check_draft(raw: str) -> tuple[int, dict[str, Any]]:
    """The ``draft check`` result document and exit code for the draft text ``raw``."""
    try:
        draft = json.loads(raw)
    except ValueError:
        doc = {"valid": False, "schema_version": None, "draft_id": None, "findings": None,
               "errors": [{"pointer": "", "message": "not valid JSON", "kind": None}]}  # fmt: skip
        return 2, doc
    errors: list[dict[str, Any]] = []
    for err in sorted(
        _schemas.validator("pr-comment/review-draft.json").iter_errors(draft),
        key=lambda e: list(map(str, e.absolute_path)),
    ):
        errors.append(
            {
                "pointer": _pointer(err.absolute_path),
                "message": "does not match the schema",
                "kind": None,
            }  # fmt: skip
        )
    findings = draft.get("findings") if isinstance(draft, dict) else None
    if isinstance(findings, list):
        seen: set[str] = set()
        with_line = 0
        for i, f in enumerate(findings):
            if not isinstance(f, dict):
                continue
            fid, line, start = f.get("id"), f.get("line"), f.get("start_line")
            if isinstance(fid, str):
                if fid in seen:
                    errors.append({"pointer": f"/findings/{i}/id", "message": "duplicate id",
                                   "kind": None})  # fmt: skip
                seen.add(fid)
            if isinstance(line, int):
                with_line += 1
            if isinstance(start, int) and isinstance(line, int) and start >= line:
                errors.append({"pointer": f"/findings/{i}/start_line",
                               "message": "start_line must be below line",
                               "kind": None})  # fmt: skip
            if "suggestion" in f and (f.get("side") == "left" or line is None):
                errors.append({"pointer": f"/findings/{i}/suggestion",
                               "message": "a suggestion needs a right-side line",
                               "kind": None})  # fmt: skip
        if with_line > INLINE_CAP:
            errors.append({"pointer": "/findings", "message": "too many inline comments",
                           "kind": None})  # fmt: skip
    if not errors:
        errors.extend(_text_errors(draft, ""))
    doc = {
        "valid": not errors,
        "schema_version": draft.get("schema_version") if isinstance(draft, dict)
        and isinstance(draft.get("schema_version"), int) else None,
        "draft_id": draft.get("draft_id") if isinstance(draft, dict)
        and isinstance(draft.get("draft_id"), str)
        and re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{26}", draft["draft_id"]) else None,
        "findings": len(findings) if isinstance(findings, list) else None,
        "errors": errors[:100],
    }  # fmt: skip
    if len(errors) > 100:
        doc["errors"].append({"pointer": "", "message": "further errors omitted", "kind": None})
    return (0 if doc["valid"] else 2), doc


def _text_errors(node: Any, pointer: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(node, str):
        if any(unicodedata.category(c) == "Cc" and c not in "\n\t" for c in node):
            found.append({"pointer": pointer, "message": "control character",
                          "kind": "control_character"})  # fmt: skip
        elif pointer.endswith("/suggestion/lines") or "/suggestion/lines/" in pointer:
            if "```" in node:
                found.append({"pointer": pointer, "message": "suggestion fence",
                              "kind": "suggestion_fence"})  # fmt: skip
    elif isinstance(node, dict):
        for k, v in node.items():
            if k != "meta":
                found += _text_errors(v, f"{pointer}/{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            found += _text_errors(v, f"{pointer}/{i}")
    return found


def _default_draft(pr: str, counter: int, cfg: dict[str, Any], event: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "draft_id": make_draft_id(counter),
        "pr": pr,
        "anchor": {
            "head_sha": cfg.get("head_sha", HEAD),
            "base_ref": cfg.get("base_ref", "main"),
            "base_sha": cfg.get("base_sha", BASE),
            "merge_base_sha": cfg.get("merge_base_sha", MERGE_BASE),
        },
        "event": event,
        "body": "",
        "findings": [],
    }


def _read_source(call: Call, path: str) -> str:
    if path == "-":
        return call.read_stdin()
    try:
        with open(path, encoding="utf-8", errors="surrogateescape") as fh:
            return fh.read()
    except OSError:
        raise Usage(
            "cannot read the draft file",
            code="invalid_draft",
            details={"errors": [{"pointer": "", "message": "cannot read the draft file"}]},
        ) from None


def handler(call: Call, parsed: Parsed) -> Out:
    if parsed.has("--verbose") and parsed.has("--quiet"):
        raise Usage("--verbose and --quiet are mutually exclusive")
    cfg = scenario(TOOL)
    if parsed.command == ("draft", "check"):
        return _check(call, parsed, cfg)
    return _new(call, parsed, cfg)


def _check(call: Call, parsed: Parsed, cfg: dict[str, Any]) -> Out:
    if parsed.has("--expect-plan"):
        raise Usage("--expect-plan applies to posting commands only")
    raw = _read_source(call, parsed.args[0])
    step = next_step(TOOL, "draft check")

    def default() -> Out:
        code, doc = check_draft(raw)
        return Out(exit=code, doc=doc, stderr="" if code == 0 else "invalid draft")

    return apply_step(TOOL, step, default, call, schema=f"{TOOL}/cli/draft-check.json")


def _new(call: Call, parsed: Parsed, cfg: dict[str, Any]) -> Out:
    if parsed.has("--expect-plan"):
        raise Usage("--expect-plan applies to posting commands only")
    host, path, number, _ = parse_pr(parsed.args[0], cfg.get("hosts", ["github.com", "gitlab.com"]))
    pr = f"{host}/{path}#{number}"
    event = parsed.get("--event", "comment")
    assert event is not None
    source: dict[str, Any] | None = None
    if parsed.has("--from"):
        raw = _read_source(call, parsed.get("--from") or "-")
        code, result = check_draft(raw)
        if code != 0:
            raise Usage(
                "the source draft is not valid",
                code="invalid_draft",
                details={
                    "errors": [
                        {"pointer": e["pointer"], "message": e["message"]} for e in result["errors"]
                    ]
                },
            )
        source = json.loads(raw)
        assert source is not None
        if source["pr"] != pr:
            raise Usage("the source draft is for another pull request", code="draft_pr_mismatch",
                        details={"draft_pr": source["pr"], "pr": pr})  # fmt: skip
    with State() as state:
        counter = state["_drafts"] = state.get("_drafts", 0) + 1
    step = next_step(TOOL, "draft new")

    given = source

    def default() -> Out:
        draft = _default_draft(pr, counter, cfg, event)
        if given is not None:
            source = given
            if source["anchor"]["head_sha"] != draft["anchor"]["head_sha"]:
                return error_out(
                    TOOL,
                    20,
                    {"code": "refused", "message": "the head moved", "reason": "stale_head",
                     "details": {"head_sha": draft["anchor"]["head_sha"]}},
                )  # fmt: skip
            for key in ("body", "findings", "unanchored", "meta"):
                if key in source:
                    draft[key] = source[key]
            if not parsed.has("--event"):
                draft["event"] = source["event"]
        return Out(doc=draft)

    return apply_step(TOOL, step, default, call, schema=f"{TOOL}/cli/draft-new.json")


def main(argv: list[str]) -> None:
    run_tool(TOOL, GRAMMAR, handler, argv)
