"""Exit codes, the error envelope and its exit-code mapping (interface.md, Exit codes, Reasons).

The envelope shape is ``schemas/cli/error.json``; ``code`` is one of the closed list of
``schemas/defs.json`` ``error_code``. Messages hold closed codes, ids and counts only, never
untrusted text.
"""

from __future__ import annotations

import enum
import unicodedata
from typing import Any

PROG = "review-run"
_HUMAN_MAX = 200


class ExitCode(enum.IntEnum):
    """Every exit code review-run uses."""

    OK = 0
    INTERNAL = 1
    USAGE = 2
    NOT_FOUND = 10
    VETOED = 11
    FAILED = 12
    ESCALATED = 13
    REFUSED = 20
    AMBIGUOUS = 21
    ARTIFACT = 22
    SIGINT = 130
    SIGTERM = 143


# Envelope code (schemas/defs.json error_code) -> exit code. Exits 14 to 18 and 22 belong to a
# run's outcome and are printed as the run object, never as an envelope.
ERROR_EXIT: dict[str, int] = {
    "usage": ExitCode.USAGE,
    "config_invalid": ExitCode.USAGE,
    "not_found": ExitCode.NOT_FOUND,
    "ambiguous": ExitCode.AMBIGUOUS,
    "vetoed": ExitCode.VETOED,
    "escalated": ExitCode.ESCALATED,
    "refused": ExitCode.REFUSED,
    "failed": ExitCode.FAILED,
    "internal": ExitCode.INTERNAL,
}


def printable(text: str, limit: int = _HUMAN_MAX) -> str:
    """One line of ``text`` safe for a terminal: control, format, separator-line and unassigned
    characters become ``?`` and the result is cut to ``limit`` characters."""
    out = []
    for ch in text[:limit]:
        category = unicodedata.category(ch)
        out.append("?" if category[0] == "C" or category in ("Zl", "Zp") else ch)
    return "".join(out)


class ReviewRunError(Exception):
    """A failure that ends the command with an error envelope."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        reason: str | None = None,
        details: dict[str, Any] | None = None,
        tool: str | None = None,
        lower_code: str | None = None,
        hook: str | None = None,
        decision_ids: list[str] | None = None,
        human_detail: str | None = None,
    ) -> None:
        if code not in ERROR_EXIT:
            raise ValueError(f"unknown error code: {code!r}")
        super().__init__(message)
        self.code = code
        self.message = message
        self.reason = reason
        self.details = details
        self.tool = tool
        self.lower_code = lower_code
        self.hook = hook
        self.decision_ids = decision_ids
        # Stderr only (sanitized when printed); never part of the envelope.
        self.human_detail = human_detail

    @property
    def exit_code(self) -> int:
        return int(ERROR_EXIT[self.code])

    def envelope(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        for key in ("reason", "details", "tool", "lower_code", "hook", "decision_ids"):
            value = getattr(self, key)
            if value is not None:
                error[key] = value
        return {"error": error}

    def human(self) -> str:
        """The one-line stderr message."""
        return f"{PROG}: {self.code}: {printable(self.human_detail or self.message)}"
