"""Semantic config checks: names, guidelines file, plugin directories, read-only binds."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from review_run.config import load_config, read_guidelines_file
from review_run.config_semantic import env_name_refusal
from review_run.errors import ReviewRunError
from tests.config_helpers import load, write_main

REPO = "github.com/example-org/example-repo"


def keys_of(text: str) -> list[str]:
    write_main(text)
    with pytest.raises(ReviewRunError) as info:
        load_config(None, {})
    assert info.value.code == "config_invalid"
    assert info.value.details is not None
    return [info.value.details["key"]]


def code_cfg(extra: str) -> str:
    return f"repos:\n  {REPO}:\n    run_pr_code:\n      command: [x]\n{extra}"


# -- env names ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "DATABASE_URL", "PGPASSFILE", "HTTPS_PROXY", "https_proxy", "GITHUB_TOKEN", "AWS_SECRET",
        "API_KEY", "MYPASS", "CREDENTIALS", "AUTH_X", "COOKIE", "SESSION_ID", "SENTRY_DSN",
        "X_URL", "x_url", "GH_PAT", "NETRC", "netrc", "ANTHROPIC_API_KEY", "ANTHROPIC_X",
        "CLAUDE_CODE_X", "CLAUDE_", "claude_x", "Anthropic_Y",
    ],
)  # fmt: skip
def test_credential_names_are_refused(name: str) -> None:
    assert env_name_refusal(name) is not None


@pytest.mark.parametrize(
    "name", ["PATH", "LANG", "HOME", "CI", "NODE_ENV", "GOFLAGS", "URL", "XPAT", "CLAUD", "_X"]
)
def test_ordinary_names_pass(name: str) -> None:
    assert env_name_refusal(name) is None


@pytest.mark.parametrize("name", ["", "1X", "A-B", "A B", "PATH\n", "A=B", "é", "PÅTH", "A\x00"])
def test_names_outside_the_grammar_are_refused(name: str) -> None:
    assert env_name_refusal(name) is not None


@pytest.mark.parametrize(
    "name", ["DATABASE_URL", "PGPASSFILE", "HTTPS_PROXY", "ANTHROPIC_X", "CLAUDE_X"]
)
def test_env_allow_and_env_allowlist_refuse_credential_names(name: str) -> None:
    assert keys_of(f"env_allowlist: [PATH, {name}]\n") == ["env_allowlist[1]"]
    assert keys_of(code_cfg(f"      env_allow: [OK, {name}]\n")) == [
        f"repos.{REPO}.run_pr_code.env_allow[1]"
    ]
    assert keys_of(f"repos:\n  {REPO}:\n    env_allowlist: [{name}]\n") == [
        f"repos.{REPO}.env_allowlist[0]"
    ]


def test_refusal_message_never_echoes_the_name() -> None:
    write_main("env_allowlist: [MY_SECRET_NAME_XYZ]\n")
    with pytest.raises(ReviewRunError) as info:
        load_config(None, {})
    assert "MY_SECRET_NAME_XYZ" not in json.dumps(info.value.envelope())


def test_allowed_names_load() -> None:
    cfg = load(code_cfg("      env_allow: [CI, LANG]\n") + "env_allowlist: []\n")
    assert cfg.resolve(REPO).values["run_pr_code"]["env_allow"] == ["CI", "LANG"]
    assert cfg.resolve().values["env_allowlist"] == []


# -- guidelines_file ---------------------------------------------------------------------------


def make_file(name: str, data: bytes = b"be kind\n", mode: int = 0o600) -> Path:
    path = Path.home() / name
    path.write_bytes(data)
    path.chmod(mode)
    return path


def test_guidelines_file_is_expanded_and_read() -> None:
    path = make_file("g.md")
    cfg = load("guidelines_file: ~/g.md\n")
    assert cfg.resolve().values["guidelines_file"] == str(path)
    assert read_guidelines_file(str(path)) == "be kind\n"


def test_guidelines_file_through_the_environment() -> None:
    path = make_file("g.md")
    cfg = load(None, {"REVIEW_RUN_GUIDELINES_FILE": str(path)})
    assert cfg.resolve().values["guidelines_file"] == str(path)


def test_guidelines_file_per_repo() -> None:
    path = make_file("g.md")
    cfg = load(f"repos:\n  {REPO}:\n    guidelines_file: {path}\n")
    assert cfg.resolve(REPO).values["guidelines_file"] == str(path)
    assert cfg.resolve().values["guidelines_file"] is None


def test_guidelines_file_refusals() -> None:
    cases = {
        "missing": None,
        "empty-dir": "dir",
        "big": b"x" * (64 * 1024 + 1),
        "latin1": b"caf\xe9",
        "nul": b"a\x00b",
    }
    for name, content in cases.items():
        if content is None:
            pass
        elif content == "dir":
            (Path.home() / name).mkdir()
        else:
            make_file(name, content)
        assert keys_of(f"guidelines_file: ~/{name}\n") == ["guidelines_file"], name


def test_guidelines_file_at_the_size_limit_is_fine() -> None:
    make_file("g.md", b"x" * (64 * 1024))
    load("guidelines_file: ~/g.md\n")


def test_guidelines_file_symlink_fifo_and_foreign_modes_are_refused() -> None:
    real = make_file("real.md")
    (Path.home() / "link.md").symlink_to(real)
    assert keys_of("guidelines_file: ~/link.md\n") == ["guidelines_file"]
    os.mkfifo(Path.home() / "fifo.md")
    assert keys_of("guidelines_file: ~/fifo.md\n") == ["guidelines_file"]
    make_file("w.md", mode=0o666)
    assert keys_of("guidelines_file: ~/w.md\n") == ["guidelines_file"]


def test_guidelines_file_with_dotdot_or_other_user_tilde_is_refused() -> None:
    make_file("g.md")
    assert keys_of("guidelines_file: ~/sub/../g.md\n") == ["guidelines_file"]
    assert keys_of("guidelines_file: ~root/g.md\n") == ["guidelines_file"]


def test_guidelines_error_never_echoes_the_path() -> None:
    write_main("guidelines_file: ~/very-private-name.md\n")
    with pytest.raises(ReviewRunError) as info:
        load_config(None, {})
    assert "very-private-name" not in json.dumps(info.value.envelope())


# -- plugin_dirs -------------------------------------------------------------------------------


def make_plugin(
    name: str, plugin: str | None = None, mode: int = 0o755, placeholders: bool = True
) -> Path:
    root = Path.home() / "plugins" / name
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": plugin or name}), encoding="utf-8"
    )
    if placeholders:
        (root / ".claude").mkdir()
        (root / ".mcp.json").write_text("{}", encoding="utf-8")
        (root / "CLAUDE.md").write_text("", encoding="utf-8")
        (root / "CLAUDE.local.md").write_text("", encoding="utf-8")
    root.chmod(mode)
    return root


def test_plugin_dirs_are_stored_by_realpath() -> None:
    root = make_plugin("alpha")
    link = Path.home() / "alpha-link"
    link.symlink_to(root)
    cfg = load(f"plugin_dirs: [{link}]\n")
    assert cfg.resolve().values["plugin_dirs"] == [str(root.resolve())]


def test_plugin_dirs_per_repo() -> None:
    root = make_plugin("alpha")
    cfg = load(f"repos:\n  {REPO}:\n    plugin_dirs: [{root}]\n")
    assert cfg.resolve(REPO).values["plugin_dirs"] == [str(root.resolve())]
    assert cfg.resolve().values["plugin_dirs"] == []


def test_two_plugins_with_one_name_are_refused() -> None:
    a = make_plugin("a", plugin="same")
    b = make_plugin("b", plugin="same")
    assert keys_of(f"plugin_dirs: [{a}, {b}]\n") == ["plugin_dirs[1]"]


def test_same_directory_twice_via_a_link_is_a_duplicate_name() -> None:
    a = make_plugin("a")
    link = Path.home() / "a-link"
    link.symlink_to(a)
    assert keys_of(f"plugin_dirs: [{a}, {link}]\n") == ["plugin_dirs[1]"]


def test_a_plugin_named_review_run_is_refused() -> None:
    a = make_plugin("a", plugin="review-run")
    assert keys_of(f"plugin_dirs: [{a}]\n") == ["plugin_dirs[0]"]
    assert keys_of(f"repos:\n  {REPO}:\n    plugin_dirs: [{a}]\n") == [
        f"repos.{REPO}.plugin_dirs[0]"
    ]


def test_group_or_world_writable_plugin_dirs_are_refused() -> None:
    for mode in (0o775, 0o757, 0o777):
        a = make_plugin(f"w{mode:o}", mode=mode)
        assert keys_of(f"plugin_dirs: [{a}]\n") == ["plugin_dirs[0]"]


def test_plugin_dir_must_be_a_directory_with_placeholders_and_a_name() -> None:
    assert keys_of(f"plugin_dirs: [{Path.home() / 'nope'}]\n") == ["plugin_dirs[0]"]
    f = Path.home() / "file"
    f.write_text("x", encoding="utf-8")
    assert keys_of(f"plugin_dirs: [{f}]\n") == ["plugin_dirs[0]"]
    for missing in (".claude", ".mcp.json", "CLAUDE.md", "CLAUDE.local.md"):
        root = make_plugin("m" + missing.strip("."))
        target = root / missing
        if target.is_dir():
            target.rmdir()
        else:
            target.unlink()
        assert keys_of(f"plugin_dirs: [{root}]\n") == ["plugin_dirs[0]"], missing
    noname = make_plugin("noname")
    (noname / ".claude-plugin" / "plugin.json").write_text("{}", encoding="utf-8")
    assert keys_of(f"plugin_dirs: [{noname}]\n") == ["plugin_dirs[0]"]
    bad = make_plugin("badjson")
    (bad / ".claude-plugin" / "plugin.json").write_text("{", encoding="utf-8")
    assert keys_of(f"plugin_dirs: [{bad}]\n") == ["plugin_dirs[0]"]


def test_plugin_errors_never_echo_the_path() -> None:
    write_main(f"plugin_dirs: [{Path.home() / 'private-thing'}]\n")
    with pytest.raises(ReviewRunError) as info:
        load_config(None, {})
    assert "private-thing" not in json.dumps(info.value.envelope())


# -- ro_binds ----------------------------------------------------------------------------------


def binds(*paths: Path | str) -> str:
    return code_cfg("      ro_binds: [" + ", ".join(f"'{p}'" for p in paths) + "]\n")


def ro_key(i: int = 0) -> str:
    return f"repos.{REPO}.run_pr_code.ro_binds[{i}]"


def test_ro_binds_store_the_realpath() -> None:
    real = Path.home().parent / "tools"
    real.mkdir()
    link = Path.home().parent / "tools-link"
    link.symlink_to(real)
    cfg = load(binds(link))
    assert cfg.resolve(REPO).values["run_pr_code"]["ro_binds"] == [str(real.resolve())]


def test_ro_binds_duplicates_after_realpath_collapse() -> None:
    real = Path.home().parent / "tools"
    real.mkdir()
    link = Path.home().parent / "tools-link"
    link.symlink_to(real)
    cfg = load(binds(real, link))
    assert cfg.resolve(REPO).values["run_pr_code"]["ro_binds"] == [str(real.resolve())]


def test_ro_binds_narrow_toolchain_directory_is_allowed() -> None:
    registry = Path.home() / ".cargo" / "registry"
    registry.mkdir(parents=True)
    cfg = load(binds(registry))
    assert cfg.resolve(REPO).values["run_pr_code"]["ro_binds"] == [str(registry.resolve())]


def test_ro_binds_cargo_directory_is_refused_even_without_the_file() -> None:
    (Path.home() / ".cargo").mkdir()
    assert keys_of(binds(Path.home() / ".cargo")) == [ro_key()]


def test_ro_binds_cargo_with_credentials_is_refused() -> None:
    cargo = Path.home() / ".cargo"
    cargo.mkdir()
    (cargo / "credentials.toml").write_text("x", encoding="utf-8")
    assert keys_of(binds(cargo)) == [ro_key()]


@pytest.mark.parametrize(
    "sub",
    [
        ".ssh",
        ".aws",
        ".gnupg",
        ".kube",
        ".docker",
        ".claude",
        ".config",
        ".local/state",
        ".local/share/keyrings",
    ],
)
def test_ro_binds_credential_locations_and_their_children_are_refused(sub: str) -> None:
    path = Path.home() / sub
    (path / "child").mkdir(parents=True)
    assert keys_of(binds(path)) == [ro_key()]
    assert keys_of(binds(path / "child")) == [ro_key()]


def test_ro_binds_through_a_symlink_to_a_credential_directory_are_refused() -> None:
    ssh = Path.home() / "dotfiles" / "ssh"
    ssh.mkdir(parents=True)
    (Path.home() / ".ssh").symlink_to(ssh)
    assert keys_of(binds(ssh)) == [ro_key()]  # compared by the target
    link = Path.home().parent / "innocent"
    link.symlink_to(Path.home() / ".ssh")
    assert keys_of(binds(link)) == [ro_key()]


def test_ro_binds_textual_credential_components_are_refused() -> None:
    odd = Path.home().parent / "x" / ".npmrc" / "d"
    odd.mkdir(parents=True)
    assert keys_of(binds(odd)) == [ro_key()]
    odd2 = Path.home().parent / "x" / "credentials.toml"
    odd2.mkdir()
    assert keys_of(binds(odd2)) == [ro_key()]


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/mnt",
        "/mnt/c",
        "/mnt/c/Users",
        "/media",
        "/root",
        "/proc",
        "/proc/self",
        "/sys",
        "/dev",
        "/run",
        "/var/run",
        "/home",
    ],
)
def test_ro_binds_system_locations_are_refused(path: str) -> None:
    if not Path(path).exists():
        pytest.skip(f"{path} does not exist here")
    assert keys_of(binds(path)) == [ro_key()]


def test_ro_binds_home_and_ancestors_are_refused() -> None:
    home = Path.home()
    assert keys_of(binds(home)) == [ro_key()]
    assert keys_of(binds(home.parent)) == [ro_key()]


def test_ro_binds_ancestor_of_a_state_directory_is_refused() -> None:
    state = Path(os.environ["XDG_STATE_HOME"]) / "review-run"
    state.mkdir(parents=True)
    assert keys_of(binds(state)) == [ro_key()]
    assert keys_of(binds(state.parent)) == [ro_key()]
    sibling = Path(os.environ["XDG_STATE_HOME"]) / "forgectl"
    sibling.mkdir()
    assert keys_of(binds(sibling)) == [ro_key()]


def test_ro_binds_must_be_existing_absolute_directories() -> None:
    assert keys_of(binds(Path.home().parent / "missing")) == [ro_key()]
    f = Path.home().parent / "afile"
    f.write_text("x", encoding="utf-8")
    assert keys_of(binds(f)) == [ro_key()]
    assert keys_of(code_cfg("      ro_binds: [relative/dir]\n")) == [ro_key()]


def test_ro_binds_errors_name_only_the_index() -> None:
    d = Path.home().parent / "ok"
    d.mkdir()
    assert keys_of(binds(d, Path.home() / ".ssh")) == [ro_key(1)]
    write_main(binds(Path.home() / ".ssh-private-thing"))
    with pytest.raises(ReviewRunError) as info:
        load_config(None, {})
    assert "private-thing" not in json.dumps(info.value.envelope())
