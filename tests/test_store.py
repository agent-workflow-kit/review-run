from __future__ import annotations

import datetime as dt
import os
import sqlite3
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from review_run import procid
from review_run.errors import ReviewRunError
from review_run.store import (
    Expect,
    Lease,
    NewEvent,
    NewRun,
    Store,
    _cas,
    default_state_root,
    generate_run_id,
    is_run_id,
    lease_name,
)
from review_run.store_schema import MIGRATIONS, SCHEMA_VERSION, check_additive


class Clock:
    def __init__(self) -> None:
        self.t = dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=dt.UTC)

    def __call__(self) -> dt.datetime:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += dt.timedelta(seconds=seconds)


def mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(tmp_path: Path, clock: Clock) -> Iterator[Store]:
    with Store.open(tmp_path / "state", now=clock) as s:
        yield s


def new_run(store: Store, **kw: object) -> str:
    run_id = store.reserve_id()
    store.create_run(
        NewRun(
            run_id=run_id,
            pr=str(kw.pop("pr", "host/o/r#1")),
            repo="host/o/r",
            head_sha="a" * 40,
            mode="headless",
            skill="review-run:review-pr",
            **kw,  # type: ignore[arg-type]
        )
    )
    return run_id


def run_lease(store: Store, run_id: str) -> Lease:
    """The run's lease: taken now, or (when this test already holds it) read back."""
    taken = store.acquire_lease(lease_name(run_id))
    if taken is not None:
        return taken
    row = store._conn.execute(  # noqa: SLF001
        "SELECT token FROM leases WHERE name = ?", (lease_name(run_id),)
    ).fetchone()
    return Lease(lease_name(run_id), row[0], procid.current(), "")


# ------------------------------------------------------------------ state root and file modes


def test_default_state_root_follows_xdg() -> None:
    assert default_state_root({"XDG_STATE_HOME": "/x/y", "HOME": "/h"}) == Path("/x/y/review-run")
    assert default_state_root({"HOME": "/h"}) == Path("/h/.local/state/review-run")
    # a relative XDG value is ignored, per the XDG spec
    assert default_state_root({"XDG_STATE_HOME": "rel", "HOME": "/h"}) == Path(
        "/h/.local/state/review-run"
    )


def test_open_creates_private_root_runs_and_db(tmp_path: Path) -> None:
    os.umask(0)
    root = tmp_path / "deep" / "er" / "state"
    with Store.open(root) as s:
        s.run_dir(s.reserve_id())
    assert mode(root) == 0o700
    assert mode(root / "runs") == 0o700
    assert mode(root / "state.db") == 0o600
    (run_dir,) = (root / "runs").iterdir()
    assert mode(run_dir) == 0o700
    for extra in root.glob("state.db-*"):  # the WAL and shm files follow the db's mode
        assert mode(extra) == 0o600


def test_0755_root_is_tightened_to_0700(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir()
    root.chmod(0o755)
    (root / "runs").mkdir()
    (root / "runs").chmod(0o755)
    with Store.open(root):
        pass
    assert mode(root) == 0o700
    assert mode(root / "runs") == 0o700


def test_loose_existing_db_and_run_dir_are_tightened(tmp_path: Path) -> None:
    root = tmp_path / "state"
    with Store.open(root) as s:
        run_id = s.reserve_id()
        s.run_dir(run_id)
    (root / "state.db").chmod(0o644)
    (root / "runs" / run_id).chmod(0o755)
    with Store.open(root) as s:
        s.run_dir(run_id)
    assert mode(root / "state.db") == 0o600
    assert mode(root / "runs" / run_id) == 0o700


def test_symlinked_root_is_state_unsafe(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(ReviewRunError) as exc:
        Store.open(link)
    assert (exc.value.exit_code, exc.value.reason) == (12, "state_unsafe")
    assert list(real.iterdir()) == []


@pytest.mark.parametrize("victim", ["runs", "state.db"])
def test_symlinked_children_are_state_unsafe(tmp_path: Path, victim: str) -> None:
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    target = tmp_path / "elsewhere"
    target.mkdir() if victim == "runs" else target.write_bytes(b"")
    (root / victim).symlink_to(target)
    with pytest.raises(ReviewRunError) as exc:
        Store.open(root)
    assert exc.value.reason == "state_unsafe"


def test_symlinked_run_dir_is_state_unsafe(store: Store, tmp_path: Path) -> None:
    run_id = store.reserve_id()
    target = tmp_path / "elsewhere"
    target.mkdir()
    (store.root / "runs" / run_id).symlink_to(target)
    with pytest.raises(ReviewRunError) as exc:
        store.run_dir(run_id)
    assert exc.value.reason == "state_unsafe"


def test_foreign_owned_root_is_state_unsafe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    monkeypatch.setattr(os, "geteuid", lambda: os.stat(root).st_uid + 1)
    with pytest.raises(ReviewRunError) as exc:
        Store.open(root)
    assert exc.value.reason == "state_unsafe"


def test_run_dir_refuses_ids_that_could_escape(store: Store) -> None:
    for bad in (
        "../x",
        "rr-../../x",
        "rr-ABCDEFGH",
        "",
        "rr-abcdefg",
        "rr-abcdefgh/",
        "rr-abcdefg1",
    ):
        with pytest.raises(ValueError):
            store.run_dir(bad)


def test_not_a_database_is_an_internal_error(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    (root / "state.db").write_bytes(b"this is not sqlite" * 100)
    (root / "state.db").chmod(0o600)
    with pytest.raises(ReviewRunError) as exc:
        Store.open(root)
    assert exc.value.exit_code == 1


# ------------------------------------------------------------------ connection safety


def test_pragmas(store: Store) -> None:
    c = store._conn  # noqa: SLF001
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert c.execute("PRAGMA busy_timeout").fetchone()[0] >= 1000
    assert c.execute("PRAGMA trusted_schema").fetchone()[0] == 0
    assert c.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL


def test_extension_loading_is_off(store: Store) -> None:
    c = store._conn  # noqa: SLF001
    with pytest.raises((sqlite3.OperationalError, sqlite3.NotSupportedError, AttributeError)):
        c.load_extension("/nonexistent/ext.so")


def test_foreign_keys_are_enforced(store: Store) -> None:
    with pytest.raises(ReviewRunError), store.transaction() as c:
        c.execute(
            "INSERT INTO labels (run_id, position, key, value) VALUES ('rr-aaaaaaaa',0,'a','b')"
        )


def test_injection_shaped_values_are_data(store: Store) -> None:
    evil = "x'); DROP TABLE runs; --"
    run_id = new_run(store, labels=(f"k={evil}", "a=b", "a=b"), pr=evil)
    run = store.get_run(run_id)
    assert run is not None
    assert run.pr == evil
    assert run.labels == (f"k={evil}", "a=b", "a=b")  # a multiset: duplicates kept, order kept
    with pytest.raises(ReviewRunError):
        store.resolve_run_id("rr-abc' OR 1=1 --")


def test_unicode_and_huge_values_roundtrip(store: Store) -> None:
    big = "é" * 100_000 + "‮\x00"
    run_id = new_run(store, pr=big)
    run = store.get_run(run_id)
    assert run is not None and run.pr == big


# ------------------------------------------------------------------ ids


def test_generated_ids_match_the_grammar() -> None:
    ids = {generate_run_id() for _ in range(200)}
    assert len(ids) == 200
    assert all(is_run_id(i) for i in ids)


def test_reserve_id_retries_a_collision(tmp_path: Path) -> None:
    seq = iter(["rr-aaaaaaaa", "rr-aaaaaaaa", "rr-aaaaaaaa", "rr-bbbbbbbb"])
    with Store.open(tmp_path / "s", id_generator=lambda: next(seq)) as s:
        assert s.reserve_id() == "rr-aaaaaaaa"
        assert s.reserve_id() == "rr-bbbbbbbb"


def test_an_id_reserved_by_a_vetoed_attempt_is_never_allocated_again(tmp_path: Path) -> None:
    """A vetoed attempt reserves an id but never creates a run; it must stay reserved."""
    forced = iter(["rr-vetoedaa"] * 6 + ["rr-freshaaa"])
    root = tmp_path / "s"
    with Store.open(root, id_generator=lambda: next(forced)) as s:
        vetoed = s.reserve_id()
        assert s.get_run(vetoed) is None  # no run record exists
    with Store.open(root, id_generator=lambda: next(forced)) as s:  # a later process
        assert s.reserve_id() == "rr-freshaaa"


def test_exhausted_collisions_fail_closed(tmp_path: Path) -> None:
    with Store.open(tmp_path / "s", id_generator=lambda: "rr-aaaaaaaa") as s:
        s.reserve_id()
        with pytest.raises(ReviewRunError) as exc:
            s.reserve_id()
    assert exc.value.exit_code == 1


def test_malformed_generator_output_is_refused(tmp_path: Path) -> None:
    with Store.open(tmp_path / "s", id_generator=lambda: "rr-../etc") as s:
        with pytest.raises(ReviewRunError):
            s.reserve_id()


def test_a_clock_going_backwards_does_not_affect_ids(tmp_path: Path, clock: Clock) -> None:
    with Store.open(tmp_path / "s", now=clock) as s:
        a = s.reserve_id()
        clock.advance(-86400 * 400)
        b = s.reserve_id()
    assert a != b


def test_create_run_needs_a_reserved_id_and_refuses_a_second_run(store: Store) -> None:
    with pytest.raises(ReviewRunError):
        store.create_run(NewRun("rr-aaaaaaaa", "p", "r", "h", "headless", "s"))
    run_id = new_run(store)
    with pytest.raises(ReviewRunError):
        store.create_run(NewRun(run_id, "p", "r", "h", "headless", "s"))


def test_create_run_defaults(store: Store, clock: Clock) -> None:
    run_id = new_run(store, hooks=("post-run=echo",), actor="human:me", decision_ids=("d1",))
    run = store.get_run(run_id)
    assert run is not None
    assert (run.state, run.phase, run.awaited_run) == ("running", "prefetch", None)
    assert run.created_at == "2026-01-01T12:00:00.000Z"
    assert run.hooks == ["post-run=echo"] and run.decision_ids == ["d1"]
    assert run.runner_pid == os.getpid()
    assert run.runner_start == procid.current().start
    assert run.key == Expect("running", "prefetch", None)


# ------------------------------------------------------------------ prefix resolution goldens


def seeded(store: Store, ids: list[str]) -> None:
    it = iter(ids)
    store._gen = lambda: next(it)  # noqa: SLF001
    for _ in ids:
        new_run(store)


def test_prefix_unique_with_and_without_rr(store: Store) -> None:
    seeded(store, ["rr-abcd2222", "rr-abce2222", "rr-zzzz2222"])
    assert store.resolve_run_id("rr-abcd") == "rr-abcd2222"
    assert store.resolve_run_id("zzzz") == "rr-zzzz2222"
    assert store.resolve_run_id("rr-zzzz2222") == "rr-zzzz2222"


def test_prefix_ambiguous_lists_matches_exit_21(store: Store) -> None:
    seeded(store, ["rr-abcd2222", "rr-abcd3333", "rr-zzzz2222"])
    with pytest.raises(ReviewRunError) as exc:
        store.resolve_run_id("abcd")
    assert exc.value.exit_code == 21
    assert exc.value.details == {
        "matches": ["rr-abcd2222", "rr-abcd3333"],
        "truncated": False,
    }


def test_prefix_too_short_exit_2(store: Store) -> None:
    seeded(store, ["rr-abcd2222"])
    for ref in ("rr-abc", "abc", "rr-", ""):
        with pytest.raises(ReviewRunError) as exc:
            store.resolve_run_id(ref)
        assert exc.value.exit_code == 2


@pytest.mark.parametrize(
    "ref", ["ABCD", "rr-abcd1", "abcd222222", "ab cd", "rr-rr-abcd", "abcd\n%"]
)
def test_prefix_malformed_is_usage(store: Store, ref: str) -> None:
    seeded(store, ["rr-abcd2222"])
    with pytest.raises(ReviewRunError) as exc:
        store.resolve_run_id(ref)
    assert exc.value.exit_code == 2


def test_prefix_not_found_and_reserved_only_ids_do_not_resolve(store: Store) -> None:
    seeded(store, ["rr-abcd2222"])
    store._gen = generate_run_id  # noqa: SLF001
    reserved_only = store.reserve_id()
    for ref in ("nnnn", reserved_only):
        with pytest.raises(ReviewRunError) as exc:
            store.resolve_run_id(ref)
        assert exc.value.exit_code == 10


def test_ambiguous_listing_is_capped(store: Store) -> None:
    alphabet = "abcdefghijklmnopqrstuvwxyz234567"
    ids = [f"rr-abcd{a}{b}{c}2" for a in alphabet[:4] for b in alphabet[:4] for c in alphabet[:4]]
    assert len(ids) == 64
    it = iter(ids)
    store._gen = lambda: next(it)  # noqa: SLF001
    for _ in ids:
        new_run(store)
    with pytest.raises(ReviewRunError) as exc:
        store.resolve_run_id("abcd")
    assert exc.value.details is not None
    assert len(exc.value.details["matches"]) == 50 and exc.value.details["truncated"] is True


# ------------------------------------------------------------------ migrations


def test_fresh_db_is_at_the_current_version(store: Store) -> None:
    assert store.schema_version() == SCHEMA_VERSION == len(MIGRATIONS)


def test_reopening_is_a_noop(tmp_path: Path) -> None:
    with Store.open(tmp_path / "s") as s:
        run_id = new_run(s)
    with Store.open(tmp_path / "s") as s:
        assert s.get_run(run_id) is not None and s.schema_version() == SCHEMA_VERSION


def test_empty_db_file_migrates_from_version_0(tmp_path: Path) -> None:
    root = tmp_path / "s"
    root.mkdir(mode=0o700)
    (root / "state.db").write_bytes(b"")
    with Store.open(root) as s:
        assert s.schema_version() == SCHEMA_VERSION


EXTRA_MIGRATIONS = (
    ("ALTER TABLE runs ADD COLUMN note TEXT",),
    ("CREATE TABLE extra (id INTEGER PRIMARY KEY)", "CREATE INDEX extra_id ON extra (id)"),
)


@pytest.mark.parametrize("start", range(0, len(MIGRATIONS) + 1))
def test_migration_from_every_older_version_keeps_data(tmp_path: Path, start: int) -> None:
    """Build a database at version ``start`` (0 .. current), then upgrade through extra steps."""
    root = tmp_path / "s"
    chain = MIGRATIONS + EXTRA_MIGRATIONS
    run_id = None
    with Store.open(root, migrations=chain[:start]) as s:
        assert s.schema_version() == start
        if start >= len(MIGRATIONS):
            run_id = new_run(s)
    with Store.open(root, migrations=chain) as s:
        assert s.schema_version() == len(chain)
        if run_id is not None:
            assert s.get_run(run_id) is not None
            with s.transaction() as c:
                c.execute("UPDATE runs SET note = 'x' WHERE run_id = ?", (run_id,))
                c.execute("INSERT INTO extra (id) VALUES (1)")


def test_newer_database_exits_12_schema_newer_and_is_left_alone(tmp_path: Path) -> None:
    root = tmp_path / "s"
    chain = MIGRATIONS + EXTRA_MIGRATIONS[:1]
    with Store.open(root, migrations=chain):
        pass
    before = (root / "state.db").read_bytes()
    with pytest.raises(ReviewRunError) as exc:
        Store.open(root)  # the "installed" tool only knows MIGRATIONS
    assert (exc.value.exit_code, exc.value.reason) == (12, "schema_newer")
    assert exc.value.details == {"found": len(chain), "known": len(MIGRATIONS)}
    conn = sqlite3.connect(root / "state.db")
    assert conn.execute("SELECT version FROM schema_version").fetchone() == (len(chain),)
    conn.close()
    del before


def test_foreign_sqlite_database_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "s"
    root.mkdir(mode=0o700)
    conn = sqlite3.connect(root / "state.db")
    conn.execute("CREATE TABLE other (a)")
    conn.commit()
    conn.close()
    (root / "state.db").chmod(0o600)
    with pytest.raises(ReviewRunError):
        Store.open(root)


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE runs",
        "ALTER TABLE runs RENAME TO old",
        "ALTER TABLE runs DROP COLUMN pr",
        "UPDATE runs SET state = 'failed'",
        "DELETE FROM ids",
        "CREATE TABLE a (x); DROP TABLE runs",
        "INSERT INTO ids VALUES ('a','b')",
        "PRAGMA writable_schema = ON",
        "CREATE TRIGGER t AFTER INSERT ON runs BEGIN SELECT 1; END",
    ],
)
def test_non_additive_migrations_are_rejected(tmp_path: Path, sql: str) -> None:
    with pytest.raises(ValueError):
        check_additive((sql,))
    with pytest.raises(ValueError):
        Store.open(tmp_path / "s", migrations=MIGRATIONS + ((sql,),))


def test_a_failing_migration_leaves_the_previous_version(tmp_path: Path) -> None:
    root = tmp_path / "s"
    with Store.open(root):
        pass
    bad = MIGRATIONS + (("CREATE TABLE ok_one (a)", "CREATE TABLE ok_one (a)"),)
    with pytest.raises(ReviewRunError):
        Store.open(root, migrations=bad)
    with Store.open(root) as s:  # rolled back as a whole: no half-applied step, version unchanged
        assert s.schema_version() == SCHEMA_VERSION
        names = {r[0] for r in s._conn.execute("SELECT name FROM sqlite_master")}  # noqa: SLF001
        assert "ok_one" not in names


# ------------------------------------------------------------------ compare-and-set


def test_update_fields_is_keyed_and_atomic(store: Store) -> None:
    run_id = new_run(store)
    key = Expect("running", "prefetch", None)
    assert store.update_fields(run_id, key, agent_id="ag-1", csession_id="cs-1", awaited_run=1)
    run = store.get_run(run_id)
    assert run is not None and (run.agent_id, run.csession_id, run.awaited_run) == (
        "ag-1",
        "cs-1",
        1,
    )
    # the key moved (awaited_run is part of it): a stale expectation loses and changes nothing
    assert not store.update_fields(run_id, key, agent_id="other")
    after = store.get_run(run_id)
    assert after is not None and after.agent_id == "ag-1"


def test_update_fields_refuses_transition_columns_and_unknown_names(store: Store) -> None:
    run_id = new_run(store)
    key = Expect("running", "prefetch", None)
    for column in ("state", "phase", "cancel_requested_at", "finished_at", "reason", "run_id"):
        with pytest.raises(ValueError):
            bad: dict[str, Any] = {column: "x"}
            store.update_fields(run_id, key, **bad)
    with pytest.raises(ValueError):
        injected: dict[str, Any] = {"agent_id = 'x', state": "done"}
        store.update_fields(run_id, key, **injected)


def test_json_fields_roundtrip(store: Store) -> None:
    run_id = new_run(store)
    key = Expect("running", "prefetch", None)
    anchor = {"head_sha": "a" * 40, "merge_base_sha": None}
    assert store.update_fields(run_id, key, anchor=anchor, failure={"errors": [{"pointer": "/x"}]})
    run = store.get_run(run_id)
    assert (
        run is not None and run.anchor == anchor and run.failure == {"errors": [{"pointer": "/x"}]}
    )


def test_advance_phase(store: Store) -> None:
    run_id = new_run(store)
    assert store.advance_phase(run_id, Expect("running", "prefetch", None), "code_run")
    assert not store.advance_phase(run_id, Expect("running", "prefetch", None), "session")
    run = store.get_run(run_id)
    assert run is not None and run.phase == "code_run"
    with pytest.raises(ValueError):
        store.advance_phase(run_id, run.key, "finalize")
    with pytest.raises(ValueError):
        store.advance_phase(run_id, run.key, "bogus")


def finalizing(store: Store, run_id: str) -> Expect:
    key = Expect("running", "prefetch", None)
    assert store.begin_finalize(run_id, key, lease=run_lease(store, run_id))
    return Expect("running", "finalize", None)


def test_finalize_and_cancel_exclude_each_other_finalize_first(store: Store) -> None:
    run_id = new_run(store)
    key = Expect("running", "prefetch", None)
    assert store.begin_finalize(run_id, key, lease=run_lease(store, run_id))
    assert not store.request_cancel(run_id, key, actor="a", exit_code=143)
    fin = Expect("running", "finalize", None)
    assert not store.request_cancel(run_id, fin, actor="a", exit_code=143)  # never in finalize
    run = store.get_run(run_id)
    assert run is not None and run.cancel_requested_at is None


def test_finalize_and_cancel_exclude_each_other_cancel_first(store: Store, clock: Clock) -> None:
    run_id = new_run(store)
    key = Expect("running", "prefetch", None)
    assert store.request_cancel(run_id, key, actor="human:me", exit_code=130)
    assert not store.begin_finalize(run_id, key, lease=run_lease(store, run_id))
    assert not store.request_cancel(run_id, key, actor="again", exit_code=143)  # once
    run = store.get_run(run_id)
    assert run is not None
    assert (run.cancel_requested_at, run.cancel_actor, run.cancel_exit_code) == (
        "2026-01-01T12:00:00.000Z",
        "human:me",
        130,
    )
    assert run.phase == "prefetch"  # recording a cancellation changes nothing else


def test_cancel_is_keyed_on_what_the_canceler_read(store: Store) -> None:
    run_id = new_run(store)
    assert store.advance_phase(run_id, Expect("running", "prefetch", None), "session")
    assert not store.request_cancel(
        run_id, Expect("running", "prefetch", None), actor="a", exit_code=143
    )
    assert store.request_cancel(
        run_id, Expect("running", "session", None), actor="a", exit_code=143
    )


def test_cancel_refused_on_terminal_runs(store: Store) -> None:
    run_id = new_run(store)
    lease = store.acquire_lease(lease_name(run_id))
    assert lease is not None
    fin = finalizing(store, run_id)
    assert store.end_run(run_id, fin, lease=lease, state="done", reason=None, exit_code=0)
    assert not store.request_cancel(run_id, Expect("done", None, None), actor="a", exit_code=143)


def test_end_run_requires_the_run_lease(store: Store) -> None:
    a, b = new_run(store), new_run(store)
    lease_a = store.acquire_lease(lease_name(a))
    assert lease_a is not None
    fin = finalizing(store, b)
    with pytest.raises(ValueError):  # another run's lease
        store.end_run(b, fin, lease=lease_a, state="done", reason=None, exit_code=0)
    forged = type(lease_a)(lease_name(b), "0" * 32, lease_a.holder, lease_a.expires_at)
    with pytest.raises(ReviewRunError):  # a lease nobody holds
        store.end_run(b, fin, lease=forged, state="done", reason=None, exit_code=0)
    run = store.get_run(b)
    assert run is not None and run.state == "running"


def test_end_run_done_sets_terminal_fields_and_outbox_atomically(store: Store) -> None:
    run_id = new_run(store)
    lease = store.acquire_lease(lease_name(run_id))
    assert lease is not None
    fin = finalizing(store, run_id)
    ev = NewEvent(f"{run_id}-post-run-1", "post-run", {"state": "done"}, ({"action": "run"},))
    assert store.end_run(
        run_id,
        fin,
        lease=lease,
        state="done",
        reason=None,
        exit_code=0,
        fields={"teardown": "pending", "artifact_sha256": "ab"},
        events=[ev],
    )
    run = store.get_run(run_id)
    assert run is not None
    assert (run.state, run.phase, run.exit_code, run.teardown) == ("done", None, 0, "pending")
    assert run.finished_at == "2026-01-01T12:00:00.000Z" and run.escalated_at is None
    (pending,) = store.pending_events(run_id)
    assert pending["event_id"] == ev.event_id and pending["payload"] == {"state": "done"}
    assert pending["audit"] == [{"action": "run"}]
    # the second finalizer loses and writes no second event
    assert not store.end_run(
        run_id, fin, lease=lease, state="done", reason=None, exit_code=0, events=[ev]
    )
    assert len(store.pending_events(run_id)) == 1
    store.mark_event(ev.event_id, delivered=True, audit_done=True)
    assert store.pending_events(run_id) == []


def test_end_run_rolls_back_everything_when_an_event_is_a_duplicate(store: Store) -> None:
    run_id = new_run(store)
    lease = store.acquire_lease(lease_name(run_id))
    assert lease is not None
    fin = finalizing(store, run_id)
    ev = NewEvent("same-id", "post-run", {})
    with pytest.raises(ReviewRunError):
        store.end_run(
            run_id, fin, lease=lease, state="done", reason=None, exit_code=0, events=[ev, ev]
        )
    run = store.get_run(run_id)
    assert run is not None and run.state == "running" and run.phase == "finalize"
    assert store.pending_events() == []


def test_done_is_refused_while_a_cancellation_is_recorded(store: Store) -> None:
    run_id = new_run(store)
    lease = store.acquire_lease(lease_name(run_id))
    assert lease is not None
    key = Expect("running", "prefetch", None)
    assert store.request_cancel(run_id, key, actor="a", exit_code=143)
    assert not store.end_run(run_id, key, lease=lease, state="done", reason=None, exit_code=0)
    assert store.end_run(run_id, key, lease=lease, state="failed", reason="canceled", exit_code=143)
    run = store.get_run(run_id)
    assert run is not None and (run.state, run.reason, run.exit_code) == ("failed", "canceled", 143)


def test_escalate_then_resume_then_revert_restores_the_record(store: Store) -> None:
    run_id = new_run(store)
    lease = store.acquire_lease(lease_name(run_id))
    assert lease is not None
    assert store.update_fields(run_id, Expect("running", "prefetch", None), awaited_run=1)
    assert store.advance_phase(run_id, Expect("running", "prefetch", 1), "session")
    assert store.end_run(
        run_id,
        Expect("running", "session", 1),
        lease=lease,
        state="escalated",
        reason="needs_input",
        exit_code=13,
        fields={"escalated_run": 1},
    )
    esc = store.get_run(run_id)
    assert esc is not None
    assert (esc.state, esc.phase, esc.reason, esc.exit_code) == (
        "escalated",
        None,
        "needs_input",
        13,
    )
    assert esc.escalated_at and esc.finished_at is None and esc.escalated_run == 1
    assert store.begin_resume(run_id, esc.key, lease=lease, awaited_run=2)
    assert not store.begin_resume(run_id, esc.key, lease=lease, awaited_run=2)  # one winner
    running = store.get_run(run_id)
    assert running is not None
    assert (running.state, running.phase, running.awaited_run) == ("running", "session", 2)
    assert (running.reason, running.exit_code, running.escalated_at) == (None, None, None)
    assert running.escalated_run == 1
    assert store.resume_revert(run_id, lease=lease, awaited_run=2)
    back = store.get_run(run_id)
    assert back is not None
    assert (back.state, back.phase, back.awaited_run) == ("escalated", None, 1)
    assert (back.reason, back.exit_code, back.escalated_at) == ("needs_input", 13, esc.escalated_at)
    assert back.escalation_reason is None


def test_resume_started_clears_the_record(store: Store) -> None:
    run_id = new_run(store)
    lease = store.acquire_lease(lease_name(run_id))
    assert lease is not None
    assert store.end_run(
        run_id,
        Expect("running", "prefetch", None),
        lease=lease,
        state="escalated",
        reason="blocked",
        exit_code=13,
    )
    esc = store.get_run(run_id)
    assert esc is not None and store.begin_resume(run_id, esc.key, lease=lease, awaited_run=4)
    assert store.resume_started(
        run_id,
        lease=lease,
        previous_awaited=4,
        awaited_run=5,
        fields={"actor": "human:bob", "hooks": ["post-run=true"], "decision_ids": ["d-1"]},
    )
    run = store.get_run(run_id)
    assert run is not None and run.awaited_run == 5 and run.escalation_reason is None
    assert (run.actor, run.hooks, run.decision_ids) == ("human:bob", ["post-run=true"], ["d-1"])
    with pytest.raises(ValueError):
        store.resume_started(
            run_id, lease=lease, previous_awaited=5, awaited_run=6, fields={"state": "done"}
        )
    assert not store.resume_revert(run_id, lease=lease, awaited_run=5)  # nothing left to restore


def test_resume_loses_to_a_recorded_cancellation_and_cancel_loses_to_resume(store: Store) -> None:
    for cancel_first in (True, False):
        run_id = new_run(store)
        lease = store.acquire_lease(lease_name(run_id))
        assert lease is not None
        assert store.end_run(
            run_id,
            Expect("running", "prefetch", None),
            lease=lease,
            state="escalated",
            reason="needs_input",
            exit_code=13,
        )
        esc = store.get_run(run_id)
        assert esc is not None
        if cancel_first:
            assert store.request_cancel(run_id, esc.key, actor="a", exit_code=143)
            assert not store.begin_resume(run_id, esc.key, lease=lease, awaited_run=2)  # exit 20
        else:
            assert store.begin_resume(run_id, esc.key, lease=lease, awaited_run=2)
            # the canceler read the escalated key; it lost and must re-read the running row
            assert not store.request_cancel(run_id, esc.key, actor="a", exit_code=143)
            running = store.get_run(run_id)
            assert running is not None
            assert store.request_cancel(run_id, running.key, actor="a", exit_code=143)
            # the resumer ends it canceled
            assert not store.resume_revert(run_id, lease=lease, awaited_run=2)
        store.release_lease(lease)


def test_add_warning_is_idempotent(store: Store) -> None:
    run_id = new_run(store)
    store.add_warning(run_id, "teardown_failed")
    store.add_warning(run_id, "teardown_failed")
    store.add_warning(run_id, "hook_failed")
    store.add_warning("rr-aaaaaaaa", "hook_failed")  # unknown run: no effect, no error
    run = store.get_run(run_id)
    assert run is not None and run.warnings == ["teardown_failed", "hook_failed"]


# ------------------------------------------------------------------ leases


def test_lease_blocks_a_live_holder_until_it_expires(store: Store, clock: Clock) -> None:
    name = lease_name(new_run(store))
    first = store.acquire_lease(name)
    assert first is not None
    assert store.acquire_lease(name) is None  # held by this live process
    clock.advance(299)
    assert store.acquire_lease(name) is None
    clock.advance(2)  # 5 minutes without renewal: the lease is lost
    second = store.acquire_lease(name)
    assert second is not None and second.token != first.token
    assert store.renew_lease(first) is None  # the old holder learns it lost the lease
    assert not store.release_lease(first)  # and cannot release the new holder's lease
    assert store.lease_info(name) is not None


def test_renewal_extends_the_lease(store: Store, clock: Clock) -> None:
    name = lease_name(new_run(store))
    lease = store.acquire_lease(name)
    assert lease is not None
    for _ in range(10):  # 10 x 60 s: far beyond one TTL, still held because it renews
        clock.advance(60)
        renewed = store.renew_lease(lease)
        assert renewed is not None
        lease = renewed
        assert store.acquire_lease(name) is None


def test_release_frees_the_lease(store: Store) -> None:
    name = lease_name(new_run(store))
    lease = store.acquire_lease(name)
    assert lease is not None and store.release_lease(lease)
    assert store.lease_info(name) is None
    assert store.acquire_lease(name) is not None


def test_a_dead_holder_loses_the_lease_before_expiry(tmp_path: Path, clock: Clock) -> None:
    dead = procid.ProcIdentity(999_999, "boot:1")
    with Store.open(tmp_path / "s", now=clock, is_alive=lambda ident: ident != dead) as s:
        assert s.acquire_lease("x", holder=dead) is not None
        assert s.acquire_lease("x") is not None


def test_an_inconclusive_liveness_check_keeps_the_lease(tmp_path: Path, clock: Clock) -> None:
    ghost = procid.ProcIdentity(999_999, "boot:1")
    with Store.open(tmp_path / "s", now=clock, is_alive=lambda ident: None) as s:
        assert s.acquire_lease("x", holder=ghost) is not None
        assert s.acquire_lease("x") is None
        clock.advance(301)
        assert s.acquire_lease("x") is not None


def test_garbled_holder_text_keeps_the_lease_until_expiry(store: Store) -> None:
    with store.transaction() as c:
        c.execute(
            "INSERT INTO leases (name, token, holder, acquired_at, expires_at)"
            " VALUES ('x', 't', 'garbage', 'a', '9999-12-31T00:00:00.000Z')"
        )
    assert store.acquire_lease("x") is None


def test_leases_of_different_names_are_independent(store: Store) -> None:
    assert store.acquire_lease("reconcile") is not None
    assert store.acquire_lease("prune") is not None
    assert store.acquire_lease("reconcile") is None


def test_begin_finalize_under_a_lost_lease_raises(store: Store, clock: Clock) -> None:
    run_id = new_run(store)
    name = lease_name(run_id)
    old = store.acquire_lease(name)
    assert old is not None
    clock.advance(301)
    assert store.acquire_lease(name) is not None
    with pytest.raises(ReviewRunError):
        store.begin_finalize(run_id, Expect("running", "prefetch", None), lease=old)
    run = store.get_run(run_id)
    assert run is not None and run.phase == "prefetch"


# ------------------------------------------------------------------ round 1 review fixes


def finished_run(store: Store) -> tuple[str, Expect]:
    run_id = new_run(store)
    lease = run_lease(store, run_id)
    fin = finalizing(store, run_id)
    assert store.end_run(run_id, fin, lease=lease, state="done", reason=None, exit_code=0)
    return run_id, Expect("done", None, None)


def test_terminal_runs_are_immutable(store: Store) -> None:
    run_id, done = finished_run(store)
    lease = run_lease(store, run_id)
    with pytest.raises(ValueError):
        store.end_run(run_id, done, lease=lease, state="failed", reason="canceled", exit_code=143)
    with pytest.raises(ValueError):
        store.update_fields(run_id, done, artifact_sha256="changed")
    with pytest.raises(ValueError):
        store.advance_phase(run_id, done, "session")
    run = store.get_run(run_id)
    assert run is not None and (run.state, run.artifact_sha256) == ("done", None)


def test_the_compare_and_set_itself_refuses_a_terminal_row(store: Store) -> None:
    run_id, _ = finished_run(store)
    with store.transaction() as conn:
        # a caller that lies about the key it read still changes nothing
        assert _cas(conn, run_id, Expect("done", None, None), {"agent_id": "x"}) == 0


def test_escalation_yields_to_a_recorded_cancellation(store: Store) -> None:
    run_id = new_run(store)
    lease = run_lease(store, run_id)
    key = Expect("running", "prefetch", None)
    assert store.request_cancel(run_id, key, actor="a", exit_code=143)
    assert not store.end_run(
        run_id, key, lease=lease, state="escalated", reason="needs_input", exit_code=13
    )
    run = store.get_run(run_id)
    assert run is not None and run.state == "running" and run.cancel_requested_at
    assert store.end_run(run_id, key, lease=lease, state="failed", reason="canceled", exit_code=143)


def test_begin_finalize_requires_a_lease(store: Store) -> None:
    run_id = new_run(store)
    with pytest.raises(TypeError):
        store.begin_finalize(run_id, Expect("running", "prefetch", None))  # type: ignore[call-arg]


def test_resume_steps_hold_the_run_lease(store: Store) -> None:
    run_id = new_run(store)
    lease = run_lease(store, run_id)
    assert store.end_run(
        run_id,
        Expect("running", "prefetch", None),
        lease=lease,
        state="escalated",
        reason="blocked",
        exit_code=13,
    )
    esc = store.get_run(run_id)
    assert esc is not None
    forged = Lease(lease_name(run_id), "0" * 32, lease.holder, lease.expires_at)
    with pytest.raises(ReviewRunError):
        store.begin_resume(run_id, esc.key, lease=forged, awaited_run=2)
    with pytest.raises(ReviewRunError):
        store.resume_started(run_id, lease=forged, previous_awaited=2, awaited_run=3)
    with pytest.raises(ReviewRunError):
        store.resume_revert(run_id, lease=forged, awaited_run=2)
    other = Lease(lease_name("rr-aaaaaaaa"), lease.token, lease.holder, lease.expires_at)
    with pytest.raises(ValueError):
        store.begin_resume(run_id, esc.key, lease=other, awaited_run=2)
    run = store.get_run(run_id)
    assert run is not None and run.state == "escalated"


def test_advance_phase_cannot_leave_finalize(store: Store) -> None:
    run_id = new_run(store)
    fin = finalizing(store, run_id)
    with pytest.raises(ValueError):
        store.advance_phase(run_id, fin, "session")
    run = store.get_run(run_id)
    assert run is not None and run.phase == "finalize"


@pytest.mark.parametrize(
    ("state", "reason", "exit_code"),
    [
        ("done", "x", 0),
        ("done", None, 1),
        ("done", None, None),
        ("failed", None, 12),
        ("failed", "", 12),
        ("failed", "canceled", None),
        ("failed", "canceled", "143"),
        ("escalated", None, 13),
        ("escalated", "needs_input", True),
    ],
)
def test_end_run_validates_reason_and_exit_code(
    store: Store, state: str, reason: object, exit_code: object
) -> None:
    run_id = new_run(store)
    lease = run_lease(store, run_id)
    with pytest.raises(ValueError):
        store.end_run(
            run_id,
            Expect("running", "prefetch", None),
            lease=lease,
            state=state,
            reason=reason,  # type: ignore[arg-type]
            exit_code=exit_code,  # type: ignore[arg-type]
        )
    run = store.get_run(run_id)
    assert run is not None and run.state == "running"


def test_update_fields_with_nothing_to_set_is_refused(store: Store) -> None:
    run_id = new_run(store)
    with pytest.raises(ValueError):
        store.update_fields(run_id, Expect("running", "prefetch", None))


def test_acquire_lease_reads_the_clock_inside_the_transaction(tmp_path: Path) -> None:
    seen: list[bool] = []
    box: dict[str, Store] = {}

    def now() -> dt.datetime:
        store = box.get("s")
        if store is not None:
            seen.append(store._conn.in_transaction)  # noqa: SLF001
        return dt.datetime(2026, 1, 1, tzinfo=dt.UTC)

    with Store.open(tmp_path / "state", now=now) as s:
        box["s"] = s
        assert s.acquire_lease("x") is not None
    assert seen and all(seen)


def test_a_hostile_holder_text_never_raises_and_keeps_the_lease(store: Store) -> None:
    for holder in ("\u00b2:a:1", "\uff11\uff12:a:1", "1:a:\u0661"):
        with store.transaction() as c:
            c.execute("DELETE FROM leases WHERE name = 'h'")
            c.execute(
                "INSERT INTO leases (name, token, holder, acquired_at, expires_at)"
                " VALUES ('h', 't', ?, 'a', '9999-12-31T00:00:00.000Z')",
                (holder,),
            )
        assert store.acquire_lease("h") is None


def test_set_teardown_records_the_state_on_a_terminal_run_under_the_lease(tmp_path: Path) -> None:
    from review_run.store import Expect, NewRun, lease_name

    with Store.open(tmp_path / "s") as store:
        rid = store.reserve_id()
        store.create_run(NewRun(rid, "h/o/r#1", "h/o/r", "a" * 40, "headless", "p:s"))
        lease = store.acquire_lease(lease_name(rid))
        assert lease is not None
        assert store.end_run(
            rid, Expect("running", "prefetch", None), lease=lease,
            state="failed", reason="killed", exit_code=15,
        )  # fmt: skip
        assert store.set_teardown(rid, "pending", lease=lease)
        assert store.set_teardown(rid, "done", lease=lease)
        run = store.get_run(rid)
        assert run is not None and (run.state, run.teardown, run.reason) == (
            "failed",
            "done",
            "killed",
        )
        with pytest.raises(ValueError):
            store.set_teardown(rid, "gone", lease=lease)
        with pytest.raises(ValueError):
            store.set_teardown("rr-bbbbbbbb", "done", lease=lease)  # another run's lease
        assert store.release_lease(lease)
        with pytest.raises(ReviewRunError):
            store.set_teardown(rid, "failed", lease=lease)  # no lease, no write
