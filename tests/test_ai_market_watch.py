from datetime import datetime, timezone

import pytest

from ai_ingest.sources.ai_market_watch import (
    normalize_profile_url,
    normalize_startup,
    parse_csv,
)


NOW = datetime(
    2026, 1, 1,
    tzinfo=timezone.utc,
)


def startup_row():
    return {
        "company_name": "Synthetic Startup",
        "category": "Synthetic Category",
        "country_code": "US",
        "year_founded": "2024",
        "funding_range": "Undisclosed",
        "profile_url": (
            "https://www.ai-market-watch.com/"
            "company/synthetic-startup"
        ),
    }


def test_startup_record_has_provenance():
    record, provenance = normalize_startup(
        startup_row(),
        collected_at=NOW,
    )

    assert record.recordType == "STARTUP"
    assert record.content.entityName == "Synthetic Startup"
    assert provenance["attribution"]
    assert provenance["profile_page_fetched"] is False


def test_employee_count_not_invented():
    record, _ = normalize_startup(
        startup_row(),
        collected_at=NOW,
    )

    assert record.content.data.employeeCount is None


def test_unexpected_profile_host_rejected():
    with pytest.raises(ValueError):
        normalize_profile_url(
            "https://example.org/company/synthetic"
        )


def test_profile_query_and_fragment_removed():
    value = normalize_profile_url(
        "https://ai-market-watch.com/company/synthetic/"
        "?utm_source=test#section"
    )

    assert value == (
        "https://www.ai-market-watch.com/company/synthetic"
    )


def test_documented_csv_headers_supported():
    raw = (
        "company_name,category,country_code,year_founded,"
        "funding_range,profile_url\n"
        "Synthetic Startup,AI,US,2024,Undisclosed,"
        "https://www.ai-market-watch.com/company/synthetic\n"
    ).encode("utf-8")

    rows = parse_csv(raw)

    assert len(rows) == 1
    assert rows[0]["company_name"] == "Synthetic Startup"


def test_unexpected_csv_schema_rejected():
    with pytest.raises(ValueError, match="Missing CSV columns"):
        parse_csv(b"name,url\nSynthetic,https://example.org\n")