"""A recording ``Gateway`` for unit tests.

Scripted results are keyed by ``argv[0]`` (a result, an exception, or a callable taking the
recorded call). A call nothing was scripted for raises, so an unexpected process fails loudly.
Every call is recorded with its argv, env (or ``None``), cwd, stdin and timeout.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field

import pytest

from review_run import gateway as gw
from review_run.gateway import CompletedRun, GatewayError


@dataclass(frozen=True)
class Call:
    argv: tuple[str, ...]
    stdin: str | None
    env: dict[str, str] | None
    cwd: str | None
    timeout: float
    term_grace: float = 0.0
    detached: bool = False

    @property
    def env_names(self) -> list[str]:
        return [] if self.env is None else sorted(self.env)

    def strings(self) -> list[str]:
        """Everything a marker could hide in: argv, env names and values, cwd."""
        out = list(self.argv)
        if self.env is not None:
            out += list(self.env) + list(self.env.values())
        if self.cwd is not None:
            out.append(self.cwd)
        return out


Script = CompletedRun | GatewayError | Callable[[Call], CompletedRun]


def ok(stdout: str = "", stderr: str = "", returncode: int = 0) -> Callable[[Call], CompletedRun]:
    """A scripted result that echoes the call's argv back."""
    return lambda call: CompletedRun(call.argv, returncode, stdout, stderr)


@dataclass
class FakeGateway:
    scripts: dict[str, list[Script]] = field(default_factory=dict)
    calls: list[Call] = field(default_factory=list)

    def script(self, argv0: str, *results: Script) -> None:
        """Queue ``results`` for ``argv0``; the last one repeats once the queue is down to it."""
        self.scripts.setdefault(argv0, []).extend(results)

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        stdin: str | None = None,
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
        term_grace: float = 0.0,
    ) -> CompletedRun:
        gw.check_call(argv, timeout, stdin=stdin, env=env, cwd=cwd, term_grace=term_grace)
        call = Call(
            tuple(argv), stdin, None if env is None else dict(env), cwd, timeout, term_grace
        )
        self.calls.append(call)
        queue = self.scripts.get(argv[0])
        if not queue:
            raise AssertionError(f"unscripted gateway call: {argv[0]!r}")
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, GatewayError):
            raise item
        result = item(call) if callable(item) else item
        for text in (result.stdout, result.stderr):  # the real gateway's per-stream cap, in bytes
            if len(text.encode("utf-8", errors="surrogateescape")) > gw.MAX_OUTPUT:
                raise gw.OutputTooLarge(f"a call printed more than {gw.MAX_OUTPUT} bytes")
        return dataclasses.replace(result, argv=call.argv)

    def spawn_detached(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        stdin: str | None = None,
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
        term_grace: float = 0.0,
    ) -> gw.DetachedHandle:
        """Record a detached start; scripted exceptions (by ``argv[0]``) raise, anything else
        starts nothing and returns a finished handle."""
        gw.check_call(argv, timeout, stdin=stdin, env=env, cwd=cwd, term_grace=term_grace)
        self.calls.append(
            Call(
                tuple(argv),
                stdin,
                None if env is None else dict(env),
                cwd,
                timeout,
                term_grace,
                True,
            )
        )
        queue = self.scripts.get(argv[0])
        if queue:
            item = queue.pop(0) if len(queue) > 1 else queue[0]
            if isinstance(item, GatewayError):
                raise item
        return _DoneHandle()

    def assert_no_marker(self, marker: str) -> None:
        """Fail when ``marker`` (planted untrusted text) is in any recorded argv, env or cwd."""
        for call in self.calls:
            for text in call.strings():
                assert marker not in text, f"untrusted marker leaked into a call: {call.argv[0]}"


class _DoneHandle:
    pid = 0

    def join(self, timeout: float | None = None) -> bool:
        return True


@pytest.fixture
def fake_gateway() -> Iterator[FakeGateway]:
    fake = FakeGateway()
    previous = gw.set_gateway(fake)
    try:
        yield fake
    finally:
        gw.set_gateway(previous)
