import asyncio
import json

import pytest

from ai_ingest.llm.engine import (
    ExtractionEngine,
    JobFacts,
    NewsFacts,
    coverage_complete,
    validate_facts,
)


def test_job_schema_requires_grounded_values():
    response = json.dumps(
        {
            "company_name": {
                "value": "Oddball",
                "quote": "Oddball builds products for government.",
            },
            "product_name": None,
            "pricing_description": None,
            "publication_date": None,
            "remote_eligibility": {
                "value": "Fully remote",
                "quote": "Benefits: Fully remote",
            },
            "role_family": None,
        }
    )

    facts = validate_facts(
        response,
        "Oddball builds products for government. Benefits: Fully remote",
        JobFacts,
    )

    assert facts.company_name.value == "Oddball"
    assert facts.remote_eligibility.value == "Fully remote"
    assert facts.product_name is None


def test_job_schema_rejects_fabricated_company():
    response = json.dumps(
        {
            "company_name": {
                "value": "Invented Corp",
                "quote": "Oddball builds products for government.",
            },
            "product_name": None,
            "pricing_description": None,
            "publication_date": None,
            "remote_eligibility": None,
            "role_family": None,
        }
    )

    with pytest.raises(ValueError, match="absent"):
        validate_facts(
            response,
            "Oddball builds products for government.",
            JobFacts,
        )


def test_news_schema_rejects_non_verbatim_quote():
    response = json.dumps(
        {
            "organization_name": {
                "value": "OpenAI",
                "quote": "OpenAI released a model.",
            },
            "product_name": None,
            "pricing_description": None,
        }
    )

    with pytest.raises(ValueError, match="verbatim"):
        validate_facts(response, "A different sentence.", NewsFacts)


def test_engine_accepts_schema_and_records_attempts():
    class FakeProvider:
        name = "fixture"
        model = "fixture-1"
        schema_name = "product"

        async def generate(self, text):
            return json.dumps(
                {
                    "company_name": None,
                    "product_name": None,
                    "pricing_description": None,
                    "publication_date": None,
                    "remote_eligibility": None,
                    "role_family": None,
                }
            )

    async def run():
        engine = ExtractionEngine(
            [FakeProvider()],
            schema="job",
            chunk_bytes=64,
        )
        return await engine.extract("A job description that is long enough. " * 3)

    result = asyncio.run(run())

    assert result["schema"] == "job"
    assert result["coverage_complete"] is True
    assert result["provider_attempts"][0]["outcome"] == "accepted"
    assert all(
        "company_name" in chunk["facts"] for chunk in result["chunks"]
    )


def test_coverage_requires_contiguous_spans():
    assert coverage_complete(
        [
            {"chunk_start": 0, "chunk_end": 10},
            {"chunk_start": 10, "chunk_end": 20},
        ],
        20,
    )
    assert not coverage_complete(
        [
            {"chunk_start": 0, "chunk_end": 10},
            {"chunk_start": 11, "chunk_end": 20},
        ],
        20,
    )
