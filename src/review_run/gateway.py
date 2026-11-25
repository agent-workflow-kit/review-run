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
* Output is decoded explicitly as UTF-8 with ``surrogateescape``, never through the locale.

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


def check_call(argv: Sequence[str], timeout: float) -> None:
    """Validate one call's arguments (shared by every gateway implementation)."""
    if isinstance(argv, str) or not argv:
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
        check_call(argv, timeout)
        args = list(argv)
        data = None if stdin is None else stdin.encode("utf-8", errors="surrogateescape")
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
        except (FileNotFoundError, PermissionError, NotADirectoryError) as exc:
            raise ToolMissing(f"cannot execute {os.path.basename(args[0])!r}") from exc
        except OSError as exc:
            raise GatewayError(
                f"cannot start {os.path.basename(args[0])!r}: errno {exc.errno}"
            ) from exc
        pgid = proc.pid  # the group this call created; valid while the leader is unreaped
        try:
            out, err = _communicate(proc, data, timeout)
        except BaseException:
            # Timeout, overflow, interrupt or any failure: end exactly the group we created. Only
            # while the leader is unreaped: its PID (our group id) cannot have been reused then.
            if proc.returncode is None and proc.poll() is None:
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

    Once the child has exited its pipes are drained without blocking and no EOF is awaited, so a
    grandchild that inherited them cannot hold the call past the child's exit.
    """
    deadline = time.monotonic() + timeout
    assert proc.stdout is not None and proc.stderr is not None
    chunks: dict[int, list[bytes]] = {proc.stdout.fileno(): [], proc.stderr.fileno(): []}
    sizes = {fd: 0 for fd in chunks}
    selector = selectors.DefaultSelector()
    pending = data or b""
    try:
        if proc.stdin is not None:
            if pending:
                os.set_blocking(proc.stdin.fileno(), False)
                selector.register(proc.stdin, selectors.EVENT_WRITE)
            else:
                proc.stdin.close()
        for stream in (proc.stdout, proc.stderr):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CallTimeout(f"call exceeded its {timeout:g} s timeout")
            exited = proc.poll() is not None
            events = selector.select(0 if exited else min(remaining, _POLL))
            if not events:
                if exited:
                    break
                continue
            for key, _ in events:
                fd = key.fd
                if proc.stdin is not None and key.fileobj is proc.stdin:
                    try:
                        pending = pending[os.write(fd, pending[:_CHUNK]) :]
                    except BlockingIOError:
                        continue
                    except OSError:  # the child closed its stdin: stop feeding it
                        pending = b""
                    if not pending:
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
        try:
            proc.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            raise CallTimeout(f"call exceeded its {timeout:g} s timeout") from None
    finally:
        selector.close()
    return b"".join(chunks[proc.stdout.fileno()]), b"".join(chunks[proc.stderr.fileno()])


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
