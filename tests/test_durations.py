import datetime as dt

import pytest

from review_run.durations import DurationError, parse_duration, parse_since


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0", dt.timedelta(0)),
        ("0s", dt.timedelta(0)),
        ("5ms", dt.timedelta(milliseconds=5)),
        ("30s", dt.timedelta(seconds=30)),
        ("10m", dt.timedelta(minutes=10)),
        ("2h", dt.timedelta(hours=2)),
        ("30d", dt.timedelta(days=30)),
    ],
)
def test_valid_durations(text: str, expected: dt.timedelta) -> None:
    assert parse_duration(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        " ",
        "5",
        "s",
        "-1s",
        "+1s",
        "1.5s",
        "1w",
        "1S",
        "1 s",
        " 1s",
        "1s ",
        "1s\n",
        "1sm",
        "0x10s",
        "١s",  # Arabic-indic digit one: not an ASCII digit
        "1\u200bs",
        "9" * 40 + "d",
        "99999999999d",
    ],
)
def test_invalid_durations(text: str) -> None:
    with pytest.raises(DurationError):
        parse_duration(text)


def test_error_message_never_echoes_the_text() -> None:
    with pytest.raises(DurationError) as exc:
        parse_duration("SECRET-VALUE")
    assert "SECRET" not in str(exc.value)


def test_positive_rejects_zero() -> None:
    assert parse_duration("1s", positive=True) == dt.timedelta(seconds=1)
    with pytest.raises(DurationError):
        parse_duration("0", positive=True)
    with pytest.raises(DurationError):
        parse_duration("0ms", positive=True)


def test_since_takes_duration_or_rfc3339() -> None:
    assert parse_since("2h") == dt.timedelta(hours=2)
    stamp = parse_since("2026-03-10T10:00:00Z")
    assert stamp == dt.datetime(2026, 3, 10, 10, 0, tzinfo=dt.UTC)
    assert parse_since("2026-03-10T10:00:00.5+02:00") == dt.datetime(
        2026, 3, 10, 8, 0, 0, 500000, tzinfo=dt.UTC
    )


@pytest.mark.parametrize(
    "text",
    ["", "2026-03-10", "2026-03-10T10:00:00", "yesterday", "2026-13-10T10:00:00Z", "1x"],
)
def test_since_rejects_other_text(text: str) -> None:
    with pytest.raises(DurationError):
        parse_since(text)


def test_leading_zeros_do_not_count_toward_the_digit_cap() -> None:
    assert parse_duration("0000000000000001s") == dt.timedelta(seconds=1)
    assert parse_duration("0" * 30 + "s") == dt.timedelta(0)
    with pytest.raises(DurationError):
        parse_duration("0" * 30 + "9" * 16 + "s")


@pytest.mark.parametrize("stamp", ["0001-01-01T00:00:00+05:00", "9999-12-31T23:59:59-05:00"])
def test_since_timestamp_overflow_is_a_duration_error(stamp: str) -> None:
    with pytest.raises(DurationError):
        parse_since(stamp)
