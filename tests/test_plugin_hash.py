"""The plugin directory hash (issue #16): deterministic, covers exactly what the interface says,
refuses links, foreign owners, group- or world-writable entries and unbounded trees."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from review_run import plugin_hash as ph
from review_run.plugin_hash import PluginTreeError, plugin_dirs_sha256


def make_plugin(root: Path, name: str = "p") -> Path:
    base = root / name
    (base / "skills" / "s").mkdir(parents=True)
    (base / ".claude").mkdir()
    (base / "skills" / "s" / "SKILL.md").write_text("skill", encoding="utf-8")
    (base / ".mcp.json").write_text("{}", encoding="utf-8")
    (base / "CLAUDE.md").write_text("", encoding="utf-8")
    for p in [base, *base.rglob("*")]:
        p.chmod(0o755 if p.is_dir() else 0o644)
    return base


def test_hash_is_deterministic_and_hex(tmp_path: Path) -> None:
    plugin = make_plugin(tmp_path)
    first = plugin_dirs_sha256([str(plugin)])
    assert first == plugin_dirs_sha256([str(plugin)])
    assert len(first) == 64 and set(first) <= set("0123456789abcdef")


def test_identical_trees_at_different_paths_hash_alike(tmp_path: Path) -> None:
    a = make_plugin(tmp_path, "a")
    b = make_plugin(tmp_path, "b")
    assert plugin_dirs_sha256([str(a)]) == plugin_dirs_sha256([str(b)])


def test_content_change_changes_the_hash(tmp_path: Path) -> None:
    plugin = make_plugin(tmp_path)
    before = plugin_dirs_sha256([str(plugin)])
    (plugin / "skills" / "s" / "SKILL.md").write_text("skilL", encoding="utf-8")
    assert plugin_dirs_sha256([str(plugin)]) != before


def test_permission_bits_are_covered(tmp_path: Path) -> None:
    plugin = make_plugin(tmp_path)
    before = plugin_dirs_sha256([str(plugin)])
    (plugin / "CLAUDE.md").chmod(0o755)
    assert plugin_dirs_sha256([str(plugin)]) != before


def test_directory_modes_are_covered(tmp_path: Path) -> None:
    plugin = make_plugin(tmp_path)
    before = plugin_dirs_sha256([str(plugin)])
    (plugin / ".claude").chmod(0o700)
    assert plugin_dirs_sha256([str(plugin)]) != before


def test_added_removed_and_renamed_entries_change_the_hash(tmp_path: Path) -> None:
    plugin = make_plugin(tmp_path)
    base = plugin_dirs_sha256([str(plugin)])
    extra = plugin / "extra"
    extra.write_text("", encoding="utf-8")
    extra.chmod(0o644)
    added = plugin_dirs_sha256([str(plugin)])
    assert added != base
    extra.rename(plugin / "extra2")
    assert plugin_dirs_sha256([str(plugin)]) != added
    (plugin / "extra2").unlink()
    assert plugin_dirs_sha256([str(plugin)]) == base


def test_an_empty_directory_is_an_entry(tmp_path: Path) -> None:
    plugin = make_plugin(tmp_path)
    before = plugin_dirs_sha256([str(plugin)])
    (plugin / "empty").mkdir(mode=0o755)
    (plugin / "empty").chmod(0o755)
    assert plugin_dirs_sha256([str(plugin)]) != before


def test_file_content_cannot_pose_as_a_path(tmp_path: Path) -> None:
    """A file holding the framing of another entry hashes differently from two entries."""
    a = tmp_path / "a"
    a.mkdir(mode=0o755)
    (a / "x").write_bytes(b"y")
    b = tmp_path / "b"
    b.mkdir(mode=0o755)
    (b / "x").write_bytes(b"")
    (b / "y").write_bytes(b"")
    for p in (*a.iterdir(), *b.iterdir()):
        p.chmod(0o644)
    assert plugin_dirs_sha256([str(a)]) != plugin_dirs_sha256([str(b)])


def test_order_of_arguments_does_not_matter_only_the_realpath_order(tmp_path: Path) -> None:
    a = make_plugin(tmp_path, "a")
    b = make_plugin(tmp_path, "b")
    (b / "CLAUDE.md").write_text("different", encoding="utf-8")
    (b / "CLAUDE.md").chmod(0o644)
    assert plugin_dirs_sha256([str(a), str(b)]) == plugin_dirs_sha256([str(b), str(a)])
    assert plugin_dirs_sha256([str(a), str(b)]) != plugin_dirs_sha256([str(a)])


def test_non_utf8_names_are_hashed_as_bytes(tmp_path: Path) -> None:
    plugin = make_plugin(tmp_path)
    before = plugin_dirs_sha256([str(plugin)])
    bad = os.path.join(os.fsencode(plugin), b"bad-\xff\xfe")
    with open(bad, "wb") as handle:
        handle.write(b"x")
    os.chmod(bad, 0o644)
    assert plugin_dirs_sha256([str(plugin)]) != before


def test_unicode_names_work(tmp_path: Path) -> None:
    plugin = make_plugin(tmp_path)
    (plugin / "ü-‮.md").write_text("x", encoding="utf-8")
    (plugin / "ü-‮.md").chmod(0o644)
    assert len(plugin_dirs_sha256([str(plugin)])) == 64


def test_a_symlink_entry_is_refused(tmp_path: Path) -> None:
    plugin = make_plugin(tmp_path)
    (plugin / "link").symlink_to("CLAUDE.md")
    with pytest.raises(PluginTreeError) as exc:
        plugin_dirs_sha256([str(plugin)])
    assert exc.value.reason == "symlink"


def test_a_symlink_to_a_directory_outside_is_not_followed(tmp_path: Path) -> None:
    plugin = make_plugin(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("s", encoding="utf-8")
    (plugin / "out").symlink_to(outside)
    with pytest.raises(PluginTreeError) as exc:
        plugin_dirs_sha256([str(plugin)])
    assert exc.value.reason == "symlink"


def test_a_dangling_symlink_is_refused(tmp_path: Path) -> None:
    plugin = make_plugin(tmp_path)
    (plugin / "dangling").symlink_to("nowhere")
    with pytest.raises(PluginTreeError) as exc:
        plugin_dirs_sha256([str(plugin)])
    assert exc.value.reason == "symlink"


def test_the_root_being_a_symlink_is_refused(tmp_path: Path) -> None:
    plugin = make_plugin(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(plugin)
    with pytest.raises(PluginTreeError) as exc:
        plugin_dirs_sha256([str(link)])
    assert exc.value.reason == "symlink"


def test_the_root_must_be_a_directory(tmp_path: Path) -> None:
    file = tmp_path / "f"
    file.write_text("x", encoding="utf-8")
    with pytest.raises(PluginTreeError) as exc:
        plugin_dirs_sha256([str(file)])
    assert exc.value.reason == "not_directory"


def test_a_missing_root_is_refused(tmp_path: Path) -> None:
    with pytest.raises(PluginTreeError) as exc:
        plugin_dirs_sha256([str(tmp_path / "gone")])
    assert exc.value.reason == "unreadable"


@pytest.mark.parametrize("mode", [0o664, 0o646, 0o666])
def test_a_file_writable_by_group_or_others_is_refused(tmp_path: Path, mode: int) -> None:
    plugin = make_plugin(tmp_path)
    (plugin / "CLAUDE.md").chmod(mode)
    with pytest.raises(PluginTreeError) as exc:
        plugin_dirs_sha256([str(plugin)])
    assert exc.value.reason == "writable"


def test_a_directory_writable_by_group_is_refused(tmp_path: Path) -> None:
    plugin = make_plugin(tmp_path)
    (plugin / "skills").chmod(0o775)
    with pytest.raises(PluginTreeError) as exc:
        plugin_dirs_sha256([str(plugin)])
    assert exc.value.reason == "writable"


def test_the_root_writable_by_others_is_refused(tmp_path: Path) -> None:
    plugin = make_plugin(tmp_path)
    plugin.chmod(0o757)
    with pytest.raises(PluginTreeError) as exc:
        plugin_dirs_sha256([str(plugin)])
    assert exc.value.reason == "writable"


def test_an_entry_owned_by_someone_else_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin = make_plugin(tmp_path)
    real = os.geteuid()
    monkeypatch.setattr(os, "geteuid", lambda: real + 1)
    with pytest.raises(PluginTreeError) as exc:
        plugin_dirs_sha256([str(plugin)])
    assert exc.value.reason == "foreign_owner"


def test_a_fifo_is_refused(tmp_path: Path) -> None:
    plugin = make_plugin(tmp_path)
    os.mkfifo(plugin / "pipe", 0o644)
    with pytest.raises(PluginTreeError) as exc:
        plugin_dirs_sha256([str(plugin)])
    assert exc.value.reason == "special_file"


def test_too_many_entries_are_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plugin = make_plugin(tmp_path)
    monkeypatch.setattr(ph, "MAX_ENTRIES", 5)
    for i in range(10):
        f = plugin / f"f{i}"
        f.write_text("", encoding="utf-8")
        f.chmod(0o644)
    with pytest.raises(PluginTreeError) as exc:
        plugin_dirs_sha256([str(plugin)])
    assert exc.value.reason == "too_large"


def test_too_many_bytes_are_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plugin = make_plugin(tmp_path)
    monkeypatch.setattr(ph, "MAX_TOTAL_BYTES", 10)
    (plugin / "big").write_bytes(b"x" * 11)
    (plugin / "big").chmod(0o644)
    with pytest.raises(PluginTreeError) as exc:
        plugin_dirs_sha256([str(plugin)])
    assert exc.value.reason == "too_large"


def test_a_deep_tree_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plugin = make_plugin(tmp_path)
    monkeypatch.setattr(ph, "MAX_DEPTH", 3)
    deep = plugin
    for i in range(5):
        deep = deep / f"d{i}"
        deep.mkdir(mode=0o755)
        deep.chmod(0o755)
    with pytest.raises(PluginTreeError) as exc:
        plugin_dirs_sha256([str(plugin)])
    assert exc.value.reason == "too_deep"


def test_errors_never_carry_a_path(tmp_path: Path) -> None:
    plugin = make_plugin(tmp_path)
    (plugin / "SECRET-NAME").symlink_to("CLAUDE.md")
    with pytest.raises(PluginTreeError) as exc:
        plugin_dirs_sha256([str(plugin)])
    assert "SECRET-NAME" not in str(exc.value) and str(tmp_path) not in str(exc.value)


def test_known_answer_pins_the_framing(tmp_path: Path) -> None:
    """The framing is part of the stored value: changing it silently invalidates stored hashes."""
    root = tmp_path / "k"
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    (root / "a").write_bytes(b"hi")
    (root / "a").chmod(0o644)
    import struct

    h = hashlib.sha256()
    h.update(b"review-run plugin dirs v1\0P")
    h.update(struct.pack(">cHQ", b"d", 0o755, 0))
    h.update(struct.pack(">cHQ", b"f", 0o644, 1) + b"a" + struct.pack(">Q", 2) + b"hi" + b"E")
    assert plugin_dirs_sha256([str(root)]) == h.hexdigest()


# -- mutation-check additions -------------------------------------------------------------------


def _frame(kind: bytes, mode: int, rel: bytes, body: bytes | None = None) -> bytes:
    import struct

    out = struct.pack(">cHQ", kind, mode, len(rel)) + rel
    if body is not None:
        out += struct.pack(">Q", len(body)) + body
    return out


def test_known_answer_nested_tree_with_byte_sorted_names(tmp_path: Path) -> None:
    """Directories carry the ``d`` tag, children follow their directory, names sort as bytes
    (``U+FF21`` is ``ef bc a1``, below the raw byte ``ff``, though its code point is above)."""
    root = tmp_path / "k"
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    base = os.fsencode(root)
    # created in an order that is neither the sorted one nor its reverse
    for name, data in ((b"z", b"3"), (b"\xff", b"2"), ("Ａ".encode(), b"1")):
        path = os.path.join(base, name)
        with open(path, "wb") as handle:
            handle.write(data)
        os.chmod(path, 0o644)
    sub = os.path.join(base, b"d")
    os.mkdir(sub, 0o755)
    os.chmod(sub, 0o755)
    with open(os.path.join(sub, b"f"), "wb") as handle:
        handle.write(b"x")
    os.chmod(os.path.join(sub, b"f"), 0o644)
    expected = (
        b"review-run plugin dirs v1\0P"
        + _frame(b"d", 0o755, b"")
        + _frame(b"d", 0o755, b"d")
        + _frame(b"f", 0o644, b"d/f", b"x")
        + _frame(b"f", 0o644, b"z", b"3")
        + _frame(b"f", 0o644, "Ａ".encode(), b"1")
        + _frame(b"f", 0o644, b"\xff", b"2")
        + b"E"
    )
    assert plugin_dirs_sha256([str(root)]) == hashlib.sha256(expected).hexdigest()


def test_the_entry_limit_is_inclusive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The root is not an entry: MAX_ENTRIES descendants pass, one more is refused."""
    monkeypatch.setattr(ph, "MAX_ENTRIES", 5)
    root = tmp_path / "r"
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    for i in range(5):
        (root / f"f{i}").write_bytes(b"")
        (root / f"f{i}").chmod(0o644)
    assert len(plugin_dirs_sha256([str(root)])) == 64
    (root / "extra").write_bytes(b"")
    (root / "extra").chmod(0o644)
    with pytest.raises(PluginTreeError) as exc:
        plugin_dirs_sha256([str(root)])
    assert exc.value.reason == "too_large"


def test_the_byte_limit_is_inclusive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ph, "MAX_TOTAL_BYTES", 10)
    root = tmp_path / "r"
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    (root / "a").write_bytes(b"x" * 6)
    (root / "b").write_bytes(b"y" * 4)
    for name in ("a", "b"):
        (root / name).chmod(0o644)
    assert len(plugin_dirs_sha256([str(root)])) == 64
    (root / "c").write_bytes(b"z")
    (root / "c").chmod(0o644)
    with pytest.raises(PluginTreeError) as exc:
        plugin_dirs_sha256([str(root)])
    assert exc.value.reason == "too_large"


def _nest(root: Path, levels: int) -> None:
    deep = root
    for i in range(levels):
        deep = deep / f"d{i}"
        deep.mkdir(mode=0o755)
        deep.chmod(0o755)


def test_the_depth_limit_is_inclusive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With MAX_DEPTH 3 the root's contents are depth 1: two nested directories pass, three are
    too deep."""
    monkeypatch.setattr(ph, "MAX_DEPTH", 3)
    ok = tmp_path / "ok"
    ok.mkdir(mode=0o755)
    ok.chmod(0o755)
    _nest(ok, 2)
    assert len(plugin_dirs_sha256([str(ok)])) == 64
    bad = tmp_path / "bad"
    bad.mkdir(mode=0o755)
    bad.chmod(0o755)
    _nest(bad, 3)
    with pytest.raises(PluginTreeError) as exc:
        plugin_dirs_sha256([str(bad)])
    assert exc.value.reason == "too_deep"


def _open_dir(path: Path) -> int:
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY)


@pytest.mark.parametrize("directory", [False, True])
def test_a_link_swapped_in_before_the_open_is_refused_as_a_symlink(
    tmp_path: Path, directory: bool
) -> None:
    """The child open does not follow links even if the earlier stat was of the target."""
    base = tmp_path / "b"
    base.mkdir(mode=0o755)
    target = base / "target"
    if directory:
        target.mkdir(mode=0o755)
    else:
        target.write_bytes(b"x")
    (base / "name").symlink_to("target")
    info = os.stat(base / "target")
    fd = _open_dir(base)
    try:
        with pytest.raises(PluginTreeError) as exc:
            ph._Walk.open_checked("name", fd, info, directory=directory)
    finally:
        os.close(fd)
    assert exc.value.reason == "symlink"


@pytest.mark.parametrize("directory", [False, True])
def test_an_entry_replaced_between_stat_and_open_is_changed(
    tmp_path: Path, directory: bool
) -> None:
    base = tmp_path / "b"
    base.mkdir(mode=0o755)
    make = (lambda p: p.mkdir(mode=0o755)) if directory else (lambda p: p.write_bytes(b"x"))
    make(base / "name")
    info = os.stat(base / "name", follow_symlinks=False)
    make(base / "other")
    os.rename(base / "other", base / "name")  # a different inode now sits at the name
    fd = _open_dir(base)
    try:
        with pytest.raises(PluginTreeError) as exc:
            ph._Walk.open_checked("name", fd, info, directory=directory)
    finally:
        os.close(fd)
    assert exc.value.reason == "changed"


@pytest.mark.parametrize("how", ["shrinks", "grows"])
def test_a_file_that_shrinks_or_grows_while_read_is_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    """The size is the opened descriptor's; a change after that is seen by the read."""
    base = tmp_path / "b"
    base.mkdir(mode=0o755)
    path = base / "f"
    path.write_bytes(b"xxx")
    path.chmod(0o644)
    info = os.stat(path, follow_symlinks=False)
    genuine = os.read

    def racing(fd: int, n: int) -> bytes:
        if how == "shrinks":
            return b""  # the file ended before its recorded size
        data = genuine(fd, n)
        return data if n != 1 else b"y"  # one more byte after the recorded size

    fd = _open_dir(base)
    monkeypatch.setattr(os, "read", racing)
    try:
        with pytest.raises(PluginTreeError) as exc:
            ph._Walk().file("f", fd, info)
    finally:
        monkeypatch.undo()
        os.close(fd)
    assert exc.value.reason == "changed"


def test_the_root_is_opened_without_following_a_link_swapped_in_after_the_stat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = tmp_path / "real"
    real.mkdir(mode=0o755)
    real.chmod(0o755)
    link = tmp_path / "link"
    link.symlink_to(real)
    genuine = os.stat

    def stat_follows_for_link(path: object, *a: object, **kw: object) -> os.stat_result:
        if path == str(link) and not a and kw.get("dir_fd") is None:
            return genuine(real)  # the check saw a plain directory ...
        return genuine(path, *a, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "stat", stat_follows_for_link)  # ... then the link was swapped in
    with pytest.raises(PluginTreeError):
        plugin_dirs_sha256([str(link)])


def test_the_root_replaced_between_stat_and_open_is_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    for d in (a, b):
        d.mkdir(mode=0o755)
        d.chmod(0o755)
    genuine = os.stat

    def stale(path: object, *args: object, **kw: object) -> os.stat_result:
        if path == str(a) and kw.get("dir_fd") is None:
            return genuine(b)  # the stat described another directory than the one then opened
        return genuine(path, *args, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "stat", stale)
    with pytest.raises(PluginTreeError) as exc:
        plugin_dirs_sha256([str(a)])
    assert exc.value.reason == "changed"


def test_a_fifo_swapped_in_before_the_open_does_not_block(tmp_path: Path) -> None:
    """Files are opened ``O_NONBLOCK``: a FIFO that replaced the file must not hang the walk."""
    import signal

    base = tmp_path / "b"
    base.mkdir(mode=0o755)
    os.mkfifo(base / "pipe", 0o644)
    info = os.stat(base / "pipe", follow_symlinks=False)
    fd = _open_dir(base)

    def hung(signum: int, frame: object) -> None:
        raise TimeoutError("open of a FIFO blocked")

    previous = signal.signal(signal.SIGALRM, hung)
    signal.alarm(5)
    try:
        opened = ph._Walk.open_checked("pipe", fd, info, directory=False)
        os.close(opened)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
        os.close(fd)


def _tree(tmp_path: Path) -> Path:
    root = tmp_path / "r"
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    (root / "a").write_bytes(b"a")
    (root / "a").chmod(0o644)
    return root


def test_a_mode_loosened_between_stat_and_open_is_refused(tmp_path: Path) -> None:
    """The checks run again on the opened descriptor, not only on the earlier lstat."""
    base = tmp_path / "b"
    base.mkdir(mode=0o755)
    (base / "f").write_bytes(b"x")
    (base / "f").chmod(0o644)
    info = os.stat(base / "f", follow_symlinks=False)
    (base / "f").chmod(0o666)  # after the stat, before the open
    fd = _open_dir(base)
    try:
        with pytest.raises(PluginTreeError) as exc:
            ph._Walk.open_checked("f", fd, info, directory=False)
    finally:
        os.close(fd)
    assert exc.value.reason == "writable"


def test_a_directory_loosened_between_stat_and_open_is_refused(tmp_path: Path) -> None:
    base = tmp_path / "b"
    base.mkdir(mode=0o755)
    (base / "d").mkdir(mode=0o755)
    (base / "d").chmod(0o755)
    info = os.stat(base / "d", follow_symlinks=False)
    (base / "d").chmod(0o777)
    fd = _open_dir(base)
    try:
        with pytest.raises(PluginTreeError) as exc:
            ph._Walk.open_checked("d", fd, info, directory=True)
    finally:
        os.close(fd)
    assert exc.value.reason == "writable"


def test_the_hashed_mode_and_size_come_from_the_opened_descriptor(tmp_path: Path) -> None:
    base = tmp_path / "b"
    base.mkdir(mode=0o755)
    (base / "f").write_bytes(b"x")
    (base / "f").chmod(0o644)
    info = os.stat(base / "f", follow_symlinks=False)
    (base / "f").chmod(0o600)  # stricter after the stat: still fine, and what is hashed
    (base / "f").write_bytes(b"xyz")
    fd = _open_dir(base)
    walk = ph._Walk()
    try:
        walk.file("f", fd, info, b"f")
    finally:
        os.close(fd)
    expected = hashlib.sha256(_frame(b"f", 0o600, b"f", b"xyz")).hexdigest()
    assert walk.hash.hexdigest() == expected


def test_an_entry_added_while_a_directory_is_walked_is_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _tree(tmp_path)
    genuine = ph._Walk.file

    def adds_a_sibling(
        self: object, name: str, dir_fd: int, info: os.stat_result, rel: bytes = b""
    ) -> None:
        genuine(self, name, dir_fd, info, rel)  # type: ignore[arg-type]
        (root / "late").write_bytes(b"")

    monkeypatch.setattr(ph._Walk, "file", adds_a_sibling)
    with pytest.raises(PluginTreeError) as exc:
        plugin_dirs_sha256([str(root)])
    assert exc.value.reason == "changed"


def test_an_entry_removed_while_a_directory_is_walked_is_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _tree(tmp_path)
    (root / "z").write_bytes(b"")
    (root / "z").chmod(0o644)
    genuine = ph._Walk.file

    def removes_a_later_one(
        self: object, name: str, dir_fd: int, info: os.stat_result, rel: bytes = b""
    ) -> None:
        genuine(self, name, dir_fd, info, rel)  # type: ignore[arg-type]
        (root / "z").unlink(missing_ok=True)

    monkeypatch.setattr(ph._Walk, "file", removes_a_later_one)
    with pytest.raises(PluginTreeError) as exc:
        plugin_dirs_sha256([str(root)])
    assert exc.value.reason in ("changed",)
