import pytest

from review_run.cli import main


def test_version_prints_version_and_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == "review-run 0.0.0"


def test_no_args_returns_usage_error() -> None:
    assert main([]) == 2
