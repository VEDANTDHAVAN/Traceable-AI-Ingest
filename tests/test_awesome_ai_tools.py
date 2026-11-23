from datetime import datetime, timezone

import pytest

from ai_ingest.sources.awesome_ai_tools import (
    normalize_pricing,
    normalize_product_url,
    normalize_tool,
)


NOW = datetime(
    2026, 1, 1,
    tzinfo=timezone.utc,
)


def tool_payload():
    return {
        "name": "Synthetic Tool",
        "url": "https://example.com/tool/",
        "description": "A synthetic tool.",
        "category": "synthetic-category",
        "subcategory": {
            "id": "synthetic-subcategory",
            "name": "Synthetic Subcategory",
        },
        "order": 1,
        "openSource": None,
        "pricing": "Freemium",
        "status": "listed",
        "source": {
            "kind": "readme-migration",
            "migratedAt": "2026-09-21",
        },
    }


def test_product_record_is_schema_valid():
    record, evidence = normalize_tool(
        tool_payload(),
        source_path="catalog/tools/synthetic-tool.json",
        collected_at=NOW,
    )

    assert record.recordType == "PRODUCT"
    assert record.content.productName == "Synthetic Tool"
    assert record.content.pricingModel == "FREEMIUM"
    assert record.content.startupName is None
    assert evidence["company_resolution_status"] == (
        "NOT_ATTEMPTED"
    )


def test_pricing_is_normalized_case_insensitively():
    assert normalize_pricing(" free ") == "FREE"
    assert normalize_pricing("Paid") == "PAID"


def test_unknown_pricing_remains_null():
    assert normalize_pricing("Contact sales") is None


def test_non_https_url_rejected():
    payload = tool_payload()
    payload["url"] = "http://example.com/tool"

    with pytest.raises(ValueError):
        normalize_tool(
            payload,
            source_path="catalog/tools/synthetic.json",
            collected_at=NOW,
        )


def test_non_listed_tool_rejected():
    payload = tool_payload()
    payload["status"] = "removed"

    with pytest.raises(ValueError, match="not listed"):
        normalize_tool(
            payload,
            source_path="catalog/tools/synthetic.json",
            collected_at=NOW,
        )


def test_query_and_fragment_removed():
    assert normalize_product_url(
        "https://example.com/tool/?utm_source=x#top"
    ) == "https://example.com/tool"