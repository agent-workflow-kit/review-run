"""Run order steps 1 and 2: every check that runs before a run id is allocated.

Nothing here allocates an id, creates a run record or writes a run file; a failure ends the
command with an error envelope. The orchestrator is ``plan_run``; the pieces are public so each
rule has its own test.

Step 1 (usage, then tools and state), all exit 2 or 12: flags and config, the PR argument's
shape, ``--override-escalation`` without a human, caller labels, ``--previous`` (unknown: 10, not
``done``: 2), the four sibling tools on ``PATH`` and the minimum versions, the state root.
Step 2: ``forgectl show`` (resolve), ``pr_too_long``, ``previous_mismatch``, the per-repo
settings, skill resolution, the plugin directories, ``wtctl`` when the repo runs PR code, and
``agent-spawn launch --dry-run --json`` with the planned flags.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import stat
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from . import gateway
from . import settings as run_settings
from .config import Config, Resolved
from .config_semantic import PLACEHOLDERS
from .config_semantic import _plugin_name as plugin_name
from .errors import ReviewRunError
from .fsmodes import DIR_MODE, create_private_file, ensure_private_dir
from .plugin_hash import PluginTreeError, plugin_dirs_sha256
from .prefetch import PrFacts, check_pr_argument, resolve_pr
from .settings import (
    ProfilePlan,
    RunSettings,
    build_settings,
    effective_profile,
    launch_env,
    launch_flags,
    strict_finite_json,
)
from .store import Run, Store, default_state_root, ensure_state_root

MAX_LABELS: Final = 60
RESERVED_LABEL_KEYS: Final = frozenset({"owner", "run", "pr", "repo", "agent", "kind"})
_LABEL_KEY: Final = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}", re.ASCII)
_LABEL_VALUE: Final = re.compile(r"[A-Za-z0-9._:/#@+=,-]{0,256}", re.ASCII)
MAX_PR_LABEL_VALUE: Final = 256
BUNDLED_PLUGIN: Final = "review-run"
_NAME: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", re.ASCII)
_WORD: Final = re.compile(r"[a-z][a-z0-9_]{0,63}")
_TOOL_NAME: Final = re.compile(r"[a-z][a-z0-9-]{0,31}")
VERSION_TIMEOUT: Final = 15.0
DRY_RUN_TIMEOUT: Final = 60.0
PLACEHOLDER_RUN_ID: Final = "rr-aaaaaaaa"  # the dry run names no real run: none exists yet
_VERSION_RE: Final = re.compile(
    r"([A-Za-z][A-Za-z0-9_-]*) v?([0-9]{1,6})\.([0-9]{1,6})\.([0-9]{1,6})", re.ASCII
)
_REPO_URL: Final = "https://github.com/agent-workflow-kit/"

# Run order step 1 (b): in this order. The minimums are the releases the pinned schemas were
# copied from (docs/pinned-schemas.md); the exact versions that support `--no-implicit-done` are
# filled in when those releases ship (design.md, Minimum sibling versions; issue #2).
REQUIRED_TOOLS: Final = ("forgectl", "pr-comment", "agent-spawn", "claude-session")
MIN_VERSIONS: Final[dict[str, tuple[int, int, int]]] = {
    "agent-spawn": (0, 38, 1),
    "claude-session": (0, 45, 2),
}

Version = tuple[int, int, int]


def install_command(tool: str) -> str:
    return f"uv tool install git+{_REPO_URL}{tool}"


# -- step 1 (a): usage ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunRequest:
    """What the command line says about one ``run``."""

    pr: str
    skill: str | None = None
    mode: str | None = None
    labels: tuple[str, ...] = ()
    previous: str | None = None
    model: str | None = None
    timeout: str | None = None  # as given (agent-spawn receives it unchanged)
    actor: str | None = None
    decision_ids: tuple[str, ...] = ()
    override_escalation: bool = False

    def flags(self) -> dict[str, Any]:
        """The config keys the user gave on the command line (``Config.resolve`` flags)."""
        pairs = {
            "skill": self.skill,
            "mode": self.mode,
            "model": self.model,
            "timeout": self.timeout,
        }
        return {k: v for k, v in pairs.items() if v is not None}


def check_labels(labels: Sequence[str]) -> tuple[str, ...]:
    """Validate the caller's ``--label k=v`` values (architecture 8.1 grammar).

    More than 60: ``too_many_labels`` (exit 2, ``details.max``); a reserved key:
    ``label_reserved``; any other violation is a plain usage error. The text of a label is never
    echoed.
    """
    if len(labels) > MAX_LABELS:
        raise ReviewRunError(
            "usage",
            "too many labels",
            reason="too_many_labels",
            details={"max": MAX_LABELS},
        )
    for label in labels:
        key, sep, value = label.partition("=")
        if not sep or not _LABEL_KEY.fullmatch(key) or not _LABEL_VALUE.fullmatch(value):
            raise ReviewRunError("usage", "a label is not key=value in the label grammar")
        if key in RESERVED_LABEL_KEYS:
            raise ReviewRunError("usage", "a label uses a reserved key", reason="label_reserved")
    return tuple(labels)


def check_override(request: RunRequest, *, stdin_is_tty: bool) -> None:
    """``--override-escalation`` needs a human: a TTY on stdin or ``--actor human:<user>``."""
    if not request.override_escalation:
        return
    human = request.actor is not None and request.actor.startswith("human:")
    if not (human or stdin_is_tty):
        raise ReviewRunError("usage", "--override-escalation needs a human actor")


def check_previous(root: Path, ref: str) -> Run:
    """``--previous``: an unknown or pruned run exits 10, a run that is not ``done`` exits 2
    (``previous_not_done``, ``details.state``). Reads the store only when it exists and the
    state root is safe (an unsafe one is ``state_unsafe``)."""
    check_state_root(root)
    if not (Path(root) / "state.db").is_file():
        # Still validate the reference's shape so a malformed one is a usage error.
        _shape_only(ref)
        raise ReviewRunError("not_found", "no such run")
    with Store.open(root) as store:
        run = store.get_run(store.resolve_run_id(ref))
    if run is None:
        raise ReviewRunError("not_found", "no such run")
    if run.state != "done":
        raise ReviewRunError(
            "usage",
            "the previous run is not done",
            reason="previous_not_done",
            details={"state": run.state},
        )
    return run


def _shape_only(ref: str) -> None:
    body = ref[3:] if ref.startswith("rr-") else ref
    if not re.fullmatch(r"[a-z2-7]{0,8}", body, re.ASCII):
        raise ReviewRunError("usage", "not a run id or run id prefix")
    if len(body) < 4:
        raise ReviewRunError(
            "usage", "a run id prefix needs at least 4 characters", details={"min": 4}
        )


# -- step 1 (b): tools and state ----------------------------------------------------------------


def parse_version(text: str, tool: str | None = None) -> Version | None:
    """``<tool> [v]X.Y.Z`` (the whole first line of ``--version``) as a tuple, else ``None``.

    Each number has one to six digits; a prerelease or build suffix, an extra part or a longer
    number makes the line unparsable (never truncated). With ``tool`` given, the leading name
    must be that tool."""
    first = text.strip().splitlines()[0].strip() if text.strip() else ""
    match = _VERSION_RE.fullmatch(first)
    if match is None or (tool is not None and match[1] != tool):
        return None
    return (int(match[2]), int(match[3]), int(match[4]))


def _lower_failed(tool: str, lower_code: str) -> ReviewRunError:
    return ReviewRunError(
        "failed",
        f"{tool} failed ({lower_code})",
        reason="lower_failed",
        tool=tool,
        lower_code=lower_code,
    )


def _tool_missing(tool: str) -> ReviewRunError:
    return ReviewRunError(
        "failed",
        f"{tool} is not installed; install it with: {install_command(tool)}",
        reason="tool_missing",
        tool=tool,
        details={"install": install_command(tool)},
    )


def tool_version(tool: str) -> str:
    """The stdout of ``<tool> --version`` through the gateway; a program that is not found is
    ``tool_missing`` (exit 12, with the install command), any other failure ``lower_failed``."""
    try:
        done = gateway.run([tool, "--version"], timeout=VERSION_TIMEOUT)
    except gateway.ToolMissing:
        raise _tool_missing(tool) from None
    except gateway.GatewayError as exc:
        raise _lower_failed(tool, exc.lower_code) from None
    if done.returncode != 0:
        raise _lower_failed(tool, "failed")
    return done.stdout


def check_tools(required: Sequence[str] = REQUIRED_TOOLS) -> dict[str, Version | None]:
    """Resolve every tool of ``required`` (``tool_missing``), then compare ``agent-spawn`` and
    ``claude-session`` with ``MIN_VERSIONS`` (``tool_too_old``, ``details`` ``found`` and
    ``minimum``). Returns each tool's version (``None`` when its output did not parse and no
    minimum applies). An unparsable version of a tool with a minimum is ``lower_failed``
    (``invalid_output``): the check fails closed."""
    outputs = {tool: tool_version(tool) for tool in required}
    versions: dict[str, Version | None] = {}
    for tool, text in outputs.items():
        found = parse_version(text, tool)
        versions[tool] = found
        minimum = MIN_VERSIONS.get(tool)
        if minimum is None:
            continue
        if found is None:
            raise _lower_failed(tool, "invalid_output")
        if found < minimum:
            raise ReviewRunError(
                "failed",
                f"{tool} is older than the required {'.'.join(map(str, minimum))}",
                reason="tool_too_old",
                tool=tool,
                details={
                    "found": ".".join(map(str, found)),
                    "minimum": ".".join(map(str, minimum)),
                    "install": install_command(tool),
                },
            )
    return versions


def state_root_is_unsafe(root: Path) -> bool:
    """The test of ``check_state_root`` without raising."""
    try:
        info = os.lstat(root)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()


def check_state_root(root: Path) -> None:
    """A state root that is a symlink or not owned by the user is ``state_unsafe`` (exit 12).
    A root that does not exist yet is fine: nothing is created here."""
    if state_root_is_unsafe(root):
        raise ReviewRunError("failed", "state path is unsafe", reason="state_unsafe")


# -- step 2: skill and plugins -------------------------------------------------------------------


def _skill_not_found() -> ReviewRunError:
    return ReviewRunError("usage", "the skill was not found", reason="skill_not_found")


def resolve_skill(skill: str, plugin_dirs: Sequence[str]) -> str | None:
    """The realpath of the extra plugin that holds ``<plugin>:<skill>``, or ``None`` for the
    bundled plugin. Raises ``skill_not_found`` (exit 2) for a bare name, an unknown plugin or an
    unknown skill (a ``skills/<skill>/SKILL.md`` regular file)."""
    plugin, sep, name = skill.partition(":")
    if not sep or not _NAME.fullmatch(plugin) or not _NAME.fullmatch(name):
        raise _skill_not_found()
    if plugin == BUNDLED_PLUGIN:
        root = str(run_settings.bundled_plugin_dir())
        extra: str | None = None
    else:
        extra = None
        for directory in plugin_dirs:
            try:
                if plugin_name(directory) == plugin:
                    extra = directory
                    break
            except ValueError:
                continue
        if extra is None:
            raise _skill_not_found()
        root = extra
    try:
        info = os.lstat(os.path.join(root, "skills", name, "SKILL.md"))
    except OSError:
        raise _skill_not_found() from None
    if not stat.S_ISREG(info.st_mode):
        raise _skill_not_found()
    return extra


def check_placeholders(directories: Sequence[str]) -> None:
    """Each plugin directory passed needs ``.claude/``, ``.mcp.json``, ``CLAUDE.md`` and
    ``CLAUDE.local.md``; a missing one is ``config_invalid`` (``details.key: plugin_dirs``)."""
    for directory in directories:
        if any(not os.path.lexists(os.path.join(directory, p)) for p in PLACEHOLDERS):
            raise ReviewRunError(
                "config_invalid",
                "a plugin directory lacks the placeholder entries",
                details={"key": "plugin_dirs"},
            )


def plugin_dirs_for(skill: str, plugin_dirs: Sequence[str]) -> tuple[str, ...]:
    """The plugin directories passed to the launch: the bundled one and, when the skill comes
    from another plugin, that plugin's directory only (sorted by realpath)."""
    extra = resolve_skill(skill, plugin_dirs)
    passed = [os.path.realpath(run_settings.bundled_plugin_dir())]
    if extra is not None:
        passed.append(extra)
    return tuple(sorted(passed, key=os.fsencode))


_BUNDLED_REFUSALS: Final = {
    "writable": "the bundled plugin directory is writable by group or others "
    "(an install or checkout made at umask 002 needs chmod -R g-w)",
    "foreign_owner": "the bundled plugin directory is owned by someone else",
}


def _bundled_refusal(directories: Sequence[str], exc: PluginTreeError) -> None:
    """Raise a message of its own when the refused tree is the bundled plugin directory (the
    user did not configure it, so ``plugin_dirs`` is not to blame)."""
    if exc.reason not in _BUNDLED_REFUSALS:
        return
    bundled = os.path.realpath(run_settings.bundled_plugin_dir())
    if bundled not in {os.path.realpath(d) for d in directories}:
        return
    try:
        plugin_dirs_sha256([bundled])
    except PluginTreeError as own:
        if own.reason in _BUNDLED_REFUSALS:
            raise ReviewRunError("config_invalid", _BUNDLED_REFUSALS[own.reason]) from None


def hash_plugin_dirs(directories: Sequence[str]) -> str:
    """``plugin_dirs_sha256`` of the directories; a refused entry (a symlink, a foreign owner,
    group or world write, a special file, an oversize tree) is ``config_invalid`` (exit 2,
    ``details.key: plugin_dirs``, the closed reason in the message, never a path)."""
    try:
        return plugin_dirs_sha256(directories)
    except PluginTreeError as exc:
        _bundled_refusal(directories, exc)
        raise ReviewRunError(
            "config_invalid",
            f"a plugin directory holds a refused entry ({exc.reason})",
            details={"key": "plugin_dirs"},
        ) from None


# -- step 2: agent-spawn's dry run ---------------------------------------------------------------

_SIBLINGS: Final = (*REQUIRED_TOOLS, "wtctl", "termctl")
_AGENT_SPAWN_REFUSALS: Final = frozenset(
    {"third_party_cwd", "third_party_run", "live", "tearing_down"}
)


def _envelope(stdout: str) -> dict[str, Any] | None:
    try:
        doc = strict_finite_json(stdout)
    except (ValueError, RecursionError):
        return None
    error = doc.get("error") if isinstance(doc, dict) else None
    return error if isinstance(error, dict) else None


def _word(value: object) -> str | None:
    return value if isinstance(value, str) and _WORD.fullmatch(value) else None


def map_dry_run(returncode: int, stdout: str) -> None:
    """Map the result of ``agent-spawn launch --dry-run --json`` (Resolve step failures).

    Exit 0 with a JSON object continues. An envelope whose ``tool`` is ``termctl`` (exit 2 or
    12) is exit 12 ``no_tmux`` with termctl's code in ``lower_code``; agent-spawn's
    ``tool_missing`` passes through; exit 20 is ``refused`` (``lower_refused``, ``lower_code``
    the plan's ``would_refuse``); every other exit, or output that does not parse, is
    ``lower_failed``. Text of the lower tool is never copied.
    """
    tool = "agent-spawn"
    if returncode == 0:
        try:
            plan = strict_finite_json(stdout)
        except (ValueError, RecursionError):
            plan = None
        if (
            not isinstance(plan, dict)
            or plan.get("dry_run") is not True
            or plan.get("would_refuse") is not None
            or "error" in plan
        ):
            raise _lower_failed(tool, "invalid_output")
        return
    if returncode == 20:
        try:
            plan = strict_finite_json(stdout)
        except (ValueError, RecursionError):
            plan = None
        reason = _word(plan.get("would_refuse")) if isinstance(plan, dict) else None
        if reason is None:
            raise _lower_failed(tool, "invalid_output")
        source = tool if reason in _AGENT_SPAWN_REFUSALS else "claude-session"
        raise ReviewRunError(
            "refused",
            f"the launch would be refused: {reason}",
            reason="lower_refused",
            tool=source,
            lower_code=reason,
        )
    error = _envelope(stdout)
    code = _word(error.get("code")) if error else None
    lower = _word(error.get("lower_code")) if error else None
    named = error.get("tool") if error else None
    source = named if isinstance(named, str) and _TOOL_NAME.fullmatch(named) else None
    if source == "termctl" and returncode in (2, 12):
        raise ReviewRunError(
            "failed",
            "tmux is not usable for an interactive session",
            reason="no_tmux",
            tool="termctl",
            lower_code=lower,
        )
    if returncode == 12 and code == "tool_missing":
        missing = source if source in _SIBLINGS else tool
        raise _tool_missing(missing)
    raise _lower_failed(source if source in _SIBLINGS else tool, lower or code or "failed")


@contextmanager
def scratch_dir(root: Path) -> Iterator[Path]:
    """A private (0700) directory ``<root>/tmp/dry-<random>`` for a dry run, removed when the block
    ends however it ends (``reconcile`` removes leftovers of a killed process)."""
    tmp = Path(root) / "tmp"
    ensure_state_root(Path(root))
    ensure_private_dir(tmp)
    scratch = tmp / f"dry-{secrets.token_hex(8)}"
    os.mkdir(scratch, DIR_MODE)
    try:
        yield scratch
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def launch_dry_run(
    settings: RunSettings,
    profile: ProfilePlan,
    *,
    repo: str,
    pr: str,
    request: RunRequest,
    root: Path,
) -> None:
    """``agent-spawn launch --dry-run --json`` with the planned flags: no prompt or input files
    (performs agent-spawn's tool resolution, claude-session's classification dry run and, for
    interactive mode, the termctl window preflight).

    The effective profile is written into a private directory under the state root that is
    removed before this returns.
    """
    with scratch_dir(Path(root)) as scratch:
        profile_path = scratch / "permission-profile.json"
        create_private_file(profile_path, profile.data)
        argv = [
            "agent-spawn",
            "launch",
            *launch_flags(
                settings,
                repo=repo,
                pr=pr,
                run_id=PLACEHOLDER_RUN_ID,
                profile_path=str(profile_path),
                result_file=str(scratch / "launch.json"),
                labels=request.labels,
                actor=request.actor,
                decision_ids=request.decision_ids,
            ),
            "--dry-run",
        ]
        try:
            done = gateway.run(argv, timeout=DRY_RUN_TIMEOUT, env=launch_env(settings))
        except gateway.ToolMissing:
            raise _tool_missing("agent-spawn") from None
        except gateway.GatewayError as exc:
            raise _lower_failed("agent-spawn", exc.lower_code) from None
        map_dry_run(done.returncode, done.stdout)


# -- the orchestrator ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunPlan:
    """The outcome of steps 1 and 2: everything step 4 writes and later steps use."""

    request: RunRequest
    facts: PrFacts
    resolved: Resolved
    settings: RunSettings
    profile: ProfilePlan
    previous: Run | None
    warnings: tuple[str, ...]

    @property
    def pr(self) -> str:
        return self.facts.pr

    @property
    def repo(self) -> str:
        return self.facts.repo

    @property
    def mode(self) -> str:
        return str(self.settings.value("mode"))

    @property
    def skill(self) -> str:
        return str(self.settings.value("skill"))


def plan_run(
    request: RunRequest,
    config: Config,
    root: Path | None = None,
    *,
    stdin_is_tty: bool = False,
    dry_run_launch: bool = True,
) -> RunPlan:
    """Run order steps 1 and 2 for ``request``. Nothing is allocated or written to a run
    directory; the first failure ends the call with its error envelope."""
    root = default_state_root() if root is None else Path(root)
    # 1 (a) usage: flags, the PR argument's shape, labels, override, previous
    check_pr_argument(request.pr)
    config.resolve(None, request.flags())  # a bad flag value is a usage error naming its key
    check_override(request, stdin_is_tty=stdin_is_tty)
    check_labels(request.labels)
    # --previous reads the store, so an unsafe state root defers it until the tools and the
    # state root were checked (tool_missing, then state_unsafe); its shape is checked now.
    previous: Run | None = None
    if request.previous is not None:
        if state_root_is_unsafe(root):
            _shape_only(request.previous)
        else:
            previous = check_previous(root, request.previous)
    # 1 (b) tools and state
    check_tools()
    check_state_root(root)
    if request.previous is not None and previous is None:
        previous = check_previous(root, request.previous)
    # 2 resolve, repo checks and preflight
    facts = resolve_pr(request.pr)
    if len(facts.pr) > MAX_PR_LABEL_VALUE:
        raise ReviewRunError("usage", "the pr label value is too long", reason="pr_too_long")
    if previous is not None and previous.pr != facts.pr:
        raise ReviewRunError(
            "usage", "the previous run is of another pull request", reason="previous_mismatch"
        )
    resolved = config.resolve(facts.repo, request.flags())
    values: Mapping[str, Any] = resolved.values
    profile, warnings = effective_profile(values)
    passed = plugin_dirs_for(str(values["skill"]), values["plugin_dirs"])
    check_placeholders(passed)
    digest = hash_plugin_dirs(passed)
    settings = build_settings(
        resolved, plugin_dirs=passed, plugin_dirs_hash=digest, profile=profile
    )
    if values.get("run_pr_code") is not None:
        tool_version("wtctl")
    if dry_run_launch:
        launch_dry_run(settings, profile, repo=facts.repo, pr=facts.pr, request=request, root=root)
    return RunPlan(request, facts, resolved, settings, profile, previous, warnings)
