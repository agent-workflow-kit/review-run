"""Gateway for review-run: the single boundary for subprocess and HTTP calls.

Every call to an external program (sibling awkit tools, git, gh, glab, claude, bwrap, ...) goes
through this module, so tests can substitute fakes. No other module imports ``subprocess`` or
starts a process (``tests/test_import_boundary.py`` enforces it).

Rules:

* Commands are argv lists, never a shell. Untrusted or agent-generated text goes through
  ``stdin`` or files, never through argv or env.
* Every call has a required, finite, positive ``timeout`` (seconds); a call without one raises.
* The child runs in its own session and process group. When it times out, or prints more than
  ``MAX_OUTPUT`` bytes on one stream, exactly that process group (created here, its id captured
  at launch while the leader is still unreaped) gets SIGKILL and the call raises. Nothing is ever
  killed by name or pattern.
* Output is decoded explicitly as UTF-8 with ``surrogateescape``, never through the locale;
  ``stdin`` is encoded the same way (unencodable or over ``MAX_OUTPUT`` bytes: ``GatewayError``).
* A normal exit kills nothing: group members that outlive the leader keep running, and their later
  output is dropped (what is buffered when the leader exits is kept, up to the cap). A member that
  calls ``setsid`` leaves the group and escapes the timeout kill.
* A bare program name is looked up on the ``PATH`` of the ``env`` argument when one is given, else
  on ours.

Seam for fakes: ``Gateway`` is a ``Protocol``. Code calls ``gateway.run(...)`` (the module
function, which uses the current gateway) or takes a ``Gateway`` argument. Tests install a fake
with ``set_gateway``; ``tests/fake_gateway.py`` has the recording ``FakeGateway``.
"""

from __future__ import annotations

import math
import os
import selectors
import signal
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

#: At most this many bytes are kept per stream; one more byte kills the child.
MAX_OUTPUT = 8 * 1024 * 1024
_CHUNK = 64 * 1024
_DRAIN_BUDGET = 1024 * 1024  # most bytes taken per stream after the leader exited
_POLL = 0.2  # seconds between checks whether the child exited while its pipes stay open


class GatewayError(Exception):
    """A call failed before or while running. ``lower_code`` is a closed code for envelopes
    (``failure.lower_code``); the exception text is trusted (argv0 and numbers only)."""

    lower_code = "failed"


class CallTimeout(GatewayError):
    """The call exceeded its timeout and its process group was killed."""

    lower_code = "timeout"


class ToolMissing(GatewayError):
    """The program is not on ``PATH`` or not executable."""

    lower_code = "tool_missing"


class OutputTooLarge(GatewayError):
    """The program printed more than ``MAX_OUTPUT`` bytes on one stream and was killed."""

    lower_code = "output_too_large"


@dataclass(frozen=True)
class CompletedRun:
    """The result of one call. A negative ``returncode`` is a signal number."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


def decode(data: bytes) -> str:
    """Decode subprocess output explicitly; never through the locale."""
    return data.decode("utf-8", errors="surrogateescape")


def _check_text(value: object, what: str) -> str:
    if not isinstance(value, str) or "\0" in value:
        raise ValueError(f"{what} must be a string without NUL")
    return value


def check_call(
    argv: Sequence[str],
    timeout: float,
    *,
    stdin: str | None = None,
    env: Mapping[str, str] | None = None,
    cwd: str | None = None,
) -> bytes | None:
    """Validate one call's arguments (shared by every gateway implementation, run first).

    Programming errors raise ``ValueError``. ``stdin`` that cannot be encoded (``surrogateescape``
    UTF-8) or is longer than ``MAX_OUTPUT`` bytes raises ``GatewayError`` without the text. Returns
    the encoded ``stdin`` (``None`` when there is none).
    """
    if not isinstance(argv, (list, tuple)) or not argv:
        raise ValueError("argv must be a non-empty list of strings")
    if not all(isinstance(a, str) and "\0" not in a for a in argv) or not argv[0]:
        raise ValueError("argv items must be strings without NUL and argv[0] must not be empty")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise ValueError("timeout must be a finite number of seconds above zero")
    if cwd is not None:
        _check_text(cwd, "cwd")
    if env is not None:
        if not isinstance(env, Mapping):
            raise ValueError("env must be a mapping of strings")
        for name, value in env.items():
            _check_text(name, "an env name")
            if not name or "=" in name:
                raise ValueError("an env name must not be empty or contain '='")
            _check_text(value, "an env value")
    if stdin is None:
        return None
    if not isinstance(stdin, str):
        raise ValueError("stdin must be a string or None")
    try:
        data = stdin.encode("utf-8", errors="surrogateescape")
    except UnicodeEncodeError:
        raise GatewayError("stdin cannot be encoded as UTF-8") from None
    if len(data) > MAX_OUTPUT:
        raise GatewayError(f"stdin is longer than {MAX_OUTPUT} bytes")
    return data


class Gateway(Protocol):
    """Everything review-run needs from the operating system."""

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        stdin: str | None = None,
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
    ) -> CompletedRun:
        """Run ``argv`` without a shell and capture its output.

        ``env`` is the child's complete environment (``None`` inherits ours). Raises
        ``CallTimeout``, ``ToolMissing``, ``OutputTooLarge`` or ``GatewayError``; a non-zero
        exit is returned, not raised.
        """
        ...


def _kill_group(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _leader_exited(proc: subprocess.Popen[bytes]) -> bool:
    """Whether the leader has exited, without reaping it where the OS allows (it stays a zombie,
    so its PID, our group id, cannot be reused until ``proc.wait()``)."""
    if hasattr(os, "waitid"):
        try:
            return os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG) is not None
        except ChildProcessError:
            return True
    return proc.poll() is not None  # no waitid (macOS): reaped at detection; nothing raises later


class SystemGateway:
    """The real implementation."""

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        stdin: str | None = None,
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
    ) -> CompletedRun:
        data = check_call(argv, timeout, stdin=stdin, env=env, cwd=cwd)
        args = list(argv)
        if cwd is not None and not os.path.isdir(cwd):
            raise GatewayError("working directory missing")
        try:
            proc = subprocess.Popen(
                args,
                # Without input the child gets /dev/null, never this process's own stdin.
                stdin=subprocess.DEVNULL if data is None else subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=None if env is None else dict(env),
                cwd=cwd,
                start_new_session=True,
            )
        except OSError as exc:
            if cwd is not None and exc.filename == cwd:  # the directory, not the program
                raise GatewayError("working directory unusable") from exc
            if isinstance(exc, (FileNotFoundError, PermissionError, NotADirectoryError)):
                raise ToolMissing(f"cannot execute {os.path.basename(args[0])!r}") from exc
            raise GatewayError(
                f"cannot start {os.path.basename(args[0])!r}: errno {exc.errno}"
            ) from exc
        pgid = proc.pid  # the group this call created; valid while the leader is unreaped
        try:
            out, err = _communicate(proc, data, timeout)
        except BaseException:
            # Timeout, overflow, interrupt or any failure: end exactly the group we created, first,
            # and reap after, so a member alive when the leader exits is still killed. The leader
            # is unreaped here (the success path never raises after reaping), so its PID, our
            # group id, cannot have been reused.
            if proc.returncode is None:
                _kill_group(pgid)
                proc.kill()
            proc.wait()
            raise
        finally:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
        return CompletedRun(tuple(args), proc.returncode, decode(out), decode(err))


def _communicate(
    proc: subprocess.Popen[bytes], data: bytes | None, timeout: float
) -> tuple[bytes, bytes]:
    """Feed ``data``; collect stdout and stderr with a per-stream cap and a deadline.

    When the leader exits, what is already buffered in the pipes is drained once (bounded, never
    raising: a call whose leader exited returns) and no EOF is awaited, so a group member that
    inherited the pipes cannot hold the call past the leader's exit.
    """
    deadline = time.monotonic() + timeout
    assert proc.stdout is not None and proc.stderr is not None
    chunks: dict[int, list[bytes]] = {proc.stdout.fileno(): [], proc.stderr.fileno(): []}
    sizes = {fd: 0 for fd in chunks}
    selector = selectors.DefaultSelector()
    view = memoryview(data or b"")
    offset = 0
    exited = False
    try:
        if proc.stdin is not None:
            if len(view):
                os.set_blocking(proc.stdin.fileno(), False)
                selector.register(proc.stdin, selectors.EVENT_WRITE)
            else:
                proc.stdin.close()
        for stream in (proc.stdout, proc.stderr):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
        while selector.get_map():
            if _leader_exited(proc):
                exited = True
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CallTimeout(f"call exceeded its {timeout:g} s timeout")
            for key, _ in selector.select(min(remaining, _POLL)):
                fd = key.fd
                if proc.stdin is not None and key.fileobj is proc.stdin:
                    try:
                        offset += os.write(fd, view[offset : offset + _CHUNK])
                    except BlockingIOError:
                        continue
                    except OSError:  # the child closed its stdin: stop feeding it
                        offset = len(view)
                    if offset >= len(view):
                        selector.unregister(proc.stdin)
                        proc.stdin.close()
                    continue
                try:
                    block = os.read(fd, _CHUNK)
                except BlockingIOError:
                    continue
                if not block:
                    selector.unregister(key.fileobj)
                    continue
                sizes[fd] += len(block)
                if sizes[fd] > MAX_OUTPUT:
                    raise OutputTooLarge(f"a call printed more than {MAX_OUTPUT} bytes")
                chunks[fd].append(block)
        if exited:
            for stream in (proc.stdout, proc.stderr):
                if stream.fileno() in {k.fd for k in selector.get_map().values()}:
                    _drain(stream.fileno(), chunks[stream.fileno()], sizes)
        try:
            proc.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            raise CallTimeout(f"call exceeded its {timeout:g} s timeout") from None
    finally:
        selector.close()
    return b"".join(chunks[proc.stdout.fileno()]), b"".join(chunks[proc.stderr.fileno()])


def _drain(fd: int, chunks: list[bytes], sizes: dict[int, int]) -> None:
    """Read what is already buffered (up to ``_DRAIN_BUDGET`` bytes); drop what passes the cap."""
    budget = _DRAIN_BUDGET
    while budget > 0:
        try:
            block = os.read(fd, _CHUNK)
        except OSError:  # nothing more buffered (EAGAIN) or a broken pipe
            return
        if not block:
            return
        budget -= len(block)
        room = MAX_OUTPUT - sizes[fd]
        if room > 0:
            chunks.append(block[:room])
            sizes[fd] += min(room, len(block))


_current: Gateway = SystemGateway()


def get_gateway() -> Gateway:
    """The gateway in use (the real one unless a test installed a fake)."""
    return _current


def set_gateway(gateway: Gateway) -> Gateway:
    """Install ``gateway`` and return the previous one (tests restore it in a fixture)."""
    global _current
    previous = _current
    _current = gateway
    return previous


def run(
    argv: Sequence[str],
    *,
    timeout: float,
    stdin: str | None = None,
    env: Mapping[str, str] | None = None,
    cwd: str | None = None,
) -> CompletedRun:
    """``get_gateway().run(...)``: the entry point other modules call."""
    return get_gateway().run(argv, timeout=timeout, stdin=stdin, env=env, cwd=cwd)
