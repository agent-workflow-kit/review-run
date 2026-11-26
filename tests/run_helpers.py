"""Setup shared by the tests of ``run`` after preflight (launch, runner, cli).

``Routed`` sends sibling names to the fake wrappers (version probes are answered in process, a
spawn costs about 0.1 s) and passes ``on_start`` through to the real gateway. ``World`` bundles a
bundled-plugin copy with sane modes, a state root, a config and the routed gateway.
"""

from __future__ import annotations

import shutil
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from review_run import gateway as gw
from review_run import preflight as pre
from review_run import settings as rs
from review_run.audit import Caller
from review_run.config import Config, load_config
from review_run.gateway import CompletedRun, ToolMissing
from review_run.runner import Options, run_review
from review_run.store import Run, Store
from tests.fakebin._common import VERSIONS
from tests.fakes import FakeBin

REPO_PLUGIN = Path(__file__).resolve().parent.parent / "plugin"
PR = "github.com/o/r#1"
REPO = "github.com/o/r"
PR_URL = "https://github.com/o/r/pull/1"
CALLER = Caller("human:alice", "flag", False, (), ())
PY = sys.executable
OnStart = Callable[[int], None]


class Routed:
    """A gateway that runs the fake siblings; names in ``missing`` are not on ``PATH``."""

    def __init__(self, fb: FakeBin) -> None:
        self.fb = fb
        self.calls: list[tuple[str, ...]] = []
        self.envs: list[dict[str, str] | None] = []
        self.missing: set[str] = set()
        self.on_run: Callable[[Sequence[str]], None] | None = None  # runs before each spawn

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        stdin: str | None = None,
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
        term_grace: float = 0.0,
        on_start: OnStart | None = None,
    ) -> CompletedRun:
        self.calls.append(tuple(argv))
        self.envs.append(None if env is None else dict(env))
        if (
            tuple(argv[1:]) == ("--version",)
            and argv[0] in VERSIONS
            and "--version" not in self.fb.scenario.get(argv[0], {})
        ):
            return CompletedRun(tuple(argv), 0, f"{argv[0]} {VERSIONS[argv[0]]}\n", "")
        if argv[0] in self.missing:
            raise ToolMissing(f"{argv[0]} not found")
        if self.on_run is not None:
            self.on_run(argv)
        child = self.fb.env()
        if env is not None and rs.DEPTH_VARIABLE in env:
            child[rs.DEPTH_VARIABLE] = env[rs.DEPTH_VARIABLE]
        return gw.SystemGateway().run(
            [self.fb.argv0(argv[0]), *argv[1:]],
            timeout=timeout,
            stdin=stdin,
            env=child,
            cwd=str(self.fb.dir),
            term_grace=term_grace,
            on_start=on_start,
        )

    def spawn_detached(self, *args: Any, **kwargs: Any) -> gw.DetachedHandle:
        raise NotImplementedError("the run tests never start a detached child")

    def called(self, tool: str, command: str | None = None) -> list[tuple[str, ...]]:
        return [c for c in self.calls if c[0] == tool and (command is None or command in c[1:2])]


@pytest.fixture
def routed(fakebin: FakeBin) -> Iterator[Routed]:
    router = Routed(fakebin)
    previous = gw.set_gateway(router)
    try:
        yield router
    finally:
        gw.set_gateway(previous)


@dataclass
class World:
    fb: FakeBin
    router: Routed
    root: Path
    plugin: Path
    tmp: Path
    config_text: str = "skill: review-run:review-pr\n"
    sleeps: list[float] = field(default_factory=list[float])

    def config(self) -> Config:
        path = self.tmp / "config.yaml"
        path.write_text(self.config_text, encoding="utf-8")
        path.chmod(0o600)
        return load_config(path)

    def plan(self, pr: str = PR_URL, **request: Any) -> pre.RunPlan:
        req = pre.RunRequest(pr=pr, **request)
        return pre.plan_run(req, self.config(), self.root, dry_run_launch=False)

    def options(self, **kw: Any) -> Options:
        kw.setdefault("caller", CALLER)
        kw.setdefault("sleep", self.sleeps.append)
        kw.setdefault("shell", (PY, "-c"))
        return Options(**kw)

    def run(self, pr: str = PR_URL, request: Mapping[str, Any] | None = None, **opts: Any) -> Run:
        plan = self.plan(pr, **dict(request or {}))
        return run_review(plan, self.root, self.options(**opts))

    def store(self) -> Store:
        return Store.open(self.root)

    def only_run(self) -> Run:
        with self.store() as store:
            ids, _ = store.list_run_ids()
            assert len(ids) == 1, ids
            run = store.get_run(ids[0])
            assert run is not None
            return run


@pytest.fixture
def world(
    fakebin: FakeBin, routed: Routed, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> World:
    plugin = tmp_path / "bundled-plugin"
    shutil.copytree(REPO_PLUGIN, plugin)
    for item in [plugin, *plugin.rglob("*")]:
        item.chmod(0o755 if item.is_dir() else 0o644)
    monkeypatch.setattr(rs, "bundled_plugin_dir", lambda: plugin)
    return World(fakebin, routed, tmp_path / "state", plugin, tmp_path)
