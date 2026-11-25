"""Run settings, the effective permission profile and the launch flags (issue #16)."""

# ruff: noqa: E501

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from pathlib import Path
from typing import Any

import pytest

from review_run import settings as rs
from review_run.config import Config, load_config
from review_run.errors import ReviewRunError
from review_run.plugin_hash import plugin_dirs_sha256

REPO_PLUGIN = Path(__file__).resolve().parent.parent / "plugin"
REPO = "github.com/o/r"
PR = "github.com/o/r#1"


def write_yaml(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return path


def make_config(tmp_path: Path, yaml_text: str = "", env: dict[str, str] | None = None) -> Config:
    path = write_yaml(tmp_path / "config.yaml", yaml_text or "skill: review-run:review-pr\n")
    return load_config(path, {**os.environ, **(env or {})})


@pytest.fixture
def plugin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A private copy of the bundled plugin with the modes a user-owned install has."""
    copy = tmp_path / "bundled-plugin"
    shutil.copytree(REPO_PLUGIN, copy)
    for item in [copy, *copy.rglob("*")]:
        item.chmod(0o755 if item.is_dir() else 0o644)
    monkeypatch.setattr(rs, "bundled_plugin_dir", lambda: copy)
    return copy


def make_settings(
    config: Config, plugin: Path, flags: dict[str, Any] | None = None
) -> tuple[rs.RunSettings, rs.ProfilePlan]:
    resolved = config.resolve(REPO, flags)
    profile, _ = rs.effective_profile(resolved.values)
    dirs = [str(plugin.resolve())]
    settings = rs.build_settings(
        resolved,
        plugin_dirs=dirs,
        plugin_dirs_hash=plugin_dirs_sha256(dirs),
        profile=profile,
    )
    return settings, profile


# -- the bundled profile -----------------------------------------------------------------------

DENIES = [
    "Agent", "Task", "WebFetch", "WebSearch", "mcp__*", "SendMessage", "ListAgents", "TaskStop",
    "Workflow", "ReportFindings", "CronCreate", "CronDelete", "CronList", "ScheduleWakeup",
    "EnterWorktree", "ExitWorktree", "RemoteTrigger", "PushNotification", "PublishPlugin",
    "ShareOnboardingGuide", "DesignSync", "Monitor", "Bash(gh:*)", "Bash(glab:*)",
    "Bash(agent-spawn:*)", "Bash(memoctl add:*)",
]  # fmt: skip


def test_the_bundled_profile_is_the_interface_list_exactly() -> None:
    doc = json.loads((REPO_PLUGIN / "permission_profile.json").read_text(encoding="utf-8"))
    assert doc == {
        "permissions": {"allow": ["Skill"], "deny": DENIES},
        "sandbox": {"network": {"allowedDomains": []}},
    }


def test_the_bundled_profile_does_not_deny_the_write_tools_by_bare_name() -> None:
    deny = json.loads((REPO_PLUGIN / "permission_profile.json").read_text("utf-8"))
    names = deny["permissions"]["deny"]
    assert not {"Edit", "Write", "NotebookEdit"} & set(names)


def test_the_bundled_plugin_has_the_placeholders() -> None:
    for name in (".claude", ".mcp.json", "CLAUDE.md", "CLAUDE.local.md"):
        assert os.path.lexists(REPO_PLUGIN / name), name


def test_the_variant_drops_exactly_agent_and_task() -> None:
    variant = json.loads(rs.variant_profile(rs.bundled_profile_bytes()))
    assert variant["permissions"]["deny"] == [d for d in DENIES if d not in ("Agent", "Task")]
    assert variant["permissions"]["allow"] == ["Skill"]
    assert variant["sandbox"] == {"network": {"allowedDomains": []}}


def test_bundled_plugin_dir_exists_and_has_the_profile() -> None:
    assert (rs.bundled_plugin_dir() / "permission_profile.json").is_file()
    assert (rs.bundled_plugin_dir() / ".claude-plugin" / "plugin.json").is_file()


# -- the effective profile ---------------------------------------------------------------------


def test_default_profile_is_the_bundled_file_bytes(tmp_path: Path, plugin: Path) -> None:
    config = make_config(tmp_path)
    profile, warnings = rs.effective_profile(config.resolve(REPO).values)
    assert (profile.kind, warnings) == ("bundled", ())
    assert profile.data == (plugin / "permission_profile.json").read_bytes()
    assert profile.sha256 == hashlib.sha256(profile.data).hexdigest()


def test_subagents_selects_the_variant(tmp_path: Path, plugin: Path) -> None:
    config = make_config(tmp_path, "subagents: true\n")
    profile, warnings = rs.effective_profile(config.resolve(REPO).values)
    doc = json.loads(profile.data)
    assert profile.kind == "variant" and warnings == ()
    assert "Agent" not in doc["permissions"]["deny"] and "Task" not in doc["permissions"]["deny"]
    assert "WebFetch" in doc["permissions"]["deny"]


def test_a_custom_profile_is_copied_unchanged_even_with_subagents(
    tmp_path: Path, plugin: Path
) -> None:
    custom = tmp_path / "profile.json"
    raw = b'{"permissions": {"deny": ["Agent"]},   "x": 1}\n'
    custom.write_bytes(raw)
    custom.chmod(0o600)
    config = make_config(tmp_path, f"permission_profile: {custom}\nsubagents: true\n")
    profile, warnings = rs.effective_profile(config.resolve(REPO).values)
    assert profile.kind == "custom" and profile.data == raw
    assert warnings == (rs.WARNING_CUSTOM_PROFILE_SUBAGENTS,)


def test_custom_profile_without_subagents_has_no_warning(tmp_path: Path, plugin: Path) -> None:
    custom = tmp_path / "profile.json"
    custom.write_text("{}", encoding="utf-8")
    custom.chmod(0o600)
    config = make_config(tmp_path, f"permission_profile: {custom}\n")
    assert rs.effective_profile(config.resolve(REPO).values)[1] == ()


@pytest.mark.parametrize(
    "content",
    [b"[]", b'"x"', b"not json", b'{"a": 1, "a": 2}', b"NaN", b'{"a": NaN}', b"\xff\xfe", b""],
)
def test_a_custom_profile_must_be_a_strict_json_object(tmp_path: Path, content: bytes) -> None:
    custom = tmp_path / "p.json"
    custom.write_bytes(content)
    custom.chmod(0o600)
    with pytest.raises(ReviewRunError) as exc:
        rs.read_profile_file(str(custom))
    assert exc.value.code == "config_invalid" and exc.value.details == {"key": "permission_profile"}


def test_a_custom_profile_that_became_a_symlink_or_world_writable_is_refused(
    tmp_path: Path,
) -> None:
    real = tmp_path / "real.json"
    real.write_text("{}", encoding="utf-8")
    real.chmod(0o600)
    link = tmp_path / "link.json"
    link.symlink_to(real)
    with pytest.raises(ReviewRunError):
        rs.read_profile_file(str(link))
    real.chmod(0o602)
    with pytest.raises(ReviewRunError):
        rs.read_profile_file(str(real))


def test_an_oversize_custom_profile_is_refused(tmp_path: Path) -> None:
    big = tmp_path / "big.json"
    big.write_text('{"a": "' + "x" * (rs.MAX_PROFILE_BYTES + 1) + '"}', encoding="utf-8")
    big.chmod(0o600)
    with pytest.raises(ReviewRunError):
        rs.read_profile_file(str(big))


# -- settings.json -----------------------------------------------------------------------------


def test_settings_hold_every_key_but_the_process_keys_with_origins(
    tmp_path: Path, plugin: Path
) -> None:
    config = make_config(tmp_path, "nits: true\nlimits:\n  max_files: 10\n")
    settings, _ = make_settings(config, plugin, {"model": "m1", "timeout": "30m"})
    keys = set(settings.entries)
    assert not {k for k in keys if k.split(".")[0] in rs.EXCLUDED}
    assert {"skill", "mode", "sandbox", "setting_sources", "env_allowlist", "model", "timeout",
            "max_turns", "subagents", "max_subagent_depth", "nits", "guidelines_file",
            "permission_profile", "plugin_dirs", "run_pr_code", "limits.max_file_bytes",
            "limits.max_total_bytes", "limits.max_files", "limits.hard_ceiling_bytes",
            "limits.max_artifact_bytes", "limits.max_answer_bytes"} <= keys  # fmt: skip
    assert settings.origin("model") == "flag" and settings.value("model") == "m1"
    assert settings.origin("timeout") == "flag" and settings.value("timeout") == "30m"
    assert settings.origin("nits") == "config" and settings.value("nits") is True
    assert settings.origin("limits.max_files") == "config"
    assert settings.value("limits.max_files") == 10
    assert settings.origin("sandbox") == "default"


def test_the_repo_origin_is_recorded(tmp_path: Path, plugin: Path) -> None:
    config = make_config(tmp_path, f"repos:\n  {REPO}:\n    nits: true\n")
    settings, _ = make_settings(config, plugin)
    assert settings.origin("nits") == "repo"


def test_the_env_origin_is_recorded(tmp_path: Path, plugin: Path) -> None:
    config = make_config(tmp_path, env={"REVIEW_RUN_SANDBOX": "preferred"})
    settings, _ = make_settings(config, plugin)
    assert (settings.value("sandbox"), settings.origin("sandbox")) == ("preferred", "env")


def test_the_document_has_the_closed_shape(tmp_path: Path, plugin: Path) -> None:
    settings, profile = make_settings(make_config(tmp_path), plugin)
    doc = json.loads(settings.to_bytes())
    assert set(doc) == {"schema_version", "settings", "plugin_dirs", "plugin_dirs_sha256",
                        "config_sha256", "permission_profile"}  # fmt: skip
    assert doc["plugin_dirs"] == [str(plugin.resolve())]
    assert doc["permission_profile"] == {"kind": "bundled", "sha256": profile.sha256}
    assert doc["settings"] == sorted(doc["settings"], key=lambda e: e["key"])
    for entry in doc["settings"]:
        assert set(entry) == {"key", "value", "origin"}


def test_settings_json_is_deterministic(tmp_path: Path, plugin: Path) -> None:
    config = make_config(tmp_path)
    assert (
        make_settings(config, plugin)[0].to_bytes() == make_settings(config, plugin)[0].to_bytes()
    )


def test_config_sha256_changes_with_a_flag_and_not_otherwise(tmp_path: Path, plugin: Path) -> None:
    config = make_config(tmp_path)
    base = make_settings(config, plugin)[0].config_sha256
    assert make_settings(config, plugin)[0].config_sha256 == base
    assert make_settings(config, plugin, {"model": "x"})[0].config_sha256 != base


def test_write_run_files_modes_and_content(tmp_path: Path, plugin: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir(mode=0o700)
    settings, profile = make_settings(make_config(tmp_path), plugin)
    rs.write_run_files(run_dir, settings, profile)
    for name in (rs.SETTINGS_FILE, rs.PROFILE_FILE):
        assert stat.S_IMODE((run_dir / name).stat().st_mode) == 0o600
    assert (run_dir / rs.PROFILE_FILE).read_bytes() == profile.data
    assert (run_dir / rs.SETTINGS_FILE).read_bytes() == settings.to_bytes()


def test_write_run_files_never_overwrites_or_follows_a_link(tmp_path: Path, plugin: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir(mode=0o700)
    settings, profile = make_settings(make_config(tmp_path), plugin)
    victim = tmp_path / "victim"
    victim.write_text("keep", encoding="utf-8")
    (run_dir / rs.PROFILE_FILE).symlink_to(victim)
    with pytest.raises(FileExistsError):
        rs.write_run_files(run_dir, settings, profile)
    assert victim.read_text(encoding="utf-8") == "keep"
    assert not (run_dir / rs.SETTINGS_FILE).exists()


def test_write_run_files_refuses_a_profile_the_settings_do_not_describe(
    tmp_path: Path, plugin: Path
) -> None:
    settings, profile = make_settings(make_config(tmp_path), plugin)
    other = rs.ProfilePlan("custom", b"{}", hashlib.sha256(b"{}").hexdigest())
    with pytest.raises(ValueError):
        rs.write_run_files(tmp_path, settings, other)


def test_load_round_trips(tmp_path: Path, plugin: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir(mode=0o700)
    settings, profile = make_settings(make_config(tmp_path, "nits: true\n"), plugin, {"model": "m"})
    rs.write_run_files(run_dir, settings, profile)
    assert rs.load_run_settings(run_dir) == settings


def test_a_later_config_or_environment_change_does_not_change_a_stored_run(
    tmp_path: Path, plugin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir(mode=0o700)
    settings, profile = make_settings(make_config(tmp_path), plugin)
    rs.write_run_files(run_dir, settings, profile)
    # a later process: a different config file and REVIEW_RUN_SANDBOX=off
    monkeypatch.setenv("REVIEW_RUN_SANDBOX", "off")
    later = make_config(tmp_path, "sandbox: preferred\nnits: true\nsubagents: true\n")
    assert later.resolve(REPO).values["sandbox"] != "required"
    stored = rs.load_run_settings(run_dir)
    assert stored.value("sandbox") == "required" and stored.value("subagents") is False
    assert stored.value("nits") is False
    assert stored == settings


def test_load_refuses_a_tampered_profile(tmp_path: Path, plugin: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir(mode=0o700)
    settings, profile = make_settings(make_config(tmp_path), plugin)
    rs.write_run_files(run_dir, settings, profile)
    (run_dir / rs.PROFILE_FILE).write_bytes(b'{"permissions": {}}')
    with pytest.raises(ReviewRunError) as exc:
        rs.load_run_settings(run_dir)
    assert exc.value.code == "internal"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(schema_version=2),
        lambda d: d.update(plugin_dirs_sha256="zz"),
        lambda d: d.update(config_sha256=None),
        lambda d: d.update(plugin_dirs="x"),
        lambda d: d.update(permission_profile={"kind": "other", "sha256": "0" * 64}),
        lambda d: d.update(settings=[{"key": "a", "value": 1}]),
        lambda d: d.update(settings=[{"key": "a", "value": 1, "origin": "nowhere"}]),
        lambda d: d.update(settings=[{"key": "a", "value": 1, "origin": "flag", "x": 1}] * 1),
        lambda d: d.update(settings=d["settings"] + d["settings"][:1]),
        lambda d: d.pop("settings"),
    ],
)
def test_load_refuses_a_malformed_settings_file(tmp_path: Path, plugin: Path, mutate: Any) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir(mode=0o700)
    settings, profile = make_settings(make_config(tmp_path), plugin)
    rs.write_run_files(run_dir, settings, profile)
    doc = json.loads((run_dir / rs.SETTINGS_FILE).read_text(encoding="utf-8"))
    mutate(doc)
    (run_dir / rs.SETTINGS_FILE).write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(ReviewRunError) as exc:
        rs.load_run_settings(run_dir)
    assert exc.value.code == "internal"


@pytest.mark.parametrize("raw", [b"", b"{", b"[]", b'{"a": 1, "a": 2}', b"\xff"])
def test_load_refuses_garbage(tmp_path: Path, raw: bytes) -> None:
    (tmp_path / rs.SETTINGS_FILE).write_bytes(raw)
    (tmp_path / rs.PROFILE_FILE).write_bytes(b"{}")
    with pytest.raises(ReviewRunError) as exc:
        rs.load_run_settings(tmp_path)
    assert exc.value.code == "internal"


def test_load_does_not_follow_a_symlinked_settings_file(tmp_path: Path, plugin: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir(mode=0o700)
    settings, profile = make_settings(make_config(tmp_path), plugin)
    rs.write_run_files(run_dir, settings, profile)
    real = tmp_path / "elsewhere.json"
    real.write_bytes((run_dir / rs.SETTINGS_FILE).read_bytes())
    (run_dir / rs.SETTINGS_FILE).unlink()
    (run_dir / rs.SETTINGS_FILE).symlink_to(real)
    with pytest.raises(ReviewRunError):
        rs.load_run_settings(run_dir)


def test_load_of_a_missing_run_directory_is_internal(tmp_path: Path) -> None:
    with pytest.raises(ReviewRunError) as exc:
        rs.load_run_settings(tmp_path / "gone")
    assert exc.value.code == "internal"


def test_load_refuses_an_oversize_settings_file(tmp_path: Path) -> None:
    (tmp_path / rs.SETTINGS_FILE).write_bytes(b" " * (rs.MAX_SETTINGS_BYTES + 1))
    (tmp_path / rs.PROFILE_FILE).write_bytes(b"{}")
    with pytest.raises(ReviewRunError):
        rs.load_run_settings(tmp_path)


# -- the plugin hash, rechecked ----------------------------------------------------------------


def test_verify_passes_for_an_unchanged_tree_and_fails_after_any_change(
    tmp_path: Path, plugin: Path
) -> None:
    settings, _ = make_settings(make_config(tmp_path), plugin)
    rs.verify_plugin_dirs(settings)
    skill = plugin / "skills" / "review-pr" / "SKILL.md"
    skill.write_text(skill.read_text(encoding="utf-8") + "\nextra", encoding="utf-8")
    with pytest.raises(rs.PluginChanged):
        rs.verify_plugin_dirs(settings)


def test_verify_fails_when_a_symlink_appears(tmp_path: Path, plugin: Path) -> None:
    settings, _ = make_settings(make_config(tmp_path), plugin)
    (plugin / "evil").symlink_to("/etc/passwd")
    with pytest.raises(rs.PluginChanged):
        rs.verify_plugin_dirs(settings)


def test_verify_fails_when_the_directory_is_gone_or_replaced_by_a_link(
    tmp_path: Path, plugin: Path
) -> None:
    settings, _ = make_settings(make_config(tmp_path), plugin)
    moved = tmp_path / "moved"
    plugin.rename(moved)
    with pytest.raises(rs.PluginChanged):
        rs.verify_plugin_dirs(settings)
    plugin.symlink_to(moved)
    with pytest.raises(rs.PluginChanged):
        rs.verify_plugin_dirs(settings)


def test_plugin_changed_envelope_is_exit_12() -> None:
    err = rs.plugin_changed_error()
    assert (err.code, err.reason, err.exit_code) == ("failed", "plugin_changed", 12)


# -- launch flags ------------------------------------------------------------------------------


def flags_for(settings: rs.RunSettings, **extra: Any) -> list[str]:
    return rs.launch_flags(
        settings,
        repo=REPO,
        pr=PR,
        run_id="rr-abcdefgh",
        profile_path="/s/runs/rr-abcdefgh/permission-profile.json",
        result_file="/s/runs/rr-abcdefgh/launch.json",
        **extra,
    )


def pairs(flags: list[str]) -> list[tuple[str, str | None]]:
    """Flags as (name, value) in order; a flag with no value has ``None``."""
    out: list[tuple[str, str | None]] = []
    boolean = {"--no-implicit-done", "--no-promote", "--json"}
    i = 0
    while i < len(flags):
        assert flags[i].startswith("--"), f"a positional or stray value: {flags[i]!r}"
        if flags[i] in boolean:
            out.append((flags[i], None))
            i += 1
        else:
            out.append((flags[i], flags[i + 1]))
            i += 2
    return out


def test_launch_flags_default_golden(tmp_path: Path, plugin: Path) -> None:
    settings, _ = make_settings(make_config(tmp_path), plugin)
    assert flags_for(settings) == [
        "--mode", "headless", "--kind", "review", "--no-implicit-done", "--cwd", "session-dir",
        "--repo", REPO, "--skill", "review-run:review-pr", "--plugin-dir", str(plugin.resolve()),
        "--permission-profile", "/s/runs/rr-abcdefgh/permission-profile.json",
        "--sandbox", "required", "--setting-sources", "",
        "--env-allowlist-only", "PATH", "--no-promote",
        "--label", "owner=review-run", "--label", "run=rr-abcdefgh", "--label", f"pr={PR}",
        "--label", f"repo={REPO}",
        "--result-file", "/s/runs/rr-abcdefgh/launch.json", "--json",
    ]  # fmt: skip


def test_launch_flags_never_wait_ephemeral_or_prompt(tmp_path: Path, plugin: Path) -> None:
    settings, _ = make_settings(make_config(tmp_path), plugin)
    names = [n for n, _ in pairs(flags_for(settings))]
    assert not {"--wait", "--ephemeral", "--worktree", "--prompt", "--prompt-file",
                "--input-file", "--wait-timeout"} & set(names)  # fmt: skip


def test_launch_flags_have_no_positional_and_no_option_like_value(
    tmp_path: Path, plugin: Path
) -> None:
    config = make_config(tmp_path, "model: m\ntimeout: 5m\nmax_turns: 7\nsetting_sources: [user]\n")
    settings, _ = make_settings(config, plugin)
    flags = flags_for(settings, labels=("a=b",), actor="human:me", decision_ids=("d1",))
    for name, value in pairs(flags):
        assert value is None or not value.startswith("-"), name


def test_launch_flags_interactive_and_optional_values(tmp_path: Path, plugin: Path) -> None:
    config = make_config(
        tmp_path, "model: m\ntimeout: 5m\nmax_turns: 7\nsetting_sources: [user, local]\n"
    )
    settings, _ = make_settings(config, plugin, {"mode": "interactive"})
    got = pairs(
        flags_for(settings, labels=("x=y", "x=y"), actor="auto:me", decision_ids=("d1", "d2"))
    )
    assert ("--mode", "interactive") in got and ("--placement", "window") in got
    assert ("--model", "m") in got and ("--timeout", "5m") in got and ("--max-turns", "7") in got
    assert ("--setting-sources", "user,local") in got
    assert got.count(("--label", "x=y")) == 2  # a multiset: kept as given
    assert ("--actor", "auto:me") in got
    assert [v for n, v in got if n == "--decision-id"] == ["d1", "d2"]


def test_launch_flags_omit_unset_values(tmp_path: Path, plugin: Path) -> None:
    settings, _ = make_settings(make_config(tmp_path), plugin)
    names = [n for n, _ in pairs(flags_for(settings))]
    assert not {"--model", "--timeout", "--max-turns", "--placement", "--actor",
                "--decision-id"} & set(names)  # fmt: skip


def test_an_empty_env_allowlist_is_passed_as_an_empty_value(tmp_path: Path, plugin: Path) -> None:
    settings, _ = make_settings(make_config(tmp_path, "env_allowlist: []\n"), plugin)
    got = pairs(flags_for(settings))
    assert [v for n, v in got if n == "--env-allowlist-only"] == [""]


def test_subagents_adds_the_depth_name_and_sets_the_value_in_the_environment(
    tmp_path: Path, plugin: Path
) -> None:
    config = make_config(tmp_path, "subagents: true\nmax_subagent_depth: 3\n")
    settings, _ = make_settings(config, plugin)
    got = pairs(flags_for(settings))
    assert [v for n, v in got if n == "--env-allowlist-only"] == ["PATH", rs.DEPTH_VARIABLE]
    env = rs.launch_env(settings)
    assert env is not None and env[rs.DEPTH_VARIABLE] == "3"
    # an empty allowlist plus subagents: only the depth name, never the empty marker
    only = make_settings(make_config(tmp_path, "subagents: true\nenv_allowlist: []\n"), plugin)[0]
    assert [v for n, v in pairs(flags_for(only)) if n == "--env-allowlist-only"] == [
        rs.DEPTH_VARIABLE
    ]


def test_without_subagents_the_environment_is_inherited_untouched(
    tmp_path: Path, plugin: Path
) -> None:
    settings, _ = make_settings(make_config(tmp_path), plugin)
    assert rs.launch_env(settings) is None


def test_launch_flags_come_from_the_stored_settings_only(
    tmp_path: Path, plugin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, _ = make_settings(make_config(tmp_path), plugin)
    before = flags_for(settings)
    monkeypatch.setenv("REVIEW_RUN_SANDBOX", "off")
    monkeypatch.setenv("REVIEW_RUN_MODEL", "other")
    assert flags_for(settings) == before


def test_settings_json_validates_against_its_schema(tmp_path: Path, plugin: Path) -> None:
    from tests.conftest import validate_against

    config = make_config(tmp_path, "subagents: true\n")
    settings, _ = make_settings(config, plugin, {"model": "m"})
    validate_against("settings.json", json.loads(settings.to_bytes()))
