"""The fake-sibling harness (issue #10).

``FakeBin`` gives a test a private directory of executable wrappers named like the sibling tools
(``forgectl``, ``pr-comment``, ``agent-spawn``, ``claude-session``, ``wtctl``), each running the
fake in ``tests/fakebin/`` with the test interpreter, plus the scenario, state and call-log files
the fakes read and write (``tests/fakebin/_common.py`` describes them).

Usage in a test::

    def test_x(fakebin):
        fakebin.use("forgectl", "files_incomplete")        # a named scenario
        fakebin.script("forgectl", "show", {"merge": {"state": "merged"}})   # or hand-written steps
        result = fakebin.run("forgectl", "show", "github.com/o/r#1", "--json")
        assert fakebin.calls("forgectl")[0].argv == ["show", "github.com/o/r#1", "--json"]

``fakebin.argv0("forgectl")`` is the absolute path for ``gateway.run([...])`` and
``fakebin.env()`` an environment whose ``PATH`` is the wrapper directory only. Every recorded
call whose scenario names a pinned schema is validated against it when the test ends
(``assert_valid``, called by the fixture): a fake emitting an invalid document fails the test.
Calls run through the gateway are killed by it on timeout; the autouse ``proc_tracker`` guard
refuses any program that is not a wrapper under the test's own directory.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from review_run import gateway as gw
from review_run.gateway import CompletedRun
from tests.fakebin import _schemas as schemas
from tests.fakebin._common import VERSIONS

TESTS = Path(__file__).resolve().parent
REPO = TESTS.parent
FAKEBIN_SRC = TESTS / "fakebin"
TOOLS = tuple(VERSIONS)


# --------------------------------------------------------------------------------------------
# pinned schemas
# --------------------------------------------------------------------------------------------


def pinned_ids() -> list[str]:
    return sorted(schemas.DOCS)


def validate_pinned(schema_id: str, instance: object) -> None:
    """Validate ``instance`` against the pinned schema ``<sibling>/<path>.json`` (its ``$id``)."""
    schemas.validate(schema_id, instance)


# --------------------------------------------------------------------------------------------
# recorded calls
# --------------------------------------------------------------------------------------------


@dataclass
class RecordedCall:
    """One call of a fake, merged from its ``start`` and ``end`` log records."""

    id: str
    tool: str
    argv: list[str]  # without argv[0]
    env: dict[str, str]
    cwd: str
    pid: int
    pgid: int
    stdin: str | None = None
    exit: int | None = None  # None: the process never finished (killed)
    stdout: str = ""
    stderr: str = ""
    schema: str | None = None
    signal: int | None = None

    @property
    def command(self) -> str:
        """The first non-flag word of argv (``draft`` for ``pr-comment draft new``)."""
        return next((a for a in self.argv if not a.startswith("-")), "")

    @property
    def finished(self) -> bool:
        return self.exit is not None

    def json(self) -> Any:
        return json.loads(self.stdout)


@dataclass
class FakeBin:
    dir: Path
    scenario: dict[str, dict[str, Any]] = field(default_factory=dict)

    # ---- setup -------------------------------------------------------------------------------

    def __post_init__(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.bin = self.dir / "bin"
        self.bin.mkdir(exist_ok=True)
        for tool in TOOLS:
            wrapper = self.bin / tool
            script = FAKEBIN_SRC / tool
            wrapper.write_text(
                "#!/bin/sh\n"
                f"FAKEBIN_DIR={shlex.quote(str(self.dir))} exec "
                f'{shlex.quote(sys.executable)} -E -s {shlex.quote(str(script))} "$@"\n',
                encoding="utf-8",
            )
            wrapper.chmod(0o755)
        self._write()

    def _write(self) -> None:
        (self.dir / "scenario.json").write_text(json.dumps(self.scenario), encoding="utf-8")

    def argv0(self, tool: str) -> str:
        return str(self.bin / tool)

    def env(self, **extra: str) -> dict[str, str]:
        """A minimal environment whose ``PATH`` holds only the wrappers (plus ``extra``)."""
        env = {"PATH": str(self.bin), "LANG": "C.UTF-8"}
        env.update({k: os.environ[k] for k in ("HOME", "XDG_CONFIG_HOME") if k in os.environ})
        env.update(extra)
        return env

    # ---- scenarios ---------------------------------------------------------------------------

    def script(self, tool: str, command: str, *steps: dict[str, Any]) -> None:
        """Set the steps for ``tool``'s ``command`` (replacing earlier ones; last step repeats)."""
        self.scenario.setdefault(tool, {})[command] = list(steps)
        self._write()

    def configure(self, tool: str, **config: Any) -> None:
        """Set tool-wide scenario keys (``hosts``, ``pr``, ``plan``, ...)."""
        self.scenario.setdefault(tool, {}).update(config)
        self._write()

    def use(self, tool: str, name: str, **params: Any) -> None:
        """Apply the named scenario from ``tests/fake_scenarios.py``."""
        from tests.fake_scenarios import SCENARIOS

        try:
            builder = SCENARIOS[tool][name]
        except KeyError:
            raise KeyError(f"no scenario {name!r} for {tool}") from None
        patch = dict(builder(**params))
        shared = patch.pop("@sessions", None)
        self.scenario.setdefault(tool, {}).update(patch)
        if shared:  # plans of runs, read by agent-spawn and claude-session alike
            from tests.fakebin._common import deep_merge

            merged = deep_merge(self.scenario.get("sessions", {}), shared)
            self.scenario["sessions"] = merged
        self._write()

    def state(self) -> dict[str, Any]:
        path = self.dir / "state.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    # ---- running -----------------------------------------------------------------------------

    def run(
        self,
        tool: str,
        *argv: str,
        stdin: str | None = None,
        timeout: float = 30,
        env: dict[str, str] | None = None,
    ) -> CompletedRun:
        """Run a fake through the real gateway (the way review-run will)."""
        return gw.SystemGateway().run(
            [self.argv0(tool), *argv],
            timeout=timeout,
            stdin=stdin,
            env=self.env() if env is None else env,
            cwd=str(self.dir),
        )

    # ---- the call log ------------------------------------------------------------------------

    def all_calls(self) -> list[RecordedCall]:
        path = self.dir / "calls.jsonl"
        if not path.exists():
            return []
        calls: dict[str, RecordedCall] = {}
        order: list[str] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            if rec["ev"] == "start":
                calls[rec["id"]] = RecordedCall(
                    id=rec["id"], tool=rec["tool"], argv=rec["argv"], env=rec["env"],
                    cwd=rec["cwd"], pid=rec["pid"], pgid=rec["pgid"],
                )  # fmt: skip
                order.append(rec["id"])
            else:
                c = calls[rec["id"]]
                c.exit, c.stdout, c.stderr = rec["exit"], rec["stdout"], rec["stderr"]
                c.stdin, c.schema, c.signal = rec["stdin"], rec["schema"], rec["signal"]
        return [calls[i] for i in order]

    def calls(self, tool: str | None = None, command: str | None = None) -> list[RecordedCall]:
        return [
            c
            for c in self.all_calls()
            if (tool is None or c.tool == tool)
            and (command is None or c.command == command)
            and "--version" not in c.argv
        ]

    def assert_valid(self) -> None:
        """Every recorded stdout that names a pinned schema must be one valid JSON document."""
        problems: list[str] = []
        for c in self.all_calls():
            if c.schema is None or not c.finished:
                continue
            try:
                validate_pinned(c.schema, json.loads(c.stdout))
            except Exception as exc:  # jsonschema.ValidationError, JSON errors, unknown schema
                problems.append(f"{c.tool} {' '.join(c.argv)} -> {c.schema}: {str(exc)[:400]}")
        assert not problems, "a fake emitted a document its pinned schema rejects:\n" + "\n".join(
            problems
        )


@pytest.fixture
def fakebin(tmp_path: Path) -> Iterator[FakeBin]:
    fb = FakeBin(tmp_path / "fakebin-run")
    yield fb
    fb.assert_valid()


@pytest.fixture(name="inputs")
def launch_inputs(tmp_path: Path) -> dict[str, Path]:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("review it", encoding="utf-8")
    patch = tmp_path / "f0001.patch"
    patch.write_text("diff", encoding="utf-8")
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    profile = tmp_path / "permission-profile.json"
    profile.write_text("{}", encoding="utf-8")
    (tmp_path / "runs").mkdir()
    return {"prompt": prompt, "patch": patch, "plugin": plugin, "profile": profile,
            "result": tmp_path / "runs" / "launch.json"}  # fmt: skip
