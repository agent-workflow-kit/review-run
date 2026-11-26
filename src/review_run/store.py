"""The run store: ``state.db`` (SQLite, WAL), run ids, run directories, leases and the
compare-and-set primitives every run transition uses (interface.md, Runs, states and phases).

Rules this module enforces:

* The state root, ``runs/`` and each run directory are private (0700, owner only, never a
  symlink); ``state.db`` is 0600. A violation is ``state_unsafe`` (exit 12).
* Every statement is parameterized. Column names that reach SQL come only from closed tuples in
  this module, never from callers' data.
* Every write is one ``BEGIN IMMEDIATE`` transaction, so a killed process leaves either the old
  or the new state, never half of one.
* A transition is an ``UPDATE ... WHERE`` keyed on ``(state, phase, awaited_run)`` (``Expect``):
  it succeeds for exactly one of any number of racing processes, the rest get ``False`` and read
  the stored row. Terminal writers must also hold the run's lease.
* Migrations are additive only; a database newer than this code is ``schema_newer`` (exit 12).

TODO(#6): the state root is a parameter; ``default_state_root`` follows the XDG rule of
interface.md, Files, until config provides it.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import secrets
import sqlite3
import stat
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Self

from . import procid
from .errors import ReviewRunError
from .fsmodes import DIR_MODE, FILE_MODE, ensure_private_dir
from .store_schema import JSON_COLUMNS, MIGRATIONS, check_additive

LEASE_TTL_SECONDS = 300
LEASE_RENEW_SECONDS = 60
BUSY_TIMEOUT_MS = 5000
ID_ATTEMPTS = 16
MIN_PREFIX = 4
MAX_MATCHES_LISTED = 50

_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"
_RUN_ID_RE = re.compile(r"^rr-[a-z2-7]{8}\Z")
_REF_BODY_RE = re.compile(r"^[a-z2-7]*\Z")

IdGenerator = Callable[[], str]
Clock = Callable[[], dt.datetime]
IsAlive = Callable[[procid.ProcIdentity], bool | None]


# ---------------------------------------------------------------- ids and time


def generate_run_id() -> str:
    """``rr-`` plus 8 base32 characters from the operating system's generator (40 bits)."""
    return "rr-" + "".join(_ALPHABET[b & 31] for b in secrets.token_bytes(8))


def is_run_id(text: str) -> bool:
    return _RUN_ID_RE.match(text) is not None


def format_time(moment: dt.datetime) -> str:
    """UTC RFC 3339 with a ``Z`` and millisecond precision (fixed width: strings sort by time)."""
    utc = moment.astimezone(dt.UTC)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


def default_state_root(env: Mapping[str, str] | None = None) -> Path:
    """``$XDG_STATE_HOME/review-run`` or ``~/.local/state/review-run``."""
    env = os.environ if env is None else env
    base = env.get("XDG_STATE_HOME")
    if base and os.path.isabs(base):
        return Path(base) / "review-run"
    return Path(env.get("HOME") or os.path.expanduser("~")) / ".local" / "state" / "review-run"


def lease_name(run_id: str) -> str:
    return f"run:{run_id}"


RECONCILE_LEASE = "reconcile"
PRUNE_LEASE = "prune"


# ---------------------------------------------------------------- errors


class LeaseLostError(ReviewRunError):
    """A write that required the run's lease found it held by someone else."""

    def __init__(self) -> None:
        super().__init__("internal", "the run lease is no longer held by this process")


def _unsafe() -> ReviewRunError:
    return ReviewRunError("failed", "state path is unsafe", reason="state_unsafe")


def _translate(exc: sqlite3.Error) -> ReviewRunError:
    code = getattr(exc, "sqlite_errorcode", None)
    if code is not None and (code & 0xFF) in (5, 6):
        return ReviewRunError("internal", "state.db is busy; try again")
    if code is not None and (code & 0xFF) in (11, 26):
        return ReviewRunError("internal", "state.db is corrupt or not a database")
    return ReviewRunError("internal", f"state.db error: {type(exc).__name__}")


# ---------------------------------------------------------------- data classes


@dataclass(frozen=True)
class Expect:
    """The compare-and-set key: the ``(state, phase, awaited_run)`` the caller read."""

    state: str
    phase: str | None
    awaited_run: int | None


@dataclass(frozen=True)
class NewRun:
    run_id: str
    pr: str
    repo: str
    head_sha: str
    mode: str
    skill: str
    labels: tuple[str, ...] = ()
    previous_run: str | None = None
    hooks: tuple[str, ...] = ()
    actor: str | None = None
    decision_ids: tuple[str, ...] = ()
    config_sha256: str | None = None
    runner: procid.ProcIdentity | None = None  # default: this process


@dataclass(frozen=True)
class NewEvent:
    """One outbox row written in a terminal transaction."""

    event_id: str
    hook: str
    payload: dict[str, Any]
    audit: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class Run:
    run_id: str
    pr: str
    repo: str
    head_sha: str
    state: str
    phase: str | None
    reason: str | None
    exit_code: int | None
    mode: str
    skill: str
    labels: tuple[str, ...]
    previous_run: str | None
    agent_id: str | None
    csession_id: str | None
    awaited_run: int | None
    escalated_run: int | None
    finalized_run: int | None
    hooks: list[Any]
    actor: str | None
    decision_ids: list[Any]
    draft_id: str | None
    anchor: dict[str, Any] | None
    created_at: str
    escalated_at: str | None
    escalation_reason: str | None
    escalation_exit_code: int | None
    escalation_at: str | None
    escalation_awaited_run: int | None
    cancel_requested_at: str | None
    cancel_actor: str | None
    cancel_exit_code: int | None
    finished_at: str | None
    artifact_path: str | None
    artifact_sha256: str | None
    artifact_size: int | None
    original_sha256: str | None
    summary: dict[str, Any] | None
    failure: dict[str, Any] | None
    warnings: list[Any]
    teardown: str | None
    runner_pid: int | None
    runner_start: str | None
    launcher_pid: int | None
    launcher_start: str | None
    launch_started_at: str | None
    throwaway_id: str | None
    config_sha256: str | None

    @property
    def key(self) -> Expect:
        return Expect(self.state, self.phase, self.awaited_run)


@dataclass(frozen=True)
class Lease:
    name: str
    token: str
    holder: procid.ProcIdentity
    expires_at: str


@dataclass(frozen=True)
class LeaseInfo:
    name: str
    holder: str
    acquired_at: str
    expires_at: str


# Columns a caller may set through ``update_fields`` (never the ones transitions own).
FREE_COLUMNS = frozenset(
    {
        "head_sha",
        "agent_id",
        "csession_id",
        "awaited_run",
        "escalated_run",
        "finalized_run",
        "hooks",
        "actor",
        "decision_ids",
        "draft_id",
        "anchor",
        "artifact_path",
        "artifact_sha256",
        "artifact_size",
        "original_sha256",
        "summary",
        "failure",
        "teardown",
        "runner_pid",
        "runner_start",
        "launcher_pid",
        "launcher_start",
        "launch_started_at",
        "throwaway_id",
        "config_sha256",
    }
)
_LIVE_STATES = ("running", "escalated")  # terminal runs are immutable in the store
_NON_SESSION_PHASES = ("prefetch", "code_run", "session")


def _encode(column: str, value: Any) -> Any:
    if column in JSON_COLUMNS and value is not None:
        return json.dumps(value, separators=(",", ":"), ensure_ascii=True)
    return value


def _decode(column: str, value: Any) -> Any:
    if column in JSON_COLUMNS and value is not None:
        return json.loads(value)
    return value


MAX_TOLERANT_JSON = 1024 * 1024  # a JSON column longer than this is not decoded on a read path


def _decode_tolerant(column: str, value: Any) -> Any:
    """``_decode`` for read paths: too long, too deeply nested or not JSON gives None."""
    if column in JSON_COLUMNS and isinstance(value, str | bytes) and len(value) > MAX_TOLERANT_JSON:
        return None
    try:
        return _decode(column, value)
    except (ValueError, TypeError, RecursionError):
        return None


def _lenient_text(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------- state root


def ensure_state_root(root: Path) -> None:
    """Create ``root`` (and missing ancestors) and make it private; ``runs/`` inside it too."""
    path = Path(root)
    if not path.is_absolute():
        raise ValueError("the state root must be an absolute path")
    missing: list[Path] = []
    probe = path.parent
    while not probe.exists() and probe != probe.parent:
        missing.append(probe)
        probe = probe.parent
    for ancestor in reversed(missing):
        try:
            ancestor.mkdir(mode=DIR_MODE, exist_ok=True)
        except OSError:
            raise _unsafe() from None
    ensure_private_dir(path)
    ensure_private_dir(path / "runs")


def _prepare_db_file(path: Path) -> None:
    """Create ``state.db`` as 0600 (``O_EXCL``) or verify and tighten an existing one."""
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        try:
            fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, FILE_MODE)
        except FileExistsError:
            fd = os.open(path, flags)
    except OSError:
        raise _unsafe() from None  # a symlink, a directory or no permission
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise _unsafe()
        if stat.S_IMODE(info.st_mode) != FILE_MODE:
            os.fchmod(fd, FILE_MODE)
    finally:
        os.close(fd)


# ---------------------------------------------------------------- the store


class Store:
    """One connection to ``state.db``. Not shared between threads or processes."""

    def __init__(
        self,
        root: Path,
        conn: sqlite3.Connection,
        *,
        now: Clock,
        id_generator: IdGenerator,
        is_alive: IsAlive,
    ) -> None:
        self.root = root
        self._conn = conn
        self._now = now
        self._gen = id_generator
        self._is_alive = is_alive

    # -- opening

    @classmethod
    def open(
        cls,
        root: Path | str,
        *,
        now: Clock | None = None,
        id_generator: IdGenerator | None = None,
        is_alive: IsAlive | None = None,
        migrations: tuple[tuple[str, ...], ...] = MIGRATIONS,
    ) -> Self:
        """Open (creating if needed) the store under ``root`` and migrate it.

        Raises ``ReviewRunError``: ``state_unsafe`` (exit 12) for an unsafe root, directory or
        file; ``schema_newer`` (exit 12) for a database newer than ``migrations``.
        """
        root = Path(root)
        ensure_state_root(root)
        db_path = root / "state.db"
        _prepare_db_file(db_path)
        try:
            conn = sqlite3.connect(
                str(db_path),
                timeout=BUSY_TIMEOUT_MS / 1000,
                isolation_level=None,  # explicit transactions only
            )
        except sqlite3.Error as exc:
            raise _translate(exc) from exc
        try:
            _configure(conn)
            _migrate(conn, migrations)
        except BaseException:
            conn.close()
            raise
        return cls(
            root,
            conn,
            now=now or (lambda: dt.datetime.now(dt.UTC)),
            id_generator=id_generator or generate_run_id,
            is_alive=is_alive or procid.check,
        )

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def db_path(self) -> Path:
        return self.root / "state.db"

    def schema_version(self) -> int:
        return _read_version(self._conn)

    # -- transactions

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """``BEGIN IMMEDIATE`` ... ``COMMIT``; rolled back on any exception.

        For modules that add their own tables or queries; always pass values as parameters.
        """
        try:
            self._conn.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            raise _translate(exc) from exc
        try:
            yield self._conn
            self._conn.execute("COMMIT")
        except BaseException as exc:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            if isinstance(exc, sqlite3.Error):
                raise _translate(exc) from exc
            raise

    # -- run directories

    def runs_dir(self) -> Path:
        ensure_private_dir(self.root / "runs")
        return self.root / "runs"

    def run_dir(self, run_id: str) -> Path:
        """The private directory ``runs/<run id>`` (created 0700, a looser one tightened)."""
        if not is_run_id(run_id):
            raise ValueError("not a run id")
        base = self.runs_dir()
        ensure_private_dir(base / run_id)
        return base / run_id

    # -- ids

    def reserve_id(self) -> str:
        """Reserve a fresh run id in ``ids``. A reserved id is never allocated again."""
        for _ in range(ID_ATTEMPTS):
            candidate = self._gen()
            if not is_run_id(candidate):
                raise ReviewRunError("internal", "the id generator produced a malformed id")
            with self.transaction() as conn:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO ids (run_id, reserved_at) VALUES (?, ?)",
                    (candidate, format_time(self._now())),
                )
                if cur.rowcount == 1:
                    return candidate
        raise ReviewRunError("internal", "no free run id after repeated collisions")

    @contextmanager
    def _lenient(self) -> Iterator[None]:
        """Read TEXT columns with invalid UTF-8 as replacement characters, not an error."""
        previous = self._conn.text_factory
        self._conn.text_factory = _lenient_text
        try:
            yield
        finally:
            self._conn.text_factory = previous

    def resolve_run_id(self, ref: str) -> str:
        """Resolve a run id or a unique prefix of at least 4 characters (``rr-`` optional).

        Raises ``usage`` (exit 2) for a malformed or too short ref, ``not_found`` (10) and
        ``ambiguous`` (21, ``details.matches`` lists the ids).
        """
        body = ref[3:] if ref.startswith("rr-") else ref
        if not _REF_BODY_RE.match(body) or len(body) > 8:
            raise ReviewRunError("usage", "not a run id or run id prefix")
        if len(body) < MIN_PREFIX:
            raise ReviewRunError(
                "usage",
                f"a run id prefix needs at least {MIN_PREFIX} characters",
                details={"min": MIN_PREFIX},
            )
        with self._lenient():
            rows = self._conn.execute(
                "SELECT run_id FROM runs WHERE substr(run_id, 4, ?) = ? ORDER BY run_id LIMIT ?",
                (len(body), body, MAX_MATCHES_LISTED + 1),
            ).fetchall()
        matches = [str(r[0]) for r in rows]
        if not matches:
            raise ReviewRunError("not_found", "no such run")
        if len(matches) > 1:
            raise ReviewRunError(
                "ambiguous",
                "the run id prefix matches several runs",
                details={
                    "matches": matches[:MAX_MATCHES_LISTED],
                    "truncated": len(matches) > MAX_MATCHES_LISTED,
                },
            )
        return matches[0]

    # -- runs

    def create_run(self, new: NewRun) -> Run:
        """Insert the run (``running``, phase ``prefetch``) and its labels in one transaction.

        The id must have been reserved with ``reserve_id``; a second run with the same id is
        refused by the primary key.
        """
        runner = new.runner or procid.current()
        now = format_time(self._now())
        with self.transaction() as conn:
            if conn.execute("SELECT 1 FROM ids WHERE run_id = ?", (new.run_id,)).fetchone() is None:
                raise ReviewRunError("internal", "the run id was never reserved")
            conn.execute(
                "INSERT INTO runs (run_id, pr, repo, head_sha, state, phase, mode, skill,"
                " previous_run, hooks, actor, decision_ids, created_at, runner_pid, runner_start,"
                " config_sha256) VALUES (?, ?, ?, ?, 'running', 'prefetch', ?, ?, ?, ?, ?, ?, ?,"
                " ?, ?, ?)",
                (
                    new.run_id,
                    new.pr,
                    new.repo,
                    new.head_sha,
                    new.mode,
                    new.skill,
                    new.previous_run,
                    _encode("hooks", list(new.hooks)),
                    new.actor,
                    _encode("decision_ids", list(new.decision_ids)),
                    now,
                    runner.pid,
                    runner.start,
                    new.config_sha256,
                ),
            )
            conn.executemany(
                "INSERT INTO labels (run_id, position, key, value) VALUES (?, ?, ?, ?)",
                [
                    (new.run_id, i, *(label.partition("=")[0::2]))
                    for i, label in enumerate(new.labels)
                ],
            )
        run = self.get_run(new.run_id)
        assert run is not None
        return run

    def get_run(self, run_id: str, *, tolerant: bool = False) -> Run | None:
        """The run, or None. ``tolerant`` (read paths only) turns a JSON column that does not
        decode (or is too long or too deeply nested) into None, and invalid UTF-8 in text into
        replacement characters, so one corrupt row cannot break ``list``."""
        # Named columns only: a newer schema may have added more (additive migrations).
        names = [f.name for f in fields(Run) if f.name != "labels"]
        sql = f"SELECT {', '.join(names)} FROM runs WHERE run_id = ?"  # noqa: S608 (closed names)
        with self._lenient() if tolerant else nullcontext():
            row = self._conn.execute(sql, (run_id,)).fetchone()
            if row is None:
                return None
            label_rows = self._conn.execute(
                "SELECT key, value FROM labels WHERE run_id = ? ORDER BY position", (run_id,)
            ).fetchall()
        decode = _decode_tolerant if tolerant else _decode
        values = {n: decode(n, v) for n, v in zip(names, row, strict=True)}
        labels = tuple(f"{k}={v}" for k, v in label_rows)
        return Run(labels=labels, **values)

    def list_run_ids(
        self,
        *,
        states: Sequence[str] = (),
        pr: str | None = None,
        repo: str | None = None,
        labels: Sequence[str] = (),
        head: str | None = None,
        since: str | None = None,
        limit: int = 100,
    ) -> tuple[list[str], bool]:
        """Run ids newest first (``created_at`` descending, then run id), filters ANDed.

        ``labels`` entries are ``k=v`` (exact pair) or ``k`` (any value); ``head`` matches
        ``head_sha`` or the anchor's head; ``since`` is a ``format_time`` string. Read-only.
        Returns the ids and whether more than ``limit`` matched.
        """
        where: list[str] = []
        args: list[Any] = []
        if states:
            where.append(f"state IN ({', '.join('?' for _ in states)})")
            args.extend(states)
        if pr is not None:
            where.append("pr = ?")
            args.append(pr)
        if repo is not None:
            where.append("repo = ?")
            args.append(repo)
        for label in labels:
            key, eq, value = label.partition("=")
            if eq:
                where.append(
                    "EXISTS (SELECT 1 FROM labels l WHERE l.run_id = runs.run_id"
                    " AND l.key = ? AND l.value = ?)"
                )
                args.extend((key, value))
            else:
                where.append(
                    "EXISTS (SELECT 1 FROM labels l WHERE l.run_id = runs.run_id AND l.key = ?)"
                )
                args.append(key)
        if head is not None:
            # CASE is lazy: json_extract never sees a malformed anchor.
            where.append(
                "(head_sha = ? OR CASE WHEN json_valid(anchor)"
                " THEN json_extract(anchor, '$.head_sha') END = ?)"
            )
            args.extend((head, head))
        if since is not None:
            where.append("created_at >= ?")
            args.append(since)
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        with self._lenient():
            rows = self._conn.execute(
                f"SELECT run_id FROM runs{clause} ORDER BY created_at DESC, run_id LIMIT ?",  # noqa: S608
                (*args, limit + 1),
            ).fetchall()
        ids = [str(r[0]) for r in rows]
        return ids[:limit], len(ids) > limit

    def update_fields(
        self, run_id: str, expect: Expect, /, *, lease: Lease | None = None, **fields: Any
    ) -> bool:
        """Set ``FREE_COLUMNS`` on a run whose key still equals ``expect`` (one compare-and-set).

        Several columns change in one transaction (for example ``agent_id``, ``csession_id`` and
        ``awaited_run`` at launch). ``False`` when the row no longer has that key. Terminal runs
        are immutable: ``expect`` must name a ``running`` or ``escalated`` run, and the update
        itself refuses other rows. With ``lease`` the run's lease is checked in the same
        transaction (``LeaseLostError`` when it is no longer held).
        """
        if expect.state not in _LIVE_STATES:
            raise ValueError("a terminal run cannot change")
        if not fields:
            raise ValueError("no fields to set")
        _check_columns(fields, FREE_COLUMNS)
        with self.transaction() as conn:
            if lease is not None:
                _require_lease(conn, lease, run_id)
            return _cas(conn, run_id, expect, fields) == 1

    def advance_phase(
        self,
        run_id: str,
        expect: Expect,
        phase: str,
        /,
        *,
        lease: Lease | None = None,
        **fields: Any,
    ) -> bool:
        """Move a ``running`` run to ``prefetch``, ``code_run`` or ``session`` (never
        ``finalize``: see ``begin_finalize``). Optional ``FREE_COLUMNS`` change with it; with
        ``lease`` the lease is checked in the same transaction."""
        if phase not in _NON_SESSION_PHASES:
            raise ValueError("advance_phase cannot enter this phase")
        if expect.state != "running":
            raise ValueError("only a running run changes phase")
        if expect.phase == "finalize":
            raise ValueError("a run cannot leave finalize")
        _check_columns(fields, FREE_COLUMNS)
        with self.transaction() as conn:
            if lease is not None:
                _require_lease(conn, lease, run_id)
            return _cas(conn, run_id, expect, {**fields, "phase": phase}) == 1

    def begin_finalize(self, run_id: str, expect: Expect, *, lease: Lease) -> bool:
        """Move into phase ``finalize``; succeeds only while no cancellation is recorded."""
        if expect.state != "running" or expect.phase == "finalize":
            raise ValueError("finalize starts from a running run outside finalize")
        with self.transaction() as conn:
            _require_lease(conn, lease, run_id)
            return _cas(conn, run_id, expect, {"phase": "finalize"}, no_cancel=True) == 1

    def assert_holds(
        self, run_id: str, lease: Lease, phases: tuple[str, ...] = ("session", "finalize")
    ) -> None:
        """Authoritative check, in the database, that ``lease`` is still the run's lease and the
        run is still ``running`` in one of ``phases``; ``LeaseLostError`` otherwise. Used before a
        file of the run directory is replaced, since the keeper's flag lags behind a takeover."""
        with self.transaction() as conn:
            _require_lease(conn, lease, run_id)
            row = conn.execute(
                "SELECT state, phase FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None or row[0] != "running" or row[1] not in phases:
                raise LeaseLostError()

    def request_cancel(self, run_id: str, expect: Expect, *, actor: str, exit_code: int) -> bool:
        """Record a cancellation, keyed on the key the canceler read.

        Succeeds only while the run is ``escalated`` or ``running`` outside ``finalize`` and no
        cancellation is recorded yet.
        """
        if not (
            expect.state == "escalated"
            or (expect.state == "running" and expect.phase != "finalize")
        ):
            return False
        with self.transaction() as conn:
            return (
                _cas(
                    conn,
                    run_id,
                    expect,
                    {
                        "cancel_requested_at": format_time(self._now()),
                        "cancel_actor": actor,
                        "cancel_exit_code": exit_code,
                    },
                    no_cancel=True,
                )
                == 1
            )

    def begin_resume(self, run_id: str, expect: Expect, *, lease: Lease, awaited_run: int) -> bool:
        """``escalated`` to ``running`` (phase ``session``) with ``awaited_run`` set. The caller
        holds the run's lease (interface.md, Resume: ``resume`` holds it throughout).

        The escalation's reason, exit code and time move to the escalation record. Refused while
        a cancellation is recorded or the row changed since ``expect``."""
        if expect.state != "escalated":
            raise ValueError("resume starts from an escalated run")
        with self.transaction() as conn:
            _require_lease(conn, lease, run_id)
            moved = conn.execute(
                "UPDATE runs SET state = 'running', phase = 'session', awaited_run = ?,"
                " escalation_reason = reason, escalation_exit_code = exit_code,"
                " escalation_at = escalated_at, escalation_awaited_run = awaited_run,"
                " reason = NULL, exit_code = NULL, escalated_at = NULL"
                " WHERE run_id = ? AND state = ? AND phase IS ? AND awaited_run IS ?"
                " AND cancel_requested_at IS NULL",
                (awaited_run, run_id, expect.state, expect.phase, expect.awaited_run),
            )
            return moved.rowcount == 1

    def resume_started(
        self, run_id: str, *, lease: Lease, previous_awaited: int, awaited_run: int
    ) -> bool:
        """The resumed session started: set ``awaited_run`` to its run and clear the record."""
        with self.transaction() as conn:
            _require_lease(conn, lease, run_id)
            cur = conn.execute(
                "UPDATE runs SET awaited_run = ?, escalation_reason = NULL,"
                " escalation_exit_code = NULL, escalation_at = NULL,"
                " escalation_awaited_run = NULL"
                " WHERE run_id = ? AND state = 'running' AND phase = 'session'"
                " AND awaited_run IS ?",
                (awaited_run, run_id, previous_awaited),
            )
            return cur.rowcount == 1

    def resume_revert(self, run_id: str, *, lease: Lease, awaited_run: int) -> bool:
        """The reverse compare-and-set: back to ``escalated`` with the stored escalation values.

        ``False`` when the row moved (a recorded cancellation included: the caller then ends the
        run ``canceled`` itself).
        """
        with self.transaction() as conn:
            _require_lease(conn, lease, run_id)
            cur = conn.execute(
                "UPDATE runs SET state = 'escalated', phase = NULL,"
                " awaited_run = escalation_awaited_run, reason = escalation_reason,"
                " exit_code = escalation_exit_code, escalated_at = escalation_at,"
                " escalation_reason = NULL, escalation_exit_code = NULL, escalation_at = NULL,"
                " escalation_awaited_run = NULL"
                " WHERE run_id = ? AND state = 'running' AND phase = 'session'"
                " AND awaited_run IS ? AND cancel_requested_at IS NULL"
                " AND escalation_at IS NOT NULL",
                (run_id, awaited_run),
            )
            return cur.rowcount == 1

    def end_run(
        self,
        run_id: str,
        expect: Expect,
        *,
        lease: Lease,
        state: str,
        reason: str | None,
        exit_code: int | None,
        fields: Mapping[str, Any] | None = None,
        events: Sequence[NewEvent] = (),
    ) -> bool:
        """The terminal (or ``escalated``) compare-and-set, with its outbox events, in one
        transaction. The caller must hold the run's lease. ``done`` and ``escalated`` are
        refused while a cancellation is recorded (only ``failed`` may end such a run). ``False``
        when another process already moved the row. ``expect`` must name a ``running`` or
        ``escalated`` run: terminal runs are immutable.

        ``done`` takes ``reason=None, exit_code=0``; ``failed`` and ``escalated`` take a
        non-empty ``reason`` and an integer ``exit_code``. Which reason goes with which state is
        the caller's mapping (#9 and #11 own it); the store checks only the shape.
        """
        if state not in ("done", "failed", "escalated"):
            raise ValueError("end_run ends a run as done, failed or escalated")
        if expect.state not in _LIVE_STATES:
            raise ValueError("a terminal run cannot change")
        if state == "done":
            if reason is not None or exit_code != 0 or isinstance(exit_code, bool):
                raise ValueError("a done run has no reason and exit code 0")
        elif (
            not isinstance(reason, str)
            or not reason
            or not isinstance(exit_code, int)
            or isinstance(exit_code, bool)
        ):
            raise ValueError("a failed or escalated run needs a reason and an integer exit code")
        if lease.name != lease_name(run_id):
            raise ValueError("the lease belongs to another run")
        extra = dict(fields or {})
        _check_columns(extra, FREE_COLUMNS)
        now = format_time(self._now())
        sets: dict[str, Any] = {
            **extra,
            "state": state,
            "phase": None,
            "reason": reason,
            "exit_code": exit_code,
        }
        if state == "escalated":
            sets["escalated_at"] = now
        else:
            sets["finished_at"] = now
        with self.transaction() as conn:
            _require_lease(conn, lease, run_id)
            if _cas(conn, run_id, expect, sets, no_cancel=(state != "failed")) != 1:
                return False
            for event in events:
                conn.execute(
                    "INSERT INTO events (event_id, run_id, hook, payload, audit, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        event.event_id,
                        run_id,
                        event.hook,
                        json.dumps(event.payload),
                        json.dumps(list(event.audit)),
                        now,
                    ),
                )
            return True

    def add_warning(self, run_id: str, code: str, *, lease: Lease | None = None) -> None:
        """Append a closed warning code once (idempotent, race-safe); with ``lease`` the lease is
        checked in the same transaction."""
        with self.transaction() as conn:
            if lease is not None:
                _require_lease(conn, lease, run_id)
            row = conn.execute("SELECT warnings FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                return
            current = json.loads(row[0])
            if code not in current:
                conn.execute(
                    "UPDATE runs SET warnings = ? WHERE run_id = ?",
                    (json.dumps(current + [code]), run_id),
                )

    def set_teardown(self, run_id: str, status: str, *, lease: Lease) -> bool:
        """Record the state of the agent teardown (``done``, ``pending`` or ``failed``). Teardown
        follows the terminal transaction, so this is the one write allowed on a terminal run; it
        needs the run's lease and touches nothing else. ``False`` when the run does not exist."""
        if status not in ("done", "pending", "failed"):
            raise ValueError("teardown is done, pending or failed")
        if lease.name != lease_name(run_id):
            raise ValueError("the lease belongs to another run")
        with self.transaction() as conn:
            _require_lease(conn, lease, run_id)
            cur = conn.execute("UPDATE runs SET teardown = ? WHERE run_id = ?", (status, run_id))
            return cur.rowcount == 1

    # -- outbox

    def pending_events(self, run_id: str | None = None) -> list[dict[str, Any]]:
        """Undelivered events (or those with pending audit records), oldest first."""
        sql = (
            "SELECT event_id, run_id, hook, payload, audit, audit_pending, attempts, delivered"
            " FROM events WHERE (delivered = 0 OR audit_pending = 1)"
        )
        params: tuple[Any, ...] = ()
        if run_id is not None:
            sql += " AND run_id = ?"
            params = (run_id,)
        rows = self._conn.execute(sql + " ORDER BY created_at, event_id", params).fetchall()
        return [
            {
                "event_id": r[0],
                "run_id": r[1],
                "hook": r[2],
                "payload": json.loads(r[3]),
                "audit": json.loads(r[4]),
                "audit_pending": bool(r[5]),
                "attempts": r[6],
                "delivered": bool(r[7]),
            }
            for r in rows
        ]

    def mark_event(
        self, event_id: str, *, delivered: bool | None = None, audit_done: bool | None = None
    ) -> None:
        with self.transaction() as conn:
            if delivered:
                conn.execute("UPDATE events SET delivered = 1 WHERE event_id = ?", (event_id,))
            if audit_done:
                conn.execute("UPDATE events SET audit_pending = 0 WHERE event_id = ?", (event_id,))

    def record_attempt(self, event_id: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE events SET attempts = attempts + 1 WHERE event_id = ?", (event_id,)
            )

    # -- leases

    def acquire_lease(
        self,
        name: str,
        *,
        holder: procid.ProcIdentity | None = None,
        ttl_seconds: int = LEASE_TTL_SECONDS,
    ) -> Lease | None:
        """Take the lease ``name`` or return ``None`` (never waits).

        Free, expired (not renewed for the TTL) or held by a process that is verifiably gone
        (PID and start time) counts as available. A live holder, or one whose liveness cannot be
        read, keeps it until it expires.
        """
        holder = holder or procid.current()
        with self.transaction() as conn:
            now = self._now()  # read under the write lock: BEGIN may have waited seconds
            row = conn.execute(
                "SELECT holder, expires_at FROM leases WHERE name = ?", (name,)
            ).fetchone()
            if row is not None and row[1] > format_time(now):
                owner = procid.ProcIdentity.parse(row[0])
                alive = self._is_alive(owner) if owner is not None else None  # unreadable: keep
                if alive is not False:
                    return None
            lease = Lease(
                name,
                secrets.token_hex(16),
                holder,
                format_time(now + dt.timedelta(seconds=ttl_seconds)),
            )
            conn.execute(
                "INSERT INTO leases (name, token, holder, acquired_at, expires_at)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT (name) DO UPDATE SET token = excluded.token,"
                " holder = excluded.holder, acquired_at = excluded.acquired_at,"
                " expires_at = excluded.expires_at",
                (name, lease.token, holder.text(), format_time(now), lease.expires_at),
            )
            return lease

    def renew_lease(self, lease: Lease, *, ttl_seconds: int = LEASE_TTL_SECONDS) -> Lease | None:
        """Extend the lease; ``None`` when it was taken over (the holder must stop)."""
        expires = format_time(self._now() + dt.timedelta(seconds=ttl_seconds))
        with self.transaction() as conn:
            cur = conn.execute(
                "UPDATE leases SET expires_at = ? WHERE name = ? AND token = ?",
                (expires, lease.name, lease.token),
            )
            if cur.rowcount != 1:
                return None
        return Lease(lease.name, lease.token, lease.holder, expires)

    def release_lease(self, lease: Lease) -> bool:
        with self.transaction() as conn:
            cur = conn.execute(
                "DELETE FROM leases WHERE name = ? AND token = ?", (lease.name, lease.token)
            )
            return cur.rowcount == 1

    def lease_info(self, name: str) -> LeaseInfo | None:
        row = self._conn.execute(
            "SELECT name, holder, acquired_at, expires_at FROM leases WHERE name = ?", (name,)
        ).fetchone()
        return None if row is None else LeaseInfo(*row)


# ---------------------------------------------------------------- helpers


def _check_columns(fields: Mapping[str, Any], allowed: frozenset[str]) -> None:
    bad = sorted(set(fields) - allowed)
    if bad:
        raise ValueError(f"column not settable here: {bad[0]}")


def _require_lease(conn: sqlite3.Connection, lease: Lease, run_id: str) -> None:
    if lease.name != lease_name(run_id):
        raise ValueError("the lease belongs to another run")
    row = conn.execute("SELECT token FROM leases WHERE name = ?", (lease.name,)).fetchone()
    if row is None or row[0] != lease.token:
        raise LeaseLostError()


def _cas(
    conn: sqlite3.Connection,
    run_id: str,
    expect: Expect,
    sets: Mapping[str, Any],
    *,
    no_cancel: bool = False,
) -> int:
    """``UPDATE runs SET <sets> WHERE <key>``; column names are checked by the callers against
    closed sets or are the fixed transition columns below."""
    allowed = FREE_COLUMNS | {
        "state",
        "phase",
        "reason",
        "exit_code",
        "escalated_at",
        "finished_at",
        "cancel_requested_at",
        "cancel_actor",
        "cancel_exit_code",
    }
    _check_columns(sets, allowed)
    assignments = ", ".join(f"{column} = ?" for column in sets)
    sql = (
        f"UPDATE runs SET {assignments} WHERE run_id = ? AND state = ? AND phase IS ?"
        " AND awaited_run IS ? AND state IN ('running', 'escalated')"
        + (" AND cancel_requested_at IS NULL" if no_cancel else "")
    )
    params = [_encode(c, v) for c, v in sets.items()]
    params += [run_id, expect.state, expect.phase, expect.awaited_run]
    return conn.execute(sql, params).rowcount


def _enable_wal(conn: sqlite3.Connection) -> None:
    """Switch to WAL (a persistent setting). SQLite does not run the busy handler for this
    switch when another connection is mid-open, so retry for the busy timeout ourselves."""
    deadline = time.monotonic() + BUSY_TIMEOUT_MS / 1000
    while True:
        try:
            row = conn.execute("PRAGMA journal_mode = WAL").fetchone()
            break
        except sqlite3.OperationalError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.02)
    if row is None or str(row[0]).lower() != "wal":
        raise ReviewRunError("internal", "state.db cannot use WAL mode")


def _configure(conn: sqlite3.Connection) -> None:
    try:
        _enable_wal(conn)
        conn.execute("PRAGMA synchronous = FULL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA trusted_schema = OFF")
        conn.execute("PRAGMA cell_size_check = ON")
        conn.execute(f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT_MS)}")
        conn.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, True)
        try:
            conn.enable_load_extension(False)
        except AttributeError:  # a build without extension support is the safe case
            pass
    except sqlite3.Error as exc:
        raise _translate(exc) from exc


def _read_version(conn: sqlite3.Connection) -> int:
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "schema_version" not in tables:
        if tables - {"sqlite_sequence"}:
            raise ReviewRunError("internal", "state.db is not a review-run database")
        return 0
    row = conn.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()
    return 0 if row is None else int(row[0])


def _migrate(conn: sqlite3.Connection, migrations: tuple[tuple[str, ...], ...]) -> None:
    """Apply missing migrations, each with its version bump in one ``BEGIN IMMEDIATE``.

    The version is read again under the write lock, so racing processes apply each step once.
    """
    for statements in migrations:
        check_additive(statements)
    known = len(migrations)
    try:
        found = _read_version(conn)
        if found > known:
            _raise_newer(found, known)
        while found < known:
            conn.execute("BEGIN IMMEDIATE")
            try:
                found = _read_version(conn)
                if found > known:
                    _raise_newer(found, known)
                if found < known:
                    for sql in migrations[found]:
                        conn.execute(sql)
                    conn.execute(
                        "INSERT INTO schema_version (id, version) VALUES (1, ?)"
                        " ON CONFLICT (id) DO UPDATE SET version = excluded.version",
                        (found + 1,),
                    )
                    found += 1
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
    except sqlite3.Error as exc:
        raise _translate(exc) from exc


def _raise_newer(found: int, known: int) -> None:
    raise ReviewRunError(
        "failed",
        f"state.db schema v{found} is newer than this review-run (v{known}); upgrade review-run",
        reason="schema_newer",
        details={"found": found, "known": known},
    )


__all__ = [
    "LEASE_RENEW_SECONDS",
    "LEASE_TTL_SECONDS",
    "PRUNE_LEASE",
    "RECONCILE_LEASE",
    "Expect",
    "Lease",
    "LeaseInfo",
    "LeaseLostError",
    "NewEvent",
    "NewRun",
    "Run",
    "Store",
    "default_state_root",
    "ensure_state_root",
    "format_time",
    "generate_run_id",
    "is_run_id",
    "lease_name",
]
