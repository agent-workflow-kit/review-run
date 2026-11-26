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
        if "=" in flags[i]:  # one item: --name=value
            name, _, value = flags[i].partition("=")
            out.append((name, value))
            i += 1
        elif flags[i] in boolean:
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


def test_a_decision_id_that_looks_like_an_option_is_one_argv_item(
    tmp_path: Path, plugin: Path
) -> None:
    settings, _ = make_settings(make_config(tmp_path), plugin)
    flags = flags_for(settings, decision_ids=("-x", "d2"))
    assert "--decision-id=-x" in flags and "--decision-id=d2" in flags
    assert "--decision-id" not in flags and "-x" not in flags


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


# -- mutation-check additions ------------------------------------------------------------------


def _stored_run(tmp_path: Path, plugin: Path) -> Path:
    run_dir = tmp_path / "run"
    run_dir.mkdir(mode=0o700)
    settings, profile = make_settings(make_config(tmp_path), plugin)
    rs.write_run_files(run_dir, settings, profile)
    return run_dir


def test_no_process_key_reaches_settings_json_by_the_literal_names(
    tmp_path: Path, plugin: Path
) -> None:
    """Spelled out here: the test must not trust ``rs.EXCLUDED`` itself."""
    settings, _ = make_settings(make_config(tmp_path), plugin)
    tops = {k.split(".")[0] for k in settings.entries}
    assert not tops & {"hooks", "retention", "escalation_ttl", "me"}
    stored = {e["key"].split(".")[0] for e in json.loads(settings.to_bytes())["settings"]}
    assert not stored & {"hooks", "retention", "escalation_ttl", "me"}


def test_build_settings_sorts_the_plugin_dirs_by_bytes(tmp_path: Path, plugin: Path) -> None:
    resolved = make_config(tmp_path).resolve(REPO)
    profile, _ = rs.effective_profile(resolved.values)
    settings = rs.build_settings(
        resolved, plugin_dirs=["/z", "/a", "/M"], plugin_dirs_hash="0" * 64, profile=profile
    )
    assert settings.plugin_dirs == ("/M", "/a", "/z")


@pytest.mark.parametrize(
    ("what", "yaml_a", "yaml_b", "env_a", "env_b", "flags_a", "flags_b"),
    [
        ("hooks", "hooks:\n  pre-run:\n    - cmd: 'true'\n", "hooks:\n  pre-run:\n    - cmd: 'false'\n", {}, {}, None, None),
        ("retention", "retention: 10d\n", "retention: 20d\n", {}, {}, None, None),
        ("escalation_ttl", "escalation_ttl: 1d\n", "escalation_ttl: 2d\n", {}, {}, None, None),
        ("me", "me:\n  name: ann\n", "me:\n  name: bob\n", {}, {}, None, None),
        ("limits", "limits:\n  max_files: 10\n", "limits:\n  max_files: 11\n", {}, {}, None, None),
        ("repo origin", f"repos:\n  {REPO}:\n    model: r1\n", f"repos:\n  {REPO}:\n    model: r2\n", {}, {}, None, None),
        ("env origin", "", "", {"REVIEW_RUN_SANDBOX": "preferred"}, {"REVIEW_RUN_SANDBOX": "off"}, None, None),
        ("flag origin", "", "", {}, {}, {"model": "x"}, {"model": "y"}),
        ("config origin", "nits: true\nmax_turns: 3\n", "nits: true\nmax_turns: 4\n", {}, {}, None, None),
    ],
)  # fmt: skip
def test_config_sha256_covers_every_resolved_value_whatever_its_origin(
    tmp_path: Path, plugin: Path, what: str, yaml_a: str, yaml_b: str,
    env_a: dict[str, str], env_b: dict[str, str],
    flags_a: dict[str, Any] | None, flags_b: dict[str, Any] | None,
) -> None:  # fmt: skip
    a = make_settings(make_config(tmp_path, yaml_a, env_a), plugin, flags_a)[0]
    b = make_settings(make_config(tmp_path, yaml_b, env_b), plugin, flags_b)[0]
    assert a.config_sha256 != b.config_sha256, what


def test_load_refuses_a_settings_file_or_profile_owned_by_someone_else(
    tmp_path: Path, plugin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = _stored_run(tmp_path, plugin)
    assert rs.load_run_settings(run_dir)
    monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 1)
    with pytest.raises(ReviewRunError) as exc:
        rs.load_run_settings(run_dir)
    assert exc.value.code == "internal"


def _fifo_holding(path: Path, data: bytes) -> int:
    """A FIFO at ``path`` that already holds ``data`` and still has a writer (the returned
    descriptor, to close), so a non-blocking reader sees the bytes and no end of file."""
    os.mkfifo(path, 0o600)
    fd = os.open(path, os.O_RDWR)
    os.write(fd, data)
    return fd


@pytest.mark.parametrize("name", [rs.SETTINGS_FILE, rs.PROFILE_FILE])
def test_load_refuses_a_fifo_even_when_it_holds_valid_content(
    tmp_path: Path, plugin: Path, name: str
) -> None:
    run_dir = _stored_run(tmp_path, plugin)
    content = (run_dir / name).read_bytes()
    (run_dir / name).unlink()
    fd = _fifo_holding(run_dir / name, content)
    try:
        with pytest.raises(ReviewRunError) as exc:
            rs.load_run_settings(run_dir)
    finally:
        os.close(fd)
    assert exc.value.code == "internal"


def test_the_settings_file_size_limit_is_inclusive(tmp_path: Path, plugin: Path) -> None:
    run_dir = _stored_run(tmp_path, plugin)
    path = run_dir / rs.SETTINGS_FILE
    body = path.read_bytes().rstrip(b"\n")
    path.write_bytes(body + b" " * (rs.MAX_SETTINGS_BYTES - len(body)))  # exactly the limit
    assert rs.load_run_settings(run_dir)
    path.write_bytes(body + b" " * (rs.MAX_SETTINGS_BYTES + 1 - len(body)))  # one byte over
    with pytest.raises(ReviewRunError) as exc:
        rs.load_run_settings(run_dir)
    assert exc.value.code == "internal"


@pytest.mark.parametrize("key", ["plugin_dirs_sha256", "config_sha256"])
@pytest.mark.parametrize("bad", ["a" * 63, "a" * 65, "A" * 64, "g" * 64, "", 7])
def test_load_refuses_a_hash_that_is_not_64_lowercase_hex(
    tmp_path: Path, plugin: Path, key: str, bad: object
) -> None:
    run_dir = _stored_run(tmp_path, plugin)
    doc = json.loads((run_dir / rs.SETTINGS_FILE).read_text(encoding="utf-8"))
    doc[key] = "a" * 64
    (run_dir / rs.SETTINGS_FILE).write_text(json.dumps(doc), encoding="utf-8")
    assert rs.load_run_settings(run_dir)  # the shape alone is fine
    doc[key] = bad
    (run_dir / rs.SETTINGS_FILE).write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(ReviewRunError) as exc:
        rs.load_run_settings(run_dir)
    assert exc.value.code == "internal"


def test_load_refuses_an_unknown_profile_kind_with_the_right_hash(
    tmp_path: Path, plugin: Path
) -> None:
    run_dir = _stored_run(tmp_path, plugin)
    doc = json.loads((run_dir / rs.SETTINGS_FILE).read_text(encoding="utf-8"))
    doc["permission_profile"]["kind"] = "other"
    (run_dir / rs.SETTINGS_FILE).write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(ReviewRunError) as exc:
        rs.load_run_settings(run_dir)
    assert exc.value.code == "internal"


@pytest.mark.parametrize("dirs", [[1], ["/a", None], [["/a"]]])
def test_load_refuses_plugin_dirs_that_are_not_strings(
    tmp_path: Path, plugin: Path, dirs: list[object]
) -> None:
    run_dir = _stored_run(tmp_path, plugin)
    doc = json.loads((run_dir / rs.SETTINGS_FILE).read_text(encoding="utf-8"))
    doc["plugin_dirs"] = dirs
    (run_dir / rs.SETTINGS_FILE).write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(ReviewRunError) as exc:
        rs.load_run_settings(run_dir)
    assert exc.value.code == "internal"


def test_write_run_files_refuses_a_profile_that_differs_only_in_kind_or_only_in_hash(
    tmp_path: Path, plugin: Path
) -> None:
    settings, profile = make_settings(make_config(tmp_path), plugin)
    only_kind = rs.ProfilePlan("custom", profile.data, profile.sha256)
    only_hash = rs.ProfilePlan(profile.kind, profile.data, "0" * 64)
    for other in (only_kind, only_hash):
        with pytest.raises(ValueError):
            rs.write_run_files(tmp_path, settings, other)
    assert not (tmp_path / rs.PROFILE_FILE).exists()  # nothing written before the refusal


def test_a_custom_profile_owned_by_someone_else_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    custom = tmp_path / "p.json"
    custom.write_text("{}", encoding="utf-8")
    custom.chmod(0o600)
    assert rs.read_profile_file(str(custom)) == b"{}"
    monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 1)
    with pytest.raises(ReviewRunError) as exc:
        rs.read_profile_file(str(custom))
    assert exc.value.details == {"key": "permission_profile"}


def test_a_custom_profile_that_is_a_fifo_or_directory_is_refused(tmp_path: Path) -> None:
    fifo = tmp_path / "fifo.json"
    fd = _fifo_holding(fifo, b"{}")
    try:
        with pytest.raises(ReviewRunError) as exc:
            rs.read_profile_file(str(fifo))
    finally:
        os.close(fd)
    assert exc.value.details == {"key": "permission_profile"}
    (tmp_path / "dir.json").mkdir(mode=0o700)
    with pytest.raises(ReviewRunError):
        rs.read_profile_file(str(tmp_path / "dir.json"))


def test_a_group_writable_custom_profile_is_accepted_only_in_a_private_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from review_run import config_yaml

    custom = tmp_path / "p.json"
    custom.write_text("{}", encoding="utf-8")
    custom.chmod(0o620)
    monkeypatch.setattr(config_yaml, "private_group", lambda gid: True)
    assert rs.read_profile_file(str(custom)) == b"{}"


def test_the_custom_profile_size_limit_is_inclusive(tmp_path: Path) -> None:
    """Valid JSON padded with trailing blanks, so only the size can make it fail."""
    custom = tmp_path / "p.json"
    for size, ok in ((rs.MAX_PROFILE_BYTES, True), (rs.MAX_PROFILE_BYTES + 1, False)):
        custom.unlink(missing_ok=True)
        custom.write_bytes(b"{}" + b" " * (size - 2))
        custom.chmod(0o600)
        if ok:
            assert len(rs.read_profile_file(str(custom))) == rs.MAX_PROFILE_BYTES
        else:
            with pytest.raises(ReviewRunError) as exc:
                rs.read_profile_file(str(custom))
            assert exc.value.details == {"key": "permission_profile"}


# -- round 1 review: validation on load, non-finite numbers, file modes ------------------------


def _stored(tmp_path: Path, plugin: Path) -> tuple[Path, dict[str, Any]]:
    run_dir = tmp_path / "run"
    run_dir.mkdir(mode=0o700)
    settings, profile = make_settings(make_config(tmp_path), plugin)
    rs.write_run_files(run_dir, settings, profile)
    doc = json.loads((run_dir / rs.SETTINGS_FILE).read_text(encoding="utf-8"))
    return run_dir, doc


def _rewrite(run_dir: Path, doc: dict[str, Any]) -> None:
    path = run_dir / rs.SETTINGS_FILE
    path.unlink()
    path.write_text(json.dumps(doc), encoding="utf-8")
    path.chmod(0o600)


def _set(doc: dict[str, Any], key: str, value: Any) -> None:
    for item in doc["settings"]:
        if item["key"] == key:
            item["value"] = value
            return
    raise AssertionError(key)


def _drop(doc: dict[str, Any], key: str) -> None:
    doc["settings"] = [i for i in doc["settings"] if i["key"] != key]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: _drop(d, "mode"),
        lambda d: _drop(d, "skill"),
        lambda d: _drop(d, "sandbox"),
        lambda d: _drop(d, "setting_sources"),
        lambda d: _drop(d, "env_allowlist"),
        lambda d: _drop(d, "subagents"),
        lambda d: _drop(d, "max_subagent_depth"),
        lambda d: _drop(d, "model"),
        lambda d: _drop(d, "timeout"),
        lambda d: _drop(d, "max_turns"),
        lambda d: _set(d, "mode", "other"),
        lambda d: _set(d, "mode", "--evil"),
        lambda d: _set(d, "skill", 3),
        lambda d: _set(d, "skill", "--evil"),
        lambda d: _set(d, "sandbox", "--evil"),
        lambda d: _set(d, "setting_sources", "user"),
        lambda d: _set(d, "setting_sources", ["--evil"]),
        lambda d: _set(d, "env_allowlist", [1]),
        lambda d: _set(d, "subagents", 1),
        lambda d: _set(d, "max_subagent_depth", True),
        lambda d: _set(d, "max_subagent_depth", "1"),
        lambda d: _set(d, "model", "--evil"),
        lambda d: _set(d, "model", 5),
        lambda d: _set(d, "timeout", "-5m"),
        lambda d: _set(d, "max_turns", "7"),
        lambda d: _set(d, "max_turns", False),
        lambda d: d["settings"].append({"key": "Bad Key", "value": 1, "origin": "flag"}),
        lambda d: d["settings"].append({"key": "unknown_key", "value": 1, "origin": "flag"}),
        lambda d: d["settings"].append({"key": "hooks.pre-run", "value": [], "origin": "flag"}),
        lambda d: d.update(plugin_dirs=["relative/dir"]),
        lambda d: d.update(schema_version=True),
        lambda d: d.update(schema_version=1.0),
        lambda d: d.update(extra=1),
        lambda d: d["permission_profile"].update(extra=1),
        lambda d: d.update(settings="x"),
    ],
)
def test_load_enforces_the_settings_schema_and_the_values_launch_reads(
    tmp_path: Path, plugin: Path, mutate: Any
) -> None:
    run_dir, doc = _stored(tmp_path, plugin)
    mutate(doc)
    _rewrite(run_dir, doc)
    with pytest.raises(ReviewRunError) as exc:
        rs.load_run_settings(run_dir)
    assert exc.value.code == "internal"


def test_load_accepts_the_values_the_schema_allows(tmp_path: Path, plugin: Path) -> None:
    run_dir, doc = _stored(tmp_path, plugin)
    _set(doc, "model", "m")
    _set(doc, "timeout", "5m")
    _set(doc, "max_turns", 7)
    _rewrite(run_dir, doc)
    assert rs.load_run_settings(run_dir).value("max_turns") == 7


def test_load_refuses_a_non_finite_number(tmp_path: Path, plugin: Path) -> None:
    run_dir, doc = _stored(tmp_path, plugin)
    _set(doc, "max_turns", 123456789)
    raw = json.dumps(doc).replace("123456789", "1e999")
    assert "1e999" in raw
    path = run_dir / rs.SETTINGS_FILE
    path.unlink()
    path.write_text(raw, encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(ReviewRunError) as exc:
        rs.load_run_settings(run_dir)
    assert exc.value.code == "internal"


def test_reject_non_finite_looks_inside_containers() -> None:
    rs.reject_non_finite({"a": [1, 2.5, {"b": None}]})
    for bad in (float("inf"), [float("nan")], {"a": {"b": [float("-inf")]}}):
        with pytest.raises(ValueError):
            rs.reject_non_finite(bad)
    with pytest.raises(ValueError):
        rs.strict_finite_json('{"a": 1e999}')


def test_a_custom_profile_with_a_non_finite_number_is_refused(tmp_path: Path) -> None:
    custom = tmp_path / "p.json"
    custom.write_text('{"x": 1e999}', encoding="utf-8")
    custom.chmod(0o600)
    with pytest.raises(ReviewRunError) as exc:
        rs.read_profile_file(str(custom))
    assert exc.value.details == {"key": "permission_profile"}


def test_the_hashes_and_the_document_are_never_written_with_nan(
    tmp_path: Path, plugin: Path
) -> None:
    settings, _ = make_settings(make_config(tmp_path), plugin)
    bad = rs.RunSettings(
        {**settings.entries, "model": (float("nan"), "flag")},
        settings.plugin_dirs,
        settings.plugin_dirs_sha256,
        settings.config_sha256,
        settings.profile_kind,
        settings.profile_sha256,
    )
    with pytest.raises(ValueError):
        bad.to_bytes()
    resolved = make_config(tmp_path).resolve(REPO)
    resolved.values["model"] = float("inf")
    with pytest.raises(ValueError):
        rs.config_sha256(resolved)


@pytest.mark.parametrize("name", [rs.SETTINGS_FILE, rs.PROFILE_FILE])
@pytest.mark.parametrize("mode", [0o640, 0o604, 0o660, 0o666])
def test_load_refuses_a_run_file_that_group_or_others_can_access(
    tmp_path: Path, plugin: Path, name: str, mode: int
) -> None:
    run_dir, _ = _stored(tmp_path, plugin)
    (run_dir / name).chmod(mode)
    with pytest.raises(ReviewRunError) as exc:
        rs.load_run_settings(run_dir)
    assert exc.value.code == "internal"


def test_a_custom_profile_follows_the_shared_group_write_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from review_run import config_yaml

    custom = tmp_path / "p.json"
    custom.write_text("{}", encoding="utf-8")
    custom.chmod(0o620)
    monkeypatch.setattr(config_yaml, "private_group", lambda gid: True)
    assert rs.read_profile_file(str(custom)) == b"{}"
    monkeypatch.setattr(config_yaml, "private_group", lambda gid: False)
    with pytest.raises(ReviewRunError) as exc:
        rs.read_profile_file(str(custom))
    assert exc.value.details == {"key": "permission_profile"}
    custom.chmod(0o602)  # world-writable: refused even in a private group
    monkeypatch.setattr(config_yaml, "private_group", lambda gid: True)
    with pytest.raises(ReviewRunError):
        rs.read_profile_file(str(custom))
