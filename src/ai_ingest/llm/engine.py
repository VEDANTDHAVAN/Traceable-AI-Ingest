"""Grounded multi-provider extraction engine.

Every non-null claim must carry a verbatim quote with character offsets into
the normalized source text. The engine validates grounding before accepting a
provider response and never relaxes that check for a fallback provider.
"""

from __future__ import annotations

import asyncio
import hashlib
import random
import time

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated, Protocol

from pydantic import BaseModel, ConfigDict, Field


NonEmptyText = Annotated[str, Field(min_length=1)]


class GroundedValue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: NonEmptyText
    quote: NonEmptyText


class ExtractedFacts(BaseModel):
    """Product/news ownership and pricing claims."""

    model_config = ConfigDict(extra="forbid")

    organization_name: GroundedValue | None
    product_name: GroundedValue | None = None
    pricing_description: GroundedValue | None


class NewsFacts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    organization_name: GroundedValue | None
    product_name: GroundedValue | None
    pricing_description: GroundedValue | None


class JobFacts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    company_name: GroundedValue | None
    product_name: GroundedValue | None
    pricing_description: GroundedValue | None
    publication_date: GroundedValue | None
    remote_eligibility: GroundedValue | None
    role_family: GroundedValue | None


FACT_MODELS: dict[str, type[BaseModel]] = {
    "product": ExtractedFacts,
    "news": NewsFacts,
    "job": JobFacts,
}


def fact_fields(model: type[BaseModel]) -> list[str]:
    return list(model.model_fields)


class ProviderFailure(RuntimeError):
    def __init__(
        self,
        status: int,
        *,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(f"Provider request failed: HTTP {status}")

        self.status = status
        self.retry_after = retry_after


class ExtractionFailed(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        events: list[dict] | None = None,
    ) -> None:
        super().__init__(message)
        self.events = list(events or [])


class Provider(Protocol):
    name: str

    async def generate(self, text: str) -> str:
        """Return JSON matching the configured fact schema."""
        ...


@dataclass(frozen=True)
class Chunk:
    start: int
    text: str


def split_text(text: str, max_bytes: int) -> list[Chunk]:
    """Split without dropping characters; prefer paragraph boundaries."""
    if max_bytes < 4:
        raise ValueError("max_bytes must be at least 4.")

    chunks = []
    start = 0

    while start < len(text):
        end = start
        used = 0

        while end < len(text):
            size = len(text[end].encode("utf-8"))

            if used + size > max_bytes:
                break

            used += size
            end += 1

        if end == start:
            raise ValueError("Cannot fit a character into the chunk.")

        if end < len(text):
            boundary = text.rfind(
                "\n\n",
                start + (end - start) // 2,
                end,
            )

            if boundary != -1:
                end = boundary + 2

        chunks.append(Chunk(start=start, text=text[start:end]))
        start = end

    return chunks


def validate_facts(
    raw_json: str,
    source_text: str,
    model: type[BaseModel] = ExtractedFacts,
) -> BaseModel:
    facts = model.model_validate_json(raw_json)

    for field in fact_fields(model):
        claim = getattr(facts, field)

        if claim is None:
            continue

        if not claim.value.strip() or not claim.quote.strip():
            raise ValueError("Empty claim or evidence.")

        if claim.quote not in source_text:
            raise ValueError("Evidence quote is not verbatim.")

        if claim.value not in claim.quote:
            raise ValueError("Claim value is absent from its quote.")

    return facts


def coverage_spans(chunks: list[dict]) -> list[tuple[int, int]]:
    return sorted(
        (chunk["chunk_start"], chunk["chunk_end"]) for chunk in chunks
    )


def coverage_complete(chunks: list[dict], source_characters: int) -> bool:
    spans = coverage_spans(chunks)
    if not spans:
        return False
    if spans[0][0] != 0 or spans[-1][1] != source_characters:
        return False
    return all(
        previous[1] == following[0]
        for previous, following in zip(spans, spans[1:])
    )


class ExtractionEngine:
    def __init__(
        self,
        providers: Sequence[Provider],
        *,
        schema: str = "product",
        chunk_bytes: int = 12_000,
        attempts: int = 3,
        timeout_seconds: float = 60,
        max_wait_seconds: float = 60,
        max_requests: int = 128,
    ) -> None:
        if not providers:
            raise ValueError("At least one provider is required.")

        if schema not in FACT_MODELS:
            raise ValueError(f"Unknown fact schema: {schema}")

        names = [provider.name for provider in providers]

        if len(names) != len(set(names)):
            raise ValueError("Provider names must be unique.")

        if (
            chunk_bytes < 4
            or attempts < 1
            or timeout_seconds <= 0
            or max_wait_seconds < 0
            or max_requests < 1
        ):
            raise ValueError("Invalid engine configuration.")

        self.providers = providers
        self.schema = schema
        self.facts_model = FACT_MODELS[schema]
        self.chunk_bytes = chunk_bytes
        self.attempts = attempts
        self.timeout_seconds = timeout_seconds
        self.max_wait_seconds = max_wait_seconds
        self.max_requests = max_requests

        # Providers expose a schema name so they can build the exact request
        # payload and stay within documented limits.
        for provider in providers:
            if hasattr(provider, "schema_name"):
                provider.schema_name = schema

        self._locks = {
            provider.name: asyncio.Lock()
            for provider in providers
        }

        self._cooldowns = {
            provider.name: 0.0
            for provider in providers
        }

    async def extract(self, text: str) -> dict:
        """Extract grounded facts and prove complete chunk coverage."""
        if not text.strip():
            raise ValueError("Source text cannot be empty.")

        chunk_size = self.chunk_bytes
        initial_chunks = split_text(text, chunk_size)

        if len(initial_chunks) > self.max_requests:
            raise ExtractionFailed(
                "Initial chunks exceed the request budget."
            )

        requests_used = 0
        events: list[dict] = []
        attempts_log: list[dict] = []
        provider_calls: list[dict] = []

        async def process(
            chunk: Chunk,
            start_provider: int = 0,
        ) -> list[dict]:
            nonlocal requests_used

            for provider_index in range(
                start_provider,
                len(self.providers),
            ):
                provider = self.providers[provider_index]
                model = getattr(provider, "model", provider.name)
                should_split = False

                async with self._locks[provider.name]:
                    for attempt in range(self.attempts):
                        remaining_cooldown = max(
                            0.0,
                            self._cooldowns[provider.name]
                            - time.monotonic(),
                        )

                        if remaining_cooldown > self.max_wait_seconds:
                            events.append(
                                {
                                    "provider": provider.name,
                                    "event": "cooldown_skip",
                                    "chunk_start": chunk.start,
                                }
                            )
                            break

                        if remaining_cooldown:
                            await asyncio.sleep(remaining_cooldown)

                        if requests_used >= self.max_requests:
                            raise ExtractionFailed(
                                "Request budget exhausted.",
                                events=events,
                            )

                        requests_used += 1
                        call_started = time.monotonic()

                        try:
                            response = await asyncio.wait_for(
                                provider.generate(chunk.text),
                                timeout=self.timeout_seconds,
                            )

                            facts = validate_facts(
                                response,
                                chunk.text,
                                self.facts_model,
                            )

                        except ProviderFailure as exc:
                            latency_ms = round(
                                (time.monotonic() - call_started) * 1000, 1
                            )
                            event = {
                                "event": "attempt",
                                "provider": provider.name,
                                "model": model,
                                "status": exc.status,
                                "attempt": attempt + 1,
                                "chunk_start": chunk.start,
                                "chunk_bytes": len(chunk.text.encode("utf-8")),
                                "latency_ms": latency_ms,
                                "outcome": "payload_too_large"
                                if exc.status == 413
                                else "rate_limited"
                                if exc.status == 429
                                else "provider_error",
                            }
                            events.append(event)
                            attempts_log.append(event)

                            if exc.status == 413:
                                should_split = True
                                break

                            retryable = (
                                exc.status in {0, 429}
                                or 500 <= exc.status <= 599
                            )

                            if not retryable:
                                break

                            delay = max(
                                exc.retry_after or 0.0,
                                random.uniform(
                                    0.0,
                                    min(30.0, 2.0 ** attempt),
                                ),
                            )

                            self._cooldowns[provider.name] = (
                                time.monotonic() + delay
                            )

                            if attempt == self.attempts - 1:
                                break

                            if delay > self.max_wait_seconds:
                                break

                        except asyncio.TimeoutError:
                            latency_ms = round(
                                (time.monotonic() - call_started) * 1000, 1
                            )
                            event = {
                                "event": "attempt",
                                "provider": provider.name,
                                "model": model,
                                "status": None,
                                "attempt": attempt + 1,
                                "chunk_start": chunk.start,
                                "latency_ms": latency_ms,
                                "outcome": "timeout",
                            }
                            events.append(event)
                            attempts_log.append(event)

                            self._cooldowns[provider.name] = (
                                time.monotonic()
                                + random.uniform(
                                    0.0,
                                    min(30.0, 2.0 ** attempt),
                                )
                            )

                        except ValueError:
                            # Invalid JSON, schema, or grounding. Do not accept
                            # it or repeatedly ask the same provider to invent
                            # a repair.
                            latency_ms = round(
                                (time.monotonic() - call_started) * 1000, 1
                            )
                            event = {
                                "event": "attempt",
                                "provider": provider.name,
                                "model": model,
                                "status": 200,
                                "attempt": attempt + 1,
                                "chunk_start": chunk.start,
                                "latency_ms": latency_ms,
                                "outcome": "ungrounded_or_invalid",
                            }
                            events.append(event)
                            attempts_log.append(event)
                            break

                        else:
                            latency_ms = round(
                                (time.monotonic() - call_started) * 1000, 1
                            )
                            event = {
                                "event": "attempt",
                                "provider": provider.name,
                                "model": model,
                                "status": 200,
                                "attempt": attempt + 1,
                                "chunk_start": chunk.start,
                                "latency_ms": latency_ms,
                                "outcome": "accepted",
                            }
                            events.append(event)
                            attempts_log.append(event)

                            usage = getattr(provider, "last_usage", None)
                            provider_calls.append(
                                {
                                    "provider": provider.name,
                                    "model": model,
                                    "status": 200,
                                    "attempt": attempt + 1,
                                    "chunk_start": chunk.start,
                                    "latency_ms": latency_ms,
                                    "usage": usage,
                                }
                            )

                            grounded = {}

                            for field in fact_fields(self.facts_model):
                                claim = getattr(facts, field)

                                if claim is None:
                                    grounded[field] = None
                                    continue

                                local_start = chunk.text.index(
                                    claim.quote
                                )

                                quote_start = chunk.start + local_start
                                quote_end = quote_start + len(claim.quote)

                                assert chunk.start <= quote_start
                                assert quote_end <= (
                                    chunk.start + len(chunk.text)
                                )

                                grounded[field] = {
                                    **claim.model_dump(),
                                    "quote_start": quote_start,
                                    "quote_end": quote_end,
                                }

                            return [
                                {
                                    "chunk_start": chunk.start,
                                    "chunk_end": (
                                        chunk.start + len(chunk.text)
                                    ),
                                    "provider": provider.name,
                                    "model": model,
                                    "facts": grounded,
                                }
                            ]

                # Release the provider lock before recursive calls.
                if should_split:
                    current_bytes = len(chunk.text.encode("utf-8"))

                    if current_bytes > 4:
                        smaller = split_text(
                            chunk.text,
                            max(4, current_bytes // 2),
                        )

                        if len(smaller) > 1:
                            events.append(
                                {
                                    "provider": provider.name,
                                    "event": "chunk_reduced",
                                    "chunk_start": chunk.start,
                                    "from_bytes": current_bytes,
                                    "to_bytes": max(4, current_bytes // 2),
                                    "parts": len(smaller),
                                }
                            )
                            results = []

                            for part in smaller:
                                absolute = Chunk(
                                    start=chunk.start + part.start,
                                    text=part.text,
                                )

                                results.extend(
                                    await process(
                                        absolute,
                                        provider_index,
                                    )
                                )

                            return results

            raise ExtractionFailed(
                f"No provider accepted chunk at offset {chunk.start}.",
                events=events,
            )

        results = []

        for chunk in initial_chunks:
            results.extend(await process(chunk))

        complete = coverage_complete(results, len(text))

        if not complete:
            raise ExtractionFailed(
                "Chunk coverage is not contiguous and complete.",
                events=events,
            )

        return {
            "source_text_sha256": hashlib.sha256(
                text.encode("utf-8")
            ).hexdigest(),
            "source_characters": len(text),
            "schema": self.schema,
            "chunk_bytes": chunk_size,
            "requests_used": requests_used,
            "coverage_complete": complete,
            "chunks": sorted(results, key=lambda chunk: chunk["chunk_start"]),
            "provider_attempts": attempts_log,
            "provider_calls": provider_calls,
            "events": events,
        }
