"""Shared fixtures: a private HOME/XDG guard, a umask guard and JSON-schema helpers."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from tests.fake_gateway import fake_gateway  # noqa: F401  (fixture)
from tests.proc_guard import proc_tracker  # noqa: F401  (autouse fixture)

SCHEMAS = Path(__file__).resolve().parent.parent / "schemas"


@pytest.fixture(autouse=True)
def private_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point HOME and every XDG directory at a private temp dir for every test.

    Nothing a test starts may read or write the real home, config or state directories.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name, sub in (
        ("XDG_CONFIG_HOME", ".config"),
        ("XDG_STATE_HOME", ".local/state"),
        ("XDG_DATA_HOME", ".local/share"),
        ("XDG_CACHE_HOME", ".cache"),
    ):
        path = home / sub
        path.mkdir(parents=True)
        monkeypatch.setenv(name, str(path))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    for name in list(os.environ):
        if name.startswith("REVIEW_RUN_"):
            monkeypatch.delenv(name)
    yield home


@pytest.fixture(autouse=True)
def restore_umask() -> Iterator[None]:
    """`main()` sets umask 077 for the process; give every test the original back."""
    original = os.umask(0o022)
    os.umask(original)
    yield
    os.umask(original)


def _registry() -> Registry[Any]:
    registry: Registry[Any] = Registry()
    for path in sorted(SCHEMAS.rglob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        registry = registry.with_resource(doc["$id"], Resource.from_contents(doc))
    return registry


def validate_against(schema_name: str, instance: object) -> None:
    """Validate ``instance`` against ``schemas/<schema_name>`` (e.g. ``cli/error.json``)."""
    doc = json.loads((SCHEMAS / schema_name).read_text(encoding="utf-8"))
    Draft202012Validator(doc, registry=_registry()).validate(instance)
