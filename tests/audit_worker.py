"""A child process for the audit tests (never imported; run as a script).

``python audit_worker.py <state root> <go file> <count> <tag> [slow]`` waits (bounded) for the go
file, then appends ``count`` audit records whose ``attempt_id`` is ``<tag>-<n>``-shaped and whose
``inputs.older_than`` is ``0``. With ``slow`` it sleeps between records so a test can kill it
between or during writes.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from review_run.audit import AuditWriter, Caller


def main() -> None:
    root, go, count, tag = Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
    slow = len(sys.argv) > 5
    deadline = time.monotonic() + 20
    while not go.exists():
        if time.monotonic() > deadline:
            raise SystemExit("go file never appeared")
        time.sleep(0.002)
    writer = AuditWriter(root)
    caller = Caller("auto:worker", "flag", False)
    for n in range(count):
        writer.record(
            "prune",
            caller=caller,
            run_id=None,
            pr=None,
            inputs={"older_than": "0"},
            result={"status": "done", "deleted_runs": n},
            attempt_id=f"{tag}-{n:05d}",
        )
        print(n, flush=True)
        if slow:
            time.sleep(0.01)


main()
