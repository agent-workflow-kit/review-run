"""Run settings and the effective permission profile (interface.md, Run settings; design D-29).

At run order step 4 two files are written into the run directory: ``permission-profile.json``
(the bundled profile, its variant without the ``Agent`` and ``Task`` denies when ``subagents`` is
on, or a copy of the configured ``permission_profile``) and ``settings.json`` (every key of the
Config table resolved for this run with its origin, except ``hooks``, ``retention``,
``escalation_ttl`` and ``me``, plus the plugin directories passed, ``plugin_dirs_sha256``,
``config_sha256`` and the profile's kind and sha256).

Every later step, in whichever process runs it, reads these files with ``load_run_settings`` and
never the current config or environment. ``verify_plugin_dirs`` recomputes the plugin directory
hash before ``launch`` and ``resume``.
"""

from __future__ import annotations

import hashlib
import importlib.resources
import json
import os
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from .config import Resolved
from .errors import ReviewRunError
from .fsmodes import create_private_file
from .plugin_hash import PluginTreeError, plugin_dirs_sha256
from .prefetch import strict_json

SCHEMA_VERSION: Final = 1
SETTINGS_FILE: Final = "settings.json"
PROFILE_FILE: Final = "permission-profile.json"
MAX_SETTINGS_BYTES: Final = 1024 * 1024
MAX_PROFILE_BYTES: Final = 256 * 1024
# Config keys that belong to the acting process's own config, not to the run (Run settings).
EXCLUDED: Final = frozenset({"hooks", "retention", "escalation_ttl", "me"})
SUBAGENT_DENIES: Final = ("Agent", "Task")
DEPTH_VARIABLE: Final = "CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH"
PROFILE_KINDS: Final = ("bundled", "variant", "custom")
ORIGINS: Final = ("flag", "repo", "env", "config", "default")
WARNING_CUSTOM_PROFILE_SUBAGENTS: Final = "custom_profile_subagents"


# -- the bundled plugin and profile ------------------------------------------------------------


def bundled_plugin_dir() -> Path:
    """The bundled plugin directory: ``review_run/plugin`` of an installed wheel, else the
    repository's ``plugin/`` (an editable install or a checkout)."""
    packaged = Path(str(importlib.resources.files("review_run") / "plugin"))
    if packaged.is_dir():
        return packaged
    return Path(__file__).resolve().parents[2] / "plugin"


def bundled_header_template() -> str:
    """The bundled prompt header template (``plugin/header.md``, rendered by ``prompt``)."""
    return (bundled_plugin_dir() / "header.md").read_text(encoding="utf-8")


def bundled_profile_bytes() -> bytes:
    return (bundled_plugin_dir() / "permission_profile.json").read_bytes()


# -- the effective permission profile ----------------------------------------------------------


@dataclass(frozen=True)
class ProfilePlan:
    kind: str  # bundled, variant or custom
    data: bytes
    sha256: str


def _json_object(data: bytes) -> dict[str, Any]:
    try:
        doc = strict_json(data.decode("utf-8"))
    except (ValueError, RecursionError):
        raise ValueError("not strict JSON") from None
    if not isinstance(doc, dict):
        raise ValueError("not a JSON object")
    return doc


def variant_profile(bundled: bytes) -> bytes:
    """The bundled profile without the ``Agent`` and ``Task`` denies (``subagents: true``)."""
    doc = _json_object(bundled)
    deny = doc["permissions"]["deny"]
    doc["permissions"]["deny"] = [d for d in deny if d not in SUBAGENT_DENIES]
    return (json.dumps(doc, indent=2, ensure_ascii=True) + "\n").encode("ascii")


def read_profile_file(path: str) -> bytes:
    """The configured profile's bytes: opened without following a final symlink, a regular file
    of the user, not writable by others, at most 256 KiB, strict JSON object. Raises
    ``config_invalid`` (``details.key: permission_profile``)."""

    def refused(why: str) -> ReviewRunError:
        return ReviewRunError(
            "config_invalid",
            f"permission_profile {why}",
            details={"key": "permission_profile"},
        )

    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        raise refused("cannot be opened") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise refused("is not a regular file")
        if info.st_uid != os.geteuid():
            raise refused("is not owned by you")
        if info.st_mode & 0o002:
            raise refused("is writable by others")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            data = handle.read(MAX_PROFILE_BYTES + 1)
    except OSError:
        raise refused("cannot be read") from None
    finally:
        os.close(fd)
    if len(data) > MAX_PROFILE_BYTES:
        raise refused("is larger than 256 KiB")
    try:
        _json_object(data)
    except ValueError:
        raise refused("is not a JSON object") from None
    return data


def effective_profile(values: Mapping[str, Any]) -> tuple[ProfilePlan, tuple[str, ...]]:
    """The profile this run uses and its warnings (closed codes).

    ``permission_profile`` set: a copy of that file, unchanged, and with ``subagents`` on the
    warning ``custom_profile_subagents`` (review-run does not edit a user's profile). Otherwise
    the bundled profile, or its variant when ``subagents`` is on.
    """
    configured = values.get("permission_profile")
    subagents = values.get("subagents") is True
    if configured is not None:
        data = read_profile_file(configured)
        kind = "custom"
        warnings = (WARNING_CUSTOM_PROFILE_SUBAGENTS,) if subagents else ()
    else:
        data = bundled_profile_bytes()
        kind = "bundled"
        warnings = ()
        if subagents:
            data, kind = variant_profile(data), "variant"
    return ProfilePlan(kind, data, hashlib.sha256(data).hexdigest()), warnings


# -- the settings document ---------------------------------------------------------------------


@dataclass(frozen=True)
class RunSettings:
    """The content of ``settings.json``: ``entries`` is ``{key: (value, origin)}`` with
    ``limits.<k>`` keys individually."""

    entries: dict[str, tuple[Any, str]]
    plugin_dirs: tuple[str, ...]
    plugin_dirs_sha256: str
    config_sha256: str
    profile_kind: str
    profile_sha256: str

    def value(self, key: str) -> Any:
        return self.entries[key][0]

    def origin(self, key: str) -> str:
        return self.entries[key][1]

    def document(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "settings": [
                {"key": k, "value": v, "origin": o} for k, (v, o) in sorted(self.entries.items())
            ],
            "plugin_dirs": list(self.plugin_dirs),
            "plugin_dirs_sha256": self.plugin_dirs_sha256,
            "config_sha256": self.config_sha256,
            "permission_profile": {"kind": self.profile_kind, "sha256": self.profile_sha256},
        }

    def to_bytes(self) -> bytes:
        return (json.dumps(self.document(), indent=2, sort_keys=True) + "\n").encode("ascii")


def config_sha256(resolved: Resolved) -> str:
    """The hash of every resolved value of the run (flags included, ``hooks`` too)."""
    text = json.dumps(resolved.values, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(text.encode("ascii")).hexdigest()


def build_settings(
    resolved: Resolved,
    *,
    plugin_dirs: Sequence[str],
    plugin_dirs_hash: str,
    profile: ProfilePlan,
) -> RunSettings:
    entries: dict[str, tuple[Any, str]] = {}
    for key, origin in resolved.origins.items():
        top, _, sub = key.partition(".")
        if top in EXCLUDED:
            continue
        value = resolved.values[top][sub] if sub else resolved.values[top]
        entries[key] = (value, origin)
    return RunSettings(
        entries=entries,
        plugin_dirs=tuple(sorted(plugin_dirs, key=os.fsencode)),
        plugin_dirs_sha256=plugin_dirs_hash,
        config_sha256=config_sha256(resolved),
        profile_kind=profile.kind,
        profile_sha256=profile.sha256,
    )


def write_run_files(run_dir: Path | str, settings: RunSettings, profile: ProfilePlan) -> None:
    """Write ``permission-profile.json`` and ``settings.json`` (0600, ``O_EXCL|O_NOFOLLOW``).

    The profile goes first: ``settings.json`` names its hash, so a crash between the two leaves
    no settings file that points at a missing profile. An existing file is an error.
    """
    if profile.sha256 != settings.profile_sha256 or profile.kind != settings.profile_kind:
        raise ValueError("the settings do not describe this profile")
    base = Path(run_dir)
    create_private_file(base / PROFILE_FILE, profile.data, fsync=True)
    create_private_file(base / SETTINGS_FILE, settings.to_bytes(), fsync=True)


# -- reading them back -------------------------------------------------------------------------


def _corrupt() -> ReviewRunError:
    return ReviewRunError("internal", "the run's settings files are missing or invalid")


def _read_regular(directory: Path, name: str, limit: int) -> bytes:
    """Open ``directory/name`` without following links, a regular file of the user."""
    try:
        fd = os.open(directory / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        raise _corrupt() from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise _corrupt()
        with os.fdopen(fd, "rb", closefd=False) as handle:
            data = handle.read(limit + 1)
    except OSError:
        raise _corrupt() from None
    finally:
        os.close(fd)
    if len(data) > limit:
        raise _corrupt()
    return data


_SHA = frozenset("0123456789abcdef")


def _is_sha(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= _SHA


def _parse_settings(data: bytes) -> RunSettings:
    try:
        doc = strict_json(data.decode("utf-8"))
    except (ValueError, RecursionError):
        raise _corrupt() from None
    if not isinstance(doc, dict) or doc.get("schema_version") != SCHEMA_VERSION:
        raise _corrupt()
    profile = doc.get("permission_profile")
    items = doc.get("settings")
    dirs = doc.get("plugin_dirs")
    if (
        not isinstance(profile, dict)
        or profile.get("kind") not in PROFILE_KINDS
        or not _is_sha(profile.get("sha256"))
        or not _is_sha(doc.get("plugin_dirs_sha256"))
        or not _is_sha(doc.get("config_sha256"))
        or not isinstance(items, list)
        or not isinstance(dirs, list)
        or not all(isinstance(d, str) for d in dirs)
    ):
        raise _corrupt()
    entries: dict[str, tuple[Any, str]] = {}
    for item in items:
        if (
            not isinstance(item, dict)
            or set(item) != {"key", "value", "origin"}
            or not isinstance(item["key"], str)
            or item["origin"] not in ORIGINS
            or item["key"] in entries
        ):
            raise _corrupt()
        entries[item["key"]] = (item["value"], item["origin"])
    return RunSettings(
        entries=entries,
        plugin_dirs=tuple(dirs),
        plugin_dirs_sha256=doc["plugin_dirs_sha256"],
        config_sha256=doc["config_sha256"],
        profile_kind=profile["kind"],
        profile_sha256=profile["sha256"],
    )


def load_run_settings(run_dir: Path | str) -> RunSettings:
    """Read ``settings.json`` and check ``permission-profile.json`` against its recorded hash.

    Both files are opened without following links and must be regular files of the user; the
    settings are strict JSON of the closed shape. Raises ``internal`` (exit 1) for anything else:
    a missing, altered or malformed file.
    """
    base = Path(run_dir)
    settings = _parse_settings(_read_regular(base, SETTINGS_FILE, MAX_SETTINGS_BYTES))
    profile = _read_regular(base, PROFILE_FILE, MAX_PROFILE_BYTES)
    if hashlib.sha256(profile).hexdigest() != settings.profile_sha256:
        raise _corrupt()
    return settings


# -- the plugin directory hash, rechecked ------------------------------------------------------


class PluginChanged(Exception):
    """The plugin directories no longer hash to the value recorded for the run."""


def verify_plugin_dirs(settings: RunSettings) -> None:
    """Recompute the plugin directory hash and compare it with the stored one.

    A tree the walk now refuses (a link was added, a mode loosened) counts as a change. Raises
    ``PluginChanged``; run before ``agent-spawn launch`` and ``agent-spawn resume``.
    """
    try:
        current = plugin_dirs_sha256(settings.plugin_dirs)
    except PluginTreeError:
        raise PluginChanged from None
    if current != settings.plugin_dirs_sha256:
        raise PluginChanged


def plugin_changed_error() -> ReviewRunError:
    """The envelope of a ``resume`` whose plugin directories changed (exit 12; the run stays
    ``escalated``). At launch the run instead ends ``failed`` (``launch_failed``,
    ``failure.lower_code: plugin_changed``)."""
    return ReviewRunError(
        "failed",
        "the plugin directories changed since the run was created",
        reason="plugin_changed",
    )


# -- the launch flags the settings decide ------------------------------------------------------


def launch_env(settings: RunSettings) -> dict[str, str] | None:
    """The environment of the launch call: ours plus the subagent depth variable when
    ``subagents`` is on, else ``None`` (inherit)."""
    if settings.value("subagents") is not True:
        return None
    env = dict(os.environ)
    env[DEPTH_VARIABLE] = str(settings.value("max_subagent_depth"))
    return env


def launch_flags(
    settings: RunSettings,
    *,
    repo: str,
    pr: str,
    run_id: str,
    profile_path: str,
    result_file: str,
    labels: Sequence[str] = (),
    actor: str | None = None,
    decision_ids: Sequence[str] = (),
) -> list[str]:
    """The flags of ``agent-spawn launch`` that come from the settings (Launch table), without
    the prompt and input files (``--prompt-file``, ``--input-file``) and without ``--wait``,
    which is never passed. No positional argument is ever added, so a value that looks like an
    option cannot swallow one; every value is a closed grammar (checked at load or by the
    caller) and none is untrusted text.
    """
    mode = settings.value("mode")
    flags = ["--mode", mode]
    if mode == "interactive":
        flags += ["--placement", "window"]
    flags += [
        "--kind", "review",
        "--no-implicit-done",
        "--cwd", "session-dir",
        "--repo", repo,
        "--skill", settings.value("skill"),
    ]  # fmt: skip
    for directory in settings.plugin_dirs:
        flags += ["--plugin-dir", directory]
    flags += [
        "--permission-profile", profile_path,
        "--sandbox", settings.value("sandbox"),
        "--setting-sources", ",".join(settings.value("setting_sources")),
    ]  # fmt: skip
    names = list(settings.value("env_allowlist"))
    if settings.value("subagents") is True:
        names.append(DEPTH_VARIABLE)
    for name in names or [""]:
        flags += ["--env-allowlist-only", name]
    for flag, key in (("--model", "model"), ("--timeout", "timeout"), ("--max-turns", "max_turns")):
        value = settings.value(key)
        if value is not None:
            flags += [flag, str(value)]
    flags += ["--no-promote"]
    for label in ("owner=review-run", f"run={run_id}", f"pr={pr}", f"repo={repo}", *labels):
        flags += ["--label", label]
    flags += ["--result-file", result_file, "--json"]
    if actor is not None:
        flags += ["--actor", actor]
    for decision in decision_ids:
        flags += ["--decision-id", decision]
    return [str(f) for f in flags]
