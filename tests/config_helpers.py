"""Helpers for the config tests: write config files under the private XDG config home."""

from __future__ import annotations

import os
from pathlib import Path

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


def load(text: str | None = None, env: dict[str, str] | None = None) -> Config:
    if text is not None:
        write_main(text)
    return load_config(None, env if env is not None else {})
