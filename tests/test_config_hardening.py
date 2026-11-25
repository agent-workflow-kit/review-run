"""Config hardening: private groups, partial drop-ins, integers, control characters, key echo,
permission profile, API misuse and the environment mapping."""

from __future__ import annotations

import grp
import json
import os
from pathlib import Path

import pytest

from review_run.config import check_config, load_config
from review_run.config_semantic import read_guidelines_file
from review_run.config_yaml import private_group
from review_run.errors import ReviewRunError
from tests.config_helpers import (
    base_env,
    config_dir,
    foreign_group,
    load,
    private_primary_group,
    shared_primary_group,
    write_dropin,
    write_main,
)

REPO = "github.com/example-org/example-repo"


def fail(text: str, env: dict[str, str] | None = None) -> ReviewRunError:
    write_main(text).chmod(0o600)
    with pytest.raises(ReviewRunError) as info:
        load_config(None, {**base_env(), **(env or {})})
    assert info.value.code == "config_invalid"
    return info.value


# -- group-writable files (items 1, 2) --------------------------------------------------------


def test_group_writable_file_in_a_foreign_group_is_refused() -> None:
    group = foreign_group()
    if group is None:
        pytest.skip("the user has no supplementary group")
    path = write_main("model: a\n")
    os.chown(path, -1, group)
    path.chmod(0o664)
    with pytest.raises(ReviewRunError):
        load_config(None, base_env())


def test_group_writable_directory_in_a_foreign_group_is_refused() -> None:
    group = foreign_group()
    if group is None:
        pytest.skip("the user has no supplementary group")
    write_main("model: a\n")
    directory = config_dir() / "config.d"
    directory.mkdir()
    os.chown(directory, -1, group)
    directory.chmod(0o775)
    with pytest.raises(ReviewRunError):
        load_config(None, base_env())


@pytest.mark.parametrize("by", ["member", "account"])
def test_group_writable_file_in_a_shared_primary_group_is_refused(
    monkeypatch: pytest.MonkeyPatch, by: str
) -> None:
    path = write_main("model: a\n")
    path.chmod(0o664)
    shared_primary_group(monkeypatch, by=by)
    with pytest.raises(ReviewRunError) as info:
        load_config(None, base_env())
    assert info.value.code == "config_invalid"


def test_group_writable_drop_in_and_directory_in_a_shared_group_are_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_main("model: a\n")
    path = write_dropin("a.yaml", "nits: true\n")
    path.chmod(0o664)
    private_primary_group(monkeypatch)
    assert load_config(None, base_env()).resolve().values["nits"] is True
    shared_primary_group(monkeypatch, by="member")
    with pytest.raises(ReviewRunError):
        load_config(None, base_env())
    path.chmod(0o644)
    (config_dir() / "config.d").chmod(0o775)
    with pytest.raises(ReviewRunError):
        load_config(None, base_env())


def test_group_writable_file_in_a_private_primary_group_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = write_main("model: a\n")
    path.chmod(0o664)
    (config_dir() / "config.d").mkdir()
    (config_dir() / "config.d").chmod(0o775)
    private_primary_group(monkeypatch)
    assert load_config(None, base_env()).resolve().values["model"] == "a"


def test_guidelines_file_follows_the_same_group_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    path = Path.home() / "g.md"
    path.write_text("x\n", encoding="utf-8")
    path.chmod(0o664)
    private_primary_group(monkeypatch)
    assert read_guidelines_file(str(path)) == "x\n"
    shared_primary_group(monkeypatch, by="account")
    with pytest.raises(ValueError):
        read_guidelines_file(str(path))


def test_world_writable_is_refused_even_in_a_private_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_main("model: a\n").chmod(0o646)
    private_primary_group(monkeypatch)
    with pytest.raises(ReviewRunError):
        load_config(None, base_env())


@pytest.mark.parametrize("error", [KeyError("gid"), OSError("nss down"), RuntimeError("boom")])
def test_a_failing_group_lookup_is_not_private(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    private_primary_group(monkeypatch)

    def broken(gid: int) -> None:
        raise error

    monkeypatch.setattr(grp, "getgrgid", broken)
    assert private_group(os.getegid()) is False


# -- partial drop-ins (item 3) ----------------------------------------------------------------


def test_a_drop_in_may_set_part_of_an_entry() -> None:
    write_main(f"repos:\n  {REPO}:\n    run_pr_code:\n      command: [make]\n")
    write_dropin("a.yaml", f"repos:\n  {REPO}:\n    run_pr_code:\n      timeout: 10s\n")
    code = load_config(None, base_env()).resolve(REPO).values["run_pr_code"]
    assert code["command"] == ["make"]
    assert code["timeout"] == "10s"


def test_a_partial_entry_without_a_command_in_the_merged_config_is_refused() -> None:
    write_dropin("a.yaml", f"repos:\n  {REPO}:\n    run_pr_code:\n      timeout: 10s\n")
    with pytest.raises(ReviewRunError) as info:
        load_config(None, base_env())
    assert info.value.details == {"key": f"repos.{REPO}.run_pr_code.command"}


def test_a_drop_in_is_still_checked_for_other_problems() -> None:
    write_dropin("a.yaml", f"repos:\n  {REPO}:\n    run_pr_code:\n      timeout: x\n")
    with pytest.raises(ReviewRunError):
        load_config(None, base_env())


# -- integers (item 4) ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("max_turns: 1e3\n", "max_turns"),
        ("max_turns: 5.0\n", "max_turns"),
        ("limits:\n  max_files: 2000.0\n", "limits.max_files"),
        ("max_subagent_depth: 2.0\n", "max_subagent_depth"),
        (f"repos:\n  {REPO}:\n    run_pr_code:\n      command: [x]\n      max_output_bytes: 1e6\n",
         f"repos.{REPO}.run_pr_code.max_output_bytes"),
    ],
)  # fmt: skip
def test_floats_are_refused_for_integer_keys(text: str, key: str) -> None:
    assert fail(text).details == {"key": key}


def test_a_float_flag_for_an_integer_key_is_a_usage_error() -> None:
    with pytest.raises(ReviewRunError) as info:
        load().resolve(None, {"max_turns": 3.0})
    assert info.value.code == "usage"


# -- control characters (item 5) ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ('model: "a\\0b"\n', "model"),
        ('model: "a\\nb"\n', "model"),
        ("model: -x\n", "model"),
        ('hooks:\n  pre-run:\n    - cmd: "a\\0b"\n', "hooks.pre-run[0].cmd"),
        ('hooks:\n  post-run:\n    - cmd: "a\\x01b"\n', "hooks.post-run[0].cmd"),
        (
            f'repos:\n  {REPO}:\n    run_pr_code:\n      command: [x, "a\\0b"]\n',
            f"repos.{REPO}.run_pr_code.command[1]",
        ),
        (
            f'repos:\n  {REPO}:\n    run_pr_code:\n      command: ["a\\nb"]\n',
            f"repos.{REPO}.run_pr_code.command[0]",
        ),
    ],
)
def test_control_characters_and_a_leading_dash_are_refused(text: str, key: str) -> None:
    err = fail(text)
    assert err.details == {"key": key}
    assert "\x00" not in err.message


def test_a_multi_line_hook_command_is_fine() -> None:
    cfg = load('hooks:\n  pre-run:\n    - cmd: "a\\n  b\\tc"\n')
    assert cfg.resolve().values["hooks"]["pre-run"][0]["cmd"] == "a\n  b\tc"


# -- unknown keys (item 6) --------------------------------------------------------------------


def test_an_unknown_key_that_looks_like_a_name_is_echoed() -> None:
    write_main("modle: x\n")
    issues = check_config(None, base_env()).issues
    assert [(i.key, i.problem) for i in issues] == [("modle", "is not a known key")]


@pytest.mark.parametrize("name", ["Sk-Live-ABCDEF123", "has space", "a" * 60, "ключ"])
def test_an_unknown_key_that_does_not_look_like_a_name_is_not_echoed(name: str) -> None:
    write_main(f'"{name}": x\nlimits:\n  "{name}": 1\n')
    issues = check_config(None, base_env()).issues
    assert {(i.key, i.problem) for i in issues} == {
        ("(top level)", "contains an unknown key"),
        ("limits", "contains an unknown key"),
    }
    assert name not in json.dumps([i.__dict__ for i in issues])


# -- permission_profile (item 7) --------------------------------------------------------------


def profile(mode: int = 0o600, data: bytes = b"{}") -> Path:
    path = Path.home() / "profile.json"
    path.write_bytes(data)
    path.chmod(mode)
    return path


def test_permission_profile_is_checked_at_load() -> None:
    path = profile()
    assert load(f"permission_profile: {path}\n").resolve().values["permission_profile"] == str(path)


def test_permission_profile_through_the_environment_and_per_repo() -> None:
    path = profile()
    assert load(None, {"REVIEW_RUN_PERMISSION_PROFILE": str(path)}).resolve().values[
        "permission_profile"
    ] == str(path)
    assert fail("", {"REVIEW_RUN_PERMISSION_PROFILE": "/nonexistent/p.json"}).details == {
        "key": "REVIEW_RUN_PERMISSION_PROFILE"
    }
    assert fail(f"repos:\n  {REPO}:\n    permission_profile: /nonexistent/p.json\n").details == {
        "key": f"repos.{REPO}.permission_profile"
    }


def test_permission_profile_refusals() -> None:
    assert fail("permission_profile: /nonexistent/p.json\n").details == {
        "key": "permission_profile"
    }
    real = profile()
    link = Path.home() / "link.json"
    link.symlink_to(real)
    assert fail(f"permission_profile: {link}\n").details == {"key": "permission_profile"}
    real.chmod(0o666)
    assert fail(f"permission_profile: {real}\n").details == {"key": "permission_profile"}
    real.chmod(0o600)
    real.write_bytes(b"x" * (256 * 1024 + 1))
    assert fail(f"permission_profile: {real}\n").details == {"key": "permission_profile"}
    assert fail(f"permission_profile: {Path.home()}\n").details == {"key": "permission_profile"}


def test_permission_profile_group_writable_in_a_shared_group_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = profile(0o664)
    private_primary_group(monkeypatch)
    load(f"permission_profile: {path}\n")
    shared_primary_group(monkeypatch, by="member")
    assert fail(f"permission_profile: {path}\n").details == {"key": "permission_profile"}


# -- plugin names (items 8, 9) ------------------------------------------------------------------


def plugin(name_json: str) -> Path:
    root = Path.home() / "plug"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(name_json, encoding="utf-8")
    (root / ".claude").mkdir()
    for name in (".mcp.json", "CLAUDE.md", "CLAUDE.local.md"):
        (root / name).write_text("", encoding="utf-8")
    root.chmod(0o755)
    return root


def test_the_reserved_plugin_name_is_compared_casefolded() -> None:
    root = plugin('{"name": "Review-Run"}')
    assert fail(f"plugin_dirs: [{root}]\n").details == {"key": "plugin_dirs[0]"}


def test_a_deeply_nested_plugin_json_is_a_config_error_not_a_crash() -> None:
    root = plugin("[" * 100000)
    assert fail(f"plugin_dirs: [{root}]\n").details == {"key": "plugin_dirs[0]"}


# -- public API misuse (item 10) --------------------------------------------------------------


@pytest.mark.parametrize(
    ("repo", "flags"),
    [("abc", "abc"), (None, ["nits"]), (5, None), ("", None), (None, {"nope": 1}), (None, {3: 1})],
)
def test_resolve_misuse_is_a_usage_error(repo: object, flags: object) -> None:
    with pytest.raises(ReviewRunError) as info:
        load().resolve(repo, flags)  # type: ignore[arg-type]
    assert info.value.code == "usage"


# -- the environment mapping (item 11) ----------------------------------------------------------


def test_check_config_reads_the_config_home_from_the_given_mapping() -> None:
    other = Path.home() / "other-config"
    (other / "review-run").mkdir(parents=True)
    (other / "review-run" / "config.yaml").write_text("model: elsewhere\n", encoding="utf-8")
    result = check_config(None, {**base_env(), "XDG_CONFIG_HOME": str(other)})
    assert result.config is not None
    assert result.config.resolve().values["model"] == "elsewhere"


def test_check_config_reads_home_from_the_given_mapping() -> None:
    other = Path.home() / "other-home"
    (other / ".config" / "review-run").mkdir(parents=True)
    (other / ".config" / "review-run" / "config.yaml").write_text("model: h\n", encoding="utf-8")
    result = check_config(None, {"HOME": str(other)})
    assert result.config is not None
    assert result.config.resolve().values["model"] == "h"


def test_ro_binds_use_the_state_home_of_the_given_mapping() -> None:
    state = Path.home() / "state2"
    (state / "tools").mkdir(parents=True)
    text = f"repos:\n  {REPO}:\n    run_pr_code:\n      command: [x]\n      ro_binds: [{state}]\n"
    write_main(text)
    assert check_config(None, base_env()).issues == []
    issues = check_config(None, {**base_env(), "XDG_STATE_HOME": str(state)}).issues
    assert [i.key for i in issues] == [f"repos.{REPO}.run_pr_code.ro_binds[0]"]


def test_guidelines_tilde_uses_the_home_of_the_given_mapping() -> None:
    other = Path.home() / "other-home"
    other.mkdir()
    (other / "g.md").write_text("x\n", encoding="utf-8")
    (other / "g.md").chmod(0o600)
    write_main("guidelines_file: ~/g.md\n")
    result = check_config(None, {**base_env(), "HOME": str(other)})
    assert result.config is not None
    assert result.config.resolve().values["guidelines_file"] == str(other / "g.md")
