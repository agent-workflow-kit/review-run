"""Shared machinery of the fake sibling binaries (standard library only).

Every fake is a small program run by the test interpreter through a generated wrapper (see
``tests/fakes.py``). The wrapper sets ``FAKEBIN_DIR``: a private per-test directory holding

* ``scenario.json``: what each command answers (written by the test through the harness),
* ``state.json``: mutable state shared by the fakes (agents, sessions, throwaway records),
* ``calls.jsonl``: one ``start`` and one ``end`` record per call: exact argv, env, cwd, stdin,
  exit code, stdout, stderr and the pinned schema the stdout must validate against.

The fakes never validate their own output; the harness validates every recorded call against the
pinned sibling schemas when the test ends (``FakeBin.assert_valid``).

A step of the scenario, ``scenario[tool][command]`` (a list; the last step repeats), is:

* ``{"merge": {...}}``: deep-merge into the command's default document (the usual case);
* ``{"json": doc}``: print ``doc`` instead of the default;
* ``{"error": {...}, "exit": N}``: print ``{"error": ...}`` and exit ``N``;
* ``{"exit": N}``: change the exit code of the default output;
* ``{"stdout_raw": text}``: print ``text`` as is (garbage, truncated JSON, ...), schema ``null``;
* ``{"stderr": text}``, ``{"delay": seconds}``, ``{"hang": true}`` (sleep until signaled),
  ``{"schema": name | null}`` (override the schema the harness validates against).
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import signal
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn

#: Versions the fakes report; ``tests/test_fakes_harness.py`` keeps them equal to the pinned ones.
VERSIONS = {
    "forgectl": "0.56.0",
    "pr-comment": "0.39.0",
    "agent-spawn": "0.38.1",
    "claude-session": "0.45.2",
    "wtctl": "1.2.0",
}

EXIT_UNIMPLEMENTED = 99  # the real tool has this command, the fake does not (a test bug)
EXIT_FAKE_BUG = 98  # the fake itself is misconfigured

ACTOR = re.compile(r"^(human|auto):[A-Za-z0-9._@-]{1,64}$")
DECISION_ID = re.compile(r"^[A-Za-z0-9._:@/-]{1,128}$")
LABEL = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}=[A-Za-z0-9._:/#@+=,-]{0,256}$")
DURATION = re.compile(r"^([0-9]+(?:\.[0-9]+)?)(ms|s|m|h|d)?$")
SHA40 = re.compile(r"^[0-9a-f]{40}$")


class Usage(Exception):
    """A usage error: the real tool exits 2 with ``code: usage``."""

    def __init__(self, message: str, *, code: str = "usage", details: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.details = details


class Unimplemented(Exception):
    """A real command the fake does not emulate."""


# --------------------------------------------------------------------------------------------
# argv grammar
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Flag:
    """One flag. ``takes``: it has a value. ``repeat``: it may be given several times."""

    takes: bool = False
    repeat: bool = False
    choices: tuple[str, ...] | None = None
    check: Callable[[str], str | None] | None = None  # returns an error message or None


@dataclass
class Parsed:
    command: tuple[str, ...]
    args: list[str]
    flags: dict[str, list[str]] = field(default_factory=dict)

    def has(self, name: str) -> bool:
        return name in self.flags

    def get(self, name: str, default: str | None = None) -> str | None:
        values = self.flags.get(name)
        return values[-1] if values else default

    def all(self, name: str) -> list[str]:
        return list(self.flags.get(name, []))


def flag(*, takes: bool = False, repeat: bool = False, choices: tuple[str, ...] | None = None,
         check: Callable[[str], str | None] | None = None) -> Flag:  # fmt: skip
    return Flag(takes=takes, repeat=repeat, choices=choices, check=check)


def duration_check(value: str) -> str | None:
    match = DURATION.fullmatch(value)
    if not match or len(value) > 32:
        return "is not a duration"
    number, unit = float(match.group(1)), match.group(2) or "s"
    if number == 0:
        return "must be above zero"
    if unit == "d" and number > 36500:
        return "is too long"
    return None


def actor_check(value: str) -> str | None:
    return None if ACTOR.fullmatch(value) else "is not an actor"


def decision_check(value: str) -> str | None:
    return None if DECISION_ID.fullmatch(value) else "is not a decision id"


def label_check(value: str) -> str | None:
    return None if LABEL.fullmatch(value) else "is not key=value"


def sha_check(value: str) -> str | None:
    return None if SHA40.fullmatch(value) else "is not a full lowercase sha"


def positive_int_check(value: str) -> str | None:
    return None if re.fullmatch(r"[1-9][0-9]{0,8}", value) else "is not a positive integer"


@dataclass(frozen=True)
class CommandSpec:
    flags: dict[str, Flag] = field(default_factory=dict)
    min_args: int = 0
    max_args: int = 0


@dataclass(frozen=True)
class Grammar:
    """The argv grammar of one tool.

    ``commands`` maps a command path (``("show",)``, ``("draft", "new")``) to its spec;
    ``global_flags`` are accepted before and after the command unless ``global_after_only``;
    ``duplicates`` is ``"error"`` (a repeated scalar flag exits 2) or ``"same_ok"`` (the same
    value twice is accepted, a different one is an error).
    """

    tool: str
    global_flags: dict[str, Flag]
    commands: dict[tuple[str, ...], CommandSpec]
    unimplemented: frozenset[tuple[str, ...]] = frozenset()
    duplicates: str = "error"
    global_before_command: frozenset[str] | None = None  # None: every global flag
    mutually_exclusive: tuple[tuple[str, str], ...] = ()


def _split(token: str) -> tuple[str, str | None]:
    if token.startswith("--") and "=" in token:
        name, _, value = token.partition("=")
        return name, value
    return token, None


def parse(grammar: Grammar, argv: list[str]) -> Parsed:
    """Parse ``argv`` (without argv[0]) as the real tool does, or raise ``Usage``.

    Flags and commands are matched in full (no abbreviations); an unknown flag, a missing value,
    a repeated scalar flag, ``--`` and a value that looks like a flag are usage errors.
    """
    for token in argv:
        if "\0" in token:
            raise Usage("an argument contains NUL")
    if len(argv) > 1024:
        raise Usage("too many arguments")
    commands = grammar.commands
    heads = {c[0] for c in commands} | {c[0] for c in grammar.unimplemented}
    cmd_words: list[str] = []
    spec: CommandSpec | None = None
    flags: dict[str, list[str]] = {}
    positionals: list[str] = []
    i = 0

    def lookup(name: str) -> Flag | None:
        if spec is not None and name in spec.flags:
            return spec.flags[name]
        if name in grammar.global_flags:
            if (
                spec is None
                and grammar.global_before_command is not None
                and name not in grammar.global_before_command
            ):
                raise Usage(f"{name} must follow the command")
            return grammar.global_flags[name]
        return None

    while i < len(argv):
        token = argv[i]
        i += 1
        if token == "--":
            raise Usage("'--' is not accepted")
        if token.startswith("-") and token != "-":
            name, inline = _split(token)
            fl = lookup(name)
            if fl is None:
                raise Usage(f"unknown flag {name}")
            if fl.takes:
                if inline is not None:
                    value = inline
                elif i < len(argv) and (argv[i] == "-" or not argv[i].startswith("-")):
                    value = argv[i]
                    i += 1
                else:
                    raise Usage(f"{name} needs a value")
                if fl.choices is not None and value not in fl.choices:
                    raise Usage(f"{name}: invalid choice")
                if fl.check is not None and (problem := fl.check(value)):
                    raise Usage(f"{name} {problem}")
            else:
                if inline is not None:
                    raise Usage(f"{name} takes no value")
                value = "true"
            seen = flags.get(name)
            if seen is not None and not fl.repeat:
                if grammar.duplicates == "error" or seen[-1] != value:
                    raise Usage(f"{name} given more than once")
            flags.setdefault(name, []).append(value)
            continue
        # a positional word
        if spec is None:
            cmd_words.append(token)
            key = tuple(cmd_words)
            if key in commands:
                spec = commands[key]
            elif key in grammar.unimplemented:
                raise Unimplemented(" ".join(key))
            elif len(cmd_words) == 1 and cmd_words[0] in heads:
                continue  # a multi-word command: wait for its second word
            else:
                raise Usage(f"unknown command {token!r}")
        else:
            positionals.append(token)
    if spec is None:
        raise Usage("a command is required" if not cmd_words else "incomplete command")
    if not spec.min_args <= len(positionals) <= spec.max_args:
        raise Usage("wrong number of arguments")
    for a, b in grammar.mutually_exclusive:
        if a in flags and b in flags:
            raise Usage(f"{a} and {b} are mutually exclusive")
    return Parsed(tuple(cmd_words), positionals, flags)


# --------------------------------------------------------------------------------------------
# the per-test directory: scenario, state, call log
# --------------------------------------------------------------------------------------------


def fake_dir() -> Path:
    raw = os.environ.get("FAKEBIN_DIR")
    if not raw or not os.path.isabs(raw):
        sys.stderr.write("fake: FAKEBIN_DIR is not set (run the fakes through tests/fakes.py)\n")
        raise SystemExit(EXIT_FAKE_BUG)
    return Path(raw)


@contextmanager
def locked() -> Iterator[None]:
    fd = os.open(fake_dir() / "lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def write_json(path: Path, value: Any) -> None:
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(value), encoding="utf-8")
    os.replace(tmp, path)


def scenario(tool: str) -> dict[str, Any]:
    data = read_json(fake_dir() / "scenario.json", {})
    section = data.get(tool, {})
    if not isinstance(section, dict):
        raise SystemExit(EXIT_FAKE_BUG)
    return section


class State:
    """``state.json``: read-modify-write under the directory lock (use as a context manager)."""

    def __enter__(self) -> dict[str, Any]:
        self._lock = locked()
        self._lock.__enter__()
        self.data: dict[str, Any] = read_json(fake_dir() / "state.json", {})
        return self.data

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        try:
            if exc_type is None:
                write_json(fake_dir() / "state.json", self.data)
        finally:
            self._lock.__exit__(exc_type, exc, tb)


def next_step(tool: str, command: str) -> dict[str, Any] | None:
    """The scenario step for this call of ``command`` (a counter per command, last one repeats)."""
    steps = scenario(tool).get(command)
    if not steps:
        return None
    if isinstance(steps, dict):
        steps = [steps]
    with State() as state:
        counters = state.setdefault("_counters", {})
        key = f"{tool}/{command}"
        index = counters.get(key, 0)
        counters[key] = index + 1
    step = steps[min(index, len(steps) - 1)]
    if not isinstance(step, dict):
        raise SystemExit(EXIT_FAKE_BUG)
    return step


# --------------------------------------------------------------------------------------------
# a call: log, output, signals
# --------------------------------------------------------------------------------------------


@dataclass
class Out:
    """What a command prints. ``doc`` is JSON (stdout); ``text`` replaces it verbatim."""

    exit: int = 0
    doc: Any = None
    text: str | None = None
    stderr: str = ""
    schema: str | None = None


class Call:
    """The process-wide record of this call and the way out of it."""

    def __init__(self, tool: str, argv: list[str]):
        self.tool = tool
        self.argv = argv
        self.id = uuid.uuid4().hex
        self.stdin_text: str | None = None
        self.done = False
        self._log(
            {
                "ev": "start",
                "id": self.id,
                "tool": tool,
                "argv": argv,
                "pid": os.getpid(),
                "ppid": os.getppid(),
                "pgid": os.getpgrp(),
                "cwd": os.getcwd(),
                "env": dict(os.environ),
                "ts": time.time(),
            }
        )
        self.on_signal: Callable[[int], Out | None] = lambda signum: None
        self.before_finish: Callable[[Out], None] = lambda out: None
        signal.signal(signal.SIGTERM, self._signal)
        signal.signal(signal.SIGINT, self._signal)
        signal.signal(signal.SIGHUP, self._signal)

    @staticmethod
    def _log(record: dict[str, Any]) -> None:
        line = (json.dumps(record) + "\n").encode("utf-8")
        fd = os.open(fake_dir() / "calls.jsonl", os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)

    def read_stdin(self) -> str:
        if self.stdin_text is None:
            data = sys.stdin.buffer.read() if not sys.stdin.isatty() else b""
            self.stdin_text = data.decode("utf-8", errors="surrogateescape")
        return self.stdin_text or ""

    def _signal(self, signum: int, _frame: object) -> None:
        """A signal ends the call by that signal's exit code (128+N) unless the tool says more."""
        out = self.on_signal(signum)
        if out is None:
            out = Out(exit=128 + signum, schema=None)
        self.finish(out, signaled=signum)

    def finish(self, out: Out, *, signaled: int | None = None) -> NoReturn:
        if self.done:
            os._exit(out.exit)
        self.done = True
        try:
            self.before_finish(out)
        except OSError:  # a result file that cannot be written is a warning, never a new failure
            pass
        if out.text is not None:
            stdout = out.text
        elif out.doc is not None:
            stdout = json.dumps(out.doc) + "\n"
        else:
            stdout = ""
        self._log(
            {
                "ev": "end",
                "id": self.id,
                "exit": out.exit,
                "stdout": stdout,
                "stderr": out.stderr,
                "stdin": self.stdin_text,
                "schema": out.schema if out.text is None else None,
                "signal": signaled,
            }
        )
        try:
            if stdout:
                sys.stdout.write(stdout)
            sys.stdout.flush()
            if out.stderr:
                sys.stderr.write(out.stderr if out.stderr.endswith("\n") else out.stderr + "\n")
            sys.stderr.flush()
        except (BrokenPipeError, OSError):
            pass
        os._exit(out.exit)


def deep_merge(base: Any, patch: Any) -> Any:
    if isinstance(base, dict) and isinstance(patch, dict):
        merged = dict(base)
        for key, value in patch.items():
            merged[key] = deep_merge(base.get(key), value) if key in base else value
        return merged
    return patch


#: Keys a tool's pinned error schema requires even when empty (agent-spawn lists them all).
ENVELOPE_DEFAULTS: dict[str, dict[str, Any]] = {
    "agent-spawn": {
        "reason": None, "agent_id": None, "tool": "agent-spawn", "lower_code": None,
        "lower_error": None, "details": None, "candidates": [], "hook": None,
        "hook_stderr": None, "decision_ids": [],
    },
}  # fmt: skip


def error_out(tool: str, exit_code: int, error: dict[str, Any], stderr: str | None = None) -> Out:
    error = {**ENVELOPE_DEFAULTS.get(tool, {}), **error}
    message = str(error.get("message", error.get("code", "error")))
    return Out(
        exit=exit_code,
        doc={"error": error},
        stderr=stderr if stderr is not None else f"{tool}: error: {message}",
        schema=f"{tool}/cli/error.json",
    )


def hang(call: Call) -> None:
    """Sleep until a signal ends the call (the gateway's timeout kills the process group)."""
    while True:
        time.sleep(0.05)
        # Keep the loop observable but cheap: the handler exits the process.
        if call.done:
            return


def apply_step(
    tool: str,
    step: dict[str, Any] | None,
    default: Callable[[], Out],
    call: Call,
    *,
    schema: str | None,
) -> Out:
    """Turn a scenario step (or nothing) into the call's output."""
    if step is None:
        out = default()
        out.schema = out.schema if out.schema is not None or out.text is not None else schema
        return out
    if step.get("hang"):
        hang(call)
    if "delay" in step:
        time.sleep(float(step["delay"]))
    if "error" in step:
        out = error_out(tool, int(step.get("exit", 12)), dict(step["error"]), step.get("stderr"))
    elif "stdout_raw" in step:
        out = Out(exit=int(step.get("exit", 0)), text=str(step["stdout_raw"]), schema=None)
    elif "json" in step:
        out = Out(exit=int(step.get("exit", 0)), doc=step["json"], schema=schema)
    else:
        out = default()
        if "merge" in step and out.doc is not None:
            out.doc = deep_merge(out.doc, step["merge"])
        if "exit" in step:
            out.exit = int(step["exit"])
        if out.schema is None and out.text is None:
            out.schema = schema
    if "stderr" in step:
        out.stderr = str(step["stderr"])
    if "schema" in step:
        out.schema = step["schema"]
    return out


def usage_out(tool: str, exc: Usage, *, json_mode: bool) -> Out:
    error: dict[str, Any] = {"code": exc.code, "message": exc.message}
    if exc.details is not None:
        error["details"] = exc.details
    out = error_out(tool, 2, error, f"{tool}: error: {exc.message}")
    if not json_mode:
        out.doc = None
        out.schema = None
    return out


def version_out(tool: str) -> Out:
    step = next_step(tool, "--version")
    text = f"{tool} {VERSIONS[tool]}\n"
    if step and "stdout_raw" in step:
        return Out(exit=int(step.get("exit", 0)), text=str(step["stdout_raw"]))
    if step and "version" in step:
        text = f"{tool} {step['version']}\n"
    return Out(text=text)


def json_requested(argv: list[str]) -> bool:
    return "--json" in argv


def run_tool(
    tool: str,
    grammar: Grammar,
    handler: Callable[[Call, Parsed], Out],
    argv: list[str],
    setup: Callable[[Call], None] | None = None,
) -> None:
    """Common ``main``: log, parse, dispatch, print, exit. Never returns."""
    call = Call(tool, argv)
    if setup is not None:
        setup(call)
    try:
        if "--version" in argv and not any(a in ("--help", "-h") for a in argv):
            call.finish(version_out(tool))
        if "--help" in argv or "-h" in argv:
            call.finish(Out(text=f"usage: {tool} ... (fake help)\n"))
        try:
            parsed = parse(grammar, argv)
        except Usage as exc:
            call.finish(usage_out(tool, exc, json_mode=json_requested(argv)))
        except Unimplemented as exc:
            call.finish(Out(exit=EXIT_UNIMPLEMENTED, stderr=f"fake {tool}: {exc} is not emulated"))
        try:
            out = handler(call, parsed)
        except Usage as exc:
            out = usage_out(tool, exc, json_mode=parsed.has("--json"))
        except Unimplemented as exc:
            out = Out(exit=EXIT_UNIMPLEMENTED, stderr=f"fake {tool}: {exc} is not emulated")
        call.finish(out)
    except SystemExit:
        raise
    except BaseException as exc:  # a bug in a fake must be loud, never a plausible answer
        call.finish(
            Out(exit=EXIT_FAKE_BUG, stderr=f"fake {tool}: bug: {type(exc).__name__}: {exc}")
        )


def iso(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts if ts is not None else time.time()))


def base32_id(prefix: str, seed: str) -> str:
    import hashlib

    alphabet = "abcdefghijklmnopqrstuvwxyz234567"
    digest = hashlib.sha256(seed.encode()).digest()
    return prefix + "".join(alphabet[b % 32] for b in digest[:8])


def result_file_write(path: str, text: str) -> None:
    """``--result-file``: exactly what ``--json`` prints, written atomically with mode 0600."""
    target = Path(path)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(tmp, target)
