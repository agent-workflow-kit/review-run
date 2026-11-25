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
