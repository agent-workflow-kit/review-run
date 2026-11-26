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
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal

from . import signals
from .errors import ReviewRunError
from .fsmodes import FILE_MODE
from .store import Store, ensure_state_root, format_time

AUDIT_NAME: Final = "audit.jsonl"
LOG_NAME: Final = "log.jsonl"
MAX_LINE_BYTES: Final = 64 * 1024
MAX_STRING: Final = 300
LOCK_WAIT_SECONDS: Final = 10.0
MAX_DEPTH: Final = 32

Clock = Callable[[], dt.datetime]
Level = Literal["debug", "info", "warn", "error"]
Action = Literal["run", "resume", "cancel", "finalize", "code_run", "teardown", "prune"]

_TS_RE: Final = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z", re.ASCII
)
_ATTEMPT_ALPHABET: Final = "abcdefghijklmnopqrstuvwxyz234567"
_USER_BAD_RE: Final = re.compile(r"[^A-Za-z0-9._@-]")
FINALIZER_ACTOR: Final = "auto:review-run"

# The value grammars of schemas/defs.json, audit.json and log.json (patterns copied verbatim).
_ID_RE: Final = re.compile(r"[A-Za-z0-9_-]{8,64}", re.ASCII)  # attempt_id, event_id
_ACTOR_RE: Final = re.compile(r"(?:human|auto):[A-Za-z0-9._@-]{1,64}", re.ASCII)
_RUN_ID_RE: Final = re.compile(r"rr-[a-z2-7]{8}", re.ASCII)
_AGENT_ID_RE: Final = re.compile(r"ag-[a-z2-7]{8}", re.ASCII)
_PR_RE: Final = re.compile(
    r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*(?::[0-9]{1,5})?"
    r"(?:/(?!\.\.?(?:/|#))[A-Za-z0-9._-]+)+#[1-9][0-9]{0,9}",
    re.ASCII,
)
_SHA_RE: Final = re.compile(r"[0-9a-f]{40}", re.ASCII)
_SHA256_RE: Final = re.compile(r"[0-9a-f]{64}", re.ASCII)
_DECISION_RE: Final = re.compile(r"[A-Za-z0-9._:@/-]{1,128}", re.ASCII)
_LABEL_RE: Final = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}=[A-Za-z0-9._:/#@+=,-]{0,256}", re.ASCII)
_SKILL_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", re.ASCII)
_DURATION_RE: Final = re.compile(r"(?:0|[0-9]+(?:ms|s|m|h|d))", re.ASCII)
_CODE_WORD_RE: Final = re.compile(r"[a-z][a-z0-9_]*", re.ASCII)

_ACTOR_SOURCES: Final = frozenset({"flag", "tty", "default"})
_STATES: Final = frozenset({"running", "done", "escalated", "failed"})
_PHASES: Final = frozenset({"prefetch", "code_run", "session", "finalize"})
_EXIT_CODES: Final = frozenset({0, 1, 2, 10, 11, 12, 13, 14, 15, 16, 17, 18, 20, 21, 22, 130, 143})
_RUN_EXIT_CODES: Final = frozenset({0, 12, 13, 14, 15, 16, 17, 18, 22, 130, 143})
_SANDBOX_LEVELS: Final = frozenset({"required", "preferred", "off"})
_SETTING_SOURCES: Final = frozenset({"user", "project", "local"})
_OUTCOME_STATUS: Final = frozenset({"attempted", "done", "failed", "escalated", "vetoed"})
_FINALIZE_STATUS: Final = frozenset({"done", "failed", "escalated"})
_OUTCOME_REASONS: Final = frozenset(
    {
        "prefetch_failed",
        "stale_head",
        "too_large",
        "launch_failed",
        "canceled",
        "session_failed",
        "unreported",
        "killed",
        "timeout",
        "lost",
        "runner_lost",
        "agent_gone",
        "expired",
        "implicit_done",
        "artifact_missing",
        "artifact_mismatch",
        "skill_missing",
        "artifact_invalid",
        "internal",
        "needs_input",
        "blocked",
    }
)
_CODE_RUN_REASONS: Final = frozenset(
    {
        "wtctl_missing",
        "throwaway_refused",
        "ref_unavailable",
        "throwaway_failed",
        "sandbox_unavailable",
        "sandbox_unsupported",
        "sandbox_failed",
        "ro_bind_refused",
        "throwaway_leaks",
    }
)
_CODE_RUN_SANDBOX: Final = frozenset({"required", "preferred_fallback", "off"})
_FINALIZE_CAUSES: Final = frozenset(
    {"runner_lost", "agent_gone", "expired", "canceled", "external_resume", "finalized"}
)
_FINALIZED_BY: Final = frozenset({"show", "wait", "reconcile"})
_LOG_LEVELS: Final = frozenset({"debug", "info", "warn", "error"})
_LOG_EVENTS: Final = frozenset(
    {
        "phase_change",
        "state_change",
        "warning",
        "normalization",
        "sibling_error",
        "hook_failed",
        "hook_skipped",
        "teardown_retry",
        "reconcile",
        "throwaway_leaked",
    }
)
_LOG_TOOLS: Final = frozenset({"forgectl", "pr-comment", "agent-spawn", "claude-session", "wtctl"})

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


def _check_strings(value: object, where: str, depth: int = 0) -> None:
    """Every string anywhere in ``value`` is printable ASCII and short; keys too. Nesting is
    capped at ``MAX_DEPTH`` (a ``ValueError``, never a ``RecursionError``)."""
    if depth > MAX_DEPTH:
        raise ValueError(f"{where}: nesting is too deep")
    if isinstance(value, str):
        if len(value) > MAX_STRING or not all(" " <= ch <= "~" for ch in value):
            raise ValueError(f"{where}: a record holds closed values only (printable ASCII)")
    elif isinstance(value, Mapping):
        for key, item in value.items():
            _check_strings(key, where, depth + 1)
            _check_strings(item, where, depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _check_strings(item, where, depth + 1)
    elif value is None or isinstance(value, (bool, int)):
        return
    else:
        raise ValueError(f"{where}: unsupported value type")


def _line(record: Mapping[str, Any], *, capped: bool = True) -> bytes:
    """The canonical line: compact, ASCII-escaped JSON plus a newline. Writers cap it at
    ``MAX_LINE_BYTES``; export does not (it caps the raw input line instead)."""
    data = json.dumps(record, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    raw = data.encode("ascii") + b"\n"
    if capped and len(raw) > MAX_LINE_BYTES:
        raise ValueError("record exceeds the line limit")
    return raw


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _pattern(value: object, regex: re.Pattern[str], what: str) -> None:
    if not isinstance(value, str) or not regex.fullmatch(value):
        raise ValueError(f"{what} does not match its grammar")


def _one_of(value: object, allowed: frozenset[str], what: str) -> None:
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(f"{what} is not one of the closed values")


def _nullable(value: object, check: Callable[[object], None]) -> None:
    if value is not None:
        check(value)


def _sha(value: object) -> None:
    _pattern(value, _SHA_RE, "sha")


def _sha256(value: object) -> None:
    _pattern(value, _SHA256_RE, "sha256")


def _run_id(value: object) -> None:
    _pattern(value, _RUN_ID_RE, "run_id")


def _agent_id(value: object) -> None:
    _pattern(value, _AGENT_ID_RE, "agent_id")


def _check_ts(value: object, what: str) -> None:
    if not isinstance(value, str) or _parse_ts(value) is None:
        raise ValueError(f"{what} ts is not a valid UTC RFC 3339 time")


def _check_exit(value: object, allowed: frozenset[int], what: str) -> None:
    if not _is_int(value) or value not in allowed:
        raise ValueError(f"{what} exit_code is not a closed value")


def _run_inputs(inputs: Mapping[str, Any], action: str) -> None:
    _nullable(inputs["head_sha"], _sha)
    _sha256(inputs["config_sha256"])
    _pattern(inputs["skill"], _SKILL_RE, "skill")
    _nullable(inputs["previous_run"], _run_id)
    _sha256(inputs["permission_profile_sha256"])
    _one_of(inputs["session_sandbox"], _SANDBOX_LEVELS, "session_sandbox")
    sources = inputs["setting_sources"]
    if not isinstance(sources, list):
        raise ValueError("setting_sources is a list")
    for item in sources:
        _one_of(item, _SETTING_SOURCES, "setting_sources member")
    if len(set(sources)) != len(sources):
        raise ValueError("setting_sources members are unique")
    _nullable(inputs["plugin_dirs_sha256"], _sha256)
    if action == "run" and not isinstance(inputs["override_escalation"], bool):
        raise ValueError("override_escalation is a boolean")


def _outcome(
    result: Mapping[str, Any], statuses: frozenset[str], exit_codes: frozenset[int]
) -> None:
    _one_of(result["status"], statuses, "result.status")
    if result["reason"] is not None:
        _one_of(result["reason"], _OUTCOME_REASONS, "result.reason")
    if result["exit_code"] is not None:  # null is allowed (an opening record has no outcome)
        _check_exit(result["exit_code"], exit_codes, "result")


def _check_inputs_result(action: str, inputs: Mapping[str, Any], result: Mapping[str, Any]) -> None:
    if action in ("run", "resume"):
        _run_inputs(inputs, action)
        _outcome(result, _OUTCOME_STATUS, _EXIT_CODES)
        if result["status"] == "vetoed" and (
            result["exit_code"] != 11 or result["reason"] is not None
        ):
            raise ValueError("vetoed has reason null and exit_code 11")
    elif action == "cancel":
        _nullable(inputs["agent_id"], _agent_id)
        _one_of(inputs["state"], _STATES, "state")
        _outcome(result, _OUTCOME_STATUS, _EXIT_CODES)
        if result["status"] == "vetoed":
            raise ValueError("only a run or a resume is vetoed")
    elif action == "finalize":
        _one_of(inputs["cause"], _FINALIZE_CAUSES, "cause")
        _one_of(inputs["finalized_by"], _FINALIZED_BY, "finalized_by")
        _one_of(result["status"], _FINALIZE_STATUS, "result.status")
        if result["reason"] is not None:
            _one_of(result["reason"], _OUTCOME_REASONS, "result.reason")
        _check_exit(result["exit_code"], _RUN_EXIT_CODES, "result")
    elif action == "code_run":
        throwaway = inputs["throwaway_id"]
        if throwaway is not None and (
            not isinstance(throwaway, str) or not 1 <= len(throwaway) <= 128
        ):
            raise ValueError("throwaway_id is a string of 1 to 128 characters or null")
        _sha256(inputs["argv_sha256"])
        if inputs["sandbox"] is not None:
            _one_of(inputs["sandbox"], _CODE_RUN_SANDBOX, "sandbox")
        _one_of(result["status"], frozenset({"ok", "failed", "skipped"}), "result.status")
        if result["reason"] is not None:
            _one_of(result["reason"], _CODE_RUN_REASONS, "result.reason")
    elif action == "teardown":
        _agent_id(inputs["agent_id"])
        _one_of(result["status"], frozenset({"done", "failed"}), "result.status")
    else:  # prune
        _pattern(inputs["older_than"], _DURATION_RE, "older_than")
        if result["status"] != "done":
            raise ValueError("a prune result is done")
        if not _is_int(result["deleted_runs"]) or result["deleted_runs"] < 0:
            raise ValueError("deleted_runs is a non-negative integer")


def validate_audit_record(record: Mapping[str, Any]) -> None:
    """Raise ``ValueError`` unless ``record`` is a record of ``schemas/audit.json``: the closed
    key sets, the value grammars (patterns, types, enums) and the per-action rules (the
    ``finalize`` actor, ``vetoed`` is a ``run`` or ``resume`` with exit code 11, a non-null
    target for every action but ``prune``). Everything the writer would refuse is refused here,
    so the writer never writes a record that ``export`` or the schema would drop."""
    if set(record) != _RECORD_KEYS:
        raise ValueError("audit record keys are not the closed set")
    _check_strings(record, "audit")
    action = record["action"]
    if not isinstance(action, str) or action not in _INPUT_KEYS:
        raise ValueError("unknown audit action")
    for part, table in (("inputs", _INPUT_KEYS), ("result", _RESULT_KEYS)):
        value = record[part]
        if not isinstance(value, Mapping) or set(value) != table[action]:
            raise ValueError(f"audit {part} keys are not the closed set of {action}")
    target = record["target"]
    if not isinstance(target, Mapping) or set(target) != {"run_id", "pr"}:
        raise ValueError("audit target keys are not run_id and pr")
    _check_ts(record["ts"], "audit")
    _pattern(record["attempt_id"], _ID_RE, "attempt_id")
    _pattern(record["actor"], _ACTOR_RE, "actor")
    _one_of(record["actor_source"], _ACTOR_SOURCES, "actor_source")
    if not isinstance(record["stdin_tty"], bool):
        raise ValueError("stdin_tty is a boolean")
    ids, labels = record["decision_id"], record["labels"]
    if not isinstance(ids, list) or len(ids) > 32:
        raise ValueError("decision_id is a list of at most 32")
    for item in ids:
        _pattern(item, _DECISION_RE, "decision_id")
    if not isinstance(labels, list) or len(labels) > 60:
        raise ValueError("labels is a list of at most 60")
    for item in labels:
        _pattern(item, _LABEL_RE, "label")
    _nullable(record["event_id"], lambda v: _pattern(v, _ID_RE, "event_id"))
    if action == "prune":
        _nullable(target["run_id"], _run_id)
        _nullable(target["pr"], lambda v: _pattern(v, _PR_RE, "pr"))
    else:
        _run_id(target["run_id"])
        _pattern(target["pr"], _PR_RE, "pr")
    if action == "finalize" and record["actor"] != FINALIZER_ACTOR:
        raise ValueError("a finalize record is by auto:review-run")
    _check_inputs_result(action, record["inputs"], record["result"])


def validate_log_record(record: Mapping[str, Any]) -> None:
    """Raise ``ValueError`` unless ``record`` is a record of ``schemas/log.json`` (closed keys,
    enums, patterns, types; ``message`` is at most ``MAX_STRING`` printable ASCII characters)."""
    keys = set(record)
    if not _LOG_REQUIRED <= keys or not keys <= _LOG_KEYS:
        raise ValueError("log record keys are not the closed set")
    _check_strings(record, "log")
    _check_ts(record["ts"], "log")
    _one_of(record["level"], _LOG_LEVELS, "level")
    _one_of(record["event"], _LOG_EVENTS, "event")
    if not isinstance(record["message"], str):
        raise ValueError("log message is a string")
    _nullable(record.get("run_id"), _run_id)
    _nullable(record.get("pr"), lambda v: _pattern(v, _PR_RE, "pr"))
    if record.get("phase") is not None:
        _one_of(record["phase"], _PHASES, "phase")
    if record.get("state") is not None:
        _one_of(record["state"], _STATES, "state")
    if record.get("tool") is not None:
        _one_of(record["tool"], _LOG_TOOLS, "tool")
    _nullable(record.get("code"), lambda v: _pattern(v, _CODE_WORD_RE, "code"))
    count = record.get("count")
    if count is not None and (not _is_int(count) or count < 0):
        raise ValueError("count is a non-negative integer")


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
            caller = cls(actor, "flag", stdin_tty, ids, label_values)
        elif stdin_tty:
            if user is not None:
                name = user
            else:
                try:
                    name = pwd.getpwuid(os.geteuid()).pw_name
                except (KeyError, OSError):
                    name = f"uid{os.geteuid()}"
            clean = _USER_BAD_RE.sub("_", name)[:64] or "_"
            caller = cls(f"human:{clean}", "tty", stdin_tty, ids, label_values)
        else:
            caller = cls("auto:unknown", "default", stdin_tty, ids, label_values)
        caller.check()
        return caller

    def check(self) -> None:
        """Raise ``ValueError`` unless the actor, decision ids and labels match their grammars."""
        _pattern(self.actor, _ACTOR_RE, "actor")
        if len(self.decision_ids) > 32 or len(self.labels) > 60:
            raise ValueError("too many decision ids or labels")
        for item in self.decision_ids:
            _pattern(item, _DECISION_RE, "decision_id")
        for item in self.labels:
            _pattern(item, _LABEL_RE, "label")

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


def _open_safe(name: str, dir_fd: int, flags: int, mode: int = FILE_MODE) -> int:
    """Open ``name`` below ``dir_fd`` as a regular, single-link file, never through a symlink
    and never blocking (a FIFO opens ``O_NONBLOCK`` and is refused after ``fstat``). The
    returned descriptor is in blocking mode. A symlink, FIFO, directory, device or hardlinked
    file is ``state_unsafe``; a missing file raises ``FileNotFoundError``."""
    try:
        fd = os.open(
            name,
            flags | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            mode,
            dir_fd=dir_fd,
        )
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EISDIR, errno.ENXIO, errno.EACCES, errno.EPERM):
            raise _unsafe() from None
        raise
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise _unsafe()
        os.set_blocking(fd, True)
    except BaseException:
        os.close(fd)
        raise
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


def _read_chunks(fd: int) -> Iterator[bytes]:
    """The whole file from offset 0 by ``pread`` (the descriptor's own offset is untouched)."""
    offset = 0
    while True:
        chunk = os.pread(fd, 1 << 20, offset)
        if not chunk:
            return
        offset += len(chunk)
        yield chunk


def _lines_from_chunks(chunks: Iterable[bytes]) -> Iterator[bytes]:
    """Lines (with their newline; the last may lack one). A line longer than ``MAX_LINE_BYTES``
    is skipped without being held in memory."""
    buf = b""
    skipping = False
    for chunk in chunks:
        buf += chunk
        pos = 0
        while True:
            end = buf.find(b"\n", pos)
            if end < 0:
                break
            line = buf[pos : end + 1]
            pos = end + 1
            if skipping:
                skipping = False
            elif len(line) <= MAX_LINE_BYTES:
                yield line
        buf = buf[pos:]
        if len(buf) > MAX_LINE_BYTES:
            skipping = True
            buf = b""
    if buf and not skipping:
        yield buf


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
            # read-write so the locked descriptor also serves the torn-tail and the
            # event scans (by pread), never a reopen by path
            fd = _open_safe(self.name, dir_fd, os.O_RDWR | os.O_APPEND | os.O_CREAT)
        finally:
            os.close(dir_fd)
        try:
            info = os.fstat(fd)
            if info.st_uid != os.geteuid():
                raise _unsafe()
            if stat.S_IMODE(info.st_mode) != FILE_MODE:
                os.fchmod(fd, FILE_MODE)
        except BaseException:
            os.close(fd)
            raise
        return fd

    def _verify_same_file(self, fd: int) -> None:
        """After the lock: the name still is the locked inode (not replaced since the open)."""
        dir_fd = _open_root(self.root)
        try:
            try:
                now = os.stat(self.name, dir_fd=dir_fd, follow_symlinks=False)
            except FileNotFoundError:
                raise _unsafe() from None
        finally:
            os.close(dir_fd)
        held = os.fstat(fd)
        if (now.st_dev, now.st_ino) != (held.st_dev, held.st_ino) or now.st_nlink != 1:
            raise _unsafe()

    @contextmanager
    def locked(self) -> Iterator[int]:
        """The file open, exclusively locked and verified to be the file the name denotes."""
        fd = self._open()
        try:
            _lock(fd)
            self._verify_same_file(fd)
            with signals.deferred():  # an append is finished before a signal is raised
                yield fd
        finally:
            os.close(fd)

    def _append_locked(self, fd: int, line: bytes) -> None:
        # A torn tail (a crash or a full disk during an earlier write) is closed first, so this
        # record starts on its own line. The byte is read from the locked descriptor; a read that
        # fails raises, it never skips the check.
        size = os.fstat(fd).st_size
        if size:
            last = os.pread(fd, 1, size - 1)
            if len(last) != 1:
                raise _unsafe()
            if last != b"\n":
                line = b"\n" + line
        _write_all(fd, line)
        if self._durable:
            os.fsync(fd)

    def append_line(self, line: bytes) -> None:
        with self.locked() as fd:
            self._append_locked(fd, line)


@dataclass
class EventsReport:
    """What ``AuditWriter.append_events_report`` did: ``written`` lines added, ``done`` the
    events whose records are all in the file now, ``rejected`` event id -> why it was refused
    (nothing of a rejected event is written)."""

    written: int = 0
    done: list[str] = field(default_factory=list[str])
    rejected: dict[str, str] = field(default_factory=dict[str, str])


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
        """Write the closing record of the pair opened under ``attempt_id``; its
        ``result.status`` is the outcome, never ``attempted`` (``ValueError``)."""
        if result.get("status") == "attempted":
            raise ValueError("the closing record of a pair carries an outcome, not attempted")
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
        processes flushing the same event write each record once. An invalid record
        refuses the event (``ValueError``, nothing written)."""
        return self.append_events({event_id: records})

    def append_events(self, events: Mapping[str, Sequence[Mapping[str, Any]]]) -> int:
        """Append the missing records of several events; returns the lines added. An event with
        an invalid record is skipped as a whole and does not hold the others back; when any
        event was skipped, ``ValueError`` names them after the valid events were written."""
        report = self.append_events_report(events)
        if report.rejected:
            raise ValueError(
                "audit events refused: "
                + ", ".join(f"{i} ({why})" for i, why in report.rejected.items())
            )
        return report.written

    def append_events_report(
        self, events: Mapping[str, Sequence[Mapping[str, Any]]]
    ) -> EventsReport:
        """Like ``append_events`` but reports instead of raising for a refused event."""
        report = EventsReport()
        lines: dict[str, list[bytes]] = {}
        for event_id, records in events.items():
            try:
                _pattern(event_id, _ID_RE, "event_id")
                built: list[bytes] = []
                for record in records:
                    validate_audit_record(record)
                    if record["event_id"] != event_id:
                        raise ValueError("an outbox audit record carries its event's id")
                    built.append(_line(record))
            except ValueError as exc:
                report.rejected[event_id] = str(exc)
                continue
            lines[event_id] = built
        if not any(lines.values()):
            report.done = list(lines)
            return report
        with self._file.locked() as fd:
            present = _lines_of_events(fd, set(lines))
            for event_id, built in lines.items():
                for line in built:
                    if line in present:
                        continue
                    self._file._append_locked(fd, line)  # pyright: ignore[reportPrivateUsage]
                    present.add(line)
                    report.written += 1
                report.done.append(event_id)
        return report


def _lines_of_events(fd: int, event_ids: set[str]) -> set[bytes]:
    """The complete lines of the open file ``fd`` that mention one of ``event_ids`` (exact
    bytes)."""
    needles = [json.dumps(i).encode("ascii") for i in event_ids]
    found: set[bytes] = set()
    for raw in _lines_from_chunks(_read_chunks(fd)):
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
        """Append one record. ``message`` is a short line (at most ``MAX_STRING`` characters) of
        closed codes, ids and counts; text outside printable ASCII is refused (``ValueError``)."""
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


class FlushResult(list[str]):
    """The ``event_id`` values completed (a plain list to existing callers) plus ``stuck``:
    the pending events refused as invalid, which stay pending (``reconcile`` counts them as
    ``stuck_audit``)."""

    stuck: list[str]

    def __init__(self, done: Iterable[str] = (), stuck: Iterable[str] = ()) -> None:
        super().__init__(done)
        self.stuck = list(stuck)


def flush_outbox(store: Store, writer: AuditWriter, run_id: str | None = None) -> FlushResult:
    """Append the audit records of every outbox event whose records are pending, then mark
    them done; returns the ``event_id`` values completed. Events are handled one by one: an
    invalid event stays pending and is listed in the result's ``stuck``, and the valid ones are
    still written and marked. Safe to repeat and to run from several processes: a record that
    is already in the file (a crash before the mark) is not appended again. Hook delivery is
    not done here (``mark_event(delivered=...)`` is the caller's)."""
    pending = {
        str(e["event_id"]): e["audit"] for e in store.pending_events(run_id) if e["audit_pending"]
    }
    if not pending:
        return FlushResult()
    report = writer.append_events_report(pending)
    for event_id in report.done:
        store.mark_event(event_id, audit_done=True)
    return FlushResult(report.done, report.rejected)


# ---------------------------------------------------------------- reading and export


def _iter_lines(name: str, dir_fd: int) -> Iterator[bytes]:
    """Lines of ``name`` below ``dir_fd`` (with their newline; the last may lack one); a missing
    file yields nothing, a symlink, FIFO, directory or hardlinked file is refused. A line longer
    than the cap is skipped without being held in memory."""
    try:
        fd = _open_safe(name, dir_fd, os.O_RDONLY)
    except FileNotFoundError:
        return
    try:
        yield from _lines_from_chunks(iter(lambda: os.read(fd, 1 << 20), b""))
    finally:
        os.close(fd)


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
                yield _line(record, capped=False)  # only the raw input line is capped
            except (ValueError, RecursionError):
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
    "EventsReport",
    "FlushResult",
    "LogWriter",
    "export_lines",
    "flush_outbox",
    "make_audit_record",
    "make_log_record",
    "new_attempt_id",
    "validate_audit_record",
    "validate_log_record",
]
