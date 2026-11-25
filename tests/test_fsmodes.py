import errno
import os
import stat
from pathlib import Path

import pytest

from review_run.errors import ReviewRunError
from review_run.fsmodes import (
    apply_umask,
    create_private_file,
    ensure_private_dir,
    write_private_file,
)


def mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def test_apply_umask_sets_077() -> None:
    os.umask(0o022)
    apply_umask()
    current = os.umask(0o077)
    assert current == 0o077


def test_new_dir_is_0700_even_under_a_permissive_umask(tmp_path: Path) -> None:
    os.umask(0)
    target = tmp_path / "state"
    ensure_private_dir(target)
    assert mode(target) == 0o700


def test_existing_loose_dir_is_tightened(tmp_path: Path) -> None:
    target = tmp_path / "state"
    target.mkdir()
    target.chmod(0o755)
    ensure_private_dir(target)
    assert mode(target) == 0o700


def test_existing_private_dir_is_kept_and_contents_untouched(tmp_path: Path) -> None:
    target = tmp_path / "state"
    ensure_private_dir(target)
    (target / "f").write_text("x")
    ensure_private_dir(target)
    assert (target / "f").read_text() == "x"


def test_symlinked_dir_is_refused_as_state_unsafe(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(ReviewRunError) as exc:
        ensure_private_dir(link)
    assert exc.value.code == "failed"
    assert exc.value.reason == "state_unsafe"
    assert exc.value.exit_code == 12


def test_symlink_target_mode_is_not_changed(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    real.chmod(0o755)
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(ReviewRunError):
        ensure_private_dir(link)
    assert mode(real) == 0o755


def test_a_file_where_a_dir_is_expected_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "state"
    target.write_text("x")
    with pytest.raises(ReviewRunError) as exc:
        ensure_private_dir(target)
    assert exc.value.reason == "state_unsafe"


def test_dir_owned_by_someone_else_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "state"
    target.mkdir()
    monkeypatch.setattr(os, "geteuid", lambda: os.stat(target).st_uid + 1)
    with pytest.raises(ReviewRunError) as exc:
        ensure_private_dir(target)
    assert exc.value.reason == "state_unsafe"


def test_missing_parent_is_refused_not_created(tmp_path: Path) -> None:
    with pytest.raises(ReviewRunError) as exc:
        ensure_private_dir(tmp_path / "a" / "b")
    assert exc.value.reason == "state_unsafe"
    assert not (tmp_path / "a").exists()


def test_new_file_is_0600_under_any_umask(tmp_path: Path) -> None:
    os.umask(0)
    target = tmp_path / "f"
    create_private_file(target, b"data")
    assert mode(target) == 0o600
    assert target.read_bytes() == b"data"


def test_create_refuses_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "f"
    target.write_text("old")
    with pytest.raises(FileExistsError):
        create_private_file(target, b"new")
    assert target.read_text() == "old"


def test_create_refuses_a_symlink_and_does_not_follow_it(tmp_path: Path) -> None:
    victim = tmp_path / "victim"
    link = tmp_path / "f"
    link.symlink_to(victim)
    with pytest.raises(FileExistsError):
        create_private_file(link, b"data")
    assert not victim.exists()


def test_write_private_file_replaces_atomically_with_0600(tmp_path: Path) -> None:
    os.umask(0)
    directory = tmp_path / "d"
    directory.mkdir()
    target = directory / "f"
    write_private_file(target, b"one")
    write_private_file(target, b"two")
    assert target.read_bytes() == b"two"
    assert mode(target) == 0o600
    assert sorted(p.name for p in directory.iterdir()) == ["f"]


def test_write_private_file_replaces_a_symlink_without_following(tmp_path: Path) -> None:
    victim = tmp_path / "victim"
    victim.write_text("keep")
    link = tmp_path / "f"
    link.symlink_to(victim)
    write_private_file(link, b"new")
    assert victim.read_text() == "keep"
    assert not link.is_symlink()
    assert link.read_bytes() == b"new"


def _enospc(*args: object, **kwargs: object) -> int:
    raise OSError(errno.ENOSPC, "no space")


def test_create_removes_a_partial_file_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = tmp_path / "d"
    d.mkdir()
    monkeypatch.setattr(os, "write", _enospc)
    with pytest.raises(OSError):
        create_private_file(d / "f", b"data")
    assert list(d.iterdir()) == []


def test_write_removes_its_temp_file_on_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = tmp_path / "d"
    d.mkdir()
    monkeypatch.setattr(os, "write", _enospc)
    with pytest.raises(OSError):
        write_private_file(d / "f", b"data")
    assert list(d.iterdir()) == []


def test_write_removes_its_temp_file_when_the_create_fails_on_fsync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = tmp_path / "d"
    d.mkdir()
    monkeypatch.setattr(os, "fsync", _enospc)
    with pytest.raises(OSError):
        write_private_file(d / "f", b"data")
    assert list(d.iterdir()) == []


def test_write_private_file_accepts_a_250_char_name(tmp_path: Path) -> None:
    d = tmp_path / "d"
    d.mkdir()
    target = d / ("a" * 250)
    write_private_file(target, b"x")
    assert target.read_bytes() == b"x"
    assert [p.name for p in d.iterdir()] == [target.name]


def test_other_os_errors_in_ensure_private_dir_propagate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise OSError(errno.ENOSPC, "no space")

    monkeypatch.setattr(os, "mkdir", fail)
    with pytest.raises(OSError) as exc:
        ensure_private_dir(tmp_path / "state")
    assert exc.value.errno == errno.ENOSPC
