"""The sanitizer for untrusted text (interface.md, Sanitizer; design D-26 and D-35).

Pure functions, no I/O. One rule set serves code-run output, every untrusted string placed in
a prompt or input file, and every untrusted string printed in human mode (``--json`` keeps raw
text and never calls this module).

Public API:

- ``sanitize(data, *, budget=None, mark_extra="")``: the full rule set, in the interface's
  order. ``data`` is ``str`` or bytes-like. Without a budget it is idempotent and keeps the
  number of LF line breaks (a patch keeps its line numbers).
- ``sanitize_inline(value, limit=200)``: the same rules for one line of human output (a
  pointer, an id, a name); TAB and LF are marked too, and the input is cut to ``limit``
  characters.
- ``has_fence_lookalike(text)``: True when any line's normalized form looks like an
  ``untrusted`` opener or closer. Sanitized text never satisfies it; callers may assert that
  on text they are about to fence.
- ``redact(text, values)``: the code-run safety net that replaces environment values.
- ``LT_CHARS``, ``REDACTED``, ``REDACT_MIN_LENGTH``: constants.

Character classes depend on the running Python's Unicode database (category ``Cn`` is "not
assigned in this version"); the output is stable for one interpreter, which is what
idempotence needs.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from functools import lru_cache

REDACTED = "<redacted>"
REDACT_MIN_LENGTH = 8

# Every character whose NFKD form begins with "<" (the ASCII sign, the fullwidth sign U+FF1C,
# the small sign U+FE64 and the not-less-than sign U+226E). A test scans the whole code point
# space against this list so a Unicode update that adds one fails loudly.
LT_CHARS = "<≮﹤＜"

# Invisible fillers that are not in a category the interface lists (interface.md, Sanitizer).
_FILLERS = frozenset(
    {
        0x034F,
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
)
# todo(#20): schemas/session-meta.json (meta.risk_reasons items, line ~27, and meta.summary,
# line ~35) uses BMP-only literal ranges for the marked characters, so (1) TAG characters
# (U+E0001, U+E0020 to U+E007F), the variation selectors U+E0100 to U+E01EF, other astral Cf
# and lone surrogates are accepted although interface.md says they are excluded; (2) U+2065
# (Cn) is rejected in summary although Cn is allowed there; (3) `$` in risk_reasons allows a
# trailing LF under Python's re. Deferred: not part of this module; fix with the session-claim
# validation.
_MARKED_CATEGORIES = frozenset({"Cf", "Co", "Cs", "Cn", "Zl", "Zp"})
# After NFKD, characters ignored when looking for an opener: all marks and every invisible
# character (a superset of what marking already removed).
_INVISIBLE_CATEGORIES = frozenset({"Mn", "Me", "Mc", "Cf", "Co", "Cs", "Cn", "Cc", "Zl", "Zp"})

# Only characters outside printable ASCII, TAB and LF can need marking.
_NEEDS_CHECK = re.compile(r"[^\x20-\x7e\t\n]")
_MARKER = re.compile(r"<U\+[0-9A-F]{4,6}>")
_MARKER_MAX_LENGTH = 10  # "<U+10FFFF>"
_LT_CHAR = re.compile("[" + re.escape(LT_CHARS) + "]")
# "<", optional whitespace, optional "/" and more whitespace, "untrusted" (the interface's
# ``<\s*/?\s*untrusted`` with the second ``\s*`` only after a "/", so a long whitespace run
# cannot make the match quadratic; the language accepted is the same).
_OPENER = re.compile(r"<\s*(?:/\s*)?untrusted")


def _marker(char: str) -> str:
    return f"<U+{ord(char):04X}>"


@lru_cache(maxsize=4096)
def _is_marked(char: str) -> bool:
    cp = ord(char)
    if char in "\t\n":
        return False
    category = unicodedata.category(char)
    return (
        category in _MARKED_CATEGORIES
        or category == "Cc"
        or (category == "Zs" and char != " ")
        or 0xFE00 <= cp <= 0xFE0F
        or 0xE0100 <= cp <= 0xE01EF
        or cp in _FILLERS
    )


@lru_cache(maxsize=16)
def _mark_pattern(extra: str) -> re.Pattern[str]:
    if not extra:
        return _NEEDS_CHECK
    return re.compile(f"{_NEEDS_CHECK.pattern}|[{re.escape(extra)}]")


def _mark(text: str, extra: str) -> str:
    def replace(match: re.Match[str]) -> str:
        char = match.group()
        return _marker(char) if char in extra or _is_marked(char) else char

    return _mark_pattern(extra).sub(replace, text)


def _to_str(data: str | bytes | bytearray | memoryview) -> str:
    if isinstance(data, str):
        return data
    if isinstance(data, bytes | bytearray | memoryview):
        return bytes(data).decode("utf-8", errors="replace")
    raise TypeError(f"expected str or bytes-like, got {type(data).__name__}")


def _marker_span(text: str, index: int) -> tuple[int, int] | None:
    """The span of the marker that strictly contains the cut before character ``index``."""
    lo = max(0, index - _MARKER_MAX_LENGTH + 1)
    for match in _MARKER.finditer(text, lo, index + _MARKER_MAX_LENGTH - 1):
        if match.start() < index < match.end():
            return match.span()
    return None


def _truncate(text: str, budget: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= budget:
        return text
    half = budget // 2
    # Cut on character boundaries: a partial character at either cut is dropped.
    head = raw[:half].decode("utf-8", errors="ignore")
    tail = raw[len(raw) - half :].decode("utf-8", errors="ignore") if half else ""
    # A cut inside a ``<U+XXXX>`` marker moves out of it: the head ends before the marker, the
    # tail starts after it.
    span = _marker_span(text, len(head))
    if span is not None:
        head = text[: span[0]]
    if tail:
        start = len(text) - len(tail)
        span = _marker_span(text, start)
        if span is not None:
            tail = text[span[1] :]
    omitted = len(raw) - len(head.encode("utf-8")) - len(tail.encode("utf-8"))
    lead = "" if not head or head.endswith("\n") else "\n"
    return f"{head}{lead}[... {omitted} bytes omitted ...]\n{tail}"


@lru_cache(maxsize=4096)
def _ignored_in_normal_form(char: str) -> bool:
    if char.isspace():
        return False
    cp = ord(char)
    return (
        unicodedata.category(char) in _INVISIBLE_CATEGORIES
        or 0xFE00 <= cp <= 0xFE0F
        or 0xE0100 <= cp <= 0xE01EF
        or cp in _FILLERS
    )


def _normal_char(char: str) -> str:
    """NFKD, ignored characters dropped, case folded, NFKD and the drop once more (case folding
    can produce characters that normalize further), for ONE character. A single character's
    NFKD never reorders marks."""

    def keep(text: str) -> str:
        return "".join(c for c in text if not _ignored_in_normal_form(c))

    return keep(unicodedata.normalize("NFKD", keep(unicodedata.normalize("NFKD", char)).casefold()))


class _NormalTable(dict[int, str]):
    """A ``str.translate`` table that computes (and caches) each character's normal form."""

    def __missing__(self, codepoint: int) -> str:
        value = _normal_char(chr(codepoint))
        if len(self) < _TABLE_LIMIT:
            self[codepoint] = value
        return value


_TABLE_LIMIT = 200_000
_NORMAL_TABLE = _NormalTable()


def _normalized(line: str) -> str:
    """The normalized form: every character NFKD-decomposed, every mark (Mn, Mc, Me) and
    invisible character dropped, case folded (then once more, since case folding can produce
    characters that normalize further). It is computed one character at a time: canonical
    reordering of combining marks is quadratic on long runs, and since every mark is dropped
    the order never matters, so the result equals the whole-line form."""
    return line.translate(_NORMAL_TABLE)


def _line_is_lookalike(line: str) -> bool:
    return _LT_CHAR.search(line) is not None and _OPENER.search(_normalized(line)) is not None


def has_fence_lookalike(text: str) -> bool:
    """True when some line of ``text`` has a normalized form matching ``<\\s*/?\\s*untrusted``.

    Works on raw or sanitized text. A line is a run of characters between LF; the sanitizer
    leaves no other line break."""
    if _LT_CHAR.search(text) is None:
        return False
    return any(_line_is_lookalike(line) for line in text.split("\n"))


def _neutralize(text: str) -> str:
    if _LT_CHAR.search(text) is None:
        return text
    lines = text.split("\n")
    for index, line in enumerate(lines):
        if _line_is_lookalike(line):
            lines[index] = _LT_CHAR.sub("&lt;", line)
    return "\n".join(lines)


def sanitize(
    data: str | bytes | bytearray | memoryview,
    *,
    budget: int | None = None,
    mark_extra: str = "",
) -> str:
    """Make untrusted text inert, in the interface's order.

    1. Invalid UTF-8 decodes with replacement (bytes input).
    2. CRLF becomes LF.
    3. Characters of categories Cc (except TAB and LF; a lone CR here), Cf, Co, Cs, Cn, Zl,
       Zp, variation selectors, every Zs other than U+0020 and the listed invisible fillers
       become ``<U+XXXX>``, as does every character in ``mark_extra`` (the manifest uses
       ``"|"``).
    4. With ``budget`` (bytes of the marked text, N below): when longer, only the first and the
       last ``budget // 2`` bytes (on character and marker boundaries) are kept, with the line
       ``[... N bytes omitted ...]`` between them. The result may exceed the budget by that
       line, and step 5 afterwards adds up to 3 bytes per ``<``-like character of a matching
       line; a second pass with a budget is not a no-op. A caller that needs a hard cap must
       measure the result.
    5. Last, line by line: a line whose normalized form matches ``<\\s*/?\\s*untrusted`` has
       every character whose NFKD form begins with ``<`` replaced by ``&lt;``. Other lines
       are unchanged.

    Linear in the input size. Without ``budget`` the function is idempotent."""
    if budget is not None:
        if isinstance(budget, bool) or not isinstance(budget, int):
            raise TypeError(f"budget must be an int or None, got {type(budget).__name__}")
        if budget < 0:
            raise ValueError("budget must not be negative")
    text = _to_str(data).replace("\r\n", "\n")
    text = _mark(text, mark_extra)
    if budget is not None:
        text = _truncate(text, budget)
    return _neutralize(text)


def sanitize_inline(value: str | bytes | bytearray | memoryview, limit: int = 200) -> str:
    """One line of ``value`` for human output: ``sanitize`` with TAB and LF marked too.

    CRLF becomes LF first, then the input is cut to ``limit`` characters before marking (so a
    marker is never split) and ``...`` follows a cut.
    Use it for pointers, ids and names printed in human mode."""
    if limit < 1:
        raise ValueError("limit must be at least 1")
    text = _to_str(value).replace("\r\n", "\n")
    cut = len(text) > limit
    body = sanitize(text[:limit], mark_extra="\n\t")
    return body + "..." if cut else body


def redact(text: str, values: Iterable[str]) -> str:
    """Replace every occurrence of each value of at least 8 characters with ``<redacted>``.

    Meant for code-run output after ``sanitize`` and before the output enters the prompt
    (interface.md, Code run, step 5): a safety net for accidents. A value matches in its raw
    form and in its sanitized form (so a secret holding CR, a marked character or
    ``<untrusted`` is found in sanitized text); any other transformation (encoded, split)
    passes. The 8-character minimum applies to the raw value. The matches of all values are
    merged where they overlap or touch and each merged span is replaced once, so no tail of a
    partially overlapping value is left and a replacement is never rescanned. The replacement
    holds no whitespace and no ``untrusted``, so it cannot create a fence look-alike.

    ``values`` must be a collection of strings, not one string."""
    if isinstance(values, str | bytes | bytearray):
        raise TypeError("values must be a collection of strings, not a single string")
    patterns: set[str] = set()
    for value in values:
        if len(value) >= REDACT_MIN_LENGTH:
            patterns.add(value)
            patterns.add(sanitize(value))
    patterns.discard("")
    if not patterns or not text:
        return text
    spans: list[tuple[int, int]] = []
    for pattern in patterns:
        start = text.find(pattern)
        while start != -1:
            spans.append((start, start + len(pattern)))
            start = text.find(pattern, start + 1)
    if not spans:
        return text
    spans.sort()
    parts: list[str] = []
    position = 0
    current_start, current_end = spans[0]
    for start, end in spans[1:]:
        if start <= current_end:
            current_end = max(current_end, end)
            continue
        parts.extend((text[position:current_start], REDACTED))
        position = current_end
        current_start, current_end = start, end
    parts.extend((text[position:current_start], REDACTED, text[current_end:]))
    return "".join(parts)
