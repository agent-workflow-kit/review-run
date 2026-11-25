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
_MARKED_CATEGORIES = frozenset({"Cf", "Co", "Cs", "Cn", "Zl", "Zp"})
# After NFKD, characters ignored when looking for an opener: all marks and every invisible
# character (a superset of what marking already removed).
_INVISIBLE_CATEGORIES = frozenset({"Mn", "Me", "Mc", "Cf", "Co", "Cs", "Cn", "Cc", "Zl", "Zp"})

# Only characters outside printable ASCII, TAB and LF can need marking.
_NEEDS_CHECK = re.compile(r"[^\x20-\x7e\t\n]")
_NON_ASCII = re.compile(r"[^\x00-\x7f]")
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


def _truncate(text: str, budget: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= budget:
        return text
    half = budget // 2
    # Cut on character boundaries: a partial character at either cut is dropped.
    head = raw[:half].decode("utf-8", errors="ignore")
    tail = raw[len(raw) - half :].decode("utf-8", errors="ignore") if half else ""
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


def _drop_ignored(text: str) -> str:
    return _NON_ASCII.sub(lambda m: "" if _ignored_in_normal_form(m.group()) else m.group(), text)


def _normalized(line: str) -> str:
    """NFKD, marks and invisible characters dropped, case folded (then once more, since case
    folding can produce characters that normalize further)."""
    text = _drop_ignored(unicodedata.normalize("NFKD", line)).casefold()
    return _drop_ignored(unicodedata.normalize("NFKD", text))


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
    4. With ``budget`` (bytes of the marked text): when longer, only the first and the last
       ``budget // 2`` bytes (on character boundaries) are kept, with the line
       ``[... N bytes omitted ...]`` between them. The result may exceed the budget by that
       line, and a second pass with a budget is not a no-op.
    5. Last, line by line: a line whose normalized form matches ``<\\s*/?\\s*untrusted`` has
       every character whose NFKD form begins with ``<`` replaced by ``&lt;``. Other lines
       are unchanged.

    Linear in the input size. Without ``budget`` the function is idempotent."""
    if budget is not None and budget < 0:
        raise ValueError("budget must not be negative")
    text = _to_str(data).replace("\r\n", "\n")
    text = _mark(text, mark_extra)
    if budget is not None:
        text = _truncate(text, budget)
    return _neutralize(text)


def sanitize_inline(value: str | bytes | bytearray | memoryview, limit: int = 200) -> str:
    """One line of ``value`` for human output: ``sanitize`` with TAB and LF marked too.

    The input is cut to ``limit`` characters before marking (so a marker is never split) and
    ``...`` follows a cut. Use it for pointers, ids and names printed in human mode."""
    if limit < 1:
        raise ValueError("limit must be at least 1")
    text = _to_str(value)
    cut = len(text) > limit
    body = sanitize(text[:limit], mark_extra="\n\t")
    return body + "..." if cut else body


def redact(text: str, values: Iterable[str]) -> str:
    """Replace every occurrence of each value of at least 8 characters with ``<redacted>``.

    Meant for code-run output after ``sanitize`` and before the output enters the prompt
    (interface.md, Code run, step 5): a safety net for accidents, so a value that was
    transformed (encoded, split, or holding a character the sanitizer marked) passes it. One
    pass, longest value first at each position, so a replacement is never rescanned and a
    value that is a prefix of another leaves no tail. The replacement holds no whitespace
    and no ``untrusted``, so it cannot create a fence look-alike."""
    secrets = sorted({v for v in values if len(v) >= REDACT_MIN_LENGTH}, key=len, reverse=True)
    if not secrets or not text:
        return text
    pattern = re.compile("|".join(re.escape(s) for s in secrets))
    return pattern.sub(REDACTED, text)
