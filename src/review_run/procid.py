"""Process identity: a PID plus its start time, so a reused PID is never taken for its holder.

The identity text is ``<pid>:<boot id>:<start ticks>`` read from ``/proc`` (Linux and WSL). A
different boot id or start time than the stored one means the stored process is gone. Where
``/proc`` is unavailable (macOS) no identity can be read: ``identity_of`` returns ``None`` and
``check`` answers ``None`` (inconclusive), so callers fall back to expiry times only.
TODO(#11): a macOS identity source (needs the gateway for ``ps``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

_BOOT_ID = Path("/proc/sys/kernel/random/boot_id")
_START_FIELD = 19  # field 22 of /proc/<pid>/stat, counted from the field after the command name


@dataclass(frozen=True)
class ProcIdentity:
    pid: int
    start: str  # "<boot id>:<start ticks>"

    def text(self) -> str:
        return f"{self.pid}:{self.start}"

    @classmethod
    def parse(cls, text: str) -> ProcIdentity | None:
        pid_s, sep, start = text.partition(":")
        boot, sep2, ticks = start.rpartition(":")
        if not (sep and sep2 and boot and ticks.isdigit() and pid_s.isdigit()):
            return None
        pid = int(pid_s)
        return cls(pid, start) if pid > 0 else None


def parse_start_ticks(stat_line: str) -> str | None:
    """The start-time field of a ``/proc/<pid>/stat`` line (the command name may hold anything)."""
    _, sep, rest = stat_line.rpartition(")")
    fields = rest.split()
    if not sep or len(fields) <= _START_FIELD or not fields[_START_FIELD].isdigit():
        return None
    return fields[_START_FIELD]


def _boot_id() -> str | None:
    try:
        return _BOOT_ID.read_text(encoding="ascii").strip() or None
    except OSError:
        return None


def identity_of(pid: int) -> ProcIdentity | None:
    """The identity of the process now holding ``pid``, or ``None`` (no such process, no /proc)."""
    boot = _boot_id()
    if boot is None or pid <= 0:
        return None
    try:
        line = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="replace")
    except OSError:
        return None
    ticks = parse_start_ticks(line)
    return None if ticks is None else ProcIdentity(pid, f"{boot}:{ticks}")


def current() -> ProcIdentity:
    """This process's identity. Without /proc it is a never-matching placeholder start."""
    ident = identity_of(os.getpid())
    return ident if ident is not None else ProcIdentity(os.getpid(), "unknown:0")


def check(ident: ProcIdentity) -> bool | None:
    """True: that exact process is alive. False: it is gone (or the PID was reused, or a reboot
    happened). None: cannot tell (no /proc), so callers must not treat it as dead."""
    if _boot_id() is None:
        return None
    now = identity_of(ident.pid)
    return now is not None and now == ident
