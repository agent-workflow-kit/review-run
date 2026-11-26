"""Launch: agent-spawn arguments, result files, the result mapping and the blocking wait (#18).

``launch_argv`` builds the ``agent-spawn launch`` argv from the run's stored settings (flags, never
a positional argument and never ``--wait``, ``--ephemeral`` or ``--worktree``; every value is a
closed grammar or an absolute path, so untrusted text has no way into argv or env). Results are
read from stdout, and from ``launch.json`` / ``wait.json`` when stdout is unusable: opened without
following links, at most 1 MiB, strict JSON of the closed shape (a file that does not parse or
validate counts as absent).

``map_launch`` and ``map_wait`` apply the interface's mapping table (Launch) and return a
``Disposition``; they do no I/O. ``wait_session`` is the blocking ``agent-spawn wait`` with its
three attempts, ``teardown_parked`` the ownership-checked teardown of an agent a failed launch left
parked.

TODO(#27): signals during launch and wait (record the cancellation, forward SIGTERM to the
launcher, ``agent-spawn stop``). A waiter that exits 130 or 143 is reported as ``interrupted``.
"""

from __future__ import annotations

import os
import re
import stat
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

from . import gateway
from .durations import DurationError, parse_duration
from .errors import ReviewRunError
from .settings import RunSettings, launch_flags, strict_finite_json

RESULT_MAX_BYTES: Final = 1024 * 1024
LAUNCH_TIMEOUT: Final = 900.0  # agent-spawn bounds itself; this is the backstop of the call
SHOW_TIMEOUT: Final = 60.0
TEARDOWN_TIMEOUT: Final = 300.0
WAIT_ATTEMPTS: Final = 3
WAIT_RETRY_SECONDS: Final = 2.0
DEFAULT_SESSION_SECONDS: Final = 120 * 60  # the kind's default timeout (config ``timeout: null``)
UNLIMITED_SESSION_SECONDS: Final = 24 * 60 * 60  # the cap of the call (and of a bad value)
WAIT_MARGIN_SECONDS: Final = 15 * 60

LAUNCH_FILE: Final = "launch.json"
WAIT_FILE: Final = "wait.json"
PROMPT_FILE: Final = "prompt.md"
PROFILE_FILE: Final = "permission-profile.json"

_AGENT_ID: Final = re.compile(r"ag-[a-z2-7]{8}\Z")
_CSESSION_ID: Final = re.compile(r"cs-[a-z2-7]{8}\Z")
_WORD: Final = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_TOOL: Final = re.compile(r"[a-z][a-z0-9-]{0,31}\Z")
_OUTCOMES: Final = frozenset(
    {
        "done",
        "blocked",
        "needs-input",
        "failed",
        "exited-unreported",
        "killed",
        "timeout",
        "lost",
        "not-started",
        "vetoed",
        "escalated",
    }
)
_SIBLING_TOOLS: Final = frozenset(
    {"forgectl", "pr-comment", "agent-spawn", "claude-session", "wtctl"}
)
# Session outcomes with their run reason and exit code (the mapping table).
_SESSION_FAILURES: Final = {
    14: "unreported",
    15: "killed",
    16: "timeout",
    17: "lost",
    18: "session_failed",
}
INTERRUPT_EXITS: Final = (130, 143)
ENVELOPE_FILE_EXIT: Final = 12  # the exit assumed for an error envelope read from a result file


# -- the argv ------------------------------------------------------------------------------------


def launch_argv(
    settings: RunSettings,
    *,
    repo: str,
    pr: str,
    run_id: str,
    run_dir: Path | str,
    input_paths: Sequence[Path | str],
    labels: Sequence[str] = (),
    actor: str | None = None,
    decision_ids: Sequence[str] = (),
) -> list[str]:
    """``agent-spawn launch`` with the flags of the Launch table, in the order of
    ``settings.launch_flags``, then ``--prompt-file`` and one ``--input-file`` per input.

    Never passed: ``--wait``, ``--ephemeral``, ``--worktree`` (``none`` is the default), a
    positional argument or ``--prompt`` (untrusted text travels in the files only).
    """
    base = Path(run_dir)
    for path in (base / PROMPT_FILE, *map(Path, input_paths)):
        if not path.is_absolute():
            raise ValueError("a launch file path is absolute")
    flags = launch_flags(
        settings,
        repo=repo,
        pr=pr,
        run_id=run_id,
        profile_path=str(base / PROFILE_FILE),
        result_file=str(base / LAUNCH_FILE),
        labels=labels,
        actor=actor,
        decision_ids=decision_ids,
    )
    flags += ["--prompt-file", str(base / PROMPT_FILE)]
    for path in input_paths:
        flags += ["--input-file", str(path)]
    return ["agent-spawn", "launch", *flags]


def wait_argv(agent_id: str, run_dir: Path | str) -> list[str]:
    if not _AGENT_ID.match(agent_id):
        raise ValueError("not an agent id")
    return [
        "agent-spawn",
        "wait",
        agent_id,
        "--result-file",
        str(Path(run_dir) / WAIT_FILE),
        "--json",
    ]


def wait_timeout_seconds(settings: RunSettings) -> float:
    """The backstop of the ``wait`` call: the session's own timeout plus a margin (agent-spawn
    bounds the session; review-run never passes a wait timeout)."""
    text = settings.value("timeout")
    if text is None:
        seconds = DEFAULT_SESSION_SECONDS
    else:
        try:
            seconds = int(parse_duration(str(text), positive=True).total_seconds())
        except DurationError:
            seconds = UNLIMITED_SESSION_SECONDS
    return float(min(seconds, UNLIMITED_SESSION_SECONDS) + WAIT_MARGIN_SECONDS)


# -- result files and parsing --------------------------------------------------------------------


def read_result_file(path: Path | str) -> dict[str, Any] | None:
    """A sibling result file, or ``None`` (absent, a link, not a regular file, over 1 MiB, not
    UTF-8, not strict JSON, not an object): a file that cannot be used counts as absent."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        with os.fdopen(fd, "rb", closefd=False) as handle:
            data = handle.read(RESULT_MAX_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    if len(data) > RESULT_MAX_BYTES:
        return None
    try:
        return parse_document(data.decode("utf-8"))
    except UnicodeDecodeError:
        return None


def parse_document(text: str) -> dict[str, Any] | None:
    """Strict, finite JSON object, or ``None``."""
    try:
        doc = strict_finite_json(text)
    except (ValueError, RecursionError):
        return None
    return doc if isinstance(doc, dict) else None


@dataclass(frozen=True)
class Envelope:
    """The closed parts of an agent-spawn error envelope (``lower_error`` text is dropped)."""

    code: str
    reason: str | None
    tool: str | None
    lower_code: str | None
    agent_id: str | None
    retryable: bool
    cause: str | None


@dataclass(frozen=True)
class SpawnResult:
    """One agent-spawn answer (``launch``, ``resume`` or ``wait``): a result object or an
    envelope. ``run_number`` is the final run's number (``run`` is an object), else ``None``."""

    exit_code: int
    envelope: Envelope | None = None
    agent_id: str | None = None
    csession_id: str | None = None
    agent_status: str | None = None
    agent_run: int | None = None
    phase: str | None = None
    outcome: str | None = None
    end_reason: str | None = None
    run_number: int | None = None
    timed_out: bool = False

    @property
    def final(self) -> bool:
        return self.run_number is not None


def _word(value: object) -> str | None:
    return value if isinstance(value, str) and _WORD.match(value) else None


def _count(value: object, minimum: int = 1) -> int | None:
    return value if type(value) is int and value >= minimum else None  # noqa: E721


def parse_envelope(doc: Mapping[str, Any]) -> Envelope | None:
    error = doc.get("error")
    if not isinstance(error, dict):
        return None
    code = _word(error.get("code"))
    if code is None:
        return None
    tool = error.get("tool")
    agent_id = error.get("agent_id")
    details = error.get("details")
    details = details if isinstance(details, dict) else {}
    cause = details.get("cause")
    return Envelope(
        code=code,
        reason=_word(error.get("reason")),
        tool=tool if isinstance(tool, str) and _TOOL.match(tool) else None,
        lower_code=_word(error.get("lower_code")),
        agent_id=agent_id if isinstance(agent_id, str) and _AGENT_ID.match(agent_id) else None,
        retryable=details.get("retryable") is True,
        cause=_word(cause),
    )


def parse_result(exit_code: int, doc: Mapping[str, Any] | None) -> SpawnResult | None:
    """The closed shape of an answer, or ``None`` when ``doc`` is neither an envelope nor a
    result object (then it counts as absent). A final run whose ``exit_code`` differs from the
    process's exit code is unusable."""
    if doc is None:
        return None
    if "error" in doc:
        envelope = parse_envelope(doc)
        return None if envelope is None else SpawnResult(exit_code, envelope=envelope)
    agent = doc.get("agent")
    if not isinstance(agent, dict):
        return None
    agent_id = agent.get("agent_id")
    csession = agent.get("csession_id")
    status = agent.get("status")
    if not isinstance(agent_id, str) or not _AGENT_ID.match(agent_id):
        return None
    if csession is not None and not (isinstance(csession, str) and _CSESSION_ID.match(csession)):
        return None
    phase = doc.get("phase")
    outcome = doc.get("outcome")
    run = doc.get("run")
    if phase not in ("launch", "session", None):
        return None
    if outcome is not None and outcome not in _OUTCOMES:
        return None
    run_number = None
    if run is not None:
        if not isinstance(run, dict) or phase is None or outcome is None:
            return None
        run_number = _count(run.get("run"))
        if run_number is None or doc.get("exit_code") != exit_code:
            return None
    return SpawnResult(
        exit_code=exit_code,
        agent_id=agent_id,
        csession_id=csession,
        agent_status=status if isinstance(status, str) and _WORD.match(status) else None,
        agent_run=_count(agent.get("run")),
        phase=phase,
        outcome=outcome,
        end_reason=_word(doc.get("end_reason")),
        run_number=run_number,
        timed_out=doc.get("timed_out") is True,
    )


def interpret_file(path: Path | str) -> SpawnResult | None:
    """A result file read after its writer was ended (a launcher the gateway killed, a crashed
    runner): the exit code is the run's own when the file holds a final run, else 0 (started)."""
    doc = read_result_file(path)
    if doc is None:
        return None
    code = doc.get("exit_code")
    if type(code) is int:  # noqa: E721
        return parse_result(code, doc)
    # An error envelope carries no exit code of its own: it came from a failed call (12), so the
    # mapping table treats it as ``launch_failed``; any other result is a started run (0).
    return parse_result(ENVELOPE_FILE_EXIT if "error" in doc else 0, doc)


def interpret(
    exit_code: int, stdout: str, result_file: Path | str | None = None
) -> SpawnResult | None:
    """The answer of one call: stdout when it is usable, else the result file."""
    found = parse_result(exit_code, parse_document(stdout))
    if found is None and result_file is not None:
        found = parse_result(exit_code, read_result_file(result_file))
    return found


# Reasons whose parked agent is torn down after the terminal transaction.
TEARDOWN_REASONS: Final = frozenset(
    {"launch_failed", "unreported", "killed", "timeout", "lost", "session_failed", "internal"}
)

# -- the mapping table ---------------------------------------------------------------------------

Kind = Literal[
    "started", "finalize", "escalate", "fail", "retry", "interrupted", "agent_gone"
]  # "unchanged" is "retry" for ``wait`` and never occurs for ``launch``


@dataclass(frozen=True)
class Disposition:
    """What a result means for the run. ``agent`` is the result to adopt (its ids and run number
    go on the row); ``teardown_agent`` names a parked agent a failed launch left."""

    kind: Kind
    reason: str | None = None
    exit_code: int | None = None
    failure: dict[str, Any] | None = None
    agent: SpawnResult | None = None
    teardown_agent: str | None = None
    envelope: Envelope | None = None


def failure_object(
    tool: str | None,
    lower_code: str | None,
    lower_reason: str | None = None,
    *,
    retryable: bool = False,
) -> dict[str, Any]:
    return {
        "tool": tool if tool in _SIBLING_TOOLS else None,
        "lower_code": lower_code,
        "lower_reason": lower_reason,
        "retryable": retryable,
        "retry_after_s": None,
        "errors": [],
    }


def _internal(result: SpawnResult | None = None) -> Disposition:
    """``internal`` (exit 12). A result that names an agent carries it, so the agent is stored and
    torn down (ownership-checked; agent-spawn refuses a live one)."""
    if result is None:
        return Disposition("fail", "internal", 12)
    agent = result if result.envelope is None else None
    teardown = result.envelope.agent_id if result.envelope is not None else None
    return Disposition("fail", "internal", 12, agent=agent, teardown_agent=teardown)


def _canceled(code: int) -> Disposition:
    return Disposition("fail", "canceled", code)


def _launch_failed(result: SpawnResult) -> Disposition:
    """A launch that did not start a session: an envelope, or a run that ended in its launch
    phase. Only closed codes are kept."""
    env = result.envelope
    if env is not None:
        failure = failure_object(
            env.tool or "agent-spawn",
            env.lower_code or env.code,
            env.reason,
            retryable=env.retryable,
        )
        return Disposition("fail", "launch_failed", 12, failure, None, env.agent_id, env)
    outcome = (result.outcome or "not-started").replace("-", "_")
    # A hook veto or escalation is agent-spawn's own; a launch never adopted is claude-session's.
    hook = result.outcome in ("vetoed", "escalated") or (result.end_reason or "").startswith(
        "hook_"
    )
    tool = "agent-spawn" if hook else "claude-session"
    failure = failure_object(tool, outcome, result.end_reason)
    return Disposition("fail", "launch_failed", 12, failure, result, result.agent_id)


def _session_outcome(result: SpawnResult) -> Disposition:
    """A run that ended in its session phase (exits 0, 13, 14 to 18)."""
    code = result.exit_code
    if code == 0 and result.outcome == "done":
        return Disposition("finalize", agent=result)
    if code == 13 and result.outcome in ("needs-input", "blocked"):
        reason = "needs_input" if result.outcome == "needs-input" else "blocked"
        return Disposition("escalate", reason, 13, agent=result)
    if code in _SESSION_FAILURES:
        return Disposition("fail", _SESSION_FAILURES[code], code, agent=result)
    return _internal(result)


def map_launch(result: SpawnResult | None, exit_code: int) -> Disposition:
    """The row of the mapping table for ``agent-spawn launch``'s answer.

    A usable result that started a run (``run`` null, exit 0) is ``started``; a final run is
    mapped like a ``wait`` result; an envelope, or a run that ended in its launch phase, is
    ``launch_failed`` (exit 12); 130 or 143 is ``canceled``; exit 1, 22 or an answer that does not
    parse is ``internal``.
    """
    if result is None:
        return _canceled(exit_code) if exit_code in INTERRUPT_EXITS else _internal()
    if result.envelope is not None:
        if exit_code in INTERRUPT_EXITS:
            return _canceled(exit_code)
        if exit_code in (0, 1, 22):
            return _internal(result)
        return _launch_failed(result)
    if result.phase == "launch" and result.final:
        return _launch_failed(result)
    if not result.final:
        if exit_code == 0 and result.csession_id is not None and result.agent_run is not None:
            return Disposition("started", agent=result)
        return _internal(result)
    if result.phase == "session":
        return _session_outcome(result)
    return _internal(result)


def map_wait(result: SpawnResult | None, exit_code: int) -> Disposition:
    """The row of the mapping table for ``agent-spawn wait``'s answer.

    Exit 10 or a torn-down agent is ``agent_gone``; 130 and 143 mean the waiter was interrupted;
    exit 22 is ``internal``; any other envelope (exit 1 included), or an answer that does not
    parse, is ``retry`` (the run is unchanged).
    """
    if exit_code in INTERRUPT_EXITS:
        return Disposition("interrupted", exit_code=exit_code)
    if result is None:
        return Disposition("retry")
    if result.envelope is not None:
        if exit_code == 10:
            return Disposition("agent_gone", "agent_gone", 12)
        if exit_code in (0, 22):
            return _internal(result)
        return Disposition("retry", envelope=result.envelope)
    if result.agent_status == "torn_down":
        return Disposition("agent_gone", "agent_gone", 12)
    if not result.final:
        return _internal(result)  # a wait that timed out (22) cannot happen: no timeout is passed
    if result.phase == "launch":
        return _launch_failed(result)
    return _session_outcome(result)


# -- calls ---------------------------------------------------------------------------------------


def wait_session(
    agent_id: str,
    run_dir: Path | str,
    *,
    timeout: float,
    sleep: Callable[[float], None] = time.sleep,
    attempts: int = WAIT_ATTEMPTS,
    retry_seconds: float = WAIT_RETRY_SECONDS,
    csession_id: str | None = None,
    awaited_run: int | None = None,
) -> Disposition:
    """``agent-spawn wait <agent id> --result-file <run dir>/wait.json --json``, retried (3
    attempts, 2 s apart) while it gives an envelope, output that does not parse or a result that is
    not the stored session's (another agent, ``csession_id`` or run number when given). Raises
    ``lower_failed`` (exit 12, the run unchanged) when every attempt failed, and at once for a
    call that timed out or printed too much (a retry could block for hours) or a missing tool."""
    argv = wait_argv(agent_id, run_dir)
    result_file = Path(run_dir) / WAIT_FILE
    last_code = "failed"
    last_retryable = False
    for attempt in range(attempts):
        if attempt:
            sleep(retry_seconds)
        result_file.unlink(missing_ok=True)  # a stale file must not stand in for this call
        try:
            done = gateway.run(argv, timeout=timeout)
        except (gateway.ToolMissing, gateway.CallTimeout, gateway.OutputTooLarge) as exc:
            raise _lower_failed(exc.lower_code, False) from None
        except gateway.GatewayError as exc:
            last_code, last_retryable = exc.lower_code, False
            continue
        found = interpret(done.returncode, done.stdout, result_file)
        if found is not None and not _is_session(found, agent_id, csession_id, awaited_run):
            found = None  # another agent's, session's or run's result is unusable
        disposition = map_wait(found, done.returncode)
        if disposition.kind != "retry":
            return disposition
        env = disposition.envelope
        last_code = (env.lower_code or env.code) if env else "invalid_output"
        last_retryable = bool(env and env.retryable)
    raise _lower_failed(last_code, last_retryable)


def _is_session(
    result: SpawnResult, agent_id: str, csession_id: str | None, awaited_run: int | None
) -> bool:
    """Whether a result is the stored session's: an envelope names nothing to compare."""
    if result.envelope is not None:
        return True
    if result.agent_id != agent_id:
        return False
    if csession_id is not None and result.csession_id != csession_id:
        return False
    return not (awaited_run is not None and result.final and result.run_number != awaited_run)


def _lower_failed(lower_code: str, retryable: bool) -> ReviewRunError:
    return ReviewRunError(
        "failed",
        f"agent-spawn failed ({lower_code})",
        reason="lower_failed",
        tool="agent-spawn",
        lower_code=lower_code,
        details={"retryable": True} if retryable else None,
    )


def owns_agent(
    doc: Mapping[str, Any], *, run_id: str, pr: str, repo: str, csession_id: str | None
) -> bool:
    """The ownership check before ``stop`` or ``teardown`` (Labels): kind ``review``, the four
    labels of this run and the stored session id when one is stored."""
    agent = doc.get("agent")
    if not isinstance(agent, dict):
        return False
    labels = agent.get("labels")
    wanted = {"owner=review-run", f"run={run_id}", f"pr={pr}", f"repo={repo}"}
    if not isinstance(labels, list) or not wanted <= {x for x in labels if isinstance(x, str)}:
        return False
    if agent.get("kind") != "review":
        return False
    return csession_id is None or agent.get("csession_id") == csession_id


Teardown = Literal["done", "gone", "mismatch", "live", "refused", "failed"]


def check_parked(
    agent_id: str, *, run_id: str, pr: str, repo: str, csession_id: str | None
) -> Literal["owned", "gone", "mismatch", "live", "failed"]:
    """``agent-spawn show`` and the ownership check (the first half of ``teardown_parked``):
    ``owned`` (parked, this run's: may be torn down), ``gone`` (unknown or torn down),
    ``mismatch`` (not this run's: never touched), ``live`` or ``failed`` (``show`` failed)."""
    if not _AGENT_ID.match(agent_id):
        raise ValueError("not an agent id")
    try:
        shown = gateway.run(["agent-spawn", "show", agent_id, "--json"], timeout=SHOW_TIMEOUT)
    except gateway.GatewayError:
        return "failed"
    doc = parse_document(shown.stdout)
    if shown.returncode == 10:
        return "gone"
    if shown.returncode != 0 or doc is None:
        return "failed"
    agent = doc.get("agent")
    status = agent.get("status") if isinstance(agent, dict) else None
    if status == "torn_down":
        return "gone"
    if not owns_agent(doc, run_id=run_id, pr=pr, repo=repo, csession_id=csession_id):
        return "mismatch"
    if status in ("live", "launching"):
        return "live"
    return "owned"


def teardown_owned(agent_id: str, *, actor: str | None = None) -> Teardown:
    """``agent-spawn teardown`` of an agent ``check_parked`` found ``owned``, without
    ``--force``: 20 is ``refused``, 10 ``gone``, any other failure ``failed``."""
    if not _AGENT_ID.match(agent_id):
        raise ValueError("not an agent id")
    argv = ["agent-spawn", "teardown", agent_id, "--json"]
    if actor is not None:
        argv += ["--actor", actor]
    try:
        done = gateway.run(argv, timeout=TEARDOWN_TIMEOUT)
    except gateway.GatewayError:
        return "failed"
    if done.returncode == 0:
        return "done"
    if done.returncode == 10:
        return "gone"
    return "refused" if done.returncode == 20 else "failed"


def teardown_parked(
    agent_id: str,
    *,
    run_id: str,
    pr: str,
    repo: str,
    csession_id: str | None,
    actor: str | None = None,
) -> Teardown:
    """Tear down the parked agent a failed launch left. ``agent-spawn show`` first: an unknown or
    torn-down agent is ``gone``, an agent that is not this run's is ``mismatch`` (never touched),
    a live one is ``live`` (never torn down: agent-spawn refuses it). ``teardown`` runs without
    ``--force``; 20 is ``refused``, any other failure ``failed``."""
    verdict = check_parked(agent_id, run_id=run_id, pr=pr, repo=repo, csession_id=csession_id)
    if verdict != "owned":
        return verdict
    return teardown_owned(agent_id, actor=actor)
