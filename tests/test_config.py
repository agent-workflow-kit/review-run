"""Config loading, drop-ins, environment, precedence and error reporting (interface.md, Config)."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest
import yaml

from review_run.config import (
    Config,
    check_config,
    default_config_path,
    defaults,
    load_config,
    schema_issues,
)
from review_run.errors import ReviewRunError
from tests.config_helpers import base_env, config_dir, load, write_dropin, write_main
from tests.conftest import SCHEMAS, validate_against

ROOT = Path(__file__).resolve().parent.parent
REPO = "github.com/example-org/example-repo"


def fail(text: str | None = None, env: dict[str, str] | None = None) -> ReviewRunError:
    with pytest.raises(ReviewRunError) as info:
        load(text, env)
    assert info.value.code == "config_invalid"
    assert info.value.exit_code == 2
    validate_against("cli/error.json", info.value.envelope())
    return info.value


# -- defaults and the example ------------------------------------------------------------------


def test_no_file_gives_the_defaults() -> None:
    values = load().resolve().values
    assert values["skill"] == "review-run:review-pr"
    assert values["mode"] == "headless"
    assert values["sandbox"] == "required"
    assert values["env_allowlist"] == ["PATH"]
    assert values["limits"]["max_files"] == 300
    assert values["escalation_ttl"] == "7d"
    assert values["me"] == {"name": None}
    assert values["hooks"] == {"pre-run": [], "post-run": [], "post-escalate": []}
    assert values["run_pr_code"] is None


def test_default_path_respects_xdg_and_ignores_a_relative_xdg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert default_config_path() == config_dir() / "config.yaml"
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/dir")
    assert default_config_path() == Path.home() / ".config" / "review-run" / "config.yaml"


def test_example_validates_and_equals_the_defaults() -> None:
    example = ROOT / "config.example.yaml"
    document = yaml.safe_load(example.read_text(encoding="utf-8"))
    assert schema_issues(document, "example") == []
    resolved = load_config(example, base_env()).resolve().values
    expected = defaults()
    expected["run_pr_code"] = None
    assert resolved == expected


def test_example_documents_every_key() -> None:
    schema = json.loads((SCHEMAS / "config.json").read_text(encoding="utf-8"))
    text = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
    document = yaml.safe_load(text)
    assert set(document) == set(schema["properties"])
    assert set(document["limits"]) == set(schema["properties"]["limits"]["properties"])
    repo_schema = next(iter(schema["properties"]["repos"]["patternProperties"].values()))
    for key in repo_schema["properties"]["run_pr_code"]["properties"]:
        assert re.search(rf"^#\s+{key}:", text, re.MULTILINE), key


def test_example_block_validates_against_the_schema() -> None:
    text = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
    block = text.split("# --- example:begin\n")[1].split("# --- example:end")[0]
    document = yaml.safe_load(
        "".join(line[2:] if line.startswith("# ") else line[1:] for line in block.splitlines(True))
    )
    assert schema_issues(document, "example") == []


# -- files and drop-ins ------------------------------------------------------------------------


def test_main_file_values_are_loaded() -> None:
    values = load("model: m1\nnits: true\nlimits:\n  max_files: 10\n").resolve().values
    assert values["model"] == "m1"
    assert values["nits"] is True
    assert values["limits"]["max_files"] == 10
    assert values["limits"]["max_file_bytes"] == 200000


def test_drop_ins_merge_in_lexical_order_after_the_main_file() -> None:
    write_main(
        "model: main\nlimits:\n  max_files: 1\n  max_total_bytes: 5\nsetting_sources: [user]\n"
    )
    write_dropin("20-b.yaml", "model: b\nlimits:\n  max_files: 3\n")
    write_dropin("10-a.yaml", "model: a\nsetting_sources: [local, project]\n")
    values = load_config(None, base_env()).resolve().values
    assert values["model"] == "b"
    assert values["limits"] == {**defaults()["limits"], "max_files": 3, "max_total_bytes": 5}
    assert values["setting_sources"] == ["local", "project"]  # lists are replaced


def test_hook_lists_append_across_files() -> None:
    write_main("hooks:\n  post-run:\n    - cmd: one\n")
    write_dropin(
        "a.yaml",
        "hooks:\n  post-run:\n    - cmd: two\n      async: true\n  pre-run:\n    - cmd: p\n",
    )
    hooks = load_config(None, base_env()).resolve().values["hooks"]
    assert [h["cmd"] for h in hooks["post-run"]] == ["one", "two"]
    assert [h["cmd"] for h in hooks["pre-run"]] == ["p"]


def test_drop_ins_without_a_main_file_and_ignored_names() -> None:
    write_dropin("a.yaml", "model: a\n")
    write_dropin("b.yml", "model: yml\n")
    write_dropin(".c.yaml", "model: hidden\n")
    write_dropin("d.yaml.bak", "model: bak\n")
    assert load_config(None, base_env()).resolve().values["model"] == "a"


def test_repos_merge_across_files() -> None:
    write_main(f"repos:\n  {REPO}:\n    nits: true\n    limits:\n      max_files: 5\n")
    write_dropin(
        "a.yaml", f"repos:\n  {REPO}:\n    model: x\n    limits:\n      max_total_bytes: 7\n"
    )
    values = load_config(None, base_env()).resolve(REPO).values
    assert (values["nits"], values["model"]) == (True, "x")
    assert values["limits"]["max_files"] == 5
    assert values["limits"]["max_total_bytes"] == 7


def test_explicit_config_must_exist_and_default_may_be_absent() -> None:
    assert load_config(None, base_env()).resolve().values["mode"] == "headless"
    with pytest.raises(ReviewRunError) as info:
        load_config(config_dir() / "nope.yaml", base_env())
    assert info.value.code == "config_invalid"


def test_empty_file_is_the_defaults() -> None:
    assert load("").resolve().values["mode"] == "headless"
    assert load("# only a comment\n").resolve().values["mode"] == "headless"


@pytest.mark.parametrize(
    "text",
    [
        "- a\n- b\n",  # not a mapping
        "model: [unclosed\n",
        "model: &a x\nskill: *a\n",  # anchors and aliases
        "model: !!python/object/apply:os.getcwd []\n",  # tags
        "model: a\nmodel: b\n",  # duplicate keys
        "1: x\n",  # non-string key
        "model: a\n\tbad: b\n",
    ],
)
def test_malformed_yaml_is_refused(text: str) -> None:
    err = fail(text)
    assert err.details is not None
    assert "key" in err.details


def test_non_utf8_and_oversized_files_are_refused() -> None:
    path = write_main("")
    path.write_bytes(b"model: \xff\xfe\n")
    with pytest.raises(ReviewRunError):
        load_config(None, base_env())
    path.write_bytes(b"# " + b"x" * (300 * 1024))
    with pytest.raises(ReviewRunError):
        load_config(None, base_env())


def test_yaml_1_2_scalars() -> None:
    # `yes` and `no` are strings, not booleans; so the boolean key refuses them.
    err = fail("nits: yes\n")
    assert err.details == {"key": "nits"}
    assert load("model: no\n").resolve().values["model"] == "no"


def test_fifo_and_directory_drop_ins_are_refused_without_blocking() -> None:
    directory = config_dir() / "config.d"
    directory.mkdir()
    os.mkfifo(directory / "a.yaml")
    with pytest.raises(ReviewRunError):
        load_config(None, base_env())


def test_directory_named_like_a_drop_in_is_refused() -> None:
    (config_dir() / "config.d" / "a.yaml").mkdir(parents=True)
    with pytest.raises(ReviewRunError):
        load_config(None, base_env())


def test_symlinked_config_file_to_an_owned_file_is_followed() -> None:
    target = config_dir() / "real.yaml"
    target.write_text("model: linked\n", encoding="utf-8")
    (config_dir() / "config.yaml").symlink_to(target)
    assert load_config(None, base_env()).resolve().values["model"] == "linked"


def test_broken_symlink_drop_in_is_refused() -> None:
    directory = config_dir() / "config.d"
    directory.mkdir()
    (directory / "a.yaml").symlink_to(config_dir() / "missing")
    with pytest.raises(ReviewRunError):
        load_config(None, base_env())


def test_group_or_world_writable_files_and_directories_are_refused() -> None:
    path = write_main("model: a\n")
    path.chmod(0o666)
    with pytest.raises(ReviewRunError) as info:
        load_config(None, base_env())
    assert info.value.code == "config_invalid"
    path.chmod(0o664)  # the user's own group is fine (private-group systems, umask 002)
    assert load_config(None, base_env()).resolve().values["model"] == "a"
    config_dir().joinpath("config.d").mkdir()
    config_dir().joinpath("config.d").chmod(0o777)
    with pytest.raises(ReviewRunError):
        load_config(None, base_env())


# -- schema errors name the key, never the value ------------------------------------------------


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("sandbox: sk-live-123\n", "sandbox"),
        ("limits:\n  max_files: 2001\n", "limits.max_files"),
        ("limits:\n  max_files: 0\n", "limits.max_files"),
        ("limits:\n  nope: 1\n", "limits.nope"),
        ("nope: sk-live-123\n", "nope"),
        ("max_turns: 0\n", "max_turns"),
        ("max_subagent_depth: 0\n", "max_subagent_depth"),
        ("timeout: sk-live-123\n", "timeout"),
        ("escalation_ttl: 1.5h\n", "escalation_ttl"),
        ("retention: null\n", "retention"),
        ("subagents: 'true'\n", "subagents"),
        ("setting_sources: [user, sk-live-123]\n", "setting_sources[1]"),
        ("env_allowlist: [PATH, 'BAD-NAME']\n", "env_allowlist[1]"),
        ("env_allowlist: [PATH, PATH]\n", "env_allowlist"),
        ("plugin_dirs: [relative]\n", "plugin_dirs[0]"),
        ("guidelines_file: relative.md\n", "guidelines_file"),
        ("me:\n  name: 'a b'\n", "me.name"),
        ("hooks:\n  post-run:\n    - cmd: ''\n", "hooks.post-run[0].cmd"),
        ("hooks:\n  nope: []\n", "hooks.nope"),
        ("hooks:\n  pre-run:\n    - cmd: x\n      async: true\n", "hooks.pre-run[0].async"),
        ("run_pr_code:\n  command: [x]\n", "run_pr_code"),
        (f"repos:\n  {REPO}:\n    mode: interactive\n", f"repos.{REPO}.mode"),
        (f"repos:\n  {REPO}:\n    hooks: {{}}\n", f"repos.{REPO}.hooks"),
        (f"repos:\n  {REPO}:\n    retention: 1d\n", f"repos.{REPO}.retention"),
        (f"repos:\n  {REPO}:\n    me: {{}}\n", f"repos.{REPO}.me"),
        (f"repos:\n  {REPO}:\n    escalation_ttl: 1d\n", f"repos.{REPO}.escalation_ttl"),
        (
            f"repos:\n  {REPO}:\n    limits:\n      max_files: 5000\n",
            f"repos.{REPO}.limits.max_files",
        ),
        (f"repos:\n  {REPO}:\n    run_pr_code: {{}}\n", f"repos.{REPO}.run_pr_code.command"),
        (
            f"repos:\n  {REPO}:\n    run_pr_code:\n      command: []\n",
            f"repos.{REPO}.run_pr_code.command",
        ),
        (
            f"repos:\n  {REPO}:\n    run_pr_code:\n      command: [x]\n      network: true\n",
            f"repos.{REPO}.run_pr_code.network",
        ),
        ("repos:\n  not-a-repo-id: {}\n", "repos.not-a-repo-id"),
    ],
)
def test_schema_errors_name_the_key_and_never_the_value(text: str, key: str) -> None:
    err = fail(text)
    assert err.details is not None
    assert err.details["key"] == key
    assert "sk-live-123" not in json.dumps(err.envelope())
    assert len(err.message) <= 512


def test_error_message_names_the_file_and_several_keys() -> None:
    err = fail("sandbox: x\nnits: 3\n")
    assert "config.yaml" in err.message
    assert "sandbox" in err.message
    assert "nits" in err.message


def test_a_drop_in_error_names_the_drop_in() -> None:
    write_dropin("50-x.yaml", "sandbox: bad\n")
    with pytest.raises(ReviewRunError) as info:
        load_config(None, base_env())
    assert "50-x.yaml" in info.value.message


def test_pattern_dollar_does_not_match_a_trailing_newline() -> None:
    err = fail('env_allowlist: ["PATH\\n"]\n')
    assert err.details == {"key": "env_allowlist[0]"}
    err = fail('me:\n  name: "bob\\n"\n')
    assert err.details == {"key": "me.name"}


def test_check_config_reports_every_issue() -> None:
    write_main("sandbox: x\nnits: 3\nmodel: ''\n")
    result = check_config(None, base_env())
    assert result.config is None
    assert [i.key for i in result.issues] == ["model", "nits", "sandbox"]
    assert all(i.problem and i.source for i in result.issues)


def test_check_config_success_has_no_issues() -> None:
    result = check_config(None, base_env())
    assert result.issues == []
    assert result.config is not None


# -- environment -------------------------------------------------------------------------------


def test_env_overrides_the_file() -> None:
    cfg = load("model: file\nnits: false\n", {"REVIEW_RUN_MODEL": "env", "REVIEW_RUN_NITS": "true"})
    resolved = cfg.resolve()
    assert resolved.values["model"] == "env"
    assert resolved.values["nits"] is True
    assert resolved.origins["model"] == "env"


@pytest.mark.parametrize(
    ("name", "value", "expected"),
    [
        ("SKILL", "plug:skill", "plug:skill"),
        ("MODE", "interactive", "interactive"),
        ("SANDBOX", "preferred", "preferred"),
        ("MODEL", "null", None),
        ("TIMEOUT", "90s", "90s"),
        ("TIMEOUT", "null", None),
        ("MAX_TURNS", "12", 12),
        ("MAX_TURNS", "null", None),
        ("SUBAGENTS", "true", True),
        ("MAX_SUBAGENT_DEPTH", "3", 3),
        ("NITS", "false", False),
        ("RETENTION", "2d", "2d"),
        ("ESCALATION_TTL", "null", None),
        ("ESCALATION_TTL", "1h", "1h"),
    ],
)
def test_env_parses_each_scalar_type(name: str, value: str, expected: object) -> None:
    resolved = load(None, {f"REVIEW_RUN_{name}": value}).resolve()
    assert resolved.values[name.lower()] == expected
    assert resolved.origins[name.lower()] == "env"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("NITS", "True"),
        ("NITS", "1"),
        ("NITS", "yes"),
        ("NITS", ""),
        ("SUBAGENTS", " true"),
        ("MAX_TURNS", "05"),
        ("MAX_TURNS", "1.5"),
        ("MAX_TURNS", "-1"),
        ("MAX_TURNS", "1_0"),
        ("MAX_TURNS", "١٢"),  # Arabic-Indic digits
        ("MAX_TURNS", "0"),
        ("MAX_TURNS", "9" * 400),
        ("MAX_TURNS", ""),
        ("SANDBOX", "sk-live-123"),
        ("SANDBOX", ""),
        ("MODE", "headless\n"),
        ("MODEL", ""),
        ("TIMEOUT", "5"),
        ("TIMEOUT", "0"),
        ("TIMEOUT", "1.5s"),
        ("TIMEOUT", "sk-live-123"),
        ("RETENTION", "null"),
        ("MODE", "sk-live-123"),
    ],
)
def test_env_values_are_parsed_strictly(name: str, value: str) -> None:
    with pytest.raises(ReviewRunError) as info:
        load(None, {f"REVIEW_RUN_{name}": value})
    err = info.value
    assert err.code == "config_invalid"
    assert err.details == {"key": f"REVIEW_RUN_{name}"}
    assert "sk-live-123" not in json.dumps(err.envelope())
    assert len(value) < 4 or value not in err.message


def test_env_ignores_non_scalar_nested_and_unknown_variables() -> None:
    env = {
        "REVIEW_RUN_PLUGIN_DIRS": "/x",
        "REVIEW_RUN_LIMITS__MAX_FILES": "5",
        "REVIEW_RUN_HOOKS": "x",
        "REVIEW_RUN_REPOS": "x",
        "REVIEW_RUN_ME__NAME": "x",
        "REVIEW_RUN_EVENT_ID": "x",
        "REVIEW_RUN_UNKNOWN": "x",
        "review_run_model": "lower",
        "OTHER_MODEL": "x",
    }
    resolved = load(None, env).resolve()
    assert resolved.values == {**defaults(), "run_pr_code": None}


def test_default_environment_is_the_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REVIEW_RUN_MODEL", "from-process")
    assert load_config(None).resolve().values["model"] == "from-process"


# -- precedence --------------------------------------------------------------------------------


def precedence_config() -> Config:
    write_main(
        "model: file\nnits: false\n"
        f"repos:\n  {REPO}:\n    model: repo\n    limits:\n      max_files: 7\n"
    )
    return load_config(None, {**base_env(), "REVIEW_RUN_MODEL": "env", "REVIEW_RUN_NITS": "true"})


def test_precedence_default_file_env_repo_flag() -> None:
    cfg = precedence_config()
    base = cfg.resolve()
    assert (base.values["model"], base.origins["model"]) == ("env", "env")
    assert (base.values["nits"], base.origins["nits"]) == (True, "env")
    assert (base.values["sandbox"], base.origins["sandbox"]) == ("required", "default")

    repo = cfg.resolve(REPO)
    assert (repo.values["model"], repo.origins["model"]) == ("repo", "repo")
    assert (repo.values["nits"], repo.origins["nits"]) == (True, "env")

    flag = cfg.resolve(REPO, {"model": "flag"})
    assert (flag.values["model"], flag.origins["model"]) == ("flag", "flag")


def test_file_beats_default_and_env_beats_file() -> None:
    write_main("sandbox: preferred\nmodel: file\n")
    resolved = load_config(None, {**base_env(), "REVIEW_RUN_MODEL": "env"}).resolve()
    assert (resolved.values["sandbox"], resolved.origins["sandbox"]) == ("preferred", "config")
    assert (resolved.values["model"], resolved.origins["model"]) == ("env", "env")


def test_a_repo_beats_a_global_environment_setting() -> None:
    write_main(f"repos:\n  {REPO}:\n    sandbox: required\n")
    cfg = load_config(None, {**base_env(), "REVIEW_RUN_SANDBOX": "off"})
    assert cfg.resolve().values["sandbox"] == "off"
    assert cfg.resolve(REPO).values["sandbox"] == "required"


def test_flags_beat_everything_even_null() -> None:
    cfg = precedence_config()
    resolved = cfg.resolve(REPO, {"model": None, "timeout": "5m"})
    assert resolved.values["model"] is None
    assert resolved.origins["model"] == "flag"
    assert resolved.values["timeout"] == "5m"


def test_limits_merge_key_by_key_with_origins() -> None:
    write_main(
        f"limits:\n  max_total_bytes: 9\nrepos:\n  {REPO}:\n    limits:\n      max_files: 7\n"
    )
    cfg = load_config(None, base_env())
    resolved = cfg.resolve(REPO, {"limits": {"max_file_bytes": 11}})
    assert resolved.values["limits"] == {
        **defaults()["limits"],
        "max_total_bytes": 9,
        "max_files": 7,
        "max_file_bytes": 11,
    }
    assert resolved.origins["limits.max_total_bytes"] == "config"
    assert resolved.origins["limits.max_files"] == "repo"
    assert resolved.origins["limits.max_file_bytes"] == "flag"
    assert resolved.origins["limits.hard_ceiling_bytes"] == "default"


def test_an_unknown_repo_gets_the_global_values() -> None:
    cfg = precedence_config()
    assert cfg.resolve("github.com/other/repo").values["model"] == "env"
    assert cfg.resolve(None).values["model"] == "env"


def test_repo_lookup_is_exact() -> None:
    cfg = precedence_config()
    assert cfg.resolve(REPO.upper()).values["model"] == "env"
    assert cfg.resolve(REPO + "/").values["model"] == "env"


def test_run_pr_code_is_per_repo_with_defaults_filled() -> None:
    write_main(f"repos:\n  {REPO}:\n    run_pr_code:\n      command: [npm, test]\n")
    cfg = load_config(None, base_env())
    assert cfg.resolve().values["run_pr_code"] is None
    code = cfg.resolve(REPO).values["run_pr_code"]
    assert code == {
        "command": ["npm", "test"],
        "sandbox": "required",
        "timeout": "300s",
        "max_output_bytes": 1048576,
        "max_workspace_bytes": 2147483648,
        "env_allow": [],
        "ro_binds": [],
    }
    assert cfg.resolve(REPO).origins["run_pr_code"] == "repo"


def test_repo_lists_replace_global_lists() -> None:
    write_main(f"setting_sources: [user]\nrepos:\n  {REPO}:\n    setting_sources: []\n")
    assert load_config(None, base_env()).resolve(REPO).values["setting_sources"] == []


def test_a_flag_value_is_validated() -> None:
    cfg = load(None, {})
    for flags in ({"timeout": "sk-live-123"}, {"timeout": "0"}, {"mode": "x"}, {"max_turns": True}):
        with pytest.raises(ReviewRunError) as info:
            cfg.resolve(None, flags)
        assert info.value.code == "usage"
        assert "sk-live-123" not in json.dumps(info.value.envelope())
    with pytest.raises(ReviewRunError) as info:
        cfg.resolve(None, {"nope": 1})
    assert info.value.code == "usage"


def test_resolve_returns_independent_copies() -> None:
    cfg = load(None, {})
    cfg.resolve().values["env_allowlist"].append("X")
    cfg.resolve().values["limits"]["max_files"] = 1
    assert cfg.resolve().values["env_allowlist"] == ["PATH"]
    assert cfg.resolve().values["limits"]["max_files"] == 300


def test_repo_entries_are_checked_at_load_even_when_unused() -> None:
    err = fail(f"repos:\n  {REPO}:\n    timeout: 0s\n")
    assert err.details == {"key": f"repos.{REPO}.timeout"}


def test_semantic_duration_checks() -> None:
    assert fail("timeout: 0\n").details == {"key": "timeout"}
    assert fail("timeout: 99999999999999999999d\n").details == {"key": "timeout"}
    assert fail("hooks:\n  post-run:\n    - cmd: x\n      timeout: 0s\n").details == {
        "key": "hooks.post-run[0].timeout"
    }
    assert fail(
        f"repos:\n  {REPO}:\n    run_pr_code:\n      command: [x]\n      timeout: 0\n"
    ).details == {"key": f"repos.{REPO}.run_pr_code.timeout"}
    cfg = load("escalation_ttl: null\nretention: '0'\n")
    assert cfg.resolve().values["escalation_ttl"] is None
