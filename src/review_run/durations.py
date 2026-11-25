"""The duration grammar (interface.md, Global flags; ``schemas/defs.json`` ``duration``).

A duration is ``0`` or an integer with a unit ``ms``, ``s``, ``m``, ``h`` or ``d``. Messages never
include the rejected text.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Final

_DURATION_RE: Final = re.compile(r"(0|[0-9]+)(ms|s|m|h|d)?", re.ASCII)
_RFC3339_RE: Final = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?"
    r"(?:Z|[+-][0-9]{2}:[0-9]{2})",
    re.ASCII,
)
_UNIT_MS: Final = {"ms": 1, "s": 1000, "m": 60_000, "h": 3_600_000, "d": 86_400_000}
_MAX_MS: Final = 100 * 365 * 86_400_000
_MAX_DIGITS: Final = 15  # more digits cannot be within the cap; never int() them


class DurationError(ValueError):
    """The text is not a valid duration."""


def parse_duration(text: str, *, positive: bool = False) -> dt.timedelta:
    """Parse ``text``; ``positive`` additionally rejects a zero duration (``--timeout``)."""
    match = _DURATION_RE.fullmatch(text)
    if match is None or (match.group(2) is None and text != "0"):
        raise DurationError("a duration is 0 or an integer with a unit: ms, s, m, h or d")
    digits = match.group(1)
    if len(digits.lstrip("0")) > _MAX_DIGITS:
        raise DurationError("a duration is at most 100 years")
    millis = int(digits) * _UNIT_MS[match.group(2) or "s"]
    if millis > _MAX_MS:
        raise DurationError("a duration is at most 100 years")
    if positive and millis == 0:
        raise DurationError("a duration here must be greater than 0")
    return dt.timedelta(milliseconds=millis)


def parse_since(text: str) -> dt.timedelta | dt.datetime:
    """``--since``: a duration (an age) or an RFC 3339 timestamp (returned in UTC)."""
    if _RFC3339_RE.fullmatch(text):
        try:
            return dt.datetime.fromisoformat(text).astimezone(dt.UTC)
        except (ValueError, OverflowError):
            raise DurationError("not a valid RFC 3339 timestamp") from None
    return parse_duration(text)
