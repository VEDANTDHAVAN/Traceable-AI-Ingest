from datetime import datetime, timezone

import pytest

from ai_ingest.sources.arxiv import (
    ArxivError,
    parse_feed,
    retry_after_seconds,
)


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)

FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/0000.00000v1</id>
    <title> Synthetic
      Paper Title </title>
    <published>2025-12-31T12:30:00Z</published>
    <author><name>Fixture Author</name></author>
  </entry>
</feed>
"""


def test_parse_paper():
    records = parse_feed(FEED, collected_at=NOW)

    assert len(records) == 1

    record = records[0]

    assert record.content.title == "Synthetic Paper Title"
    assert record.content.authors == ["Fixture Author"]
    assert str(record.content.paper_url) == (
        "https://arxiv.org/abs/0000.00000v1"
    )
    assert record.source.name == "arXiv"
    assert record.collectedAt == NOW


def test_github_metrics_are_not_invented():
    record = parse_feed(FEED, collected_at=NOW)[0]

    assert record.content.github_url is None
    assert record.content.github_stars is None
    assert record.content.github_metrics_collected_at is None


def test_empty_feed_is_valid():
    raw = b'<feed xmlns="http://www.w3.org/2005/Atom"/>'

    assert parse_feed(raw, collected_at=NOW) == []


def test_invalid_xml_rejected():
    with pytest.raises(ArxivError):
        parse_feed(b"<broken", collected_at=NOW)


def test_non_atom_response_rejected():
    with pytest.raises(ArxivError):
        parse_feed(b"<html>Blocked</html>", collected_at=NOW)


def test_api_error_entry_rejected():
    raw = b"""
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <id>http://arxiv.org/api/errors#invalid_query</id>
        <summary>Synthetic error</summary>
      </entry>
    </feed>
    """

    with pytest.raises(ArxivError, match="Synthetic error"):
        parse_feed(raw, collected_at=NOW)


def test_missing_title_rejected():
    raw = FEED.replace(
        b"<title> Synthetic\n      Paper Title </title>",
        b"",
    )

    with pytest.raises(ArxivError, match="title"):
        parse_feed(raw, collected_at=NOW)


def test_retry_after_seconds():
    assert retry_after_seconds("15", now=NOW) == 15.0


def test_retry_after_http_date():
    assert retry_after_seconds(
        "Thu, 01 Jan 2026 00:00:30 GMT",
        now=NOW,
    ) == 30.0


def test_invalid_retry_after():
    assert retry_after_seconds("not-a-date", now=NOW) is None