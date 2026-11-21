from datetime import timedelta

import pytest
from pydantic import TypeAdapter, ValidationError

from ai_ingest.models import (
    ProductRecord,
    Record,
    ResearchPaperRecord,
    StartupRecord,
)


BASE = {
    "source": {
        "name": "Synthetic test fixture",
        "url": "https://example.org/item",
    },
    "collectedAt": "2026-01-01T10:00:00Z",
}


def test_startup_unknown_count_is_null():
    record = StartupRecord.model_validate(
        {
            **BASE,
            "content": {"entityName": "Fixture Startup"},
        }
    )

    assert record.content.data.employeeCount is None


@pytest.mark.parametrize("count", [-1, True, 2.5, "12"])
def test_invalid_counts_rejected(count):
    with pytest.raises(ValidationError):
        StartupRecord.model_validate(
            {
                **BASE,
                "content": {
                    "entityName": "Fixture Startup",
                    "data": {"employeeCount": count},
                },
            }
        )


def test_naive_timestamp_rejected():
    payload = {
        **BASE,
        "collectedAt": "2026-01-01T10:00:00",
    }

    with pytest.raises(ValidationError):
        StartupRecord.model_validate(
            {
                **payload,
                "content": {"entityName": "Fixture Startup"},
            }
        )


def test_unknown_pricing_preserved():
    record = ProductRecord.model_validate(
        {
            **BASE,
            "content": {"productName": "Fixture Product"},
        }
    )

    assert record.content.pricingModel is None


def test_invalid_pricing_rejected():
    with pytest.raises(ValidationError):
        ProductRecord.model_validate(
            {
                **BASE,
                "content": {
                    "productName": "Fixture Product",
                    "pricingModel": "UNKNOWN",
                },
            }
        )


def test_stars_need_observation():
    with pytest.raises(ValidationError):
        ResearchPaperRecord.model_validate(
            {
                **BASE,
                "content": {
                    "title": "Synthetic Research Paper",
                    "authors": ["Fixture Author"],
                    "paper_url": "https://example.org/paper",
                    "published_date": "2026-01-01T00:00:00Z",
                    "github_url": (
                        "https://github.com/"
                        "synthetic-fixture/synthetic-repository"
                    ),
                    "github_stars": 0,
                },
            }
        )


def test_record_discriminator():
    adapter = TypeAdapter(Record)

    record = adapter.validate_python(
        {
            **BASE,
            "recordType": "NEWS",
            "content": {
                "title": "Synthetic News",
                "full_text": "Synthetic test content.",
            },
        }
    )

    assert record.recordType == "NEWS"
    assert record.content.title == "Synthetic News"


def test_timezone_normalized():
    payload = {
        **BASE,
        "collectedAt": "2026-01-01T10:00:00+02:00",
    }

    record = StartupRecord.model_validate(
        {
            **payload,
            "content": {"entityName": "Fixture Startup"},
        }
    )

    assert record.collectedAt.hour == 8
    assert record.collectedAt.utcoffset() == timedelta(0)