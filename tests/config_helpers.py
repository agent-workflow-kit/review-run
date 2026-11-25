"""Helpers for the config tests: write config files under the private XDG config home."""

from __future__ import annotations

import grp
import os
import pwd
from pathlib import Path
from types import SimpleNamespace

import pytest

from review_run.config import Config, load_config


def config_dir() -> Path:
    path = Path(os.environ["XDG_CONFIG_HOME"]) / "review-run"
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_main(text: str) -> Path:
    path = config_dir() / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def write_dropin(name: str, text: str) -> Path:
    directory = config_dir() / "config.d"
    directory.mkdir(exist_ok=True)
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


def base_env() -> dict[str, str]:
    """The path variables of the private home: an explicit env passed to the loader holds them."""
    names = ("HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME")
    return {name: os.environ[name] for name in names if name in os.environ}


def load(text: str | None = None, env: dict[str, str] | None = None) -> Config:
    if text is not None:
        write_main(text)
    return load_config(None, {**base_env(), **(env or {})})


def private_primary_group(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the account database say the user's effective group has no other member."""
    me = pwd.getpwuid(os.geteuid())
    monkeypatch.setattr(grp, "getgrgid", lambda gid: SimpleNamespace(gr_mem=[me.pw_name]))
    monkeypatch.setattr(pwd, "getpwall", lambda: [me])


def shared_primary_group(monkeypatch: pytest.MonkeyPatch, *, by: str) -> None:
    """Make the effective group shared: ``by`` is ``member`` (listed in the group) or
    ``account`` (another account has it as its primary group)."""
    me = pwd.getpwuid(os.geteuid())
    other = SimpleNamespace(pw_name="someone-else", pw_uid=os.geteuid() + 1, pw_gid=os.getegid())
    members = [me.pw_name, "someone-else"] if by == "member" else [me.pw_name]
    monkeypatch.setattr(grp, "getgrgid", lambda gid: SimpleNamespace(gr_mem=members))
    monkeypatch.setattr(pwd, "getpwall", lambda: [me, other] if by == "account" else [me])


def foreign_group() -> int | None:
    """A supplementary group of the user other than the effective one, or None."""
    return next((g for g in os.getgroups() if g != os.getegid()), None)
