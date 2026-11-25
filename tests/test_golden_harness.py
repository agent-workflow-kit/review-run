from pathlib import Path

import pytest
from jsonschema import ValidationError

from tests import golden_harness
from tests.golden_harness import check_golden, render

GOOD = {"error": {"code": "internal", "message": "x"}}


@pytest.fixture
def golden_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "golden"
    directory.mkdir()
    monkeypatch.setattr(golden_harness, "GOLDEN", directory)
    monkeypatch.delenv("UPDATE_GOLDEN", raising=False)
    return directory


def test_matching_golden_passes(golden_dir: Path) -> None:
    (golden_dir / "g.json").write_text(render(GOOD))
    check_golden("g.json", "cli/error.json", GOOD)


def test_changed_output_fails_against_the_golden(golden_dir: Path) -> None:
    (golden_dir / "g.json").write_text(render(GOOD))
    changed = {"error": {"code": "internal", "message": "y"}}
    with pytest.raises(AssertionError):
        check_golden("g.json", "cli/error.json", changed)


def test_output_that_no_longer_validates_fails_even_if_the_golden_matches(
    golden_dir: Path,
) -> None:
    bad = {"error": {"code": "internal", "message": "x", "extra": 1}}
    (golden_dir / "g.json").write_text(render(bad))
    with pytest.raises(ValidationError):
        check_golden("g.json", "cli/error.json", bad)


def test_update_golden_still_validates_before_writing(
    golden_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UPDATE_GOLDEN", "1")
    with pytest.raises(ValidationError):
        check_golden("g.json", "cli/error.json", {"nope": 1})
    assert not (golden_dir / "g.json").exists()
    check_golden("g.json", "cli/error.json", GOOD)
    assert (golden_dir / "g.json").read_text() == render(GOOD)


def test_missing_golden_fails(golden_dir: Path) -> None:
    with pytest.raises(FileNotFoundError):
        check_golden("absent.json", "cli/error.json", GOOD)
