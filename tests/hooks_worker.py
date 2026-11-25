"""A child process for the hook delivery tests (never imported; run as a script).

``python hooks_worker.py <state root> <out file> crash`` runs one delivery pass whose hook appends
its event id to ``<out file>`` and which SIGKILLs itself when the pass tries to mark the event
delivered: the crash between delivery and ``mark_event``.
"""

from __future__ import annotations

import os
import signal
import sys
from pathlib import Path

from hooks_helpers import PY, appender

from review_run import hooks
from review_run.audit import AuditWriter, LogWriter
from review_run.store import Store


def main() -> None:
    root, out = Path(sys.argv[1]), Path(sys.argv[2])
    with Store.open(root) as store:
        original = store.mark_event

        def mark(event_id: str, **kw: bool) -> None:
            if kw.get("delivered"):
                os.kill(os.getpid(), signal.SIGKILL)
            original(event_id, **kw)  # type: ignore[arg-type]

        store.mark_event = mark  # type: ignore[method-assign]
        hooks.deliver_pending(
            store,
            AuditWriter(root),
            LogWriter(root),
            {"post-run": [{"cmd": appender(out)}]},
            base_env={},
            shell=(PY, "-c"),
        )


if __name__ == "__main__":
    main()
