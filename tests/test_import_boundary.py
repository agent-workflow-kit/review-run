"""Only ``gateway.py`` may spawn a process or touch the network (CLAUDE.md, plan conventions).

An AST scan of every module under ``src/`` (never a text grep, so comments and strings do not
trip it) refuses imports of process and HTTP machinery and calls that start a process.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "review_run"

FORBIDDEN_MODULES = {
    "subprocess",
    "pty",
    "multiprocessing",
    "socket",
    "ssl",
    "http",
    "urllib",
    "requests",
    "httpx",
    "aiohttp",
    "asyncio.subprocess",
    "pexpect",
    "sh",
    "plumbum",
    "_posixsubprocess",
    "ctypes",
    "webbrowser",
    "concurrent.futures.process",
}
FORBIDDEN_OS = {
    "system", "popen", "fork", "forkpty", "execv", "execve", "execvp", "execvpe", "execl",
    "execle", "execlp", "execlpe", "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv",
    "spawnve", "spawnvp", "spawnvpe", "posix_spawn", "posix_spawnp",
}  # fmt: skip
FORBIDDEN_NAMES = {"__import__", "import_module"}  # dynamic imports dodge the scan
ASYNC_SPAWN = {
    "create_subprocess_exec", "create_subprocess_shell", "subprocess_exec", "subprocess_shell",
}  # fmt: skip
#: Names that must not even appear as a string constant handed to getattr.
GETATTR_FORBIDDEN = FORBIDDEN_OS | FORBIDDEN_NAMES | ASYNC_SPAWN | {"subprocess", "Popen"}


def violations(source: str) -> list[str]:
    found: list[str] = []
    tree = ast.parse(source)
    os_aliases = {"os"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if alias.name in FORBIDDEN_MODULES or top in FORBIDDEN_MODULES:
                    found.append(f"import {alias.name}")
                if alias.name == "os" and alias.asname:
                    os_aliases.add(alias.asname)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module in FORBIDDEN_MODULES or module.split(".")[0] in FORBIDDEN_MODULES:
                found.append(f"from {module} import ...")
            found += [
                f"from {module} import {a.name}"
                for a in node.names
                if a.name in FORBIDDEN_NAMES | ASYNC_SPAWN
                or f"{module}.{a.name}" in FORBIDDEN_MODULES
            ]
            if module == "asyncio" and any(a.name == "subprocess" for a in node.names):
                found.append("from asyncio import subprocess")
            if module == "os" and any(a.name == "*" for a in node.names):
                found.append("from os import *")
            if module == "os":
                found += [f"from os import {a.name}" for a in node.names if a.name in FORBIDDEN_OS]
        elif isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name) and node.value.id in os_aliases:
                if node.attr in FORBIDDEN_OS:
                    found.append(f"os.{node.attr}")
            if node.attr in FORBIDDEN_NAMES | ASYNC_SPAWN:
                found.append(node.attr)
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES | ASYNC_SPAWN:
            found.append(node.id)
        elif isinstance(node, ast.Call) and getattr(node.func, "id", None) == "getattr":
            if (
                len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value in GETATTR_FORBIDDEN
            ):
                found.append(f"getattr(..., {node.args[1].value!r})")
    return sorted(set(found))


def modules() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py") if p.name != "gateway.py" or p.parent != SRC)


def test_the_scan_sees_the_source_tree() -> None:
    assert len(modules()) >= 5  # cli, errors, durations, fsmodes, ... (a wrong path fails here)


@pytest.mark.parametrize("path", modules(), ids=lambda p: str(p.relative_to(SRC)))
def test_no_module_outside_the_gateway_spawns_a_process(path: Path) -> None:
    assert violations(path.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize(
    "source",
    [
        "import subprocess",
        "import subprocess as sp",
        "from subprocess import run",
        "import os, subprocess",
        "import socket",
        "import http.client",
        "from urllib.request import urlopen",
        "import urllib.request",
        "import multiprocessing",
        "from asyncio import subprocess",
        "import asyncio.subprocess",
        "import os\nos.system('x')",
        "import os as o\no.execv('/x', [])",
        "from os import popen",
        "import os\nos.posix_spawn('/x', [], {})",
        "__import__('subprocess')",
        "import importlib\nimportlib.import_module('subprocess')",
        "import asyncio\nasyncio.create_subprocess_exec('x')",
        "import asyncio\nasyncio.create_subprocess_shell('x')",
        "from asyncio import create_subprocess_exec",
        "import asyncio\nasyncio.get_event_loop().subprocess_exec(None, 'x')",
        "loop.subprocess_shell(None, 'x')",
        "import _posixsubprocess",
        "from _posixsubprocess import fork_exec",
        "import ctypes",
        "from ctypes import CDLL",
        "import webbrowser",
        "import concurrent.futures.process",
        "from concurrent.futures import process",
        "from concurrent.futures.process import ProcessPoolExecutor",
        "import multiprocessing.pool",
        "from os import *",
        "import os\ngetattr(os, 'system')('x')",
        "import os\ngetattr(os, 'posix_spawn')",
        "getattr(__builtins__, '__import__')('subprocess')",
        "import asyncio\ngetattr(asyncio, 'create_subprocess_exec')",
        "import importlib\ngetattr(importlib, 'import_module')",
    ],
)
def test_the_scan_catches_every_way_in(source: str) -> None:
    assert violations(source) != []


@pytest.mark.parametrize(
    "source",
    [
        "import os\nos.getpid()\nos.path.join('a', 'b')",
        "# import subprocess\nx = 'subprocess.run'",
        "from review_run import gateway\ngateway.run(['x'], timeout=1)",
        "from concurrent.futures import ThreadPoolExecutor",
        "getattr(obj, 'name')",
    ],
)
def test_the_scan_allows_ordinary_code(source: str) -> None:
    assert violations(source) == []


def test_the_gateway_is_the_one_place_that_does() -> None:
    assert "import subprocess" in (SRC / "gateway.py").read_text(encoding="utf-8")
