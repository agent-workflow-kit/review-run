"""Real concurrent processes against one ``state.db``.

Every child is a process this test started (recorded by the autouse ``proc_tracker``, which kills
exactly those at teardown). Outcomes are read from each child's own printed result, never from
the order of any log. Workers are few (4 to 6) and short; nothing here loops under load.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from review_run.store import Expect, NewRun, Store, lease_name

WORKER = Path(__file__).resolve().parent / "store_worker.py"
KEY = Expect("running", "prefetch", None)
WORKERS = 6


def spawn(mode: str, root: Path, go: Path, *args: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, str(WORKER), mode, str(root), str(go), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def collect(procs: list[subprocess.Popen[str]]) -> list[dict[str, Any]]:
    results = []
    for proc in procs:
        stdout, stderr = proc.communicate(timeout=60)
        assert proc.returncode == 0, stderr
        results.append(json.loads(stdout.strip().splitlines()[-1]))
    return results


def make_run(root: Path) -> str:
    with Store.open(root) as store:
        run_id = store.reserve_id()
        store.create_run(NewRun(run_id, "host/o/r#1", "host/o/r", "a" * 40, "headless", "s"))
    return run_id


def wait_file(path: Path, limit: float = 20.0) -> None:
    deadline = time.monotonic() + limit
    while not path.exists():
        assert time.monotonic() < deadline, f"{path} never appeared"
        time.sleep(0.01)


def test_concurrent_first_open_migrates_once(tmp_path: Path) -> None:
    root, go = tmp_path / "state", tmp_path / "go"
    procs = [spawn("open", root, go) for _ in range(WORKERS)]
    go.write_text("go")
    assert [r["version"] for r in collect(procs)] == [1] * WORKERS


def test_exactly_one_finalizer_wins(tmp_path: Path) -> None:
    root, go = tmp_path / "state", tmp_path / "go"
    run_id = make_run(root)
    procs = [spawn("finalize", root, go, run_id) for _ in range(WORKERS)]
    go.write_text("go")
    wins = [r["won"] for r in collect(procs)]
    assert wins.count(True) == 1
    with Store.open(root) as store:
        run = store.get_run(run_id)
        assert run is not None and run.phase == "finalize" and run.cancel_requested_at is None


def test_exactly_one_canceler_wins(tmp_path: Path) -> None:
    root, go = tmp_path / "state", tmp_path / "go"
    run_id = make_run(root)
    procs = [spawn("cancel", root, go, run_id) for _ in range(WORKERS)]
    go.write_text("go")
    results = collect(procs)
    assert [r["won"] for r in results].count(True) == 1
    winner = next(r["pid"] for r in results if r["won"])
    with Store.open(root) as store:
        run = store.get_run(run_id)
        assert run is not None and run.cancel_actor == f"w{winner}"


def test_finalize_and_cancel_race_has_exactly_one_winner_in_total(tmp_path: Path) -> None:
    root, go = tmp_path / "state", tmp_path / "go"
    run_id = make_run(root)
    procs = [spawn("finalize", root, go, run_id) for _ in range(3)]
    procs += [spawn("cancel", root, go, run_id) for _ in range(3)]
    go.write_text("go")
    results = collect(procs)
    assert [r["won"] for r in results].count(True) == 1
    with Store.open(root) as store:
        run = store.get_run(run_id)
        assert run is not None
        # never both: a finalizing run has no cancellation and a canceled one never finalizes
        assert (run.phase == "finalize") != (run.cancel_requested_at is not None)


def test_exactly_one_process_takes_a_lease(tmp_path: Path) -> None:
    root, go = tmp_path / "state", tmp_path / "go"
    run_id = make_run(root)
    name = lease_name(run_id)
    procs = [spawn("lease", root, go, name, "1.0") for _ in range(WORKERS)]
    go.write_text("go")
    results = collect(procs)
    winners = [r for r in results if r["won"]]
    assert len(winners) == 1
    with Store.open(root) as store:
        info = store.lease_info(name)
        assert info is not None and info.holder.startswith(f"{winners[0]['pid']}:")


def test_ids_stay_unique_when_processes_share_a_generator_sequence(tmp_path: Path) -> None:
    root, go = tmp_path / "state", tmp_path / "go"
    procs = [spawn("reserve", root, go, "4", str(i)) for i in range(WORKERS)]
    go.write_text("go")
    ids = [i for r in collect(procs) for i in r["ids"]]
    assert len(ids) == 4 * WORKERS and len(set(ids)) == len(ids)


@pytest.mark.parametrize("op", ["cancel", "finalize"])
def test_killed_in_the_middle_of_a_transaction_leaves_the_old_state(
    tmp_path: Path, op: str
) -> None:
    root, ready = tmp_path / "state", tmp_path / "ready"
    run_id = make_run(root)
    child = spawn("tx-then-hang", root, tmp_path / "unused", run_id, str(ready))
    wait_file(ready)
    child.kill()  # exactly the child this test started
    child.wait(timeout=10)
    with Store.open(root) as store:
        run = store.get_run(run_id)
        assert run is not None and run.phase == "prefetch"  # the half-done write never landed
        if op == "cancel":
            assert store.request_cancel(run_id, KEY, actor="a", exit_code=143)
        else:
            assert store.begin_finalize(run_id, KEY)
        assert store.schema_version() == 1


def test_a_killed_lease_holder_is_replaced_at_once(tmp_path: Path) -> None:
    root, ready = tmp_path / "state", tmp_path / "ready"
    run_id = make_run(root)
    child = spawn("hold-lease-then-hang", root, tmp_path / "unused", run_id, str(ready))
    wait_file(ready)
    with Store.open(root) as store:
        assert store.acquire_lease(lease_name(run_id)) is None  # alive: held for 5 minutes
        child.kill()
        child.wait(timeout=10)
        taken = store.acquire_lease(lease_name(run_id))  # dead (PID and start time): no wait
        assert taken is not None
        assert taken.holder.pid != child.pid
