"""The lease keeper: renews a run's lease from a thread of its own while the main thread is
blocked in a call (shared by ``run`` and the finalizers of ``show``, ``wait`` and ``reconcile``)."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Final

from .store import LEASE_RENEW_SECONDS, Lease, Store

RENEW_FAILURES_LIMIT: Final = 3  # consecutive failed renewals (TTL 300 s, interval 60 s)


class LeaseKeeper:
    """Renews the run's lease from a thread of its own (its own connection) every
    ``interval`` seconds while the main thread is blocked in a call. It sets ``lost`` and stops
    when ``renew_lease`` returns ``None`` (the lease was taken over) or after
    ``RENEW_FAILURES_LIMIT`` consecutive failures (a transient error such as a locked database
    is retried at the next tick)."""

    def __init__(
        self, root: Path, lease: Lease, interval: float = float(LEASE_RENEW_SECONDS)
    ) -> None:
        self.root = Path(root)
        self.lease = lease
        self.interval = interval
        self.lost = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="review-run-lease", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=30)

    def _loop(self) -> None:
        store: Store | None = None
        failures = 0
        try:
            while not self._stop.wait(self.interval):
                try:
                    if store is None:
                        store = Store.open(self.root)
                    renewed = store.renew_lease(self.lease)
                except Exception:  # transient: retried at the next tick
                    failures += 1
                    if failures >= RENEW_FAILURES_LIMIT:
                        self.lost.set()
                        return
                    continue
                if renewed is None:
                    self.lost.set()
                    return
                failures = 0
        finally:
            if store is not None:
                try:
                    store.close()
                except Exception:  # nothing to do for a connection that will not close
                    pass


__all__ = ["RENEW_FAILURES_LIMIT", "LeaseKeeper"]
