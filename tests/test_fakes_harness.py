"""The fake harness itself: pinned versions, the src boundary, scenario coverage, isolation."""

from __future__ import annotations

import ast
import json
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

from tests.fake_scenarios import SCENARIOS
from tests.fakebin._common import VERSIONS
from tests.fakes import FakeBin, pinned_ids, validate_pinned
from tests.proc_guard import RealProgramRefused

TESTS = Path(__file__).resolve().parent
SRC = TESTS.parent / "src"


@pytest.mark.parametrize("tool", sorted(VERSIONS))
def test_fake_versions_equal_the_pinned_versions(tool: str) -> None:
    pinned = {
        json.loads(p.read_text(encoding="utf-8"))["x-pinned-from"]["version"]
        for p in (TESTS / "pinned_schemas" / tool).rglob("*.json")
    }
    assert pinned == {VERSIONS[tool]}


def test_every_pinned_schema_resolves_with_its_refs() -> None:
    assert len(pinned_ids()) >= 30
    with pytest.raises(Exception, match="rejects|None is not|valid"):
        validate_pinned("forgectl/cli/show.json", {})  # resolves refs, then fails on content
    with pytest.raises(KeyError):
        validate_pinned("forgectl/cli/nope.json", {})


def test_nothing_in_src_imports_or_names_a_fake_or_a_pinned_schema() -> None:
    markers = ("fakebin", "pinned_schemas", "fake_scenarios", "fake_gateway", "tests.fakes")
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            elif (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and len(node.value) < 120
            ):
                names = [node.value]
            for name in names:
                if name == "tests" or name.startswith("tests.") or any(m in name for m in markers):
                    offenders.append(f"{path.relative_to(SRC)}: {name!r}")
    assert offenders == []


def _exercised(tool: str, name: str, texts: list[str]) -> bool:
    """A test file applies scenario ``name`` of ``tool``: ``use("<tool>", "<name>"`` literally,
    or ``use("<tool>", <variable>`` in a file that lists the quoted name (a parametrized test)."""
    literal = rf"""use\(\s*["']{re.escape(tool)}["']\s*,\s*["']{re.escape(name)}["']"""
    variable = rf"""use\(\s*["']{re.escape(tool)}["']\s*,\s*[A-Za-z_][A-Za-z_0-9]*\s*[,)]"""
    quoted = rf"""["']{re.escape(name)}["']"""
    return any(
        re.search(literal, t) or (re.search(variable, t) and re.search(quoted, t)) for t in texts
    )


def test_every_scenario_is_exercised_by_a_test_of_its_own_tool() -> None:
    texts = [p.read_text(encoding="utf-8") for p in sorted(TESTS.glob("test_fake_*.py"))]
    missing = [
        f"{tool}:{name}"
        for tool, scenarios in SCENARIOS.items()
        for name in scenarios
        if not _exercised(tool, name, texts)
    ]
    assert missing == []


def test_the_scenario_guard_checks_the_tool_not_just_the_name() -> None:
    texts = ['fakebin.use("forgectl", "hang")']
    assert _exercised("forgectl", "hang", texts)
    assert not _exercised("wtctl", "hang", texts)  # same name, another tool
    assert _exercised("wtctl", "x", ['use("wtctl", name)\n("x", 1)'])


def test_every_fake_tool_has_an_executable_script_and_a_wrapper(fakebin: FakeBin) -> None:
    for tool in VERSIONS:
        script = TESTS / "fakebin" / tool
        assert script.is_file() and os.access(script, os.X_OK)
        wrapper = Path(fakebin.argv0(tool))
        assert stat.S_IMODE(wrapper.stat().st_mode) == 0o755


def test_a_fake_cannot_be_started_by_name_or_found_on_the_real_path(fakebin: FakeBin) -> None:
    with pytest.raises(RealProgramRefused):
        subprocess.Popen(["forgectl", "--version"])  # by name: refused by the process guard
    with pytest.raises(RealProgramRefused):
        subprocess.Popen(["/usr/bin/git", "--version"])


def test_the_fakes_run_without_the_callers_environment(fakebin: FakeBin) -> None:
    """The wrapper finds its directory itself: an empty environment still works."""
    result = fakebin.run("forgectl", "show", "github.com/o/r#1", "--json", env={})
    assert result.returncode == 0
    assert fakebin.calls("forgectl")[0].env.get("FAKEBIN_DIR") == str(fakebin.dir)


def test_fakes_run_with_a_private_home_and_write_only_under_their_directory(
    fakebin: FakeBin, tmp_path: Path
) -> None:
    for tool, argv in (("forgectl", ["show", "github.com/o/r#1", "--json"]),
                       ("wtctl", ["list", "--json"]),
                       ("agent-spawn", ["list", "--json"])):  # fmt: skip
        fakebin.run(tool, *argv)
    private = str(fakebin.dir / "home")
    for call in fakebin.calls():
        assert call.env["HOME"] == private and call.env["XDG_CONFIG_HOME"] == private
    assert fakebin.env()["HOME"] == private == fakebin.env()["XDG_CONFIG_HOME"]
    assert list((fakebin.dir / "home").iterdir()) == []  # the fakes never wrote their home
    # nothing was created beside the harness directory (and the test session's own ``home``)
    assert {p.name for p in tmp_path.iterdir()} <= {fakebin.dir.name, "home"}


def test_a_missing_fakebin_dir_is_a_loud_failure(tmp_path: Path) -> None:
    script = TESTS / "fakebin" / "forgectl"
    import sys

    proc = subprocess.run(  # noqa: S603  (the test interpreter and a script under tests/)
        [sys.executable, str(script), "--version"], capture_output=True, text=True,
        env={"PATH": "/nonexistent"}, check=False,
    )  # fmt: skip
    assert proc.returncode == 98 and "FAKEBIN_DIR" in proc.stderr


def test_scenarios_are_json_serializable(fakebin: FakeBin) -> None:
    for tool, scenarios in SCENARIOS.items():
        for name, builder in scenarios.items():
            required = [
                p for p in builder.__code__.co_varnames[: builder.__code__.co_argcount]
                if builder.__defaults__ is None or p not in builder.__code__.co_varnames[
                    builder.__code__.co_argcount - len(builder.__defaults__ or ()) :
                ]
            ]  # fmt: skip
            params = {"sha": "a" * 40} if required == ["sha"] else {}
            fakebin.use(tool, name, **params)
    json.loads((fakebin.dir / "scenario.json").read_text(encoding="utf-8"))
