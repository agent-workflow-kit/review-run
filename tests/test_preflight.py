"""Run order steps 1 and 2 before any allocation (issue #16).

Most tests run the real fake siblings through the real gateway (``Routed``); table-driven ones
call the mapping functions directly.
"""

# ruff: noqa: E501

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from review_run import gateway as gw
from review_run import preflight as pre
from review_run import settings as rs
from review_run.config import Config, load_config
from review_run.errors import ReviewRunError
from review_run.gateway import CompletedRun, ToolMissing
from review_run.store import Expect, NewRun, Store, lease_name
from tests.fakebin._common import VERSIONS
from tests.fakes import FakeBin

REPO_PLUGIN = Path(__file__).resolve().parent.parent / "plugin"
PR_URL = "https://github.com/o/r/pull/1"
PR = "github.com/o/r#1"
REPO = "github.com/o/r"


class Routed:
    """Send sibling names to the fake wrappers; names in ``missing`` are not on ``PATH``."""

    def __init__(self, fb: FakeBin) -> None:
        self.fb = fb
        self.missing: set[str] = set()
        self.calls: list[tuple[str, ...]] = []
        self.envs: list[dict[str, str] | None] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        stdin: str | None = None,
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
    ) -> CompletedRun:
        self.calls.append(tuple(argv))
        self.envs.append(None if env is None else dict(env))
        if (
            tuple(argv[1:]) == ("--version",)
            and argv[0] not in self.missing
            and argv[0] in VERSIONS
            and "--version" not in self.fb.scenario.get(argv[0], {})
        ):  # an unscripted version probe is answered in-process (a spawn costs ~0.1 s)
            return CompletedRun(tuple(argv), 0, f"{argv[0]} {VERSIONS[argv[0]]}\n", "")
        if argv[0] in self.missing:
            raise ToolMissing(f"{argv[0]} not found")
        child = self.fb.env()
        if env is not None and rs.DEPTH_VARIABLE in env:
            child[rs.DEPTH_VARIABLE] = env[rs.DEPTH_VARIABLE]
        return gw.SystemGateway().run(
            [self.fb.argv0(argv[0]), *argv[1:]],
            timeout=timeout,
            stdin=stdin,
            env=child,
            cwd=str(self.fb.dir),
        )

    def tools_called(self) -> list[str]:
        return [c[0] for c in self.calls]


@pytest.fixture
def routed(fakebin: FakeBin) -> Iterator[Routed]:
    router = Routed(fakebin)
    previous = gw.set_gateway(router)
    try:
        yield router
    finally:
        gw.set_gateway(previous)


@pytest.fixture
def plugin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    copy = tmp_path / "bundled-plugin"
    shutil.copytree(REPO_PLUGIN, copy)
    for item in [copy, *copy.rglob("*")]:
        item.chmod(0o755 if item.is_dir() else 0o644)
    monkeypatch.setattr(rs, "bundled_plugin_dir", lambda: copy)
    return copy


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "state"


def config_of(tmp_path: Path, text: str = "") -> Config:
    path = tmp_path / "config.yaml"
    path.write_text(text or "skill: review-run:review-pr\n", encoding="utf-8")
    path.chmod(0o600)
    return load_config(path)


def extra_plugin(tmp_path: Path, name: str = "other", skill: str = "deep") -> Path:
    base = tmp_path / f"plugin-{name}"
    (base / ".claude-plugin").mkdir(parents=True)
    (base / ".claude").mkdir()
    (base / "skills" / skill).mkdir(parents=True)
    (base / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": name}), "utf-8")
    (base / "skills" / skill / "SKILL.md").write_text("s", encoding="utf-8")
    for placeholder in (".mcp.json", "CLAUDE.md", "CLAUDE.local.md"):
        (base / placeholder).write_text("", encoding="utf-8")
    for item in [base, *base.rglob("*")]:
        item.chmod(0o755 if item.is_dir() else 0o644)
    return base


def request(**kw: Any) -> pre.RunRequest:
    return pre.RunRequest(pr=kw.pop("pr", PR_URL), **kw)


def fails(fn: Any, *args: Any, **kw: Any) -> ReviewRunError:
    with pytest.raises(ReviewRunError) as info:
        fn(*args, **kw)
    return info.value


def nothing_allocated(root: Path) -> None:
    """No id, no run record, no run directory content, no database rows."""
    if (root / "state.db").exists():
        with Store.open(root) as store:
            assert store._conn.execute("SELECT count(*) FROM ids").fetchone()[0] == 0
            assert store._conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
    runs = root / "runs"
    assert not runs.exists() or list(runs.iterdir()) == []
    tmp = root / "tmp"
    assert not tmp.exists() or list(tmp.iterdir()) == []


# -- labels -------------------------------------------------------------------------------------


def test_labels_are_accepted_as_a_multiset() -> None:
    assert pre.check_labels(["a=b", "a=b", "k.x_y-z=v:/#@+,="]) == (
        "a=b",
        "a=b",
        "k.x_y-z=v:/#@+,=",
    )
    assert pre.check_labels(["e="]) == ("e=",)


@pytest.mark.parametrize("key", ["owner", "run", "pr", "repo", "agent", "kind"])
def test_a_reserved_label_key_is_label_reserved(key: str) -> None:
    err = fails(pre.check_labels, ["ok=1", f"{key}=x"])
    assert (err.code, err.reason, err.exit_code) == ("usage", "label_reserved", 2)


def test_sixty_labels_pass_and_sixty_one_are_too_many() -> None:
    assert len(pre.check_labels([f"k{i}=v" for i in range(60)])) == 60
    err = fails(pre.check_labels, [f"k{i}=v" for i in range(61)])
    assert (err.code, err.reason, err.details, err.exit_code) == (
        "usage", "too_many_labels", {"max": 60}, 2,
    )  # fmt: skip


@pytest.mark.parametrize(
    "bad", ["novalue-ZZ", "=ZZ", "UPPER=ZZ", "-a=ZZ", "a=has ZZ space", "a=ZZ\u202e",
            "a=ZZ" + "x" * 257, "k" * 65 + "=ZZ", "a=ZZ\nbreak", "a=ZZ\x00"],
)  # fmt: skip
def test_a_malformed_label_is_usage_and_is_never_echoed(bad: str) -> None:
    err = fails(pre.check_labels, [bad])
    assert err.code == "usage" and err.reason is None
    assert bad not in json.dumps(err.envelope()) and bad not in err.human()


# -- --override-escalation ------------------------------------------------------------------------


def test_override_needs_a_human() -> None:
    off = request()
    on = request(override_escalation=True)
    pre.check_override(off, stdin_is_tty=False)
    assert fails(pre.check_override, on, stdin_is_tty=False).exit_code == 2
    assert fails(pre.check_override, request(override_escalation=True, actor="auto:x"),
                 stdin_is_tty=False).exit_code == 2  # fmt: skip
    pre.check_override(request(override_escalation=True, actor="human:me"), stdin_is_tty=False)
    pre.check_override(on, stdin_is_tty=True)


# -- --previous -----------------------------------------------------------------------------------


def done_run(root: Path, state: str = "done", pr: str = PR) -> str:
    with Store.open(root) as store:
        run_id = store.reserve_id()
        store.create_run(NewRun(run_id, pr, REPO, "a" * 40, "headless", "review-run:review-pr"))
        if state == "running":
            return run_id
        lease = store.acquire_lease(lease_name(run_id))
        assert lease is not None
        assert store.begin_finalize(run_id, Expect("running", "prefetch", None), lease=lease)
        reason, code = {
            "done": (None, 0),
            "failed": ("internal", 12),
            "escalated": ("blocked", 13),
        }[state]
        assert store.end_run(
            run_id, Expect("running", "finalize", None), lease=lease, state=state,
            reason=reason, exit_code=code,
        )  # fmt: skip
    return run_id


def test_previous_unknown_is_exit_10_even_with_no_state(root: Path) -> None:
    err = fails(pre.check_previous, root, "rr-abcd")
    assert (err.code, err.exit_code) == ("not_found", 10)
    assert not root.exists()  # nothing created by the check


def test_previous_short_or_malformed_is_usage(root: Path) -> None:
    assert fails(pre.check_previous, root, "abc").exit_code == 2
    assert fails(pre.check_previous, root, "!!!!").exit_code == 2


def test_previous_unknown_in_an_existing_store_is_exit_10(root: Path) -> None:
    done_run(root)
    assert fails(pre.check_previous, root, "rr-zzzz").exit_code == 10


def test_previous_done_is_returned(root: Path) -> None:
    run_id = done_run(root)
    assert pre.check_previous(root, run_id[3:7]).run_id == run_id


@pytest.mark.parametrize("state", ["running", "failed", "escalated"])
def test_previous_not_done_is_exit_2_with_the_state(root: Path, state: str) -> None:
    run_id = done_run(root, state)
    err = fails(pre.check_previous, root, run_id)
    assert (err.code, err.reason, err.details, err.exit_code) == (
        "usage", "previous_not_done", {"state": state}, 2,
    )  # fmt: skip


# -- tools and versions ---------------------------------------------------------------------------


def test_parse_version() -> None:
    assert pre.parse_version("agent-spawn 0.38.1\n") == (0, 38, 1)
    assert pre.parse_version("claude-session v1.2.3\nmore") == (1, 2, 3)
    for bad in ("", "\n", "agent-spawn", "agent-spawn 1.2", "1.2.3", "x " + "9" * 7 + ".1.1"):
        assert pre.parse_version(bad) is None


@pytest.mark.parametrize(
    "bad",
    [
        "agent-spawn 0.38.1-rc1",
        "agent-spawn 0.38.1+build",
        "agent-spawn 0.38.1234567",
        "agent-spawn 0.38.1.4",
        "agent-spawn 0.38.1 extra",
        "agent-spawn  0.38.1",
        "agent-spawn V0.38.1",
    ],
)
def test_parse_version_refuses_what_it_would_have_to_truncate_or_guess(bad: str) -> None:
    assert pre.parse_version(bad) is None


def test_parse_version_compares_the_tool_name() -> None:
    assert pre.parse_version("agent-spawn 0.38.1", "agent-spawn") == (0, 38, 1)
    assert pre.parse_version("claude-session 0.45.2", "agent-spawn") is None


def test_a_version_line_naming_another_tool_fails_closed(fakebin: FakeBin, routed: Routed) -> None:
    fakebin.script("agent-spawn", "--version", {"stdout_raw": "claude-session 0.99.0\n"})
    err = fails(pre.check_tools)
    assert (err.reason, err.tool, err.lower_code) == (
        "lower_failed",
        "agent-spawn",
        "invalid_output",
    )


def test_a_prerelease_version_of_a_checked_tool_fails_closed(
    fakebin: FakeBin, routed: Routed
) -> None:
    fakebin.script("claude-session", "--version", {"stdout_raw": "claude-session 0.45.2-rc1\n"})
    err = fails(pre.check_tools)
    assert (err.reason, err.tool, err.lower_code) == (
        "lower_failed",
        "claude-session",
        "invalid_output",
    )


def test_a_version_probe_through_a_real_fake_sibling_spawns_and_parses(
    fakebin: FakeBin, routed: Routed
) -> None:
    """One real spawn: the fake wrapper's own ``--version`` output is what the parser reads."""
    done = gw.SystemGateway().run(
        [fakebin.argv0("agent-spawn"), "--version"],
        timeout=15.0,
        env=fakebin.env(),
        cwd=str(fakebin.dir),
    )
    assert pre.parse_version(done.stdout, "agent-spawn") == (0, 38, 1)


def test_every_required_tool_is_resolved_and_passes(fakebin: FakeBin, routed: Routed) -> None:
    versions = pre.check_tools()
    assert set(versions) == set(pre.REQUIRED_TOOLS)
    assert routed.tools_called() == list(pre.REQUIRED_TOOLS)


@pytest.mark.parametrize("tool", pre.REQUIRED_TOOLS)
def test_a_missing_tool_is_exit_12_with_the_install_command(
    fakebin: FakeBin, routed: Routed, tool: str
) -> None:
    routed.missing.add(tool)
    err = fails(pre.check_tools)
    assert (err.code, err.reason, err.tool, err.exit_code) == ("failed", "tool_missing", tool, 12)
    assert err.details == {
        "install": f"uv tool install git+https://github.com/agent-workflow-kit/{tool}"
    }
    assert pre.install_command(tool) in err.message


def test_the_first_missing_tool_in_order_is_reported(fakebin: FakeBin, routed: Routed) -> None:
    routed.missing.update({"claude-session", "pr-comment"})
    assert fails(pre.check_tools).tool == "pr-comment"


@pytest.mark.parametrize(
    ("tool", "found"),
    [("agent-spawn", "0.38.0"), ("agent-spawn", "0.1.9"), ("claude-session", "0.45.1")],
)
def test_a_tool_older_than_the_minimum_is_tool_too_old(
    fakebin: FakeBin, routed: Routed, tool: str, found: str
) -> None:
    fakebin.script(tool, "--version", {"version": found})
    err = fails(pre.check_tools)
    minimum = ".".join(map(str, pre.MIN_VERSIONS[tool]))
    assert (err.code, err.reason, err.tool, err.exit_code) == ("failed", "tool_too_old", tool, 12)
    assert err.details is not None
    assert err.details["found"] == found and err.details["minimum"] == minimum


@pytest.mark.parametrize("found", ["0.38.1", "0.38.2", "0.39.0", "1.0.0", "12.0.0"])
def test_the_minimum_and_newer_versions_pass(fakebin: FakeBin, routed: Routed, found: str) -> None:
    fakebin.script("agent-spawn", "--version", {"version": found})
    pre.check_tools()


@pytest.mark.parametrize("raw", ["garbage\n", "", "agent-spawn\n", "agent-spawn 0.38\n"])
def test_an_unparsable_version_of_a_checked_tool_fails_closed(
    fakebin: FakeBin, routed: Routed, raw: str
) -> None:
    fakebin.script("claude-session", "--version", {"stdout_raw": raw})
    err = fails(pre.check_tools)
    assert (err.reason, err.lower_code, err.tool) == (
        "lower_failed",
        "invalid_output",
        "claude-session",
    )


def test_a_version_call_that_fails_is_lower_failed(fakebin: FakeBin, routed: Routed) -> None:
    fakebin.script("forgectl", "--version", {"stdout_raw": "", "exit": 3})
    err = fails(pre.check_tools)
    assert (err.reason, err.tool, err.exit_code) == ("lower_failed", "forgectl", 12)


def test_a_version_timeout_is_lower_failed_with_its_code(fake_gateway: Any) -> None:
    from tests.fake_gateway import FakeGateway  # noqa: F401

    fake_gateway.script("forgectl", gw.CallTimeout("t"))
    err = fails(pre.check_tools)
    assert (err.reason, err.lower_code) == ("lower_failed", "timeout")


# -- the state root -------------------------------------------------------------------------------


def test_a_missing_state_root_passes_and_is_not_created(root: Path) -> None:
    pre.check_state_root(root)
    assert not root.exists()


def test_a_symlinked_state_root_is_state_unsafe(tmp_path: Path, root: Path) -> None:
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    root.symlink_to(real)
    err = fails(pre.check_state_root, root)
    assert (err.code, err.reason, err.exit_code) == ("failed", "state_unsafe", 12)


def test_a_state_root_that_is_a_file_or_foreign_is_state_unsafe(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root.write_text("x", encoding="utf-8")
    assert fails(pre.check_state_root, root).reason == "state_unsafe"
    root.unlink()
    root.mkdir(mode=0o700)
    pre.check_state_root(root)
    real = os.geteuid()
    monkeypatch.setattr(os, "geteuid", lambda: real + 1)
    assert fails(pre.check_state_root, root).reason == "state_unsafe"


# -- skill resolution and the plugin directories --------------------------------------------------


def test_the_bundled_skill_resolves_to_the_bundled_plugin(plugin: Path, tmp_path: Path) -> None:
    assert pre.resolve_skill("review-run:review-pr", []) is None
    assert pre.plugin_dirs_for("review-run:review-pr", []) == (str(plugin.resolve()),)


def test_an_extra_plugin_skill_passes_the_bundled_and_that_plugin_only(
    plugin: Path, tmp_path: Path
) -> None:
    other = extra_plugin(tmp_path)
    third = extra_plugin(tmp_path, "third", "x")
    dirs = [str(other.resolve()), str(third.resolve())]
    assert pre.resolve_skill("other:deep", dirs) == str(other.resolve())
    assert pre.plugin_dirs_for("other:deep", dirs) == tuple(
        sorted([str(plugin.resolve()), str(other.resolve())])
    )


@pytest.mark.parametrize(
    "skill",
    ["review-pr", "nope:deep", "other:nope", "review-run:nope", ":deep", "other:", "a:b:c",
     "other:../deep", "other:de/ep", "../x:deep", "other:deep\n"],
)  # fmt: skip
def test_an_unknown_skill_is_skill_not_found_exit_2(
    plugin: Path, tmp_path: Path, skill: str
) -> None:
    other = extra_plugin(tmp_path)
    err = fails(pre.resolve_skill, skill, [str(other.resolve())])
    assert (err.code, err.reason, err.exit_code) == ("usage", "skill_not_found", 2)


def test_a_skill_that_is_a_symlink_or_directory_is_not_found(plugin: Path, tmp_path: Path) -> None:
    other = extra_plugin(tmp_path)
    skill = other / "skills" / "deep" / "SKILL.md"
    skill.unlink()
    skill.symlink_to("/etc/hostname")
    assert (
        fails(pre.resolve_skill, "other:deep", [str(other.resolve())]).reason == "skill_not_found"
    )


def test_a_missing_placeholder_is_config_invalid_plugin_dirs(plugin: Path, tmp_path: Path) -> None:
    other = extra_plugin(tmp_path)
    for name in (".claude", ".mcp.json", "CLAUDE.md", "CLAUDE.local.md"):
        target = other / name
        hidden = other / f"{name}.off"
        target.rename(hidden)
        err = fails(pre.check_placeholders, [str(other)])
        assert (err.code, err.details, err.exit_code) == (
            "config_invalid",
            {"key": "plugin_dirs"},
            2,
        )
        hidden.rename(target)
    pre.check_placeholders([str(other)])


def test_the_bundled_plugin_dir_is_checked_for_placeholders_too(plugin: Path) -> None:
    (plugin / "CLAUDE.local.md").unlink()
    err = fails(pre.check_placeholders, pre.plugin_dirs_for("review-run:review-pr", []))
    assert err.details == {"key": "plugin_dirs"}


def test_hash_refusals_are_config_invalid_plugin_dirs_without_names(
    plugin: Path, tmp_path: Path
) -> None:
    (plugin / "SECRET-LINK").symlink_to("CLAUDE.md")
    err = fails(pre.hash_plugin_dirs, [str(plugin)])
    assert (err.code, err.details, err.exit_code) == ("config_invalid", {"key": "plugin_dirs"}, 2)
    assert "SECRET-LINK" not in err.message and "SECRET-LINK" not in err.human()


@pytest.mark.parametrize(
    "damage",
    ["symlink", "group_writable", "world_writable_dir", "fifo"],
)
def test_a_damaged_plugin_dir_refuses_the_run(
    fakebin: FakeBin, routed: Routed, plugin: Path, tmp_path: Path, root: Path, damage: str
) -> None:
    if damage == "symlink":
        (plugin / "skills" / "link").symlink_to("review-pr")
    elif damage == "group_writable":
        (plugin / "CLAUDE.md").chmod(0o664)
    elif damage == "world_writable_dir":
        (plugin / "skills").chmod(0o757)
    else:
        os.mkfifo(plugin / "pipe", 0o644)
    err = fails(pre.plan_run, request(), config_of(tmp_path), root)
    expected = (
        None if damage in ("group_writable", "world_writable_dir") else {"key": "plugin_dirs"}
    )
    assert (err.code, err.details, err.exit_code) == ("config_invalid", expected, 2)
    assert "agent-spawn" not in routed.tools_called()[4:]  # the dry run never started
    nothing_allocated(root)


# -- agent-spawn's dry run mapped -----------------------------------------------------------------


def plan_doc(**kw: Any) -> str:
    return json.dumps({"dry_run": True, "would_refuse": None, **kw})


def envelope(code: str, **kw: Any) -> str:
    return json.dumps({"error": {"code": code, "message": "m", **kw}})


def test_dry_run_exit_0_continues() -> None:
    pre.map_dry_run(0, plan_doc())


@pytest.mark.parametrize("stdout", ["", "nope", "[]", "null", '{"a": 1, "a": 2}', "NaN"])
def test_dry_run_exit_0_with_unparsable_output_is_lower_failed(stdout: str) -> None:
    err = fails(pre.map_dry_run, 0, stdout)
    assert (err.reason, err.lower_code, err.tool, err.exit_code) == (
        "lower_failed", "invalid_output", "agent-spawn", 12,
    )  # fmt: skip


@pytest.mark.parametrize(
    ("reason", "tool"),
    [("third_party_cwd", "agent-spawn"), ("live", "agent-spawn"), ("tainted", "claude-session")],
)
def test_dry_run_exit_20_is_lower_refused(reason: str, tool: str) -> None:
    err = fails(pre.map_dry_run, 20, plan_doc(would_refuse=reason))
    assert (err.code, err.reason, err.lower_code, err.tool, err.exit_code) == (
        "refused", "lower_refused", reason, tool, 20,
    )  # fmt: skip


@pytest.mark.parametrize("stdout", ["", "x", plan_doc(), plan_doc(would_refuse="Bad Reason")])
def test_dry_run_exit_20_without_a_usable_reason_is_lower_failed(stdout: str) -> None:
    assert fails(pre.map_dry_run, 20, stdout).reason == "lower_failed"


@pytest.mark.parametrize("code", [2, 12])
def test_a_termctl_envelope_is_no_tmux(code: int) -> None:
    err = fails(
        pre.map_dry_run, code, envelope("failed", tool="termctl", lower_code="tmux_missing")
    )
    assert (err.code, err.reason, err.tool, err.lower_code, err.exit_code) == (
        "failed", "no_tmux", "termctl", "tmux_missing", 12,
    )  # fmt: skip


def test_agent_spawns_tool_missing_passes_through() -> None:
    err = fails(pre.map_dry_run, 12, envelope("tool_missing", tool="claude-session"))
    assert (err.reason, err.tool, err.exit_code) == ("tool_missing", "claude-session", 12)
    assert "claude-session" in err.message
    err = fails(pre.map_dry_run, 12, envelope("tool_missing"))
    assert err.tool == "agent-spawn"


@pytest.mark.parametrize(
    ("code", "stdout"),
    [
        (13, envelope("escalated", hook="pre-launch")),
        (11, envelope("vetoed")),
        (2, envelope("usage")),
        (1, envelope("internal")),
        (12, envelope("failed", tool="agent-spawn")),
        (12, "unparsable"),
        (3, ""),
        (-9, ""),
    ],
)
def test_every_other_exit_is_lower_failed(code: int, stdout: str) -> None:
    err = fails(pre.map_dry_run, code, stdout)
    assert (err.code, err.reason, err.tool, err.exit_code) == (
        "failed", "lower_failed", "agent-spawn", 12,
    )  # fmt: skip


def test_lower_text_is_never_copied() -> None:
    hostile = "PLANTED-MARKER \x1b[31m"
    stdout = envelope(
        "failed", tool="termctl", lower_code="tmux_missing", lower_error={"m": hostile}
    )
    err = fails(pre.map_dry_run, 12, stdout)
    assert hostile not in json.dumps(err.envelope()) and hostile not in err.human()
    err = fails(pre.map_dry_run, 12, json.dumps({"error": {"code": hostile, "message": hostile}}))
    assert hostile not in json.dumps(err.envelope())
    assert err.lower_code == "failed"


@pytest.mark.parametrize(
    "stdout",
    [
        "{}",
        json.dumps({"dry_run": False, "would_refuse": None}),
        json.dumps({"dry_run": 1, "would_refuse": None}),
        json.dumps({"dry_run": True, "would_refuse": "third_party_cwd"}),
        json.dumps({"dry_run": True, "would_refuse": "", "x": 1}),
        json.dumps({"dry_run": True, "would_refuse": None, "error": {"code": "x"}}),
        '{"dry_run": true, "would_refuse": null, "n": 1e999}',
    ],
)
def test_dry_run_exit_0_needs_a_clean_plan(stdout: str) -> None:
    err = fails(pre.map_dry_run, 0, stdout)
    assert (err.reason, err.lower_code, err.tool) == (
        "lower_failed",
        "invalid_output",
        "agent-spawn",
    )


def test_dry_run_exit_20_with_a_non_finite_number_is_lower_failed() -> None:
    stdout = '{"dry_run": true, "would_refuse": "live", "n": 1e999}'
    assert fails(pre.map_dry_run, 20, stdout).reason == "lower_failed"


def test_a_failed_dry_run_keeps_the_envelopes_tool_and_lower_code() -> None:
    err = fails(
        pre.map_dry_run, 12, envelope("failed", tool="claude-session", lower_code="start_failed")
    )
    assert (err.reason, err.tool, err.lower_code) == (
        "lower_failed",
        "claude-session",
        "start_failed",
    )
    err = fails(pre.map_dry_run, 12, envelope("failed", tool="claude-session"))
    assert (err.tool, err.lower_code) == ("claude-session", "failed")
    err = fails(pre.map_dry_run, 12, envelope("failed", tool="evil tool", lower_code="Bad Code"))
    assert (err.tool, err.lower_code) == ("agent-spawn", "failed")


# -- the whole of steps 1 and 2 -------------------------------------------------------------------


def test_a_clean_plan_allocates_nothing(
    fakebin: FakeBin, routed: Routed, plugin: Path, tmp_path: Path, root: Path
) -> None:
    plan = pre.plan_run(request(), config_of(tmp_path), root)
    assert (plan.pr, plan.repo, plan.mode, plan.skill) == (
        PR,
        REPO,
        "headless",
        "review-run:review-pr",
    )
    assert plan.settings.plugin_dirs == (str(plugin.resolve()),)
    assert plan.profile.kind == "bundled" and plan.warnings == ()
    nothing_allocated(root)
    assert not (root / "state.db").exists()
    # the dry run saw the planned flags and no prompt or input file
    launch = fakebin.calls("agent-spawn", "launch")
    assert len(launch) == 1 and "--dry-run" in launch[0].argv
    assert "--prompt-file" not in launch[0].argv and "--input-file" not in launch[0].argv
    assert "--wait" not in launch[0].argv
    assert ["--skill", "review-run:review-pr"] == launch[0].argv[
        launch[0].argv.index("--skill") : launch[0].argv.index("--skill") + 2
    ]


def test_the_order_of_calls_is_tools_then_resolve_then_dry_run(
    fakebin: FakeBin, routed: Routed, plugin: Path, tmp_path: Path, root: Path
) -> None:
    pre.plan_run(request(), config_of(tmp_path), root)
    names = [c[:2] for c in routed.calls]
    assert [n[0] for n in names] == [
        "forgectl",
        "pr-comment",
        "agent-spawn",
        "claude-session",  # step 1 (b)
        "forgectl",  # resolve
        "agent-spawn",  # dry run
    ]
    assert routed.calls[4][1] == "show" and routed.calls[5][1] == "launch"


def test_the_plan_is_deterministic_apart_from_nothing(
    fakebin: FakeBin, routed: Routed, plugin: Path, tmp_path: Path, root: Path
) -> None:
    config = config_of(tmp_path)
    a = pre.plan_run(request(), config, root)
    b = pre.plan_run(request(), config, root)
    assert a.settings.to_bytes() == b.settings.to_bytes()


@pytest.mark.parametrize(
    ("req", "reason"),
    [
        (request(labels=("owner=x",)), "label_reserved"),
        (request(labels=tuple(f"k{i}=v" for i in range(61))), "too_many_labels"),
    ],
)
def test_usage_failures_start_no_process(
    fakebin: FakeBin, routed: Routed, plugin: Path, tmp_path: Path, root: Path,
    req: pre.RunRequest, reason: str,
) -> None:  # fmt: skip
    err = fails(pre.plan_run, req, config_of(tmp_path), root)
    assert (err.exit_code, err.reason) == (2, reason)
    assert routed.calls == []
    nothing_allocated(root)


@pytest.mark.parametrize(
    "req",
    [
        request(pr="not a pr"),
        request(pr="-x#1"),
        request(override_escalation=True),
        request(model="-bad"),
        request(timeout="0"),
        request(mode="fast"),
        request(skill="x" * 200),
    ],
)
def test_other_usage_failures_start_no_process(
    fakebin: FakeBin, routed: Routed, plugin: Path, tmp_path: Path, root: Path, req: pre.RunRequest
) -> None:
    err = fails(pre.plan_run, req, config_of(tmp_path), root)
    assert err.exit_code == 2
    assert routed.calls == []


def test_previous_is_checked_before_any_tool_runs(
    fakebin: FakeBin, routed: Routed, plugin: Path, tmp_path: Path, root: Path
) -> None:
    run_id = done_run(root, "failed")
    err = fails(pre.plan_run, request(previous=run_id), config_of(tmp_path), root)
    assert err.reason == "previous_not_done" and routed.calls == []
    err = fails(pre.plan_run, request(previous="rr-zzzz"), config_of(tmp_path), root)
    assert err.exit_code == 10 and routed.calls == []


def test_tool_too_old_stops_before_the_resolve_step_and_allocates_nothing(
    fakebin: FakeBin, routed: Routed, plugin: Path, tmp_path: Path, root: Path
) -> None:
    fakebin.script("agent-spawn", "--version", {"version": "0.1.0"})
    err = fails(pre.plan_run, request(), config_of(tmp_path), root)
    assert (err.reason, err.exit_code) == ("tool_too_old", 12)
    assert fakebin.calls("forgectl", "show") == []
    nothing_allocated(root)
    assert not root.exists()


def test_a_missing_tool_stops_before_the_resolve_step(
    fakebin: FakeBin, routed: Routed, plugin: Path, tmp_path: Path, root: Path
) -> None:
    routed.missing.add("claude-session")
    err = fails(pre.plan_run, request(), config_of(tmp_path), root)
    assert (err.reason, err.tool) == ("tool_missing", "claude-session")
    assert fakebin.calls("forgectl", "show") == []


def test_an_unsafe_state_root_stops_before_the_resolve_step(
    fakebin: FakeBin, routed: Routed, plugin: Path, tmp_path: Path, root: Path
) -> None:
    real = tmp_path / "real"
    real.mkdir()
    root.symlink_to(real)
    err = fails(pre.plan_run, request(), config_of(tmp_path), root)
    assert err.reason == "state_unsafe"
    assert fakebin.calls("forgectl", "show") == []
    assert list(real.iterdir()) == []


def real_store_dir(tmp_path: Path) -> Path:
    """A real state dir holding a valid store at the current schema, so that opening it
    would succeed (and could migrate or touch it) if the root safety check were skipped."""
    real = tmp_path / "real"
    real.mkdir()
    Store.open(real).close()
    assert (real / "state.db").is_file()
    return real


def snapshot(real: Path) -> tuple[bytes, int, list[str]]:
    db = real / "state.db"
    return db.read_bytes(), db.stat().st_mtime_ns, sorted(p.name for p in real.iterdir())


def test_a_missing_tool_beats_an_unsafe_state_root(
    fakebin: FakeBin, routed: Routed, plugin: Path, tmp_path: Path, root: Path
) -> None:
    real = real_store_dir(tmp_path)
    before = snapshot(real)
    root.symlink_to(real)
    routed.missing.add("pr-comment")
    err = fails(pre.plan_run, request(previous="rr-aaaa"), config_of(tmp_path), root)
    assert (err.reason, err.tool) == ("tool_missing", "pr-comment")
    assert snapshot(real) == before  # the store was never opened or migrated


def test_an_unknown_previous_under_an_unsafe_state_root_is_state_unsafe(
    fakebin: FakeBin,
    routed: Routed,
    plugin: Path,
    tmp_path: Path,
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = real_store_dir(tmp_path)
    before = snapshot(real)
    root.symlink_to(real)
    forbid_store_open(monkeypatch)
    err = fails(pre.plan_run, request(previous="rr-aaaa"), config_of(tmp_path), root)
    assert err.reason == "state_unsafe"
    assert snapshot(real) == before


def test_a_malformed_previous_under_an_unsafe_state_root_is_still_usage(
    fakebin: FakeBin, routed: Routed, plugin: Path, tmp_path: Path, root: Path
) -> None:
    root.symlink_to(tmp_path)
    err = fails(pre.plan_run, request(previous="x"), config_of(tmp_path), root)
    assert err.exit_code == 2 and routed.calls == []


def forbid_store_open(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: Any, **kw: Any) -> Any:
        raise AssertionError("Store.open was called under an unsafe state root")

    monkeypatch.setattr(pre.Store, "open", boom)


def test_check_previous_does_not_open_a_store_under_an_unsafe_root(
    tmp_path: Path, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = real_store_dir(tmp_path)
    before = snapshot(real)
    root.symlink_to(real)
    forbid_store_open(monkeypatch)
    assert fails(pre.check_previous, root, "rr-aaaa").reason == "state_unsafe"
    assert snapshot(real) == before


@pytest.mark.parametrize(
    ("scenario", "code"),
    [("not_found", 10), ("unknown_host", 20), ("merged", 20), ("closed", 20)],
)
def test_resolve_failures_keep_their_exit_codes_and_start_no_dry_run(
    fakebin: FakeBin, routed: Routed, plugin: Path, tmp_path: Path, root: Path,
    scenario: str, code: int,
) -> None:  # fmt: skip
    fakebin.use("forgectl", scenario)
    err = fails(pre.plan_run, request(), config_of(tmp_path), root)
    assert err.exit_code == code
    assert fakebin.calls("agent-spawn", "launch") == []
    nothing_allocated(root)


def test_pr_too_long_is_usage_after_resolve(
    fakebin: FakeBin, routed: Routed, plugin: Path, tmp_path: Path, root: Path
) -> None:
    long = "x" * 260
    fakebin.configure("forgectl", hosts=["github.com"])
    fakebin.script(
        "forgectl", "show",
        {"merge": {"pr": f"github.com/o/{long}#1", "repo": f"github.com/o/{long}"}},
    )  # fmt: skip
    err = fails(pre.plan_run, request(), config_of(tmp_path), root)
    assert (err.code, err.reason, err.exit_code) == ("usage", "pr_too_long", 2)
    assert fakebin.calls("agent-spawn", "launch") == []


def test_previous_of_another_pr_is_previous_mismatch(
    fakebin: FakeBin, routed: Routed, plugin: Path, tmp_path: Path, root: Path
) -> None:
    run_id = done_run(root, "done", pr="github.com/o/r#2")
    err = fails(pre.plan_run, request(previous=run_id), config_of(tmp_path), root)
    assert (err.code, err.reason, err.exit_code) == ("usage", "previous_mismatch", 2)
    assert fakebin.calls("agent-spawn", "launch") == []


def test_previous_of_the_same_pr_is_kept_in_the_plan(
    fakebin: FakeBin, routed: Routed, plugin: Path, tmp_path: Path, root: Path
) -> None:
    run_id = done_run(root)
    plan = pre.plan_run(request(previous=run_id), config_of(tmp_path), root)
    assert plan.previous is not None and plan.previous.run_id == run_id


def test_unknown_skill_is_skill_not_found_before_the_dry_run(
    fakebin: FakeBin, routed: Routed, plugin: Path, tmp_path: Path, root: Path
) -> None:
    err = fails(pre.plan_run, request(skill="nope:nope"), config_of(tmp_path), root)
    assert err.reason == "skill_not_found"
    assert fakebin.calls("agent-spawn", "launch") == []


def test_a_bare_skill_name_is_skill_not_found(
    fakebin: FakeBin, routed: Routed, plugin: Path, tmp_path: Path, root: Path
) -> None:
    assert fails(pre.plan_run, request(skill="review-pr"), config_of(tmp_path), root).reason == (
        "skill_not_found"
    )


def test_an_extra_plugin_skill_is_planned_with_both_directories(
    fakebin: FakeBin, routed: Routed, plugin: Path, tmp_path: Path, root: Path
) -> None:
    other = extra_plugin(tmp_path)
    config = config_of(tmp_path, f"plugin_dirs: ['{other}']\n")
    plan = pre.plan_run(request(skill="other:deep"), config, root)
    assert set(plan.settings.plugin_dirs) == {str(plugin.resolve()), str(other.resolve())}
    argv = fakebin.calls("agent-spawn", "launch")[0].argv
    assert argv.count("--plugin-dir") == 2


def test_wtctl_is_required_only_when_the_repo_runs_pr_code(
    fakebin: FakeBin, routed: Routed, plugin: Path, tmp_path: Path, root: Path
) -> None:
    routed.missing.add("wtctl")
    pre.plan_run(request(), config_of(tmp_path), root)
    assert "wtctl" not in routed.tools_called()
    config = config_of(
        tmp_path, f"repos:\n  {REPO}:\n    run_pr_code:\n      command: [make, test]\n"
    )
    err = fails(pre.plan_run, request(), config, root)
    assert (err.reason, err.tool, err.exit_code) == ("tool_missing", "wtctl", 12)
    assert fakebin.calls("agent-spawn", "launch") == [fakebin.calls("agent-spawn", "launch")[0]]
    routed.missing.clear()
    pre.plan_run(request(), config, root)
    assert "wtctl" in routed.tools_called()


def test_another_repos_run_pr_code_does_not_require_wtctl(
    fakebin: FakeBin, routed: Routed, plugin: Path, tmp_path: Path, root: Path
) -> None:
    routed.missing.add("wtctl")
    config = config_of(
        tmp_path, "repos:\n  github.com/x/y:\n    run_pr_code:\n      command: [make]\n"
    )
    pre.plan_run(request(), config, root)


def test_flags_beat_the_repo_entry_and_the_origin_is_recorded(
    fakebin: FakeBin, routed: Routed, plugin: Path, tmp_path: Path, root: Path
) -> None:
    config = config_of(tmp_path, f"model: g\nrepos:\n  {REPO}:\n    model: r\n")
    plan = pre.plan_run(request(model="f", timeout="9m"), config, root)
    assert (plan.settings.value("model"), plan.settings.origin("model")) == ("f", "flag")
    assert plan.settings.value("timeout") == "9m"
    argv = fakebin.calls("agent-spawn", "launch")[0].argv
    assert argv[argv.index("--model") + 1] == "f" and argv[argv.index("--timeout") + 1] == "9m"
    plan = pre.plan_run(request(), config, root)
    assert (plan.settings.value("model"), plan.settings.origin("model")) == ("r", "repo")


@pytest.mark.parametrize(
    ("scenario", "kwargs", "reason", "code"),
    [
        ("dry_run_would_refuse", {}, "lower_refused", 20),
        ("dry_run_no_tmux", {}, "no_tmux", 12),
    ],
)
def test_the_dry_run_failures_end_the_plan(
    fakebin: FakeBin, routed: Routed, plugin: Path, tmp_path: Path, root: Path,
    scenario: str, kwargs: dict[str, Any], reason: str, code: int,
) -> None:  # fmt: skip
    fakebin.use("agent-spawn", scenario, **kwargs)
    err = fails(pre.plan_run, request(), config_of(tmp_path), root)
    assert (err.reason, err.exit_code) == (reason, code)
    nothing_allocated(root)


@pytest.mark.parametrize(
    ("exit_code", "code", "reason"),
    [
        (13, "escalated", "lower_failed"),
        (12, "failed", "lower_failed"),
        (2, "usage", "lower_failed"),
    ],
)
def test_other_dry_run_exits_are_lower_failed(
    fakebin: FakeBin, routed: Routed, plugin: Path, tmp_path: Path, root: Path,
    exit_code: int, code: str, reason: str,
) -> None:  # fmt: skip
    fakebin.use("agent-spawn", "dry_run_error", exit_code=exit_code, code=code)
    err = fails(pre.plan_run, request(), config_of(tmp_path), root)
    assert (err.reason, err.exit_code, err.tool) == (reason, 12, "agent-spawn")


def test_a_dry_run_tool_missing_envelope_passes_through(
    fakebin: FakeBin, routed: Routed, plugin: Path, tmp_path: Path, root: Path
) -> None:
    error = {"code": "tool_missing", "message": "m", "tool": "claude-session"}
    fakebin.script("agent-spawn", "launch --dry-run", {"error": error, "exit": 12})
    err = fails(pre.plan_run, request(), config_of(tmp_path), root)
    assert (err.reason, err.tool, err.exit_code) == ("tool_missing", "claude-session", 12)


def test_interactive_mode_asks_for_a_window_in_the_dry_run(
    fakebin: FakeBin, routed: Routed, plugin: Path, tmp_path: Path, root: Path
) -> None:
    pre.plan_run(request(mode="interactive"), config_of(tmp_path), root)
    argv = fakebin.calls("agent-spawn", "launch")[0].argv
    assert argv[argv.index("--mode") + 1] == "interactive"
    assert argv[argv.index("--placement") + 1] == "window"


def test_subagents_sets_the_depth_in_the_dry_runs_environment(
    fakebin: FakeBin, routed: Routed, plugin: Path, tmp_path: Path, root: Path
) -> None:
    config = config_of(tmp_path, "subagents: true\nmax_subagent_depth: 2\n")
    plan = pre.plan_run(request(), config, root)
    assert plan.profile.kind == "variant"
    call = fakebin.calls("agent-spawn", "launch")[0]
    assert call.env.get(rs.DEPTH_VARIABLE) == "2"
    assert rs.DEPTH_VARIABLE in call.argv


def test_a_custom_profile_with_subagents_warns(
    fakebin: FakeBin, routed: Routed, plugin: Path, tmp_path: Path, root: Path
) -> None:
    custom = tmp_path / "p.json"
    custom.write_text("{}", encoding="utf-8")
    custom.chmod(0o600)
    config = config_of(tmp_path, f"permission_profile: {custom}\nsubagents: true\n")
    plan = pre.plan_run(request(), config, root)
    assert plan.warnings == (rs.WARNING_CUSTOM_PROFILE_SUBAGENTS,)
    assert plan.profile.kind == "custom"


def test_no_untrusted_text_reaches_argv_or_env(
    fakebin: FakeBin, routed: Routed, plugin: Path, tmp_path: Path, root: Path
) -> None:
    marker = "PLANTED-MARKER-9c1e"
    fakebin.configure("forgectl", pr={"title": marker, "body": marker})
    pre.plan_run(request(), config_of(tmp_path), root)
    for call in fakebin.all_calls():
        assert marker not in " ".join(call.argv)
        assert marker not in json.dumps(call.env)


def test_the_dry_run_leaves_no_files_and_uses_private_modes(
    fakebin: FakeBin, routed: Routed, plugin: Path, tmp_path: Path, root: Path
) -> None:
    pre.plan_run(request(), config_of(tmp_path), root)
    assert (root.stat().st_mode & 0o777) == 0o700
    assert ((root / "tmp").stat().st_mode & 0o777) == 0o700
    assert list((root / "tmp").iterdir()) == []


def test_the_dry_runs_profile_file_is_the_effective_profile(
    fakebin: FakeBin, routed: Routed, plugin: Path, tmp_path: Path, root: Path
) -> None:
    seen: dict[str, bytes] = {}
    real_run = routed.run

    def spy(argv: Sequence[str], **kw: Any) -> CompletedRun:
        if argv[:2] == ["agent-spawn", "launch"]:
            path = argv[list(argv).index("--permission-profile") + 1]
            seen["data"] = Path(path).read_bytes()
            seen["mode"] = oct(Path(path).stat().st_mode & 0o777).encode()
        return real_run(argv, **kw)

    routed.run = spy  # type: ignore[method-assign]
    plan = pre.plan_run(request(), config_of(tmp_path), root)
    assert seen["data"] == plan.profile.data and seen["mode"] == b"0o600"


# -- the CLI wiring -------------------------------------------------------------------------------


def test_cli_run_reports_a_reserved_label_as_a_usage_envelope(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from tests.conftest import validate_against
    from tests.helpers import run_cli

    res = run_cli(capsys, "--json", "run", PR_URL, "--label", "run=x")
    assert res.code == 2
    doc = res.envelope()
    validate_against("cli/error.json", doc)
    assert doc["error"]["reason"] == "label_reserved"


def test_cli_run_passes_its_checks_then_stops_at_the_seam(
    capsys: pytest.CaptureFixture[str], fakebin: FakeBin, routed: Routed, plugin: Path
) -> None:
    from tests.helpers import run_cli

    res = run_cli(capsys, "--json", "run", PR_URL, "--label", "team=x", "--timeout", "10m")
    assert res.code == 1 and res.envelope()["error"]["code"] == "internal"
    launch = fakebin.calls("agent-spawn", "launch")[0].argv
    assert launch[launch.index("--timeout") + 1] == "10m"
    assert "team=x" in launch
    state = Path(os.environ["XDG_STATE_HOME"]) / "review-run"
    assert not (state / "state.db").exists()


def test_cli_run_prints_profile_warnings_on_stderr(
    capsys: pytest.CaptureFixture[str], fakebin: FakeBin, routed: Routed, plugin: Path,
    tmp_path: Path,
) -> None:  # fmt: skip
    from tests.helpers import run_cli

    custom = tmp_path / "p.json"
    custom.write_text("{}", encoding="utf-8")
    custom.chmod(0o600)
    cfg = tmp_path / "c.yaml"
    cfg.write_text(f"permission_profile: {custom}\nsubagents: true\n", encoding="utf-8")
    cfg.chmod(0o600)
    res = run_cli(capsys, "--config", str(cfg), "run", PR_URL)
    assert "warning: custom_profile_subagents" in res.err
    quiet = run_cli(capsys, "--config", str(cfg), "--quiet", "run", PR_URL)
    assert "warning" not in quiet.err


# -- the documented order of the checks (mutation-check additions) -------------------------------
# Each case breaks two adjacent steps at once; the earlier step's error must win.


@pytest.mark.parametrize(
    ("kwargs", "expect"),
    [
        (
            dict(pr="not a pr", model="-bad"),
            ("the pull request argument has an invalid shape", None, None),
        ),
        (
            dict(model="-bad", override_escalation=True),
            ("flag for model: must not start with '-'", None, {"key": "model"}),
        ),
        (
            dict(override_escalation=True, labels=("owner=x",)),
            ("--override-escalation needs a human actor", None, None),
        ),
        (
            dict(labels=("owner=x",), previous="rr-zzzz"),
            ("a label uses a reserved key", "label_reserved", None),
        ),
    ],
    ids=[
        "pr-shape-then-flags",
        "flags-then-override",
        "override-then-labels",
        "labels-then-previous",
    ],
)
def test_the_usage_checks_run_in_the_documented_order(
    fakebin: FakeBin, routed: Routed, plugin: Path, tmp_path: Path, root: Path,
    kwargs: dict[str, Any], expect: tuple[str, str | None, dict[str, Any] | None],
) -> None:  # fmt: skip
    err = fails(pre.plan_run, request(**kwargs), config_of(tmp_path), root)
    assert (err.message, err.reason, err.details) == expect
    assert err.exit_code == 2 and routed.calls == []


def test_tools_are_checked_before_the_state_root(
    fakebin: FakeBin, routed: Routed, plugin: Path, tmp_path: Path, root: Path
) -> None:
    real = tmp_path / "real"
    real.mkdir()
    root.symlink_to(real)  # state_unsafe, but a tool is too old as well
    fakebin.script("agent-spawn", "--version", {"version": "0.1.0"})
    assert fails(pre.plan_run, request(), config_of(tmp_path), root).reason == "tool_too_old"


def test_the_state_root_is_checked_before_the_resolve_step_even_when_the_pr_is_unknown(
    fakebin: FakeBin, routed: Routed, plugin: Path, tmp_path: Path, root: Path
) -> None:
    real = tmp_path / "real"
    real.mkdir()
    root.symlink_to(real)
    fakebin.use("forgectl", "not_found")
    assert fails(pre.plan_run, request(), config_of(tmp_path), root).reason == "state_unsafe"


def test_pr_too_long_is_reported_before_previous_mismatch(
    fakebin: FakeBin, routed: Routed, plugin: Path, tmp_path: Path, root: Path
) -> None:
    long = "x" * 260
    run_id = done_run(root, "done", pr="github.com/o/r#2")  # would be a mismatch as well
    fakebin.configure("forgectl", hosts=["github.com"])
    fakebin.script(
        "forgectl", "show",
        {"merge": {"pr": f"github.com/o/{long}#1", "repo": f"github.com/o/{long}"}},
    )  # fmt: skip
    err = fails(pre.plan_run, request(previous=run_id), config_of(tmp_path), root)
    assert err.reason == "pr_too_long"


def test_previous_mismatch_is_reported_before_the_repo_settings_and_skill(
    fakebin: FakeBin, routed: Routed, plugin: Path, tmp_path: Path, root: Path
) -> None:
    run_id = done_run(root, "done", pr="github.com/o/r#2")
    err = fails(
        pre.plan_run, request(previous=run_id, skill="nope:nope"), config_of(tmp_path), root
    )
    assert err.reason == "previous_mismatch"


def test_the_profile_is_chosen_before_the_skill_is_resolved(
    fakebin: FakeBin, routed: Routed, plugin: Path, tmp_path: Path, root: Path
) -> None:
    bad = tmp_path / "profile.json"
    bad.write_text("[]", encoding="utf-8")
    bad.chmod(0o600)
    config = config_of(tmp_path, f"permission_profile: {bad}\n")
    err = fails(pre.plan_run, request(skill="nope:nope"), config, root)
    assert (err.code, err.details) == ("config_invalid", {"key": "permission_profile"})


def test_placeholders_are_checked_before_the_hash_walk(
    fakebin: FakeBin, routed: Routed, plugin: Path, tmp_path: Path, root: Path
) -> None:
    (plugin / "CLAUDE.md").unlink()  # a missing placeholder ...
    (plugin / "evil").symlink_to("CLAUDE.local.md")  # ... and a refused entry
    err = fails(pre.plan_run, request(), config_of(tmp_path), root)
    assert err.message == "a plugin directory lacks the placeholder entries"


def test_the_plugin_steps_come_before_wtctl_and_wtctl_before_the_dry_run(
    fakebin: FakeBin, routed: Routed, plugin: Path, tmp_path: Path, root: Path
) -> None:
    routed.missing.add("wtctl")
    config = config_of(
        tmp_path, f"repos:\n  {REPO}:\n    run_pr_code:\n      command: [make, test]\n"
    )
    assert fails(pre.plan_run, request(skill="nope:nope"), config, root).reason == "skill_not_found"
    fakebin.use("agent-spawn", "dry_run_would_refuse")
    err = fails(pre.plan_run, request(), config, root)
    assert (err.reason, err.tool) == ("tool_missing", "wtctl")
    assert fakebin.calls("agent-spawn", "launch") == []


# -- mapping grammar and previous shape (mutation-check additions) -------------------------------


@pytest.mark.parametrize(
    ("reason", "tool"),
    [
        ("third_party_cwd", "agent-spawn"),
        ("third_party_run", "agent-spawn"),
        ("live", "agent-spawn"),
        ("tearing_down", "agent-spawn"),
        ("anything_else", "claude-session"),
        ("a" * 64, "claude-session"),  # the longest closed word
    ],
)
def test_every_agent_spawn_refusal_names_agent_spawn_and_others_claude_session(
    reason: str, tool: str
) -> None:
    err = fails(pre.map_dry_run, 20, plan_doc(would_refuse=reason))
    assert (err.reason, err.tool, err.lower_code) == ("lower_refused", tool, reason)


@pytest.mark.parametrize(
    "reason",
    ["Live", "LIVE", "9live", "_live", "live-x", "live x", "live\n", "live!", "a" * 65, "", "é"],
)
def test_a_refusal_reason_outside_the_closed_word_grammar_is_lower_failed(reason: str) -> None:
    err = fails(pre.map_dry_run, 20, plan_doc(would_refuse=reason))
    assert (err.reason, err.lower_code) == ("lower_failed", "invalid_output")


@pytest.mark.parametrize(
    ("code", "lower", "expected"),
    [
        ("usage", None, "usage"),
        ("Usage", None, "failed"),  # case variants are not words
        ("9usage", "tmux_gone", "tmux_gone"),  # an invalid code falls back to the lower code
        ("us-age", None, "failed"),
        ("usage\n", None, "failed"),
        ("a" * 64, None, "a" * 64),
        ("a" * 65, None, "failed"),
        (None, "b" * 64, "b" * 64),
        (None, "b" * 65, "failed"),
        (None, "Bad", "failed"),
    ],
)
def test_the_lower_code_of_a_failed_dry_run_is_a_closed_word(
    code: str | None, lower: str | None, expected: str
) -> None:
    fields = {k: v for k, v in (("lower_code", lower),) if v is not None}
    stdout = envelope(code, **fields) if code is not None else envelope("-", **fields)
    err = fails(pre.map_dry_run, 3, stdout)
    assert (err.reason, err.lower_code) == ("lower_failed", expected)


def test_a_run_id_prefix_longer_than_eight_characters_is_usage_without_a_store(
    root: Path,
) -> None:
    assert fails(pre.check_previous, root, "rr-" + "a" * 8).exit_code == 10
    assert fails(pre.check_previous, root, "rr-" + "a" * 9).exit_code == 2
    assert not root.exists()


# -- skill lookup, plugin directories, previous (mutation-check additions) -----------------------


@pytest.mark.parametrize("skill", ["other:deep/../deep", "other:../skills/deep", "other:./deep"])
def test_a_skill_name_with_path_syntax_is_not_found_even_when_the_path_exists(
    plugin: Path, tmp_path: Path, skill: str
) -> None:
    other = extra_plugin(tmp_path)  # skills/deep/SKILL.md exists, and so does each path above
    assert os.path.isfile(other / "skills" / "deep" / ".." / "deep" / "SKILL.md")
    err = fails(pre.resolve_skill, skill, [str(other.resolve())])
    assert err.reason == "skill_not_found"


def test_an_unknown_plugin_does_not_fall_back_to_the_bundled_plugin(
    plugin: Path, tmp_path: Path
) -> None:
    assert (plugin / "skills" / "review-pr" / "SKILL.md").is_file()
    other = extra_plugin(tmp_path)
    for dirs in ([], [str(other.resolve())]):
        err = fails(pre.resolve_skill, "nope:review-pr", dirs)
        assert err.reason == "skill_not_found"


def test_the_passed_plugin_directories_are_realpaths_in_sorted_order(
    plugin: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    early = tmp_path / "aaa-first"
    extra_plugin(tmp_path).rename(early)  # sorts before the bundled copy
    link = tmp_path / "zzz-link"
    link.symlink_to(plugin)
    monkeypatch.setattr(rs, "bundled_plugin_dir", lambda: link)  # reached through a link
    passed = pre.plugin_dirs_for("other:deep", [str(early.resolve())])
    assert passed == (str(early.resolve()), str(plugin.resolve()))


def test_a_hash_refusal_names_its_closed_reason_in_the_message(
    plugin: Path,
) -> None:
    (plugin / "x-link").symlink_to("CLAUDE.md")
    assert "(symlink)" in fails(pre.hash_plugin_dirs, [str(plugin)]).message
    (plugin / "x-link").unlink()
    other = extra_plugin(tmp_path_of(plugin), "w")
    (other / "CLAUDE.md").chmod(0o664)
    assert "(writable)" in fails(pre.hash_plugin_dirs, [str(other)]).message
    assert fails(pre.hash_plugin_dirs, [str(other)]).details == {"key": "plugin_dirs"}


def tmp_path_of(plugin: Path) -> Path:
    return plugin.parent


def test_a_group_writable_bundled_plugin_has_its_own_message(plugin: Path) -> None:
    (plugin / "CLAUDE.md").chmod(0o664)  # an install made at umask 002
    err = fails(pre.hash_plugin_dirs, [str(plugin.resolve())])
    assert err.code == "config_invalid" and err.details is None and err.exit_code == 2
    assert "the bundled plugin directory is writable by group or others" in err.message
    assert "chmod -R g-w" in err.message and "plugin_dirs" not in err.message
    (plugin / "CLAUDE.md").chmod(0o644)
    (plugin / "skills").chmod(0o775)
    assert (
        "the bundled plugin directory is writable"
        in fails(pre.hash_plugin_dirs, [str(plugin.resolve())]).message
    )


def test_a_foreign_owned_bundled_plugin_has_its_own_message(
    plugin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 1)
    err = fails(pre.hash_plugin_dirs, [str(plugin.resolve())])
    assert "the bundled plugin directory is owned by someone else" in err.message
    assert err.details is None


def test_a_symlink_in_the_bundled_plugin_keeps_the_generic_message(plugin: Path) -> None:
    (plugin / "x-link").symlink_to("CLAUDE.md")
    err = fails(pre.hash_plugin_dirs, [str(plugin.resolve())])
    assert err.details == {"key": "plugin_dirs"} and "(symlink)" in err.message


def test_previous_that_is_reserved_but_has_no_run_record_is_exit_10(root: Path) -> None:
    with Store.open(root) as store:
        run_id = store.reserve_id()
    err = fails(pre.check_previous, root, run_id)
    assert (err.code, err.exit_code) == ("not_found", 10)


@pytest.mark.parametrize("code", [1, 3, 11, 13, 21])
def test_a_termctl_envelope_is_no_tmux_only_for_exit_2_or_12(code: int) -> None:
    err = fails(pre.map_dry_run, code, envelope("failed", tool="termctl", lower_code="tmux_gone"))
    assert (err.reason, err.tool, err.lower_code) == ("lower_failed", "termctl", "tmux_gone")


@pytest.mark.parametrize("code", [1, 2, 3, 13])
def test_tool_missing_passes_through_only_for_exit_12(code: int) -> None:
    err = fails(pre.map_dry_run, code, envelope("tool_missing", tool="claude-session"))
    assert (err.reason, err.tool, err.lower_code) == (
        "lower_failed", "claude-session", "tool_missing",
    )  # fmt: skip


@pytest.mark.parametrize(
    ("named", "expected"),
    [
        ("forgectl", "forgectl"),
        ("pr-comment", "pr-comment"),
        ("agent-spawn", "agent-spawn"),
        ("claude-session", "claude-session"),
        ("wtctl", "wtctl"),
        ("some-other-tool", "agent-spawn"),  # an unknown name is never reported as missing
        ("Claude-Session", "agent-spawn"),
    ],
)
def test_a_tool_missing_envelope_names_only_a_known_sibling(named: str, expected: str) -> None:
    err = fails(pre.map_dry_run, 12, envelope("tool_missing", tool=named))
    assert (err.reason, err.tool) == ("tool_missing", expected)


@pytest.mark.parametrize("stdout", ['{"error": "boom"}', '{"error": []}', '{"error": null}', "[]"])
def test_a_dry_run_envelope_whose_error_is_not_an_object_is_lower_failed(stdout: str) -> None:
    err = fails(pre.map_dry_run, 3, stdout)
    assert (err.reason, err.lower_code) == ("lower_failed", "failed")


def test_the_dry_runs_scratch_directory_and_files_are_private_while_it_exists(
    fakebin: FakeBin, routed: Routed, plugin: Path, tmp_path: Path, root: Path
) -> None:
    seen: dict[str, int] = {}
    real_run = routed.run

    def spy(argv: Sequence[str], **kw: Any) -> CompletedRun:
        if argv[:2] == ["agent-spawn", "launch"]:
            profile = Path(argv[list(argv).index("--permission-profile") + 1])
            seen["dir"] = profile.parent.stat().st_mode & 0o777
            seen["tmp"] = profile.parent.parent.stat().st_mode & 0o777
            seen["file"] = profile.stat().st_mode & 0o777
        return real_run(argv, **kw)

    routed.run = spy  # type: ignore[method-assign]
    pre.plan_run(request(), config_of(tmp_path), root)
    assert seen == {"dir": 0o700, "tmp": 0o700, "file": 0o600}


def test_the_dry_runs_scratch_directory_is_removed_when_the_dry_run_fails(
    fakebin: FakeBin, routed: Routed, plugin: Path, tmp_path: Path, root: Path
) -> None:
    fakebin.use("agent-spawn", "dry_run_would_refuse")
    fails(pre.plan_run, request(), config_of(tmp_path), root)
    assert list((root / "tmp").iterdir()) == []
    routed.missing.add("agent-spawn")  # the gateway raising instead of returning
    routed.calls.clear()
    settings, profile = _dry_run_inputs(tmp_path, plugin)
    err = fails(
        pre.launch_dry_run, settings, profile, repo=REPO, pr=PR, request=request(), root=root
    )
    assert err.reason == "tool_missing" and list((root / "tmp").iterdir()) == []


def _dry_run_inputs(tmp_path: Path, plugin: Path) -> tuple[rs.RunSettings, rs.ProfilePlan]:
    resolved = config_of(tmp_path).resolve(REPO, {})
    profile, _ = rs.effective_profile(resolved.values)
    dirs = [str(plugin.resolve())]
    settings = rs.build_settings(
        resolved, plugin_dirs=dirs, plugin_dirs_hash=pre.hash_plugin_dirs(dirs), profile=profile
    )
    return settings, profile


# -- what the dry run is asked and how its failures map (mutation-check additions) ---------------


def _spy_launch(routed: Routed, raises: Exception | None = None) -> None:
    """Make the ``agent-spawn launch`` call raise ``raises`` (other calls pass through)."""
    real_run = routed.run

    def spy(argv: Sequence[str], **kw: Any) -> CompletedRun:
        if raises is not None and argv[:2] == ["agent-spawn", "launch"]:
            raise raises
        return real_run(argv, **kw)

    routed.run = spy  # type: ignore[method-assign]


def test_the_dry_run_carries_the_actor_decision_ids_and_the_normalized_pr(
    fakebin: FakeBin, routed: Routed, plugin: Path, tmp_path: Path, root: Path
) -> None:
    pre.plan_run(
        request(actor="human:me", decision_ids=("d1", "d2"), labels=("team=x",)),
        config_of(tmp_path),
        root,
    )
    argv = list(fakebin.calls("agent-spawn", "launch")[0].argv)
    assert argv[argv.index("--actor") + 1] == "human:me"
    assert [a for a in argv if a.startswith("--decision-id")] == [
        "--decision-id=d1",
        "--decision-id=d2",
    ]
    labels = [argv[i + 1] for i, a in enumerate(argv) if a == "--label"]
    assert f"pr={PR}" in labels and f"repo={REPO}" in labels and "team=x" in labels
    assert f"run={pre.PLACEHOLDER_RUN_ID}" in labels  # the dry run names no real run


def test_a_decision_id_that_looks_like_an_option_is_one_dry_run_argv_item(
    fakebin: FakeBin, routed: Routed, plugin: Path, tmp_path: Path, root: Path
) -> None:
    pre.plan_run(request(decision_ids=("-x",)), config_of(tmp_path), root)
    argv = list(fakebin.calls("agent-spawn", "launch")[0].argv)
    assert "--decision-id=-x" in argv and "-x" not in argv and "--decision-id" not in argv


def test_agent_spawn_missing_at_the_dry_run_is_tool_missing(
    fakebin: FakeBin, routed: Routed, plugin: Path, tmp_path: Path, root: Path
) -> None:
    _spy_launch(routed, ToolMissing("agent-spawn not found"))
    err = fails(pre.plan_run, request(), config_of(tmp_path), root)
    assert (err.reason, err.tool, err.exit_code) == ("tool_missing", "agent-spawn", 12)
    assert list((root / "tmp").iterdir()) == []


def test_a_gateway_failure_at_the_dry_run_keeps_its_lower_code(
    fakebin: FakeBin, routed: Routed, plugin: Path, tmp_path: Path, root: Path
) -> None:
    _spy_launch(routed, gw.CallTimeout("t"))
    err = fails(pre.plan_run, request(), config_of(tmp_path), root)
    assert (err.reason, err.tool, err.lower_code) == ("lower_failed", "agent-spawn", "timeout")
    assert list((root / "tmp").iterdir()) == []


def test_cli_run_prints_profile_warnings_on_stderr_only(
    capsys: pytest.CaptureFixture[str], fakebin: FakeBin, routed: Routed, plugin: Path,
    tmp_path: Path,
) -> None:  # fmt: skip
    from tests.helpers import run_cli

    custom = tmp_path / "p.json"
    custom.write_text("{}", encoding="utf-8")
    custom.chmod(0o600)
    cfg = tmp_path / "c.yaml"
    cfg.write_text(f"permission_profile: {custom}\nsubagents: true\n", encoding="utf-8")
    cfg.chmod(0o600)
    res = run_cli(capsys, "--json", "--config", str(cfg), "run", PR_URL)
    assert "custom_profile_subagents" in res.err
    assert "custom_profile_subagents" not in res.out
    assert res.envelope()["error"]["code"] == "internal"  # stdout is still the one envelope
