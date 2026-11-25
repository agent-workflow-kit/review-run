"""Private file modes: process umask 077, directories 0700 and files 0600.

Helpers only; the state root and its layout belong to the store. A path that is a symlink, is not
a directory or is not owned by the user is refused as ``state_unsafe`` (exit 12) and never
followed.
"""

from __future__ import annotations

import errno
import os
import secrets
import stat
import sys
from pathlib import Path

from .errors import ReviewRunError

DIR_MODE = 0o700
FILE_MODE = 0o600
UMASK = 0o077
_UNSAFE_ERRNOS = frozenset(
    {errno.ELOOP, errno.ENOTDIR, errno.EACCES, errno.EPERM, errno.EEXIST, errno.ENOENT}
)
_TMP_NAME_MAX = 100


def apply_umask() -> None:
    """Set the process umask to 077; every file review-run creates is then private."""
    os.umask(UMASK)


def _unsafe() -> ReviewRunError:
    return ReviewRunError("failed", "state path is unsafe", reason="state_unsafe")


def ensure_private_dir(path: Path | str) -> None:
    """Create ``path`` (one level; its parent must exist) or tighten it to 0700.

    Refuses a symlink, a non-directory and a directory owned by someone else.
    """
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        try:
            fd = os.open(path, flags)
        except FileNotFoundError:
            try:
                os.mkdir(path, DIR_MODE)
            except FileExistsError:
                pass  # another process created it first: open and verify it like any existing one
            fd = os.open(path, flags)
    except OSError as exc:
        # A symlink (ELOOP or ENOTDIR), a file, a missing parent or no permission. Anything
        # else (ENOSPC, EROFS, ...) is not a state-safety problem and propagates.
        if exc.errno not in _UNSAFE_ERRNOS:
            raise
        raise _unsafe() from None
    try:
        info = os.fstat(fd)
        if info.st_uid != os.geteuid():
            raise _unsafe()
        if stat.S_IMODE(info.st_mode) != DIR_MODE:
            os.fchmod(fd, DIR_MODE)
    finally:
        os.close(fd)


def create_private_file(path: Path | str, data: bytes, *, fsync: bool = False) -> None:
    """Create ``path`` with mode 0600 (``O_CREAT|O_EXCL|O_NOFOLLOW``) holding ``data``.

    Raises ``FileExistsError`` when the path exists, a symlink included (it is not followed).
    """
    fd = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        FILE_MODE,
    )
    try:
        try:
            os.fchmod(fd, FILE_MODE)  # exact whatever the umask
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view) :]
            if fsync:
                os.fsync(fd)
        finally:
            os.close(fd)
    except BaseException:
        # Never leave a partial file at the final path.
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        raise


def write_private_file(path: Path | str, data: bytes) -> None:
    """Replace ``path`` atomically with a 0600 file holding ``data``.

    The data goes to a fresh temporary file in the same directory that is renamed over the
    target; a symlink at the target is replaced, never followed.
    """
    target = Path(path)
    stem = os.fsencode(target.name)[:_TMP_NAME_MAX].decode(
        sys.getfilesystemencoding(), errors="ignore"
    )
    tmp = target.with_name(f".{stem}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        create_private_file(tmp, data, fsync=True)
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    _fsync_dir(target.parent)


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
