"""The recording fake: scripting, recording, and the rules every gateway call must meet."""

from __future__ import annotations

import pytest

from review_run import gateway as gw
from review_run.gateway import CallTimeout, CompletedRun, GatewayError, OutputTooLarge
from tests.fake_gateway import FakeGateway, ok

MARKER = "UNTRUSTED-MARKER-7f3a"


def test_records_argv_env_names_cwd_and_timeout(fake_gateway: FakeGateway) -> None:
    fake_gateway.script("forgectl", ok('{"x": 1}'))
    result = gw.run(["forgectl", "show", "1"], timeout=60, env={"B": "2", "A": "1"}, cwd="/w")
    assert result.stdout == '{"x": 1}'
    (call,) = fake_gateway.calls
    assert call.argv == ("forgectl", "show", "1")
    assert call.env_names == ["A", "B"]
    assert call.cwd == "/w"
    assert call.timeout == 60


def test_an_unscripted_call_raises(fake_gateway: FakeGateway) -> None:
    with pytest.raises(AssertionError, match="unscripted"):
        gw.run(["gh", "pr", "view"], timeout=5)


def test_a_call_without_a_timeout_raises_on_the_fake_too(fake_gateway: FakeGateway) -> None:
    fake_gateway.script("x", ok())
    with pytest.raises(TypeError):
        gw.run(["x"])  # type: ignore[call-arg]
    with pytest.raises(ValueError):
        gw.run(["x"], timeout=0)
    assert fake_gateway.calls == []  # a refused call is not recorded as made


def test_scripted_errors_and_queues(fake_gateway: FakeGateway) -> None:
    fake_gateway.script("w", CallTimeout("t"), ok("second"), ok("third"))
    with pytest.raises(CallTimeout):
        gw.run(["w"], timeout=1)
    assert gw.run(["w"], timeout=1).stdout == "second"
    assert gw.run(["w"], timeout=1).stdout == "third"
    assert gw.run(["w"], timeout=1).stdout == "third"  # the last result repeats


def test_a_literal_result_is_returned(fake_gateway: FakeGateway) -> None:
    fake_gateway.script("w", CompletedRun(("w",), 12, "", "boom"))
    assert gw.run(["w"], timeout=1).returncode == 12


def test_stdin_is_recorded_and_the_marker_check_sees_only_argv_env_and_cwd(
    fake_gateway: FakeGateway,
) -> None:
    fake_gateway.script("t", ok())
    gw.run(["t", "draft", "check", "-"], timeout=1, stdin=f"body {MARKER}")
    fake_gateway.assert_no_marker(MARKER)  # stdin is where untrusted text belongs


@pytest.mark.parametrize(
    "kwargs",
    [
        {"argv": ["t", f"--title={MARKER}"]},
        {"argv": ["t"], "env": {"K": f"x{MARKER}"}},
        {"argv": ["t"], "env": {f"K{MARKER}": "v"}},
        {"argv": ["t"], "cwd": f"/tmp/{MARKER}"},
    ],
)
def test_the_marker_check_fails_when_untrusted_text_reaches_a_call(
    fake_gateway: FakeGateway, kwargs: dict
) -> None:
    fake_gateway.script("t", ok())
    gw.run(kwargs["argv"], timeout=1, env=kwargs.get("env"), cwd=kwargs.get("cwd"))
    with pytest.raises(AssertionError, match="leaked"):
        fake_gateway.assert_no_marker(MARKER)


def test_the_fixture_restores_the_real_gateway() -> None:
    assert isinstance(gw.get_gateway(), gw.SystemGateway)


# --- the fake enforces the same call rules as the real gateway --------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"cwd": "a\0b"},
        {"cwd": 5},
        {"env": {"": "v"}},
        {"env": {"A=B": "v"}},
        {"env": {"A\0": "v"}},
        {"env": {"A": "v\0"}},
        {"env": {"A": 1}},
        {"env": ["A=1"]},
        {"stdin": b"bytes"},
    ],
)
def test_the_fake_refuses_bad_arguments_like_the_real_gateway(
    fake_gateway: FakeGateway, kwargs: dict
) -> None:
    fake_gateway.script("x", ok())
    with pytest.raises(ValueError):
        gw.run(["x"], timeout=1, **kwargs)
    assert fake_gateway.calls == []


def test_the_fake_refuses_unencodable_and_oversized_stdin(
    fake_gateway: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_gateway.script("x", ok())
    with pytest.raises(GatewayError) as info:
        gw.run(["x"], timeout=1, stdin="s\ud800")
    assert info.value.lower_code == "failed"
    monkeypatch.setattr(gw, "MAX_OUTPUT", 10)
    with pytest.raises(GatewayError):
        gw.run(["x"], timeout=1, stdin="x" * 11)
    gw.run(["x"], timeout=1, stdin="x" * 10)


def test_a_scripted_result_carries_the_calls_argv(fake_gateway: FakeGateway) -> None:
    fake_gateway.script("w", CompletedRun(("something", "else"), 0, "o", ""))
    assert gw.run(["w", "a"], timeout=1).argv == ("w", "a")


def test_scripted_output_over_the_cap_raises_like_the_real_gateway(
    fake_gateway: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gw, "MAX_OUTPUT", 10)
    fake_gateway.script("big", CompletedRun(("big",), 0, "x" * 11, ""), ok(stderr="é" * 6))
    with pytest.raises(OutputTooLarge):
        gw.run(["big"], timeout=1)
    with pytest.raises(OutputTooLarge):  # counted in encoded bytes, like the real stream
        gw.run(["big"], timeout=1)
    fake_gateway.script("fine", CompletedRun(("fine",), 0, "x" * 10, "y" * 10))
    assert gw.run(["fine"], timeout=1).returncode == 0
