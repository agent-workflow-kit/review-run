"""The plugin directory hash (interface.md, Run settings; design D-33).

sha256 over every passed plugin directory, in the order of their realpaths, of each entry's
sorted relative path, permission bits and contents. The tree is walked through directory
descriptors without following links, and the walk refuses what a plugin directory must not hold:
a symlink, a special file, an entry owned by someone else or writable by group or others.

Framing (so that no two different trees hash alike): a header, then per directory ``P`` followed
by its entries in pre-order with names sorted as bytes; each entry is ``kind`` (``d`` or ``f``),
the permission bits (``>H``), the length (``>Q``) and the bytes of its relative path (components
joined by ``/``, the root is empty) and, for a file, its size (``>Q``) and contents; ``E`` closes
the directory. File names are bytes, so a non-UTF-8 name is hashed as it is.
"""

from __future__ import annotations

import hashlib
import os
import stat
import struct
from collections.abc import Sequence
from typing import Final

MAX_ENTRIES: Final = 20_000
MAX_TOTAL_BYTES: Final = 64 * 1024 * 1024
MAX_DEPTH: Final = 32
_CHUNK: Final = 1024 * 1024
_HEADER: Final = b"review-run plugin dirs v1\0"

# Closed reasons (never a path or a file name).
REASONS: Final = (
    "symlink",
    "special_file",
    "foreign_owner",
    "writable",
    "too_large",
    "too_deep",
    "changed",
    "unreadable",
    "not_directory",
)


class PluginTreeError(Exception):
    """A plugin directory the walk refuses; ``reason`` is one of ``REASONS``."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"plugin directory refused: {reason}")
        self.reason = reason


def _check(info: os.stat_result, uid: int) -> None:
    if stat.S_ISLNK(info.st_mode):
        raise PluginTreeError("symlink")
    if info.st_uid != uid:
        raise PluginTreeError("foreign_owner")
    if info.st_mode & 0o022:
        raise PluginTreeError("writable")


class _Walk:
    def __init__(self) -> None:
        self.hash = hashlib.sha256()
        self.entries = 0
        self.bytes = 0
        self.uid = os.geteuid()

    def entry(self, kind: bytes, mode: int, rel: bytes, *, counted: bool = True) -> None:
        if counted:  # the root directory is not an entry of the limit
            self.entries += 1
            if self.entries > MAX_ENTRIES:
                raise PluginTreeError("too_large")
        self.hash.update(struct.pack(">cHQ", kind, stat.S_IMODE(mode), len(rel)) + rel)

    def check(self, info: os.stat_result) -> None:
        _check(info, self.uid)

    def directory(self, fd: int, rel: bytes, depth: int) -> None:
        if depth > MAX_DEPTH:
            raise PluginTreeError("too_deep")
        try:
            names = sorted(os.listdir(fd), key=os.fsencode)
        except OSError:
            raise PluginTreeError("unreadable") from None
        for name in names:
            child = rel + b"/" + os.fsencode(name) if rel else os.fsencode(name)
            try:
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except OSError:
                raise PluginTreeError("changed") from None
            self.check(info)
            if stat.S_ISDIR(info.st_mode):
                sub, after = self.open_stat(name, fd, info, directory=True)
                try:
                    self.entry(b"d", after.st_mode, child)
                    self.directory(sub, child, depth + 1)
                finally:
                    os.close(sub)
            elif stat.S_ISREG(info.st_mode):
                self.file(name, fd, info, child)
            else:
                raise PluginTreeError("special_file")
        try:
            again = sorted(os.listdir(fd), key=os.fsencode)
        except OSError:
            raise PluginTreeError("unreadable") from None
        if again != names:
            raise PluginTreeError("changed")  # an entry was added or removed during the walk

    @staticmethod
    def open_checked(name: str, dir_fd: int, info: os.stat_result, *, directory: bool) -> int:
        """``open_stat`` for a caller that needs only the descriptor."""
        return _Walk.open_stat(name, dir_fd, info, directory=directory)[0]

    @staticmethod
    def open_stat(
        name: str, dir_fd: int, info: os.stat_result, *, directory: bool
    ) -> tuple[int, os.stat_result]:
        """Open the entry and check what was opened: the same inode as the earlier ``lstat``
        and the owner and mode checks again on the descriptor (the entry may have been
        replaced or its mode changed in between). Returns the descriptor and its ``fstat``."""
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
        flags |= os.O_DIRECTORY if directory else os.O_NONBLOCK
        try:
            fd = os.open(name, flags, dir_fd=dir_fd)
        except OSError:
            raise PluginTreeError("symlink" if _is_link(name, dir_fd) else "unreadable") from None
        try:
            after = os.fstat(fd)
            if (after.st_dev, after.st_ino) != (info.st_dev, info.st_ino):
                raise PluginTreeError("changed")
            _check(after, os.geteuid())
        except OSError:
            os.close(fd)
            raise PluginTreeError("unreadable") from None
        except PluginTreeError:
            os.close(fd)
            raise
        return fd, after

    def file(self, name: str, dir_fd: int, info: os.stat_result, rel: bytes = b"") -> None:
        fd, after = self.open_stat(name, dir_fd, info, directory=False)
        try:
            if not stat.S_ISREG(after.st_mode):
                raise PluginTreeError("special_file")
            self.entry(b"f", after.st_mode, rel)
            self.bytes += after.st_size
            if self.bytes > MAX_TOTAL_BYTES:
                raise PluginTreeError("too_large")
            self.hash.update(struct.pack(">Q", after.st_size))
            remaining = after.st_size
            while remaining:
                chunk = os.read(fd, min(_CHUNK, remaining))
                if not chunk:
                    raise PluginTreeError("changed")  # shrank while read
                self.hash.update(chunk)
                remaining -= len(chunk)
            if os.read(fd, 1):
                raise PluginTreeError("changed")  # grew while read
        except OSError:
            raise PluginTreeError("unreadable") from None
        finally:
            os.close(fd)


def _is_link(name: str, dir_fd: int) -> bool:
    try:
        return stat.S_ISLNK(os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode)
    except OSError:
        return False


def plugin_dirs_sha256(paths: Sequence[str]) -> str:
    """The hash of the plugin directories at ``paths`` (realpaths; sorted here).

    Raises ``PluginTreeError`` for a directory that is missing, is a link or a non-directory, or
    holds a refused entry.
    """
    walk = _Walk()
    walk.hash.update(_HEADER)
    for path in sorted(paths, key=os.fsencode):
        try:
            info = os.stat(path, follow_symlinks=False)
        except OSError:
            raise PluginTreeError("unreadable") from None
        if stat.S_ISLNK(info.st_mode):
            raise PluginTreeError("symlink")
        if not stat.S_ISDIR(info.st_mode):
            raise PluginTreeError("not_directory")
        walk.check(info)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError:
            raise PluginTreeError("unreadable") from None
        try:
            after = os.fstat(fd)
            if (after.st_dev, after.st_ino) != (info.st_dev, info.st_ino):
                raise PluginTreeError("changed")
            walk.hash.update(b"P")
            walk.check(after)
            walk.entry(b"d", after.st_mode, b"", counted=False)
            walk.directory(fd, b"", 1)
            walk.hash.update(b"E")
        finally:
            os.close(fd)
    return walk.hash.hexdigest()
