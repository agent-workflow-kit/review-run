"""The fake forgectl: grammar, scenarios, exit codes and pinned-schema validity."""

from __future__ import annotations

import json
from typing import Any

import pytest

from review_run.gateway import CallTimeout
from tests.fakebin import _forgectl as fg
from tests.fakes import FakeBin

PR = "github.com/acme/widgets#7"


def run(fb: FakeBin, *argv: str) -> tuple[int, Any, str]:
    result = fb.run("forgectl", *argv)
    try:
        doc = json.loads(result.stdout)
    except ValueError:
        doc = None
    return result.returncode, doc, result.stderr


@pytest.mark.parametrize("command", ["show", "files", "diff", "ci"])
def test_default_outputs_validate_and_echo_the_canonical_pr(fakebin: FakeBin, command: str) -> None:
    code, doc, _ = run(fakebin, command, "https://github.com/Acme/widgets/pull/7", "--json")
    assert code == 0
    assert doc["pr"] == "github.com/Acme/widgets#7"
    fakebin.assert_valid()
    assert fakebin.calls("forgectl")[0].schema == f"forgectl/cli/{command}.json"


def test_show_reports_an_open_pr_with_a_head(fakebin: FakeBin) -> None:
    _, doc, _ = run(fakebin, "show", PR, "--json")
    assert doc["state"] == "open" and doc["head"]["sha"] == fg.HEAD


def test_merged_scenario(fakebin: FakeBin) -> None:
    fakebin.use("forgectl", "merged")
    code, doc, _ = run(fakebin, "show", PR, "--json")
    assert code == 0 and doc["state"] == "merged" and doc["merged_head_sha"] == fg.HEAD


def test_closed_and_draft_scenarios(fakebin: FakeBin) -> None:
    fakebin.use("forgectl", "closed")
    assert run(fakebin, "show", PR, "--json")[1]["state"] == "closed"
    fakebin.use("forgectl", "draft")
    assert run(fakebin, "show", PR, "--json")[1]["draft"] is True


def test_ci_head_flip_happens_once(fakebin: FakeBin) -> None:
    fakebin.use("forgectl", "head_flip", on="ci")
    first = run(fakebin, "ci", PR, "--json")[1]
    second = run(fakebin, "ci", PR, "--json")[1]
    assert first["head_sha"] == first["ci"]["head_sha"] == fg.NEW_HEAD
    assert second["head_sha"] == fg.HEAD


def test_head_flip_on_diff_and_always(fakebin: FakeBin) -> None:
    fakebin.use("forgectl", "head_flip_always", on="diff")
    assert run(fakebin, "diff", PR, "--json")[1]["head_sha"] == fg.NEW_HEAD
    assert run(fakebin, "diff", PR, "--json")[1]["head_sha"] == fg.NEW_HEAD


def test_files_incomplete(fakebin: FakeBin) -> None:
    fakebin.use("forgectl", "files_incomplete")
    code, doc, _ = run(fakebin, "files", PR, "--json")
    assert code == 0 and doc["files_complete"] is False


def test_diff_too_large_is_a_refusal_envelope(fakebin: FakeBin) -> None:
    fakebin.use("forgectl", "too_large")
    code, doc, err = run(fakebin, "diff", PR, "--json")
    assert code == 20
    assert doc["error"]["code"] == "refused" and doc["error"]["reason"] == "too_large"
    assert "too large" in err


def test_not_found_and_rate_limited(fakebin: FakeBin) -> None:
    fakebin.use("forgectl", "not_found")
    assert run(fakebin, "show", PR, "--json")[0] == 10
    fakebin.use("forgectl", "rate_limited")
    code, doc, _ = run(fakebin, "show", PR, "--json")
    assert code == 12 and doc["error"]["retry_after"] == 30


def test_unknown_host_exits_20(fakebin: FakeBin) -> None:
    fakebin.use("forgectl", "unknown_host")
    code, doc, _ = run(fakebin, "show", PR, "--json")
    assert code == 20 and doc["error"]["reason"] == "unknown_host"


def test_ci_failing_and_none(fakebin: FakeBin) -> None:
    fakebin.use("forgectl", "ci_failing")
    assert run(fakebin, "ci", PR, "--json")[1]["ci"]["state"] == "failing"
    fakebin.use("forgectl", "ci_none")
    assert run(fakebin, "ci", PR, "--json")[1]["ci"]["state"] == "none"


@pytest.mark.parametrize(
    "pr",
    [
        "",
        "not a pr",
        "github.com/acme/widgets",  # no number
        "github.com/acme/widgets#0",
        "github.com/acme/widgets#01",
        "github.com/acme/widgets#12345678901",
        "github.com/acme#7",  # GitHub takes exactly two segments
        "github.com/a/b/c#7",
        "github.com/../widgets#7",
        "github.com/-acme/widgets#7",
        "http://github.com/acme/widgets/pull/7",
        "https://user@github.com/acme/widgets/pull/7",
        "https://github.com/acme/widgets/pull/7%20",
        "https://github.com/acme/widégets/pull/7",
        "https://github.com/acme/widgets/issues/7",
        "github.com/acme/widgets#7\n",
        "x" * 3000,
    ],
)
def test_invalid_pr_is_a_usage_error(fakebin: FakeBin, pr: str) -> None:
    code, doc, _ = run(fakebin, "show", pr, "--json")
    assert code == 2
    assert doc["error"]["code"] == "invalid_pr"


@pytest.mark.parametrize(
    "argv",
    [
        ["show"],  # missing PR
        ["show", PR, "extra"],
        ["show", PR, "--nope"],
        ["show", PR, "--jso"],  # abbreviations are not accepted
        ["show", PR, "--json", "--json"],
        ["show", PR, "--timeout"],
        ["show", PR, "--timeout", "0"],
        ["show", PR, "--timeout", "soon"],
        ["show", PR, "--verbose", "--quiet"],
        ["show", PR, "--yes"],  # write-only flag
        ["show", PR, "--actor", "human:me"],
        ["frobnicate", PR],
        ["--", "show", PR],
        [],
    ],
)
def test_the_grammar_rejects_what_the_real_tool_rejects(fakebin: FakeBin, argv: list[str]) -> None:
    code, doc, err = run(fakebin, *argv, "--json")
    assert code == 2, argv
    assert doc["error"]["code"] in ("usage", "invalid_pr")
    assert err.startswith("forgectl: error:")


def test_global_flags_are_accepted_before_and_after_the_command(fakebin: FakeBin) -> None:
    assert run(fakebin, "--json", "--timeout", "30s", "show", PR)[0] == 0
    assert run(fakebin, "show", PR, "--json", "--config", "/nonexistent")[0] == 0
    assert run(fakebin, "show", "--json", PR)[0] == 0


def test_a_usage_error_without_json_prints_text_only(fakebin: FakeBin) -> None:
    code, doc, err = run(fakebin, "show")
    assert code == 2 and doc is None and "forgectl" in err


def test_without_json_the_output_is_not_json(fakebin: FakeBin) -> None:
    code, doc, _ = run(fakebin, "show", PR)
    assert code == 0 and doc is None
    assert fakebin.calls("forgectl")[0].schema is None


def test_real_commands_the_fake_does_not_emulate_exit_99(fakebin: FakeBin) -> None:
    assert run(fakebin, "merge", PR, "--json")[0] == 99


def test_version(fakebin: FakeBin) -> None:
    result = fakebin.run("forgectl", "--version")
    assert (result.returncode, result.stdout) == (0, "forgectl 0.56.0\n")


def test_every_call_is_recorded_with_exact_argv(fakebin: FakeBin) -> None:
    run(fakebin, "show", PR, "--json")
    (call,) = fakebin.calls("forgectl")
    assert call.argv == ["show", PR, "--json"] and call.exit == 0 and call.signal is None
    assert call.env["PATH"] == str(fakebin.bin)


def test_garbage_and_exit_overrides(fakebin: FakeBin) -> None:
    fakebin.script("forgectl", "show", {"stdout_raw": "{not json", "exit": 0})
    result = fakebin.run("forgectl", "show", PR, "--json")
    assert result.stdout == "{not json"
    fakebin.assert_valid()  # a raw step records no schema: nothing to validate


def test_an_invalid_document_fails_the_harness(fakebin: FakeBin) -> None:
    fakebin.script("forgectl", "show", {"merge": {"state": "exploded"}})
    run(fakebin, "show", PR, "--json")
    with pytest.raises(AssertionError, match="pinned schema rejects"):
        fakebin.assert_valid()
    # the fixture would re-raise at teardown: forget the bad call
    (fakebin.dir / "calls.jsonl").write_text("", encoding="utf-8")


def test_a_hanging_fake_is_ended_by_the_gateway_timeout(fakebin: FakeBin) -> None:
    fakebin.use("forgectl", "hang", command="diff")
    with pytest.raises(CallTimeout):
        fakebin.run("forgectl", "diff", PR, "--json", timeout=0.6)
    (call,) = fakebin.calls("forgectl")
    assert not call.finished  # killed with SIGKILL: no end record
    with pytest.raises(ProcessLookupError):
        import os

        os.kill(call.pid, 0)


def test_sigterm_ends_a_hanging_fake_with_exit_143(fakebin: FakeBin) -> None:
    import os
    import signal
    import subprocess
    import time

    fakebin.use("forgectl", "hang")
    proc = subprocess.Popen(
        [fakebin.argv0("forgectl"), "show", PR, "--json"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=fakebin.env(),
        start_new_session=True,
    )
    deadline = time.monotonic() + 10
    while not fakebin.all_calls() and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(0.2)
    os.kill(proc.pid, signal.SIGTERM)  # the PID we launched
    assert proc.wait(timeout=10) == 143
    assert fakebin.calls("forgectl")[0].signal == signal.SIGTERM
