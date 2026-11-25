import json
from importlib import metadata

import pytest

from review_run import __version__, cli
from review_run.cli import build_parser, main
from tests.conftest import SCHEMAS, validate_against
from tests.golden_harness import check_golden
from tests.helpers import run_cli

COMMANDS: list[list[str]] = [
    ["run", "https://example.test/o/r/pull/1"],
    ["resume", "rr-aaaa", "--prompt-file", "-"],
    ["wait", "rr-aaaa"],
    ["list"],
    ["show", "rr-aaaa"],
    ["reconcile"],
    ["prune"],
    ["cancel", "rr-aaaa"],
    ["cron", "install"],
    ["cron", "uninstall"],
    ["export"],
]


def test_version_prints_version_and_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"review-run {metadata.version('review-run')}"


def test_dunder_version_is_the_installed_version() -> None:
    assert __version__ == metadata.version("review-run")


def test_no_args_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    res = run_cli(capsys)
    assert res.code == 2
    assert res.out == ""
    assert "usage" in res.err


def test_bad_flag_json_prints_usage_envelope(capsys: pytest.CaptureFixture[str]) -> None:
    res = run_cli(capsys, "--json", "list", "--bogus")
    assert res.code == 2
    doc = res.envelope()
    validate_against("cli/error.json", doc)
    assert doc["error"]["code"] == "usage"
    assert res.err.count("\n") == 1


def test_bad_flag_json_flag_after_subcommand(capsys: pytest.CaptureFixture[str]) -> None:
    res = run_cli(capsys, "list", "--bogus", "--json")
    assert res.code == 2
    assert res.envelope()["error"]["code"] == "usage"


def test_bad_flag_human_is_one_line_on_stderr_only(capsys: pytest.CaptureFixture[str]) -> None:
    res = run_cli(capsys, "list", "--bogus")
    assert res.code == 2
    assert res.out == ""
    lines = res.err.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("review-run: usage: ")


def test_unknown_subcommand_is_usage(capsys: pytest.CaptureFixture[str]) -> None:
    res = run_cli(capsys, "--json", "frobnicate")
    assert res.code == 2
    assert res.envelope()["error"]["code"] == "usage"


def test_missing_required_argument_is_usage(capsys: pytest.CaptureFixture[str]) -> None:
    res = run_cli(capsys, "--json", "show")
    assert res.code == 2
    assert res.envelope()["error"]["code"] == "usage"


def test_hostile_argument_is_not_echoed_unsanitized(capsys: pytest.CaptureFixture[str]) -> None:
    res = run_cli(capsys, "list", "--state", "\x1b[31mevil\n\u202e")
    assert res.code == 2
    assert "\x1b" not in res.err and "\u202e" not in res.err
    assert len(res.err.splitlines()) == 1


@pytest.mark.parametrize("argv", COMMANDS, ids=lambda a: " ".join(a[:2]))
def test_every_command_is_a_stub_exiting_1_with_internal_envelope(
    capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    res = run_cli(capsys, "--json", *argv)
    assert res.code == 1
    doc = res.envelope()
    validate_against("cli/error.json", doc)
    assert doc["error"]["code"] == "internal"
    assert res.err.count("\n") == 1


def test_stub_human_mode_prints_one_line_to_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    res = run_cli(capsys, "list")
    assert res.code == 1
    assert res.out == ""
    assert res.err.startswith("review-run: internal: ")


def test_global_flags_accepted_before_and_after_the_subcommand() -> None:
    parser = build_parser()
    before = parser.parse_args(["--json", "--verbose", "--config", "/x.yaml", "list"])
    after = parser.parse_args(["list", "--json", "--verbose", "--config", "/x.yaml"])
    for ns in (before, after):
        assert (ns.json, ns.verbose, ns.config) == (True, True, "/x.yaml")


def test_global_flag_after_the_subcommand_wins_over_before() -> None:
    ns = build_parser().parse_args(["--config", "a", "list", "--config", "b"])
    assert ns.config == "b"


def test_verbose_with_quiet_is_accepted(capsys: pytest.CaptureFixture[str]) -> None:
    assert run_cli(capsys, "--verbose", "--quiet", "list").code == 1


def test_decision_id_limit(capsys: pytest.CaptureFixture[str]) -> None:
    def argv(n: int) -> list[str]:
        return ["run", "p", *[a for i in range(n) for a in ("--decision-id", f"d{i}")]]

    assert run_cli(capsys, "--json", *argv(32)).code == 1
    assert run_cli(capsys, "--json", *argv(33)).code == 2


def test_timeout_text_is_kept_as_given() -> None:
    parser = build_parser()
    one = parser.parse_args(["run", "p", "--timeout", "1h"])
    sixty = parser.parse_args(["run", "p", "--timeout", "60m"])
    assert one.timeout == sixty.timeout
    assert (one.timeout_text, sixty.timeout_text) == ("1h", "60m")
    assert parser.parse_args(["wait", "rr-aaaa", "--timeout", "90s"]).timeout_text == "90s"
    assert parser.parse_args(["run", "p"]).timeout_text is None


@pytest.mark.parametrize("command", ["list", "export"])
@pytest.mark.parametrize("stamp", ["0001-01-01T00:00:00+05:00", "9999-12-31T23:59:59-05:00"])
def test_since_overflow_is_a_usage_error(
    capsys: pytest.CaptureFixture[str], command: str, stamp: str
) -> None:
    res = run_cli(capsys, "--json", command, "--since", stamp)
    assert res.code == 2
    assert res.envelope()["error"]["code"] == "usage"


def _boom(args: object) -> int:
    raise RuntimeError("SECRET message")


def test_unexpected_exception_is_an_internal_envelope(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(cli._HANDLERS, "list", _boom)
    res = run_cli(capsys, "--json", "list")
    assert res.code == 1
    validate_against("cli/error.json", res.envelope())
    assert res.envelope()["error"]["code"] == "internal"
    assert res.err.count("\n") == 1
    assert "SECRET" not in res.err + res.out


def test_unexpected_exception_verbose_names_type_and_location(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(cli._HANDLERS, "list", _boom)
    res = run_cli(capsys, "--json", "--verbose", "list")
    assert res.code == 1
    assert json.loads(res.out)["error"]["code"] == "internal"
    lines = res.err.splitlines()
    assert len(lines) == 2
    assert "RuntimeError" in lines[1] and "test_cli.py:" in lines[1]
    assert "SECRET" not in res.err


def test_keyboard_interrupt_returns_130_without_traceback(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupt(args: object) -> int:
        raise KeyboardInterrupt

    monkeypatch.setitem(cli._HANDLERS, "list", interrupt)
    res = run_cli(capsys, "list")
    assert res.code == 130
    assert "Traceback" not in res.err


class _BrokenStdout:
    def write(self, text: str) -> int:
        raise BrokenPipeError

    def flush(self) -> None:
        raise BrokenPipeError

    def fileno(self) -> int:
        raise OSError


def test_broken_stdout_keeps_the_exit_code(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_silence_stdout", lambda: None)
    monkeypatch.setattr(cli.sys, "stdout", _BrokenStdout())
    assert main(["--json", "list"]) == 1


class _ClosedStdout:
    def write(self, text: str) -> int:
        raise ValueError("I/O operation on closed file.")

    def flush(self) -> None:
        raise ValueError("I/O operation on closed file.")


def test_missing_stdout_keeps_the_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.sys, "stdout", None)
    assert main(["--json", "list"]) == 1


def test_closed_stdout_keeps_the_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_silence_stdout", lambda: None)
    monkeypatch.setattr(cli.sys, "stdout", _ClosedStdout())
    assert main(["--json", "list"]) == 1


def test_main_sets_umask_077(capsys: pytest.CaptureFixture[str]) -> None:
    import os

    os.umask(0o022)
    run_cli(capsys, "list")
    assert os.umask(0o077) == 0o077


# -- durations -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["run", "p", "--timeout", "30s"],
        ["wait", "rr-aaaa", "--timeout", "5m"],
        ["prune", "--older-than", "0"],
        ["prune", "--older-than", "30d"],
        ["list", "--since", "2h"],
        ["list", "--since", "2026-03-10T10:00:00Z"],
        ["export", "--since", "500ms"],
    ],
)
def test_valid_durations_reach_the_stub(
    capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    assert run_cli(capsys, "--json", *argv).code == 1


@pytest.mark.parametrize(
    "argv",
    [
        ["run", "p", "--timeout", "0"],  # --timeout must be greater than 0
        ["run", "p", "--timeout", "10"],
        ["run", "p", "--timeout", "1w"],
        ["run", "p", "--timeout", "-5s"],
        ["wait", "rr-aaaa", "--timeout", "abc"],
        ["prune", "--older-than", "1.5h"],
        ["prune", "--older-than", ""],
        ["list", "--since", "yesterday"],
        ["export", "--since", "2026-03-10"],
    ],
)
def test_invalid_durations_exit_2(capsys: pytest.CaptureFixture[str], argv: list[str]) -> None:
    res = run_cli(capsys, "--json", *argv)
    assert res.code == 2
    doc = res.envelope()
    validate_against("cli/error.json", doc)
    assert doc["error"]["code"] == "usage"


# -- other argument grammars -----------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["run", "p", "--mode", "fast"],
        ["run", "p", "--actor", "root"],
        ["run", "p", "--decision-id", "bad id"],
        ["run", "p", "--hook", "nocmd"],
        ["list", "--state", "unknown"],
        ["list", "--limit", "0"],
        ["list", "--limit", "x"],
        ["cron", "frobnicate"],
        ["resume", "rr-aaaa"],
    ],
)
def test_bad_values_exit_2(capsys: pytest.CaptureFixture[str], argv: list[str]) -> None:
    assert run_cli(capsys, "--json", *argv).code == 2


def test_repeatable_flags_parse(capsys: pytest.CaptureFixture[str]) -> None:
    res = run_cli(
        capsys,
        "--json",
        "run",
        "p",
        "--label",
        "a=b",
        "--label",
        "c=d",
        "--hook",
        "pre-run=true",
        "--decision-id",
        "d1",
        "--decision-id",
        "d2",
        "--actor",
        "human:me",
        "--override-escalation",
        "--no-wait",
        "--dry-run",
    )
    assert res.code == 1


# -- goldens ---------------------------------------------------------------------------------


def test_usage_envelope_golden(capsys: pytest.CaptureFixture[str]) -> None:
    res = run_cli(capsys, "--json", "list", "--limit", "0")
    assert res.code == 2
    check_golden("usage.json", "cli/error.json", res.envelope())


def test_internal_envelope_golden(capsys: pytest.CaptureFixture[str]) -> None:
    res = run_cli(capsys, "--json", "list")
    assert res.code == 1
    check_golden("internal.json", "cli/error.json", res.envelope())


def test_stdout_is_one_json_document(capsys: pytest.CaptureFixture[str]) -> None:
    res = run_cli(capsys, "--json", "list", "--bogus")
    assert res.out.endswith("\n")
    assert json.loads(res.out) == res.envelope()


def test_decision_id_grammar_matches_the_schema() -> None:
    schema = json.loads((SCHEMAS / "defs.json").read_text(encoding="utf-8"))
    node = schema["$defs"]["decision_ids"]
    assert node["items"]["pattern"] == f"^{cli._DECISION_ID_RE.pattern}$"
    assert node["maxItems"] == cli.MAX_DECISION_IDS
