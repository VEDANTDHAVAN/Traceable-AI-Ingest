from datetime import datetime, timedelta, timezone

import pytest

from ai_ingest.freshness import classify_freshness, parse_publication


NOW = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("value", "expected_delta"),
    [
        ("2 hours ago", timedelta(hours=2)),
        ("1 day ago", timedelta(days=1)),
        ("3 min ago", timedelta(minutes=3)),
        ("just now", timedelta(0)),
        ("yesterday", None),
    ],
)
def test_relative_dates(value, expected_delta):
    parsed = parse_publication(value, now=NOW)
    assert parsed is not None
    if expected_delta is not None:
        assert NOW - parsed == expected_delta


def test_absolute_timezone_aware_date():
    parsed = parse_publication("2026-11-23T10:00:00+02:00", now=NOW)
    assert parsed == datetime(2026, 11, 23, 8, 0, tzinfo=timezone.utc)


def test_naive_datetime_is_rejected_not_assumed():
    assert parse_publication("2026-11-23T10:00:00", now=NOW) is None
    assert parse_publication(datetime(2026, 11, 23, 10, 0), now=NOW) is None


def test_epoch_is_utc():
    parsed = parse_publication(1_792_000_000, now=NOW)
    assert parsed is not None
    assert parsed.tzinfo == timezone.utc


def test_unknown_and_garbage_are_unknown():
    assert parse_publication(None, now=NOW) is None
    assert parse_publication("", now=NOW) is None
    assert parse_publication("last Tuesday", now=NOW) is None


def test_freshness_classification():
    assert classify_freshness(NOW - timedelta(hours=1), now=NOW) == "FRESH_24H"
    assert classify_freshness(NOW - timedelta(hours=25), now=NOW) == "STALE"
    assert classify_freshness(NOW + timedelta(hours=1), now=NOW) == "FUTURE"
    assert classify_freshness(None, now=NOW) == "UNKNOWN"
