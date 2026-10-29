"""Gateway for review-run: the single boundary for subprocess and HTTP calls.

Every call to an external program (sibling awkit tools, git, gh, glab, claude, ...) or an HTTP
endpoint goes through this module, so tests can substitute fakes (e.g. via tests/fakebin/).
Commands are always passed as argv lists, never through a shell. Untrusted or agent-generated
text must go through ``stdin`` or files, never through argv or env.
"""

from __future__ import annotations

import subprocess


def run(
    argv: list[str],
    *,
    stdin: str | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``argv`` without a shell and capture its text output."""
    return subprocess.run(
        argv,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
