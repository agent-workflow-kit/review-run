"""Pinned sibling schemas (issue #3).

Sibling output schemas that review-run reads are copied under
``tests/pinned_schemas/<sibling>/schemas/`` and each file records the sibling
version it was taken from in a top-level ``x-pinned-from`` header. Refresh
procedure: ``docs/pinned-schemas.md``. Only tests read these files; nothing in
``src/`` may import a sibling or touch them.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PINNED = Path(__file__).resolve().parent / "pinned_schemas"
SRC = REPO / "src"

HEADER_KEY = "x-pinned-from"
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
SIBLINGS = {"forgectl", "pr-comment", "agent-spawn", "claude-session", "wtctl"}
# Importable names of the sibling tools, hyphenated and underscored.
SIBLING_MODULES = {"forgectl", "pr_comment", "agent_spawn", "claude_session", "wtctl"}


def header_problems(path: Path, sibling: str, root: Path) -> list[str]:
    """Return what is wrong with a pinned file's version header (empty when fine)."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"unreadable JSON: {exc}"]
    if not isinstance(doc, dict):
        return ["top level is not an object"]
    header = doc.get(HEADER_KEY)
    if not isinstance(header, dict):
        return [f"missing {HEADER_KEY} header"]
    problems = []
    if header.get("sibling") != sibling:
        problems.append(f"header sibling {header.get('sibling')!r} != directory {sibling!r}")
    version = header.get("version")
    if not isinstance(version, str) or not SEMVER.match(version):
        problems.append(f"header version {version!r} is not MAJOR.MINOR.PATCH")
    source = header.get("source")
    if not isinstance(source, str) or source != path.relative_to(root).as_posix():
        problems.append(f"header source {source!r} does not match the file's path")
    return problems


def pinned_files() -> list[tuple[str, Path]]:
    return [
        (sib.name, f)
        for sib in sorted(PINNED.iterdir())
        if sib.is_dir()
        for f in sorted(sib.rglob("*.json"))
    ]


def test_every_expected_sibling_is_pinned() -> None:
    present = {name for name, _ in pinned_files()}
    assert SIBLINGS <= present
    assert present <= SIBLINGS, f"unexpected pinned sibling(s): {present - SIBLINGS}"


def test_every_pinned_file_has_a_version_header() -> None:
    files = pinned_files()
    assert files, "no pinned schema files found"
    failures = {
        str(f.relative_to(PINNED)): problems
        for sib, f in files
        if (problems := header_problems(f, sib, PINNED / sib))
    }
    assert not failures


def test_one_version_per_sibling() -> None:
    versions: dict[str, set[str]] = {}
    for sib, f in pinned_files():
        v = json.loads(f.read_text(encoding="utf-8"))[HEADER_KEY]["version"]
        versions.setdefault(sib, set()).add(v)
    assert {s: v for s, v in versions.items() if len(v) != 1} == {}


def test_local_refs_resolve_to_pinned_files() -> None:
    """A copy missing a file it $refs would make later validation fail confusingly."""

    def refs(node: object):
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "$ref" and isinstance(v, str):
                    yield v
                else:
                    yield from refs(v)
        elif isinstance(node, list):
            for v in node:
                yield from refs(v)

    missing = []
    for sib, f in pinned_files():
        for ref in refs(json.loads(f.read_text(encoding="utf-8"))):
            target = ref.split("#", 1)[0]
            if target and "://" not in target and not (f.parent / target).resolve().is_file():
                missing.append(f"{sib}: {f.name} -> {ref}")
    assert not missing


def test_header_check_fails_without_header(tmp_path: Path) -> None:
    root = tmp_path / "forgectl"
    (root / "schemas").mkdir(parents=True)
    bare = root / "schemas" / "x.json"
    bare.write_text('{"type": "object"}', encoding="utf-8")
    assert header_problems(bare, "forgectl", root) == [f"missing {HEADER_KEY} header"]


@pytest.mark.parametrize(
    "header",
    [
        {"sibling": "forgectl", "source": "schemas/x.json"},
        {"sibling": "forgectl", "version": "v1.0", "source": "schemas/x.json"},
        {"sibling": "wtctl", "version": "1.0.0", "source": "schemas/x.json"},
        {"sibling": "forgectl", "version": "1.0.0", "source": "schemas/other.json"},
    ],
)
def test_header_check_fails_on_bad_header(tmp_path: Path, header: dict[str, str]) -> None:
    root = tmp_path / "forgectl"
    (root / "schemas").mkdir(parents=True)
    f = root / "schemas" / "x.json"
    f.write_text(json.dumps({HEADER_KEY: header}), encoding="utf-8")
    assert header_problems(f, "forgectl", root)


def test_header_check_accepts_good_header(tmp_path: Path) -> None:
    root = tmp_path / "forgectl"
    (root / "schemas").mkdir(parents=True)
    f = root / "schemas" / "x.json"
    header = {"sibling": "forgectl", "version": "1.2.3", "source": "schemas/x.json"}
    f.write_text(json.dumps({HEADER_KEY: header}), encoding="utf-8")
    assert header_problems(f, "forgectl", root) == []


def test_header_check_rejects_non_json(tmp_path: Path) -> None:
    f = tmp_path / "x.json"
    f.write_text("{not json", encoding="utf-8")
    assert header_problems(f, "forgectl", tmp_path)


def _src_files() -> list[Path]:
    return sorted(SRC.rglob("*.py")) if SRC.is_dir() else []


def _imported_roots(tree: ast.AST) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots


_SIBLING_ALTERNATION = "|".join(m.replace("_", "[-_]") for m in sorted(SIBLING_MODULES))
_SIBLING_PATH = re.compile(r"\.\./(?:" + _SIBLING_ALTERNATION + ")")


def _dynamic_violations(tree: ast.AST) -> list[str]:
    """Dynamic imports of a sibling, and string paths that climb to a sibling checkout."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = ""
            if isinstance(fn, ast.Name):
                name = fn.id
            elif isinstance(fn, ast.Attribute):
                name = fn.attr
            if name in ("__import__", "import_module") and node.args:
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    if arg.value.lstrip(".").split(".")[0] in SIBLING_MODULES:
                        found.append(f"dynamically imports sibling {arg.value!r}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _SIBLING_PATH.search(node.value):
                found.append(f"reaches a sibling checkout by path {node.value!r}")
    return found


def source_violations(files: list[Path]) -> list[str]:
    found = []
    for f in files:
        text = f.read_text(encoding="utf-8")
        bad = _imported_roots(ast.parse(text)) & SIBLING_MODULES
        if bad:
            found.append(f"{f}: imports sibling {sorted(bad)}")
        found.extend(f"{f}: {msg}" for msg in _dynamic_violations(ast.parse(text)))
        for marker in ("pinned_schemas", HEADER_KEY):
            if marker in text:
                found.append(f"{f}: references pinned files ({marker})")
    return found


def test_src_does_not_import_siblings_or_read_pinned_files() -> None:
    # Passes trivially when src/ is absent or empty.
    assert source_violations(_src_files()) == []


@pytest.mark.parametrize(
    "code",
    [
        "import importlib\nimportlib.import_module('forgectl')\n",
        "__import__('agent_spawn')\n",
        "from importlib import import_module\nimport_module('wtctl.cli')\n",
        "P = '../forgectl/src'\n",
        "import forgectl\n",
        "from agent_spawn.cli import main\n",
        "import claude_session.status as s\n",
        "from pr_comment import x\n",
        "P = 'tests/pinned_schemas/wtctl'\n",
    ],
)
def test_source_check_detects_violations(tmp_path: Path, code: str) -> None:
    f = tmp_path / "mod.py"
    f.write_text(code, encoding="utf-8")
    assert source_violations([f])


def test_source_check_allows_clean_code(tmp_path: Path) -> None:
    f = tmp_path / "mod.py"
    f.write_text("import json\nfrom pathlib import Path\n", encoding="utf-8")
    assert source_violations([f]) == []


def test_source_check_allows_bare_sibling_name_strings(tmp_path: Path) -> None:
    f = tmp_path / "mod.py"
    f.write_text("BINARY = 'forgectl'\n", encoding="utf-8")
    assert source_violations([f]) == []
