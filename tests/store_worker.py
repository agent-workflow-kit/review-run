"""A child process for the store's multiprocess tests (never imported; run as a script).

``python store_worker.py <mode> <state root> <go file> [args...]``. Every mode waits (bounded)
for the go file so all workers start their contended step together, then prints one JSON line.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from review_run import procid
from review_run.store import Expect, Store, lease_name

KEY = Expect("running", "prefetch", None)


def wait_for(path: Path, limit: float = 20.0) -> None:
    deadline = time.monotonic() + limit
    while not path.exists():
        if time.monotonic() > deadline:
            raise SystemExit("go file never appeared")
        time.sleep(0.002)


def main() -> None:
    mode, root, go = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
    args = sys.argv[4:]
    out: dict[str, object] = {"pid": procid.current().pid}
    if mode == "open":
        wait_for(go)
        with Store.open(root) as store:
            out["version"] = store.schema_version()
    elif mode in ("finalize", "cancel"):
        run_id = args[0]
        with Store.open(root) as store:
            wait_for(go)
            if mode == "finalize":
                out["won"] = store.begin_finalize(run_id, KEY)
            else:
                out["won"] = store.request_cancel(
                    run_id, KEY, actor=f"w{out['pid']}", exit_code=143
                )
    elif mode == "lease":
        name = args[0]
        with Store.open(root) as store:
            wait_for(go)
            lease = store.acquire_lease(name)
            out["won"] = lease is not None
            if lease is not None:
                out["token"] = lease.token
                time.sleep(float(args[1]))  # hold it (alive) while the others try
    elif mode == "reserve":
        count, index = int(args[0]), int(args[1])
        # Three ids every worker tries first (they collide), then ids no other worker has.
        own = [f"rr-{chr(98 + index)}bbbbbb{chr(97 + i)}" for i in range(count)]
        pool = iter(["rr-aaaaaaaa", "rr-aaaaaaab", "rr-aaaaaaac", *own])
        with Store.open(root, id_generator=lambda: next(pool)) as store:
            wait_for(go)
            out["ids"] = [store.reserve_id() for _ in range(count)]
    elif mode == "tx-then-hang":  # killed by the parent in the middle of a transaction
        run_id, ready = args[0], Path(args[1])
        with Store.open(root) as store, store.transaction() as conn:
            conn.execute("UPDATE runs SET phase = 'code_run' WHERE run_id = ?", (run_id,))
            ready.write_text("in transaction")
            time.sleep(60)
    elif mode == "hold-lease-then-hang":
        run_id, ready = args[0], Path(args[1])
        with Store.open(root) as store:
            assert store.acquire_lease(lease_name(run_id)) is not None
            ready.write_text("lease held")
            time.sleep(60)
    else:
        raise SystemExit(f"unknown mode {mode}")
    print(json.dumps(out), flush=True)


if __name__ == "__main__":
    main()
