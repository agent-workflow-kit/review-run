"""Artifact validation: finalize steps 6 to 10 (issue #20; interface.md, Finalize, Session claims
and Normalization).

The artifact is the session's output, so every byte of it is untrusted. This module turns it into
a ``Validated`` draft or raises one of two exceptions, and never puts a value of the artifact (or
any text of a sibling) in an exception or an error object:

* ``Invalid``: the run ends ``artifact_invalid``; ``errors`` holds at most 20 ``{pointer, kind}``
  objects (a pointer is JSON-pointer text of at most 200 characters, a kind a closed word).
* ``CheckFailed``: ``pr-comment draft check`` failed in a way that is not a verdict on the draft
  (the run ends ``internal`` with ``failure.tool: pr-comment`` and this ``lower_code``).

Steps:

6. ``parse_artifact``: bytes to one JSON object. At most ``max_bytes``; UTF-8 without a BOM; no
   duplicate keys, ``NaN``, ``Infinity`` or float literal that overflows; integer literals of at
   most 20 characters and float literals of at most 64 (so a 5000-digit integer never reaches
   Python's conversion); no lone surrogate in a key or a string; nothing after the object; nesting
   at most 32. Depth is checked on the parsed value with an explicit stack (the C scanner is
   already bounded by the interpreter's recursion limit, whose ``RecursionError`` is the same
   ``depth`` error), so no pre-scan of the text is needed and nothing is quadratic.
7. ``extract_claims``: the session's ``meta`` against the table of Session claims
   (``schemas/session-meta.json`` expresses its structure; the character rules are applied here
   with the sanitizer's own marked set, because a regular expression of literal BMP ranges cannot
   express them).
8. ``apply_identity``: ``schema_version``, ``draft_id``, ``pr`` and ``anchor`` from ``draft new``.
9. ``normalize``: the closed list of lossless fixes, in order, with the ``normalized`` entries.
10. ``run_check``: ``pr-comment draft check - --json`` on the canonical form of the normalized
    draft without ``meta`` (stdin only; the draft never reaches argv). The form sent is compact,
    sorted-keys UTF-8 (not the ASCII-escaped form of ``review.json``, which would inflate
    non-ASCII text up to six times against the 8 MiB stdin cap); the ``length`` check measures
    its UTF-8 bytes. More than 500 findings is ``schema`` at ``/findings`` before normalization.

``validate`` runs all of them. It does not derive ``meta`` or write ``review.json`` (steps 11 to
13, issue #21), which take its ``Validated`` result.

Kinds of ``Invalid`` errors (all closed words): parse errors ``length``, ``encoding``, ``syntax``,
``duplicate_key``, ``non_finite``, ``number``, ``depth``, ``surrogate`` and ``type`` (not an
object), all at pointer ``""``; claim errors ``meta_missing``, ``type``, ``enum``, ``length``,
``count`` and ``pattern`` (``count`` also for a duplicate file id); ``draft check`` errors
``schema``, ``control_character`` and ``suggestion_fence``.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from . import gateway
from .prefetch import lower_failure, strict_json
from .sanitizer import is_marked

MAX_DEPTH: Final = 32
MAX_ERRORS: Final = 20
MAX_POINTER: Final = 200
MAX_NORMALIZED: Final = 200
MAX_INT_CHARS: Final = 20
MAX_FLOAT_CHARS: Final = 64
MAX_FINDINGS: Final = 500
CHECK_TIMEOUT: Final = 300.0
CHECK_ARGV: Final = ("pr-comment", "draft", "check", "-", "--json")

VERDICTS: Final = frozenset({"approve", "comment", "request_changes"})
RISKS: Final = frozenset({"low", "medium", "high"})
SUMMARY_MAX: Final = 2000
REASON_MAX: Final = 200
REASONS_MAX: Final = 10
_FILE_ID = re.compile(r"f[0-9]{4}")
_POINTER = re.compile(r"(?:/.*)?", re.DOTALL)
CHECK_KINDS: Final = frozenset({"control_character", "suggestion_fence"})

# The review-draft schema's properties (pinned pr-comment 0.39.0; tests compare these sets with
# the pinned schema). ``meta`` is never normalized.
DRAFT_KEYS: Final = frozenset(
    {
        "schema_version",
        "draft_id",
        "pr",
        "anchor",
        "event",
        "body",
        "findings",
        "unanchored",
        "meta",
    }
)
FINDING_KEYS: Final = frozenset(
    {"id", "path", "line", "start_line", "side", "severity", "title", "body", "suggestion"}
)
SUGGESTION_KEYS: Final = frozenset({"lines"})
NULLABLE_FINDING_FIELDS: Final = ("title", "severity", "start_line", "side", "suggestion")
BLOCKING_SEVERITIES: Final = ("blocker", "major")


class Invalid(Exception):
    """The artifact is not acceptable: ``errors`` are ``{pointer, kind}`` objects only."""

    def __init__(self, errors: Sequence[Mapping[str, str]]) -> None:
        super().__init__("artifact_invalid")
        self.errors = tuple(dict(e) for e in errors[:MAX_ERRORS])


def _invalid(kind: str, pointer: str = "") -> Invalid:
    return Invalid([{"pointer": pointer, "kind": kind}])


class CheckFailed(Exception):
    """``pr-comment draft check`` could not give a verdict; ``lower_code`` is a closed word."""

    def __init__(self, lower_code: str) -> None:
        super().__init__("draft check failed")
        self.lower_code = lower_code


# -- pointers ------------------------------------------------------------------------------------


def pointer_of(*tokens: str | int) -> str:
    """A JSON pointer (RFC 6901 escapes) cut to ``MAX_POINTER`` characters."""
    text = "".join("/" + str(t).replace("~", "~0").replace("/", "~1") for t in tokens)
    return text[:MAX_POINTER]


def _clean_pointer(value: object) -> str:
    """A pointer from a sibling's output: a string of pointer shape, cut to ``MAX_POINTER`` and
    printable ASCII only (every other character becomes ``?``); anything else is the root
    pointer."""
    if not isinstance(value, str) or not _POINTER.fullmatch(value):
        return ""
    return "".join(c if " " <= c <= "~" else "?" for c in value[:MAX_POINTER])


# -- step 6: strict parse --------------------------------------------------------------------------


class _Reject(ValueError):
    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


def _constant(_name: str) -> Any:
    raise _Reject("non_finite")


def _parse_int(text: str) -> int:
    if len(text) > MAX_INT_CHARS:
        raise _Reject("number")
    return int(text)


def _parse_float(text: str) -> float:
    if len(text) > MAX_FLOAT_CHARS:
        raise _Reject("number")
    value = float(text)
    if not math.isfinite(value):
        raise _Reject("non_finite")
    return value


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out = dict(pairs)
    if len(out) != len(pairs):
        raise _Reject("duplicate_key")
    return out


def _check_tree(root: Any) -> None:
    """Depth at most ``MAX_DEPTH`` and no lone surrogate in any key or string, iteratively."""
    stack: list[tuple[Any, int]] = [(root, 1)]
    while stack:
        node, depth = stack.pop()
        if depth > MAX_DEPTH:
            raise _invalid("depth")
        if isinstance(node, dict):
            for key in node:
                _check_text(key)
            children: Any = node.values()
        else:
            children = node
        for child in children:
            if isinstance(child, str):
                _check_text(child)
            elif isinstance(child, (dict, list)):
                if depth >= MAX_DEPTH:
                    raise _invalid("depth")
                if child:  # a childless container has no text and no children to walk
                    stack.append((child, depth + 1))


def _check_text(text: str) -> None:
    if text.isascii():
        return
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise _invalid("surrogate") from None


def parse_artifact(data: bytes, *, max_bytes: int) -> dict[str, Any]:
    """Step 6. The one JSON object ``data`` holds, or ``Invalid`` (never any other exception)."""
    if len(data) > max_bytes:
        raise _invalid("length")
    if data.startswith(b"\xef\xbb\xbf"):
        raise _invalid("encoding")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise _invalid("encoding") from None
    try:
        value = json.loads(
            text,
            parse_constant=_constant,
            parse_int=_parse_int,
            parse_float=_parse_float,
            object_pairs_hook=_pairs,
        )
    except _Reject as exc:
        raise _invalid(exc.kind) from None
    except RecursionError:
        raise _invalid("depth") from None
    except ValueError:  # syntax, trailing data, and Python's integer-digit limit
        raise _invalid("syntax") from None
    if not isinstance(value, dict):
        raise _invalid("type")
    _check_tree(value)
    return value


# -- step 7: session claims ----------------------------------------------------------------------


@dataclass(frozen=True)
class Claims:
    """The validated session claims (untrusted text, closed shapes)."""

    verdict: str
    risk: str
    risk_reasons: tuple[str, ...]
    summary: str
    reviewed: tuple[str, ...]


class _Errors:
    def __init__(self) -> None:
        self.items: list[dict[str, str]] = []

    def add(self, pointer: str, kind: str) -> None:
        if len(self.items) < MAX_ERRORS:
            self.items.append({"pointer": pointer, "kind": kind})


def forbidden_in_claim(char: str, *, allow_lf: bool) -> bool:
    """Whether ``char`` may not appear in a claim text: categories Cc (LF allowed in the summary
    only), Cf, Cs, Zl and Zp and the extra characters the sanitizer marks, and U+2060 to U+206F
    whatever their category (``schemas/meta.json`` excludes the block); other ``Co`` and ``Cn``
    characters are allowed (they are marked when printed)."""
    if char == "\n":
        return not allow_lf
    if char == "\t" or "\u2060" <= char <= "\u206f":  # the schema patterns exclude the whole block
        return True
    return is_marked(char) and unicodedata.category(char) not in ("Co", "Cn")


def _text_error(value: Any, *, minimum: int, maximum: int, allow_lf: bool) -> str | None:
    """The error kind of a claim text, or ``None``. The length is checked before any scan, so a
    huge string costs one ``len``."""
    if not isinstance(value, str):
        return "type"
    if not minimum <= len(value) <= maximum:
        return "length"
    if any(forbidden_in_claim(c, allow_lf=allow_lf) for c in value):
        return "pattern"
    return None


def _enum(value: Any, allowed: frozenset[str], pointer: str, errors: _Errors) -> str:
    if not isinstance(value, str):
        errors.add(pointer, "type")
    elif value not in allowed:
        errors.add(pointer, "enum")
    else:
        return value
    return ""


def extract_claims(doc: Mapping[str, Any], *, max_files: int) -> Claims:
    """Step 7: validate ``doc["meta"]`` (a session claim table violation raises ``Invalid`` with
    every violation found, at most 20, in a fixed order). Other keys under ``meta`` and under
    ``meta.coverage`` are ignored."""
    errors = _Errors()
    meta = doc.get("meta", _MISSING)
    if meta is _MISSING:
        raise _invalid("meta_missing", "/meta")
    if not isinstance(meta, dict):
        raise _invalid("type", "/meta")
    verdict = _field(meta, "verdict", errors)
    out_verdict = (
        _enum(verdict, VERDICTS, "/meta/verdict", errors) if verdict is not _MISSING else ""
    )
    risk = _field(meta, "risk", errors)
    out_risk = _enum(risk, RISKS, "/meta/risk", errors) if risk is not _MISSING else ""
    reasons = _field(meta, "risk_reasons", errors)
    out_reasons = _reasons(reasons, errors) if reasons is not _MISSING else ()
    summary = _field(meta, "summary", errors)
    out_summary = ""
    if summary is not _MISSING:
        kind = _text_error(summary, minimum=1, maximum=SUMMARY_MAX, allow_lf=True)
        if kind is None:
            out_summary = summary
        else:
            errors.add("/meta/summary", kind)
    coverage = _field(meta, "coverage", errors)
    out_reviewed = _coverage(coverage, errors, max_files) if coverage is not _MISSING else ()
    if errors.items:
        raise Invalid(errors.items)
    return Claims(out_verdict, out_risk, out_reasons, out_summary, out_reviewed)


class _Missing:
    pass


_MISSING: Final = _Missing()


def _field(meta: Mapping[str, Any], name: str, errors: _Errors) -> Any:
    if name not in meta:
        errors.add(f"/meta/{name}", "meta_missing")
        return _MISSING
    return meta[name]


def _reasons(value: Any, errors: _Errors) -> tuple[str, ...]:
    if not isinstance(value, list):
        errors.add("/meta/risk_reasons", "type")
        return ()
    if len(value) > REASONS_MAX:
        errors.add("/meta/risk_reasons", "count")
        return ()
    bad = False
    for index, item in enumerate(value):
        kind = _text_error(item, minimum=1, maximum=REASON_MAX, allow_lf=False)
        if kind is not None:
            errors.add(pointer_of("meta", "risk_reasons", index), kind)
            bad = True
    return () if bad else tuple(value)


def _coverage(value: Any, errors: _Errors, max_files: int) -> tuple[str, ...]:
    if not isinstance(value, dict):
        errors.add("/meta/coverage", "type")
        return ()
    if "reviewed" not in value:
        errors.add("/meta/coverage/reviewed", "meta_missing")
        return ()
    reviewed = value["reviewed"]
    if not isinstance(reviewed, list):
        errors.add("/meta/coverage/reviewed", "type")
        return ()
    if len(reviewed) > max_files:
        errors.add("/meta/coverage/reviewed", "count")
        return ()
    seen: set[str] = set()
    bad = False
    for index, item in enumerate(reviewed):
        pointer = pointer_of("meta", "coverage", "reviewed", index)
        if not isinstance(item, str):
            errors.add(pointer, "type")
            bad = True
        elif not _FILE_ID.fullmatch(item):
            errors.add(pointer, "pattern")
            bad = True
        elif item in seen:
            errors.add(pointer, "count")
            bad = True
        else:
            seen.add(item)
    return () if bad else tuple(reviewed)


# -- step 8: identity ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Identity:
    """What ``pr-comment draft new`` gave: the values the stored draft carries."""

    draft_id: str
    pr: str
    anchor: Mapping[str, str | None]


def apply_identity(doc: dict[str, Any], identity: Identity) -> None:
    """Step 8: replace whatever the session wrote in ``schema_version``, ``draft_id``, ``pr`` and
    ``anchor`` (all four keys, in place)."""
    doc["schema_version"] = 1
    doc["draft_id"] = identity.draft_id
    doc["pr"] = identity.pr
    doc["anchor"] = {
        "head_sha": identity.anchor["head_sha"],
        "base_ref": identity.anchor["base_ref"],
        "base_sha": identity.anchor["base_sha"],
        "merge_base_sha": identity.anchor["merge_base_sha"],
    }


# -- step 9: normalization -----------------------------------------------------------------------


def _drop_unknown(node: dict[str, Any], pointer: str, known: frozenset[str]) -> tuple[str, int]:
    extra = [k for k in node if k not in known]
    for key in extra:
        del node[key]
    return pointer, len(extra)


def normalize(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """Step 9, in place: the closed list of lossless fixes of Normalization, on a draft that
    already carries the identity and has no ``meta``. Returns the ``normalized`` entries
    (``{rule, pointer, count}``, at most 200 and a final ``truncated`` entry when more were
    applied). A normalized draft normalizes to itself with no entries.

    Entries are listed by rule in the table's order, each rule in document order, one per parent
    object. A pointer is built from fixed keys and indexes only. ``unanchored`` is removed whole
    (``drop_unanchored``) and its content is not normalized first."""
    found: list[tuple[str, str, int]] = []
    pairs: list[tuple[str, int]] = []
    pairs.append(_drop_unknown(doc, "", DRAFT_KEYS))
    findings = doc.get("findings")
    items: list[tuple[int, dict[str, Any]]] = []
    if isinstance(findings, list):
        items = [(i, f) for i, f in enumerate(findings) if isinstance(f, dict)]
    for index, finding in items:
        pairs.append(_drop_unknown(finding, pointer_of("findings", index), FINDING_KEYS))
        suggestion = finding.get("suggestion")
        if isinstance(suggestion, dict):
            where = pointer_of("findings", index, "suggestion")
            pairs.append(_drop_unknown(suggestion, where, SUGGESTION_KEYS))
    found += [("drop_unknown", p, n) for p, n in pairs if n]
    for index, finding in items:
        dropped = [k for k in NULLABLE_FINDING_FIELDS if k in finding and finding[k] is None]
        for key in dropped:
            del finding[key]
        if dropped:
            found.append(("drop_null", pointer_of("findings", index), len(dropped)))
    if "unanchored" in doc:
        del doc["unanchored"]
        found.append(("drop_unanchored", "", 1))
    event = doc.get("event")
    blocking = any(f.get("severity") in BLOCKING_SEVERITIES for _, f in items)
    if event == "approve" or (event == "request_changes" and not blocking):
        doc["event"] = "comment"
        found.append(("event_downgrade", "", 1))
    entries = [{"rule": r, "pointer": p, "count": n} for r, p, n in found[:MAX_NORMALIZED]]
    if len(found) > MAX_NORMALIZED:
        entries.append({"rule": "truncated", "pointer": "", "count": len(found) - MAX_NORMALIZED})
    return entries


def canonical_json(value: object, *, ensure_ascii: bool = True) -> str:
    """The deterministic form of a JSON value: sorted keys, no whitespace; ASCII with ``\\u``
    escapes by default (``review.json``), UTF-8 text with ``ensure_ascii=False`` (what the draft
    check receives, so the stdin cap is not inflated by escapes)."""
    return json.dumps(
        value, ensure_ascii=ensure_ascii, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


# -- step 10: draft check ------------------------------------------------------------------------


def _check_errors(doc: Mapping[str, Any]) -> list[dict[str, str]]:
    raw = doc.get("errors")
    if not isinstance(raw, list) or not raw:
        raise CheckFailed("invalid_output")
    out: list[dict[str, str]] = []
    for item in raw[:MAX_ERRORS]:
        kind = item.get("kind") if isinstance(item, dict) else None
        pointer = item.get("pointer") if isinstance(item, dict) else None
        out.append(
            {
                "pointer": _clean_pointer(pointer),
                "kind": kind if isinstance(kind, str) and kind in CHECK_KINDS else "schema",
            }
        )
    return out


def run_check(draft_json: str) -> None:
    """Step 10: ``pr-comment draft check - --json`` with ``draft_json`` on stdin. Returns when the
    draft is valid (exit 0); raises ``Invalid`` for exit 2 with ``valid: false`` (the first 20
    errors as ``{pointer, kind}``, ``kind`` ``schema`` unless pr-comment names one of its two text
    kinds, never the message) and ``CheckFailed`` for anything else."""
    if len(draft_json) > gateway.MAX_OUTPUT or (
        len(draft_json.encode("utf-8")) > gateway.MAX_OUTPUT  # stdin is capped in bytes
    ):
        raise _invalid("length")
    try:
        done = gateway.run(list(CHECK_ARGV), timeout=CHECK_TIMEOUT, stdin=draft_json)
    except gateway.GatewayError as exc:
        raise CheckFailed(exc.lower_code) from None
    if done.returncode not in (0, 2):
        raise CheckFailed(lower_failure("pr-comment", done).lower_code)
    try:
        doc = strict_json(done.stdout)
    except ValueError:
        raise CheckFailed("invalid_output") from None
    if not isinstance(doc, dict) or type(doc.get("valid")) is not bool:  # noqa: E721
        # exit 2 with an error envelope (not a verdict): its closed code, never its message
        code = lower_failure("pr-comment", done).lower_code if done.returncode == 2 else None
        raise CheckFailed(code or "invalid_output")
    if done.returncode == 0:
        if doc["valid"] is not True or doc.get("errors") != []:
            raise CheckFailed("invalid_output")
        return
    if doc["valid"] is not False:
        raise CheckFailed("invalid_output")
    raise Invalid(_check_errors(doc))


# -- all together --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Validated:
    """What steps 6 to 10 produce: the normalized draft (identity set, no ``meta``), the session's
    claims and the ``normalized`` entries, for meta derivation (#21)."""

    draft: dict[str, Any]
    claims: Claims
    normalized: tuple[dict[str, Any], ...]


def validate(original: bytes, *, max_bytes: int, max_files: int, identity: Identity) -> Validated:
    """Steps 6 to 10 on the verified copy ``original``. Raises ``Invalid`` or ``CheckFailed``."""
    doc = parse_artifact(original, max_bytes=max_bytes)
    claims = extract_claims(doc, max_files=max_files)
    doc.pop("meta", None)
    apply_identity(doc, identity)
    findings = doc.get("findings")
    if isinstance(findings, list) and len(findings) > MAX_FINDINGS:
        # pr-comment's schema error (pointer /findings), reported before normalization costs
        # anything on a flood of findings; ``unanchored`` is dropped whole, so only these count
        raise _invalid("schema", "/findings")
    entries = normalize(doc)
    run_check(canonical_json(doc, ensure_ascii=False))
    return Validated(doc, claims, tuple(entries))


__all__ = [
    "CheckFailed",
    "Claims",
    "Identity",
    "Invalid",
    "Validated",
    "apply_identity",
    "canonical_json",
    "extract_claims",
    "forbidden_in_claim",
    "normalize",
    "parse_artifact",
    "pointer_of",
    "run_check",
    "validate",
]
