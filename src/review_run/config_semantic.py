"""Semantic config checks the JSON schema cannot express (interface.md, Config, run_pr_code).

Every check returns fixed problem texts: a problem never carries the offending value (a name, a
path or a file's content), so a config error cannot leak a secret. Checks that touch the file
system (guidelines file, plugin directories, read-only binds) are early diagnostics at load; the
authoritative checks happen where the file is used.
"""

from __future__ import annotations

import glob
import json
import os
import pwd
import re
import stat
from pathlib import Path
from typing import Final

from .config_yaml import ConfigFileError, read_private_file, writable_by_others

# -- environment names (env_allowlist, run_pr_code.env_allow) -----------------------------------

_NAME_RE: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*", re.ASCII)
_NAME_CONTAINS: Final = (
    "TOKEN", "SECRET", "KEY", "PASS", "CREDENTIAL", "AUTH", "COOKIE", "SESSION", "DSN", "PROXY",
)  # fmt: skip
_NAME_SUFFIXES: Final = ("_URL", "_PAT")
_NAME_PREFIXES: Final = ("ANTHROPIC_", "CLAUDE_")


def env_name_refusal(name: str) -> str | None:
    """Why ``name`` cannot be passed to the session or the code run, or None when it can."""
    if _NAME_RE.fullmatch(name) is None:
        return "not a valid environment variable name"
    upper = name.upper()
    if (
        any(part in upper for part in _NAME_CONTAINS)
        or upper.endswith(_NAME_SUFFIXES)
        or upper == "NETRC"
        or upper.startswith(_NAME_PREFIXES)
    ):
        return "a name that can carry a credential or belongs to Claude Code is refused"
    return None


# -- guidelines_file ----------------------------------------------------------------------------

GUIDELINES_MAX_BYTES: Final = 64 * 1024


class GuidelinesError(ValueError):
    """The guidelines file cannot be used; the message never names the path or its content."""


def expand_guidelines_path(text: str) -> str:
    """``~/x`` becomes the home directory plus ``x``; anything else must already be absolute.
    ``..`` components are refused."""
    path = os.path.join(os.path.expanduser("~"), text[2:]) if text.startswith("~/") else text
    if not os.path.isabs(path) or "\x00" in path or ".." in path.split("/"):
        raise GuidelinesError("must be an absolute path without '..'")
    return path


def read_guidelines_file(path: str) -> str:
    """The file's text: a regular file the user owns (not writable by others), opened without
    following a final symlink, at most 64 KiB of UTF-8 without NUL."""
    expanded = expand_guidelines_path(path)
    try:
        fd = os.open(expanded, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        raise GuidelinesError("cannot be opened (missing, a symlink or unreadable)") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise GuidelinesError("not a regular file")
        if info.st_uid != os.geteuid():
            raise GuidelinesError("not owned by you")
        if writable_by_others(info):
            raise GuidelinesError("writable by group or others")
        if info.st_size > GUIDELINES_MAX_BYTES:
            raise GuidelinesError("larger than 64 KiB")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            data = handle.read(GUIDELINES_MAX_BYTES + 1)
    except OSError:
        raise GuidelinesError("cannot be read") from None
    finally:
        os.close(fd)
    if len(data) > GUIDELINES_MAX_BYTES:
        raise GuidelinesError("larger than 64 KiB")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise GuidelinesError("not valid UTF-8") from None
    if "\x00" in text:
        raise GuidelinesError("contains a NUL character")
    return text


# -- plugin_dirs --------------------------------------------------------------------------------

PLACEHOLDERS: Final = (".claude", ".mcp.json", "CLAUDE.md", "CLAUDE.local.md")
RESERVED_PLUGIN: Final = "review-run"
_PLUGIN_NAME_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", re.ASCII)

Problem = tuple[int, str]  # (index in the list, fixed text)


def _plugin_name(root: str) -> str:
    try:
        data = read_private_file(Path(root, ".claude-plugin", "plugin.json"))
        document = json.loads(data.decode("utf-8"))
    except (ConfigFileError, ValueError):
        raise ValueError("its .claude-plugin/plugin.json cannot be read as JSON") from None
    name = document.get("name") if isinstance(document, dict) else None
    if not isinstance(name, str) or _PLUGIN_NAME_RE.fullmatch(name) is None:
        raise ValueError("its plugin.json has no valid name")
    return name


def check_plugin_dirs(paths: list[str]) -> tuple[list[str], list[Problem]]:
    """The realpaths of ``paths`` and the problems found, by index."""
    real: list[str] = []
    problems: list[Problem] = []
    names: set[str] = set()
    for index, path in enumerate(paths):
        if "\x00" in path or not os.path.isabs(path):
            problems.append((index, "must be an absolute path"))
            continue
        resolved = os.path.realpath(path)
        try:
            info = os.stat(resolved)
        except OSError:
            problems.append((index, "must be an existing directory"))
            continue
        if not stat.S_ISDIR(info.st_mode):
            problems.append((index, "must be an existing directory"))
        elif info.st_uid != os.geteuid():
            problems.append((index, "the directory must be owned by you"))
        elif info.st_mode & 0o022:
            problems.append((index, "the directory must not be writable by group or others"))
        elif missing := [p for p in PLACEHOLDERS if not os.path.lexists(os.path.join(resolved, p))]:
            problems.append((index, f"missing placeholder entries: {', '.join(missing)}"))
        else:
            try:
                name = _plugin_name(resolved)
            except ValueError as exc:
                problems.append((index, str(exc)))
                continue
            if name == RESERVED_PLUGIN:
                problems.append((index, "a plugin cannot be named review-run"))
            elif name in names:
                problems.append((index, "another entry has the same plugin name"))
            else:
                names.add(name)
                real.append(resolved)
    return real, problems


# -- run_pr_code.ro_binds -----------------------------------------------------------------------

_CREDENTIAL_COMPONENTS: Final = frozenset(
    (".ssh", ".gnupg", ".aws", ".kube", ".docker", ".config", ".claude", ".netrc", ".npmrc",
     ".pypirc", "credentials.toml", ".git-credentials")
)  # fmt: skip
# claude-session's credential locations, home relative (a missing one still counts: a directory
# above it is refused, so a toolchain directory must be narrowed before the file appears).
_CREDENTIAL_HOME_PATHS: Final = (
    ".ssh", ".aws", ".gnupg", ".kube", ".docker", ".config", ".claude", ".netrc",
    ".git-credentials", ".local/state", ".local/share/keyrings", ".cargo/credentials",
    ".cargo/credentials.toml", ".gem/credentials", ".gradle/gradle.properties",
    ".m2/settings.xml", ".m2/settings-security.xml",
)  # fmt: skip
_CREDENTIAL_GLOBS: Final = (".cargo/credentials*", ".m2/settings*.xml")
_SYSTEM_PREFIXES: Final = ("/proc", "/sys", "/dev", "/run", "/var/run", "/root", "/media")
_WSL_DRIVE_RE: Final = re.compile(r"/mnt/[A-Za-z](?:/|\Z)")


def _inside(path: str, base: str) -> bool:
    """``path`` is ``base`` or below it (``/`` contains everything)."""
    return path == base or path.startswith(base.rstrip("/") + "/")


def _homes() -> set[str]:
    homes = {os.path.realpath(os.path.expanduser("~"))}
    try:
        homes.add(os.path.realpath(pwd.getpwuid(os.geteuid()).pw_dir))
    except KeyError:
        pass
    return homes


def _xdg(name: str) -> str | None:
    value = os.environ.get(name, "")
    return os.path.realpath(value) if value.startswith("/") else None


def _credential_locations(homes: set[str]) -> set[str]:
    raw: set[str] = set()
    for home in homes:
        raw.update(os.path.join(home, p) for p in _CREDENTIAL_HOME_PATHS)
        for pattern in _CREDENTIAL_GLOBS:
            raw.update(glob.glob(os.path.join(glob.escape(home), pattern)))
    for name in ("XDG_CONFIG_HOME", "XDG_STATE_HOME"):
        if (value := _xdg(name)) is not None:
            raw.add(value)
    if (data := _xdg("XDG_DATA_HOME")) is not None:
        raw.add(os.path.join(data, "keyrings"))
    return raw | {os.path.realpath(p) for p in raw}


def _state_dir() -> str:
    state = _xdg("XDG_STATE_HOME") or os.path.join(
        os.path.realpath(os.path.expanduser("~")), ".local/state"
    )
    return os.path.join(state, "review-run")


def _bind_refusal(given: str, real: str, homes: set[str], credentials: set[str]) -> str | None:
    if real == "/" or any(_inside(h, real) for h in homes):
        return "the home directory and its ancestors are refused"
    if real in ("/mnt", "/media", "/root") or _WSL_DRIVE_RE.match(real):
        return "a system or drive-mount location is refused"
    if any(_inside(real, p) for p in _SYSTEM_PREFIXES):
        return "a system location is refused"
    if _inside(_state_dir(), real):
        return "an ancestor of a tool's state directory is refused"
    if any(_inside(real, c) or _inside(c, real) for c in credentials):
        return "a credential location, or a directory holding one, is refused (narrow it)"
    if _CREDENTIAL_COMPONENTS.intersection(given.split("/")) or _CREDENTIAL_COMPONENTS.intersection(
        real.split("/")
    ):
        return "a path with a credential component is refused"
    return None


def check_ro_binds(paths: list[str]) -> tuple[list[str], list[Problem]]:
    """The realpaths of ``paths`` (duplicates collapsed) and the problems found, by index."""
    homes = _homes()
    credentials = _credential_locations(homes)
    real: list[str] = []
    problems: list[Problem] = []
    for index, path in enumerate(paths):
        if "\x00" in path or not os.path.isabs(path):
            problems.append((index, "must be an absolute path"))
            continue
        resolved = os.path.realpath(path)
        if (reason := _bind_refusal(path, resolved, homes, credentials)) is not None:
            problems.append((index, reason))
        elif not os.path.isdir(resolved):
            problems.append((index, "must be an existing directory"))
        elif resolved not in real:
            real.append(resolved)
    return real, problems
