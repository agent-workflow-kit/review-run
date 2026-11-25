"""Read one config file: a private regular file, strict YAML 1.2 core scalars, no echo of input.

Files are opened ``O_NONBLOCK`` (a FIFO never blocks), must be regular, at most
``MAX_FILE_BYTES``, owned by the user and not writable by others or a foreign group (checked on
the opened descriptor, so a symlink to a good file is followed and a link to a bad one is
refused). "Not writable" means not by others and not by a group that others can be in: a
group-writable file is accepted only when its group is the user's effective group and that
group is private (no other member, no other account with it as primary group), so a
private-group system (umask 002) still works.

Anchors, aliases (no expansion bombs), explicit tags, merge keys, non-string keys and duplicate
keys are refused. Failure messages hold a position (line and column) and never the input text,
because a parser message would quote it.
"""

from __future__ import annotations

import grp
import os
import pwd
import re
import stat
from pathlib import Path
from typing import Any, Final

import yaml

MAX_FILE_BYTES: Final = 256 * 1024


class ConfigFileError(Exception):
    """A file that cannot be used; the message never carries a value from the file."""


def _at(mark: Any) -> str:
    return f"line {mark.line + 1}, column {mark.column + 1}"


class _StrictLoader(yaml.SafeLoader):
    """A SafeLoader with YAML 1.2 core-schema scalars and the refusals listed above."""

    yaml_implicit_resolvers: dict[Any, Any] = {}

    def compose_node(self, parent: Any, index: Any) -> Any:
        event = self.peek_event()
        where = _at(event.start_mark)
        if isinstance(event, yaml.AliasEvent):
            raise ConfigFileError(f"aliases are not allowed at {where}")
        if getattr(event, "anchor", None) is not None:
            raise ConfigFileError(f"anchors are not allowed at {where}")
        if getattr(event, "tag", None) is not None:
            raise ConfigFileError(f"YAML tags are not allowed at {where}")
        return super().compose_node(parent, index)

    def construct_object(self, node: Any, deep: bool = False) -> Any:
        try:
            return super().construct_object(node, deep)
        except (ValueError, OverflowError):
            raise ConfigFileError(f"invalid value at {_at(node.start_mark)}") from None

    def construct_mapping(self, node: Any, deep: bool = False) -> dict[Any, Any]:
        seen: set[str] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=True)
            if not isinstance(key, str):
                raise ConfigFileError(
                    f"mapping key at {_at(key_node.start_mark)} must be a string (quote it)"
                )
            if key == "<<":
                raise ConfigFileError(f"merge keys are not allowed at {_at(key_node.start_mark)}")
            if key in seen:
                raise ConfigFileError(f"duplicate key at {_at(key_node.start_mark)}")
            seen.add(key)
        return super().construct_mapping(node, deep)


# YAML 1.2 core schema, narrowed: decimal integers without leading zeros or underscores; `yes`,
# `no`, `on` and `off` are plain strings.
for _tag, _pattern, _first in (
    ("bool", r"^(?:true|True|TRUE|false|False|FALSE)\Z", "tTfF"),
    ("int", r"^[-+]?(?:0|[1-9][0-9]*)\Z", "-+0123456789"),
    (
        "float",
        r"^(?:[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?"
        r"|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))\Z",
        "-+0123456789.",
    ),
    ("null", r"^(?:~|null|Null|NULL|)\Z", ["~", "n", "N", ""]),
):
    _StrictLoader.add_implicit_resolver(
        f"tag:yaml.org,2002:{_tag}", re.compile(_pattern), list(_first)
    )


def private_group(gid: int) -> bool:
    """``gid`` is the user's effective group and nobody else is in it: no other member in the
    group database and no other account with it as primary group."""
    if gid != os.getegid():
        return False
    try:
        me = pwd.getpwuid(os.geteuid()).pw_name
    except KeyError:
        return False
    try:
        members = grp.getgrgid(gid).gr_mem
    except Exception:  # noqa: BLE001 (a failed group lookup cannot prove privacy)
        return False
    if any(member != me for member in members):
        return False
    try:
        accounts = pwd.getpwall()
    except Exception:  # noqa: BLE001 (an unreadable account database cannot prove privacy)
        return False
    return not any(entry.pw_gid == gid and entry.pw_uid != os.geteuid() for entry in accounts)


def writable_by_others(info: os.stat_result) -> bool:
    """Writable by anyone but the owner: others, or a group that is not the user's private one."""
    if info.st_mode & 0o002:
        return True
    return bool(info.st_mode & 0o020 and not private_group(info.st_gid))


def read_private_file(path: Path) -> bytes:
    """The bytes of a regular, user-owned, not group- or world-writable file (size capped)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as exc:
        raise ConfigFileError(f"cannot be read ({exc.strerror})") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ConfigFileError("not a regular file")
        if info.st_uid != os.geteuid():
            raise ConfigFileError("not owned by you")
        if writable_by_others(info):
            raise ConfigFileError("writable by group or others")
        if info.st_size > MAX_FILE_BYTES:
            raise ConfigFileError(f"larger than {MAX_FILE_BYTES} bytes")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            data = handle.read(MAX_FILE_BYTES + 1)
    except OSError as exc:
        raise ConfigFileError(f"cannot be read ({exc.strerror})") from None
    finally:
        os.close(fd)
    if len(data) > MAX_FILE_BYTES:
        raise ConfigFileError(f"larger than {MAX_FILE_BYTES} bytes")
    return data


def check_private_dir(path: Path) -> bool:
    """True when ``path`` is a directory owned by the user and not writable by group or others;
    False when it does not exist. Anything else raises."""
    try:
        info = os.stat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ConfigFileError(f"cannot be read ({exc.strerror})") from None
    if not stat.S_ISDIR(info.st_mode):
        raise ConfigFileError("not a directory")
    if info.st_uid != os.geteuid():
        raise ConfigFileError("not owned by you")
    if writable_by_others(info):
        raise ConfigFileError("writable by group or others")
    return True


def parse_yaml(data: bytes) -> dict[str, Any]:
    """One document as a mapping; an empty document is ``{}``."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ConfigFileError("not valid UTF-8") from None
    try:
        document = yaml.load(text, Loader=_StrictLoader)  # noqa: S506 (a SafeLoader subclass)
    except ConfigFileError:
        raise
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        raise ConfigFileError(f"invalid YAML at {_at(mark)}" if mark else "invalid YAML") from None
    except RecursionError:
        raise ConfigFileError("invalid YAML (too deeply nested)") from None
    except Exception:  # noqa: BLE001 (any parser failure; its text may quote the input)
        raise ConfigFileError("invalid YAML") from None
    if document is None:
        return {}
    if not isinstance(document, dict):
        raise ConfigFileError("the top level must be a mapping")
    return document
