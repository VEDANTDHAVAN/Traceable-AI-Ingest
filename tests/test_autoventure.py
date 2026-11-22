from datetime import datetime, timezone

import pytest

from ai_ingest.sources.autoventure import (
    normalize_pricing,
    normalize_tool,
)


NOW = datetime(
    2026, 1, 1,
    tzinfo=timezone.utc,
)


def tool():
    return {
        "slug": "synthetic-product",
        "name": "Synthetic Product",
        "pricing": "Freemium",
        "website": "https://example.org",
        "review_url": (
            "https://autoventure.live/"
            "tools/synthetic-product"
        ),
    }


def test_product_schema_and_provenance():
    record, provenance = normalize_tool(
        tool(),
        collected_at=NOW,
    )

    assert record.content.productName == "Synthetic Product"
    assert record.content.pricingModel == "FREEMIUM"
    assert provenance["source_record_id"] == "synthetic-product"


def test_company_not_inferred_from_product():
    record, _ = normalize_tool(
        tool(),
        collected_at=NOW,
    )

    assert record.content.startupName is None


def test_free_trial_not_mislabeled():
    assert normalize_pricing("Free Trial") is None


def test_free_label_preserved():
    assert normalize_pricing("Free") == "FREE"


def test_missing_name_rejected():
    row = tool()
    row["name"] = ""

    with pytest.raises(ValueError, match="product name"):
        normalize_tool(row, collected_at=NOW)


def test_unexpected_review_host_rejected():
    row = tool()
    row["review_url"] = "https://example.org/review"

    with pytest.raises(ValueError, match="review URL host"):
        normalize_tool(row, collected_at=NOW)