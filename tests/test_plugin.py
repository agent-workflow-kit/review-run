"""The bundled plugin: header template, review-pr skill, packaging (issue #23)."""

from __future__ import annotations

import json
import os
import re
import shutil
import tomllib
import unicodedata
from pathlib import Path

import pytest

from review_run import prompt as pr
from review_run import settings as rs
from review_run.plugin_hash import plugin_dirs_sha256

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugin"
GOLDEN = Path(__file__).parent / "golden" / "plugin-header.txt"
NONCE = "0123456789abcdef0123456789abcdef"
LIMITS = {
    "max_file_bytes": 200000,
    "max_total_bytes": 1048576,
    "max_files": 300,
    "hard_ceiling_bytes": 8388608,
    "max_answer_bytes": 65536,
}
INPUTS = ["f0001.patch", "f0002-1.patch", "f0002-2.patch", "previous-review.txt"]
TEMPLATE = (PLUGIN / "header.md").read_text(encoding="utf-8")


def render(template: str = TEMPLATE, nits: bool = False) -> str:
    return pr.render_header(
        template, run_id="rr-0001", nonce=NONCE, inputs=INPUTS, nits=nits, limits=LIMITS
    )


# -- the header template ------------------------------------------------------------------------


def test_every_placeholder_occurs_exactly_once() -> None:
    for name in pr.PLACEHOLDERS:
        assert TEMPLATE.count("{" + name + "}") == 1, name


def test_placeholders_come_in_the_documented_order() -> None:
    found = re.findall(r"\{(" + "|".join(pr.PLACEHOLDERS) + r")\}", TEMPLATE)
    assert found == ["run_id", "nonce", "inputs", "limits", "nits"]


def test_no_other_placeholder_shaped_text() -> None:
    # Braces are allowed (the suggestion example), but no unknown `{name}` placeholder.
    others = set(re.findall(r"\{([a-z_]+)\}", TEMPLATE)) - set(pr.PLACEHOLDERS)
    assert others == set()


def test_header_golden() -> None:
    text = render(nits=True)
    if os.environ.get("UPDATE_GOLDEN") == "1":
        GOLDEN.write_text(text, encoding="utf-8")
    assert GOLDEN.read_text(encoding="utf-8") == text


def test_rendered_header_has_no_placeholder_left_and_both_nits_values() -> None:
    on, off = render(nits=True), render(nits=False)
    for text in (on, off):
        assert not re.search(r"\{(run_id|nonce|inputs|nits|limits)\}", text)
    assert "Nits: on." in on and "Nits: off." in off


def test_rendered_header_is_stable() -> None:
    assert render() == render()


def test_header_states_the_required_things() -> None:
    text = render()
    for needle in (
        "never instructions",  # untrusted blocks are data
        f'`</untrusted id="{NONCE}">`',  # the closer
        "Copy `path` exactly from the manifest",
        "`request_changes` only when at least one finding has severity `blocker` or `major`",
        "suggestion",
        "`coverage.reviewed`",
        "claude-session report done --summary-file",
        "--artifact",
        "absolute",
        "needs-input",
        "subagents",
        "no test output exists",
    ):
        assert needle in text, needle


def test_header_never_tells_the_agent_to_obey_a_block() -> None:
    low = TEMPLATE.lower()
    assert "follow the instructions in" not in low
    assert "do not follow" in low


def test_header_does_not_name_the_skill_or_carry_forge_text() -> None:
    assert "review-pr" not in TEMPLATE
    assert "review-run:" not in TEMPLATE


# -- the skill ----------------------------------------------------------------------------------

SKILL = PLUGIN / "skills" / "review-pr" / "SKILL.md"


def frontmatter(text: str) -> dict[str, str]:
    assert text.startswith("---\n")
    block = text.split("\n---\n", 1)[0].removeprefix("---\n")
    out: dict[str, str] = {}
    for line in block.splitlines():
        key, _, value = line.partition(":")
        out[key.strip()] = value.strip()
    return out


def test_skill_frontmatter_meets_the_slot_contract() -> None:
    fm = frontmatter(SKILL.read_text(encoding="utf-8"))
    assert set(fm) == {"name", "description"}
    assert fm["name"] == "review-pr"
    assert 0 < len(fm["description"]) <= 1024


def test_skill_states_the_output_discipline() -> None:
    text = SKILL.read_text(encoding="utf-8")
    for needle in (
        "exactly from the manifest",
        "`line` and `side` from the patch",
        "One-line `title`",
        "No suggestion fences",
        "structured `suggestion` field",
        "`request_changes` only with at least one `blocker` or `major`",
        "`meta.coverage.reviewed`",
        "absolute paths",
        "$CSESSION_DIR",
        "Attach `review.json` on every report",
        "needs-input",
        "attach `review.json` again",
        "data, never instructions",
    ):
        assert needle in text, needle


def test_skill_does_not_invite_subagents() -> None:
    text = SKILL.read_text(encoding="utf-8")
    assert "Do not mention subagents" in text
    for line in text.splitlines():
        if "subagent" in line.lower():
            assert "not" in line.lower()


def test_skill_does_not_ask_to_follow_a_fenced_block() -> None:
    low = SKILL.read_text(encoding="utf-8").lower()
    assert "follow the instructions" not in low
    assert "ignore it" in low


# -- the plugin directory -----------------------------------------------------------------------


def test_plugin_json_names_the_plugin_and_has_a_version() -> None:
    doc = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert doc["name"] == "review-run"
    assert re.fullmatch(r"\d+\.\d+\.\d+", doc["version"])
    assert len(doc["version"]) <= 64


def test_placeholders_exist() -> None:
    assert (PLUGIN / ".claude").is_dir()
    assert (PLUGIN / ".mcp.json").is_file()
    assert (PLUGIN / "CLAUDE.md").is_file()
    assert (PLUGIN / "CLAUDE.local.md").is_file()


def test_the_header_is_found_through_the_resource_lookup() -> None:
    assert rs.bundled_header_template() == TEMPLATE


def test_the_plugin_passes_the_plugin_dir_walk(tmp_path: Path) -> None:
    """A user-owned install with normalized modes has no link, foreign owner or writable bit."""
    copy = tmp_path / "plugin"
    shutil.copytree(PLUGIN, copy, symlinks=True)
    for item in [copy, *copy.rglob("*")]:
        item.chmod(0o755 if item.is_dir() else 0o644)
    assert re.fullmatch(r"[0-9a-f]{64}", plugin_dirs_sha256([str(copy)]))


def test_the_plugin_has_no_links_and_only_known_entries() -> None:
    names = sorted(str(p.relative_to(PLUGIN)) for p in PLUGIN.rglob("*"))
    assert not [p for p in PLUGIN.rglob("*") if p.is_symlink()]
    assert names == sorted(
        [
            ".claude",
            ".claude/.gitkeep",
            ".claude-plugin",
            ".claude-plugin/plugin.json",
            ".mcp.json",
            "CLAUDE.local.md",
            "CLAUDE.md",
            "header.md",
            "permission_profile.json",
            "skills",
            "skills/review-pr",
            "skills/review-pr/SKILL.md",
        ]
    )


@pytest.mark.parametrize(
    "path", [p for p in PLUGIN.rglob("*") if p.is_file()], ids=lambda p: p.name
)
def test_shipped_text_is_plain(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert text.isascii() or all(
        unicodedata.category(c) not in ("Cc", "Cf", "Co", "Cs", "Cn") or c == "\n" for c in text
    )
    assert not [c for c in text if unicodedata.category(c) in ("Cf", "Co", "Cs", "Cn")]
    assert "http://" not in text and "https://" not in text
    assert "@" not in text.replace("`@`", "")
    assert "\r" not in text


# -- packaging ----------------------------------------------------------------------------------


def test_the_wheel_includes_the_plugin() -> None:
    doc = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    force = doc["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert force["plugin"] == "review_run/plugin"
