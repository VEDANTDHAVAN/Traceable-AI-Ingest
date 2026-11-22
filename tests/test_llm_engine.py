import asyncio
import json

import pytest

from ai_ingest.llm.engine import (
    ExtractionEngine,
    ExtractionFailed,
    ProviderFailure,
    split_text,
    validate_facts,
)


EMPTY = json.dumps(
    {
        "organization_name": None,
        "pricing_description": None,
    }
)


class FakeProvider:
    def __init__(self, name, handler):
        self.name = name
        self.handler = handler
        self.calls = []

    async def generate(self, text):
        self.calls.append(text)
        return self.handler(text)


def run(engine, text):
    return asyncio.run(engine.extract(text))


def test_chunking_preserves_all_characters():
    text = "First paragraph.\n\nSecond paragraph.\n\nLast."

    chunks = split_text(text, 20)

    assert "".join(chunk.text for chunk in chunks) == text
    assert chunks[0].start == 0


def test_unicode_chunks_respect_byte_budget():
    text = "你好🙂" * 10

    chunks = split_text(text, 12)

    assert "".join(chunk.text for chunk in chunks) == text
    assert all(
        len(chunk.text.encode("utf-8")) <= 12
        for chunk in chunks
    )


def test_rate_limit_falls_back_without_waiting_for_long_pause():
    def limited(_text):
        raise ProviderFailure(429, retry_after=3600)

    first = FakeProvider("first", limited)
    second = FakeProvider("second", lambda _text: EMPTY)

    engine = ExtractionEngine(
        [first, second],
        attempts=1,
        max_wait_seconds=0,
    )

    result = run(engine, "Synthetic source text.")

    assert result["chunks"][0]["provider"] == "second"
    assert len(first.calls) == 1


def test_invalid_evidence_falls_back():
    invalid = json.dumps(
        {
            "organization_name": {
                "value": "Invented Company",
                "quote": "Invented Company operates this product.",
            },
            "pricing_description": None,
        }
    )

    first = FakeProvider("first", lambda _text: invalid)
    second = FakeProvider("second", lambda _text: EMPTY)

    result = run(
        ExtractionEngine([first, second]),
        "Synthetic source text.",
    )

    assert result["chunks"][0]["provider"] == "second"


def test_413_splits_instead_of_dropping_text():
    def size_limited(text):
        if len(text.encode("utf-8")) > 8:
            raise ProviderFailure(413)

        return EMPTY

    provider = FakeProvider("limited", size_limited)

    result = run(
        ExtractionEngine(
            [provider],
            chunk_bytes=64,
        ),
        "abcdefghijklmnopqrstuvwx",
    )

    spans = [
        (chunk["chunk_start"], chunk["chunk_end"])
        for chunk in result["chunks"]
    ]

    assert spans[0][0] == 0
    assert spans[-1][1] == 24

    assert all(
        previous[1] == following[0]
        for previous, following in zip(
            spans,
            spans[1:],
        )
    )


def test_all_providers_failing_raises():
    def unavailable(_text):
        raise ProviderFailure(401)

    provider = FakeProvider("unavailable", unavailable)

    with pytest.raises(ExtractionFailed):
        run(
            ExtractionEngine([provider]),
            "Synthetic source.",
        )


def test_claim_value_must_appear_in_quote():
    response = json.dumps(
        {
            "organization_name": {
                "value": "Different Company",
                "quote": "Fixture Company operates this product.",
            },
            "pricing_description": None,
        }
    )

    with pytest.raises(ValueError, match="absent"):
        validate_facts(
            response,
            "Fixture Company operates this product.",
        )


def test_empty_source_rejected():
    provider = FakeProvider("first", lambda _text: EMPTY)

    with pytest.raises(ValueError, match="empty"):
        run(
            ExtractionEngine([provider]),
            "   ",
        )