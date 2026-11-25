"""``audit.jsonl`` and ``log.jsonl``: the append-only public record files, and ``export``.

Contracts: ``schemas/audit.json`` and ``schemas/log.json``; interface.md, Audit and log.

Rules this module enforces:

* Each record is one line, written with one ``write`` on an ``O_APPEND`` descriptor opened
  relative to the state root's directory descriptor with ``O_NOFOLLOW``; files are created 0600.
  Writers also hold an exclusive ``flock`` for the write, so lines of concurrent processes never
  interleave and a torn tail (a crash during a write, a full disk) is closed with a newline
  before the next record. Readers skip any line that is not a JSON object with a valid ``ts``.
* A record holds closed values only. Free text is refused (``ValueError``), not sanitized: every
  string must be printable ASCII and at most ``MAX_STRING`` characters, and the keys of
  ``inputs`` and ``result`` are the closed sets of the schema, so an untrusted string cannot ride
  along in an extra field. No record holds PR text, the artifact or meta text.
* Audit records of a transition are stored in the outbox (``Store.end_run`` events) and appended
  by ``flush_outbox`` once per ``event_id``: a line already in the file is not appended again,
  even when the process died before it marked the event.
* Retention is forever: there is no rotation and no size cap on the files, only on one line
  (``MAX_LINE_BYTES``).

Schema validation happens in the tests (``jsonschema`` is a development dependency here); the
runtime checks above are the cheap structural guard.
"""

from __future__ import annotations

import datetime as dt
import errno
import fcntl
import json
import os
import pwd
import re
import secrets
import stat
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

from .errors import ReviewRunError
from .fsmodes import FILE_MODE
from .store import Store, ensure_state_root, format_time

AUDIT_NAME: Final = "audit.jsonl"
LOG_NAME: Final = "log.jsonl"
MAX_LINE_BYTES: Final = 64 * 1024
MAX_STRING: Final = 300
LOCK_WAIT_SECONDS: Final = 10.0

Clock = Callable[[], dt.datetime]
Level = Literal["debug", "info", "warn", "error"]
Action = Literal["run", "resume", "cancel", "finalize", "code_run", "teardown", "prune"]

_TS_RE: Final = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z", re.ASCII
)
_ATTEMPT_ALPHABET: Final = "abcdefghijklmnopqrstuvwxyz234567"
_USER_BAD_RE: Final = re.compile(r"[^A-Za-z0-9._@-]")
FINALIZER_ACTOR: Final = "auto:review-run"

# Closed key sets of ``inputs`` and ``result`` per action (schemas/audit.json). A run and a
# resume have the same shape except that only a run records ``override_escalation``.
_RUN_INPUTS: Final = frozenset(
    {
        "head_sha",
        "config_sha256",
        "skill",
        "previous_run",
        "permission_profile_sha256",
        "session_sandbox",
        "setting_sources",
        "plugin_dirs_sha256",
    }
)
_OUTCOME: Final = frozenset({"status", "reason", "exit_code"})
_INPUT_KEYS: Final[dict[str, frozenset[str]]] = {
    "run": _RUN_INPUTS | {"override_escalation"},
    "resume": _RUN_INPUTS,
    "cancel": frozenset({"agent_id", "state"}),
    "finalize": frozenset({"cause", "finalized_by"}),
    "code_run": frozenset({"throwaway_id", "argv_sha256", "sandbox"}),
    "teardown": frozenset({"agent_id"}),
    "prune": frozenset({"older_than"}),
}
_RESULT_KEYS: Final[dict[str, frozenset[str]]] = {
    "run": _OUTCOME,
    "resume": _OUTCOME,
    "cancel": _OUTCOME,
    "finalize": _OUTCOME,
    "code_run": frozenset({"status", "reason"}),
    "teardown": frozenset({"status"}),
    "prune": frozenset({"status", "deleted_runs"}),
}
_RECORD_KEYS: Final = frozenset(
    {
        "ts",
        "attempt_id",
        "actor",
        "actor_source",
        "stdin_tty",
        "decision_id",
        "labels",
        "event_id",
        "action",
        "target",
        "inputs",
        "result",
    }
)
_LOG_KEYS: Final = frozenset(
    {"ts", "level", "event", "message", "run_id", "pr", "phase", "state", "tool", "code", "count"}
)
_LOG_REQUIRED: Final = frozenset({"ts", "level", "event", "message"})
ATTEMPTED: Final = {"status": "attempted", "reason": None, "exit_code": None}


def _unsafe() -> ReviewRunError:
    return ReviewRunError("failed", "state path is unsafe", reason="state_unsafe")


# ---------------------------------------------------------------- shape checks


def _check_strings(value: object, where: str) -> None:
    """Every string anywhere in ``value`` is printable ASCII and short; keys too."""
    if isinstance(value, str):
        if len(value) > MAX_STRING or not all(" " <= ch <= "~" for ch in value):
            raise ValueError(f"{where}: a record holds closed values only (printable ASCII)")
    elif isinstance(value, Mapping):
        for key, item in value.items():
            _check_strings(key, where)
            _check_strings(item, where)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _check_strings(item, where)
    elif value is None or isinstance(value, (bool, int)):
        return
    else:
        raise ValueError(f"{where}: unsupported value type")


def _line(record: Mapping[str, Any]) -> bytes:
    """The canonical line: compact, ASCII-escaped JSON plus a newline, within the line cap."""
    data = json.dumps(record, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    raw = data.encode("ascii") + b"\n"
    if len(raw) > MAX_LINE_BYTES:
        raise ValueError("record exceeds the line limit")
    return raw


def validate_audit_record(record: Mapping[str, Any]) -> None:
    """Raise ``ValueError`` unless ``record`` has the closed shape of ``schemas/audit.json``.

    Structural only (key sets, string charset, target shape); the value grammars are the
    schema's and are checked by the tests against it.
    """
    if set(record) != _RECORD_KEYS:
        raise ValueError("audit record keys are not the closed set")
    _check_strings(record, "audit")
    action = record["action"]
    if action not in _INPUT_KEYS:
        raise ValueError("unknown audit action")
    for part, table in (("inputs", _INPUT_KEYS), ("result", _RESULT_KEYS)):
        value = record[part]
        if not isinstance(value, Mapping) or set(value) != table[action]:
            raise ValueError(f"audit {part} keys are not the closed set of {action}")
    target = record["target"]
    if not isinstance(target, Mapping) or set(target) != {"run_id", "pr"}:
        raise ValueError("audit target keys are not run_id and pr")
    if not isinstance(record["ts"], str) or not _TS_RE.fullmatch(record["ts"]):
        raise ValueError("audit ts is not a UTC RFC 3339 time")
    if not isinstance(record["decision_id"], list) or not isinstance(record["labels"], list):
        raise ValueError("decision_id and labels are lists")


def validate_log_record(record: Mapping[str, Any]) -> None:
    """Raise ``ValueError`` unless ``record`` has the closed shape of ``schemas/log.json``."""
    keys = set(record)
    if not _LOG_REQUIRED <= keys or not keys <= _LOG_KEYS:
        raise ValueError("log record keys are not the closed set")
    _check_strings(record, "log")
    if not isinstance(record["message"], str) or len(record["message"]) > 512:
        raise ValueError("log message is a short string")
    if not isinstance(record["ts"], str) or not _TS_RE.fullmatch(record["ts"]):
        raise ValueError("log ts is not a UTC RFC 3339 time")


# ---------------------------------------------------------------- who and what


@dataclass(frozen=True)
class Caller:
    """The caller facts every audit record carries."""

    actor: str
    actor_source: Literal["flag", "tty", "default"]
    stdin_tty: bool
    decision_ids: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()

    @classmethod
    def resolve(
        cls,
        actor: str | None,
        *,
        stdin_tty: bool,
        decision_ids: Iterable[str] = (),
        labels: Iterable[str] = (),
        user: str | None = None,
    ) -> Caller:
        """``--actor`` when given, else ``human:<user>`` from a TTY, else ``auto:unknown``
        (design guide section 6). ``user`` defaults to the login name from the password
        database, characters outside ``[A-Za-z0-9._@-]`` replaced by ``_``."""
        ids, label_values = tuple(decision_ids), tuple(labels)
        if actor is not None:
            return cls(actor, "flag", stdin_tty, ids, label_values)
        if stdin_tty:
            name = user if user is not None else pwd.getpwuid(os.geteuid()).pw_name
            clean = _USER_BAD_RE.sub("_", name)[:64] or "_"
            return cls(f"human:{clean}", "tty", stdin_tty, ids, label_values)
        return cls("auto:unknown", "default", stdin_tty, ids, label_values)

    @classmethod
    def finalizer(cls, *, labels: Iterable[str] = ()) -> Caller:
        """The caller of a ``finalize`` record: ``auto:review-run`` (schema constant)."""
        return cls(FINALIZER_ACTOR, "default", False, (), tuple(labels))


def new_attempt_id() -> str:
    """``at-`` plus 16 random base32 characters (``attempt_id`` pattern of the schema)."""
    return "at-" + "".join(_ATTEMPT_ALPHABET[b & 31] for b in secrets.token_bytes(16))


def make_audit_record(
    action: Action,
    *,
    caller: Caller,
    run_id: str | None,
    pr: str | None,
    inputs: Mapping[str, Any],
    result: Mapping[str, Any],
    attempt_id: str | None = None,
    event_id: str | None = None,
    ts: str | None = None,
    now: Clock | None = None,
) -> dict[str, Any]:
    """Build and structurally validate one audit record. A fresh ``attempt_id`` is made when
    none is given (a one-record action); pass the opening record's id to close a pair. Records
    that go into an outbox event should be built with that event's ``event_id``."""
    stamp = ts if ts is not None else format_time((now or _utc_now)())
    record: dict[str, Any] = {
        "ts": stamp,
        "attempt_id": attempt_id if attempt_id is not None else new_attempt_id(),
        "actor": caller.actor,
        "actor_source": caller.actor_source,
        "stdin_tty": caller.stdin_tty,
        "decision_id": list(caller.decision_ids),
        "labels": list(caller.labels),
        "event_id": event_id,
        "action": action,
        "target": {"run_id": run_id, "pr": pr},
        "inputs": dict(inputs),
        "result": dict(result),
    }
    validate_audit_record(record)
    return record


def make_log_record(
    event: str,
    message: str,
    *,
    level: Level = "info",
    run_id: str | None = None,
    pr: str | None = None,
    phase: str | None = None,
    state: str | None = None,
    tool: str | None = None,
    code: str | None = None,
    count: int | None = None,
    ts: str | None = None,
    now: Clock | None = None,
) -> dict[str, Any]:
    """Build one log record; ``None`` members are omitted."""
    record: dict[str, Any] = {
        "ts": ts if ts is not None else format_time((now or _utc_now)()),
        "level": level,
        "event": event,
        "message": message,
    }
    for key, value in (
        ("run_id", run_id),
        ("pr", pr),
        ("phase", phase),
        ("state", state),
        ("tool", tool),
        ("code", code),
        ("count", count),
    ):
        if value is not None:
            record[key] = value
    validate_log_record(record)
    return record


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


# ---------------------------------------------------------------- the append-only file


def _open_root(root: Path) -> int:
    """The state root's directory descriptor, never through a symlink."""
    try:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.EACCES, errno.EPERM, errno.ENOENT):
            raise _unsafe() from None
        raise
    if os.fstat(fd).st_uid != os.geteuid():
        os.close(fd)
        raise _unsafe()
    return fd


def _lock(fd: int) -> None:
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if exc.errno not in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EINTR):
                raise
        if time.monotonic() > deadline:
            raise ReviewRunError("failed", "a record file is busy")
        time.sleep(0.005)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


class _Appender:
    """One append-only JSONL file under the state root."""

    def __init__(self, root: Path | str, name: str, *, durable: bool) -> None:
        self.root = Path(root)
        self.name = name
        self._durable = durable
        self._prepared = False

    @property
    def path(self) -> Path:
        return self.root / self.name

    def _open(self) -> int:
        if not self._prepared:
            ensure_state_root(self.root)
            self._prepared = True
        dir_fd = _open_root(self.root)
        try:
            fd = os.open(
                self.name,
                os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                FILE_MODE,
                dir_fd=dir_fd,
            )
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.EISDIR, errno.ENXIO, errno.EACCES, errno.EPERM):
                raise _unsafe() from None
            raise
        finally:
            os.close(dir_fd)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
                raise _unsafe()
            if stat.S_IMODE(info.st_mode) != FILE_MODE:
                os.fchmod(fd, FILE_MODE)
        except BaseException:
            os.close(fd)
            raise
        return fd

    def _append_locked(self, fd: int, line: bytes) -> None:
        # A torn tail (a crash or a full disk during an earlier write) is closed first, so this
        # record starts on its own line.
        size = os.fstat(fd).st_size
        if size:
            last = _read_last_byte(self.path, size)
            if last is not None and last != b"\n":
                line = b"\n" + line
        _write_all(fd, line)
        if self._durable:
            os.fsync(fd)

    def append_line(self, line: bytes) -> None:
        fd = self._open()
        try:
            _lock(fd)
            self._append_locked(fd, line)
        finally:
            os.close(fd)


def _read_last_byte(path: Path, size: int) -> bytes | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        return os.pread(fd, 1, size - 1)
    finally:
        os.close(fd)


class AuditWriter:
    """Appends audit records to ``<root>/audit.jsonl`` (0600), fsynced."""

    def __init__(self, root: Path | str, *, now: Clock | None = None) -> None:
        self._file = _Appender(root, AUDIT_NAME, durable=True)
        self._now = now

    @property
    def path(self) -> Path:
        return self._file.path

    def append(self, record: Mapping[str, Any]) -> None:
        """Append one validated record (a line of its own)."""
        validate_audit_record(record)
        self._file.append_line(_line(record))

    def record(
        self,
        action: Action,
        *,
        caller: Caller,
        run_id: str | None,
        pr: str | None,
        inputs: Mapping[str, Any],
        result: Mapping[str, Any],
        attempt_id: str | None = None,
    ) -> str:
        """Build and append one record; returns its ``attempt_id``."""
        record = make_audit_record(
            action,
            caller=caller,
            run_id=run_id,
            pr=pr,
            inputs=inputs,
            result=result,
            attempt_id=attempt_id,
            now=self._now,
        )
        self.append(record)
        return str(record["attempt_id"])

    def begin(
        self,
        action: Literal["run", "resume", "cancel"],
        *,
        caller: Caller,
        run_id: str,
        pr: str,
        inputs: Mapping[str, Any],
    ) -> str:
        """Write the opening record of a pair (``result.status`` ``attempted``); returns the
        ``attempt_id`` that the closing record must carry. Only the invocation that opened a
        pair closes it; when its process dies the pair stays open and a ``finalize`` record
        (its own ``attempt_id``) ends the run."""
        return self.record(
            action,
            caller=caller,
            run_id=run_id,
            pr=pr,
            inputs=inputs,
            result=ATTEMPTED,
        )

    def end(
        self,
        action: Literal["run", "resume", "cancel"],
        attempt_id: str,
        *,
        caller: Caller,
        run_id: str,
        pr: str,
        inputs: Mapping[str, Any],
        result: Mapping[str, Any],
    ) -> None:
        """Write the closing record of the pair opened under ``attempt_id``."""
        self.record(
            action,
            caller=caller,
            run_id=run_id,
            pr=pr,
            inputs=inputs,
            result=result,
            attempt_id=attempt_id,
        )

    def append_event_records(self, event_id: str, records: Sequence[Mapping[str, Any]]) -> int:
        """Append the records of one outbox event that are not yet in the file; returns how
        many lines were added. Holds the file lock across the scan and the writes, so two
        processes flushing the same event write each record once."""
        return self.append_events({event_id: records})

    def append_events(self, events: Mapping[str, Sequence[Mapping[str, Any]]]) -> int:
        lines: dict[str, list[bytes]] = {}
        for event_id, records in events.items():
            built: list[bytes] = []
            for record in records:
                validate_audit_record(record)
                if record["event_id"] != event_id:
                    raise ValueError("an outbox audit record carries its event's id")
                built.append(_line(record))
            lines[event_id] = built
        if not any(lines.values()):
            return 0
        fd = self._file._open()  # pyright: ignore[reportPrivateUsage]
        try:
            _lock(fd)
            present = _lines_of_events(self.path, set(lines))
            written = 0
            for built in lines.values():
                for line in built:
                    if line in present:
                        continue
                    self._file._append_locked(fd, line)  # pyright: ignore[reportPrivateUsage]
                    present.add(line)
                    written += 1
            return written
        finally:
            os.close(fd)


def _lines_of_events(path: Path, event_ids: set[str]) -> set[bytes]:
    """The complete lines in ``path`` that mention one of ``event_ids`` (exact bytes)."""
    needles = [json.dumps(i).encode("ascii") for i in event_ids]
    found: set[bytes] = set()
    for raw in _iter_lines(path):
        if any(n in raw for n in needles):
            found.add(raw if raw.endswith(b"\n") else raw + b"\n")
    return found


class LogWriter:
    """Appends structured records to ``<root>/log.jsonl`` (0600)."""

    def __init__(self, root: Path | str, *, now: Clock | None = None) -> None:
        self._file = _Appender(root, LOG_NAME, durable=False)
        self._now = now

    @property
    def path(self) -> Path:
        return self._file.path

    def write(
        self,
        event: str,
        message: str,
        *,
        level: Level = "info",
        run_id: str | None = None,
        pr: str | None = None,
        phase: str | None = None,
        state: str | None = None,
        tool: str | None = None,
        code: str | None = None,
        count: int | None = None,
    ) -> None:
        """Append one record. ``message`` is a short line of closed codes, ids and counts;
        text outside printable ASCII is refused (``ValueError``)."""
        record = make_log_record(
            event,
            message,
            level=level,
            run_id=run_id,
            pr=pr,
            phase=phase,
            state=state,
            tool=tool,
            code=code,
            count=count,
            now=self._now,
        )
        self._file.append_line(_line(record))


def flush_outbox(store: Store, writer: AuditWriter, run_id: str | None = None) -> list[str]:
    """Append the audit records of every outbox event whose records are pending, then mark
    them done; returns the ``event_id`` values completed. Safe to repeat and to run from
    several processes: a record that is already in the file (a crash before the mark) is not
    appended again. Hook delivery is not done here (``mark_event(delivered=...)`` is the
    caller's)."""
    pending = {
        str(e["event_id"]): e["audit"] for e in store.pending_events(run_id) if e["audit_pending"]
    }
    if not pending:
        return []
    writer.append_events(pending)
    for event_id in pending:
        store.mark_event(event_id, audit_done=True)
    return list(pending)


# ---------------------------------------------------------------- reading and export


def _iter_lines(path: Path | str, dir_fd: int | None = None) -> Iterator[bytes]:
    """Lines of ``path`` (with their newline; the last may lack one); a missing file yields
    nothing, a symlink or non-regular file is refused. A line longer than the cap is skipped
    without being held in memory."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd)
    except FileNotFoundError:
        return
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise _unsafe() from None
        raise
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise _unsafe()
        skipping = False
        while True:
            raw = handle.readline(MAX_LINE_BYTES + 1)
            if not raw:
                return
            complete = raw.endswith(b"\n")
            if skipping:
                skipping = not complete
                continue
            if len(raw) > MAX_LINE_BYTES:
                skipping = not complete
                continue
            yield raw


def _parse_ts(text: str) -> dt.datetime | None:
    if not _TS_RE.fullmatch(text):
        return None
    try:
        return dt.datetime.fromisoformat(text).astimezone(dt.UTC)
    except (ValueError, OverflowError):
        return None


def since_cutoff(since: dt.timedelta | dt.datetime | None, now: dt.datetime) -> dt.datetime | None:
    """``--since`` as an instant: a duration is an age before ``now``."""
    if since is None:
        return None
    if isinstance(since, dt.timedelta):
        try:
            return now - since
        except OverflowError:
            return dt.datetime.min.replace(tzinfo=dt.UTC)
    return since


def export_lines(
    root: Path | str,
    *,
    log: bool = False,
    since: dt.timedelta | dt.datetime | None = None,
    now: Clock | None = None,
) -> Iterator[bytes]:
    """The records of ``audit.jsonl`` (or ``log.jsonl``) at or after ``since``, one line each.

    Read only: nothing is created. A line that is not a JSON object with a valid ``ts`` (a torn
    line, foreign text) is skipped; each kept record is re-serialized as compact ASCII JSON, so
    no raw control character reaches the terminal. A missing file prints nothing.
    """
    name = LOG_NAME if log else AUDIT_NAME
    cutoff = since_cutoff(since, (now or _utc_now)())
    root_fd = _open_root_if_present(Path(root))
    if root_fd is None:
        return
    try:
        for raw in _iter_lines(name, root_fd):
            try:
                record = json.loads(raw)
            except (ValueError, RecursionError):
                continue
            if not isinstance(record, dict):
                continue
            ts = record.get("ts")
            moment = _parse_ts(ts) if isinstance(ts, str) else None
            if moment is None:
                continue
            if cutoff is not None and moment < cutoff:
                continue
            try:
                yield _line(record)
            except ValueError:
                continue
    finally:
        os.close(root_fd)


def _open_root_if_present(root: Path) -> int | None:
    """The root's descriptor, ``None`` when it does not exist (nothing to export yet)."""
    if not os.path.lexists(root):
        return None
    return _open_root(root)


__all__ = [
    "ATTEMPTED",
    "AUDIT_NAME",
    "LOG_NAME",
    "MAX_LINE_BYTES",
    "AuditWriter",
    "Caller",
    "LogWriter",
    "export_lines",
    "flush_outbox",
    "make_audit_record",
    "make_log_record",
    "new_attempt_id",
    "validate_audit_record",
    "validate_log_record",
]
