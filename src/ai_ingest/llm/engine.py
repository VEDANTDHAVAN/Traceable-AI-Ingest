import asyncio
import hashlib
import random
import time

from dataclasses import dataclass
from typing import Annotated, Protocol

from pydantic import BaseModel, ConfigDict, Field


NonEmptyText = Annotated[str, Field(min_length=1)]


class GroundedValue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: NonEmptyText
    quote: NonEmptyText


class ExtractedFacts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Required keys; unknown values must explicitly be null.
    organization_name: GroundedValue | None
    pricing_description: GroundedValue | None


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
    pass


class Provider(Protocol):
    name: str

    async def generate(self, text: str) -> str:
        """Return JSON matching ExtractedFacts."""
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
) -> ExtractedFacts:
    facts = ExtractedFacts.model_validate_json(raw_json)

    for field in ("organization_name", "pricing_description"):
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


class ExtractionEngine:
    def __init__(
        self,
        providers: list[Provider],
        *,
        chunk_bytes: int = 12_000,
        attempts: int = 3,
        timeout_seconds: float = 60,
        max_wait_seconds: float = 60,
        max_requests: int = 128,
    ) -> None:
        if not providers:
            raise ValueError("At least one provider is required.")

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
        self.chunk_bytes = chunk_bytes
        self.attempts = attempts
        self.timeout_seconds = timeout_seconds
        self.max_wait_seconds = max_wait_seconds
        self.max_requests = max_requests

        self._locks = {
            provider.name: asyncio.Lock()
            for provider in providers
        }

        self._cooldowns = {
            provider.name: 0.0
            for provider in providers
        }

    async def extract(self, text: str) -> dict:
        if not text.strip():
            raise ValueError("Source text cannot be empty.")

        initial_chunks = split_text(text, self.chunk_bytes)

        if len(initial_chunks) > self.max_requests:
            raise ExtractionFailed(
                "Initial chunks exceed the request budget."
            )

        requests_used = 0
        events = []

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
                                }
                            )
                            break

                        if remaining_cooldown:
                            await asyncio.sleep(remaining_cooldown)

                        if requests_used >= self.max_requests:
                            raise ExtractionFailed(
                                "Request budget exhausted."
                            )

                        requests_used += 1

                        try:
                            response = await asyncio.wait_for(
                                provider.generate(chunk.text),
                                timeout=self.timeout_seconds,
                            )

                            facts = validate_facts(
                                response,
                                chunk.text,
                            )

                        except ProviderFailure as exc:
                            events.append(
                                {
                                    "provider": provider.name,
                                    "event": "provider_error",
                                    "status": exc.status,
                                    "attempt": attempt + 1,
                                }
                            )

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
                            events.append(
                                {
                                    "provider": provider.name,
                                    "event": "timeout",
                                    "attempt": attempt + 1,
                                }
                            )

                            self._cooldowns[provider.name] = (
                                time.monotonic()
                                + random.uniform(
                                    0.0,
                                    min(30.0, 2.0 ** attempt),
                                )
                            )

                        except ValueError:
                            # Invalid JSON, schema, or grounding.
                            # Do not accept it or repeatedly ask
                            # the same provider to invent a repair.
                            events.append(
                                {
                                    "provider": provider.name,
                                    "event": "invalid_output",
                                }
                            )
                            break

                        else:
                            grounded = {}

                            for field in (
                                "organization_name",
                                "pricing_description",
                            ):
                                claim = getattr(facts, field)

                                if claim is None:
                                    grounded[field] = None
                                    continue

                                local_start = chunk.text.index(
                                    claim.quote
                                )

                                quote_start = (
                                    chunk.start + local_start
                                )

                                grounded[field] = {
                                    **claim.model_dump(),
                                    "quote_start": quote_start,
                                    "quote_end": (
                                        quote_start + len(claim.quote)
                                    ),
                                }

                            return [
                                {
                                    "chunk_start": chunk.start,
                                    "chunk_end": (
                                        chunk.start + len(chunk.text)
                                    ),
                                    "provider": provider.name,
                                    "facts": grounded,
                                }
                            ]

                # Release the provider lock before recursive calls.
                if should_split:
                    current_bytes = len(
                        chunk.text.encode("utf-8")
                    )

                    if current_bytes > 4:
                        smaller = split_text(
                            chunk.text,
                            max(4, current_bytes // 2),
                        )

                        if len(smaller) > 1:
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
                f"No provider accepted chunk at offset {chunk.start}."
            )

        results = []

        for chunk in initial_chunks:
            results.extend(await process(chunk))

        return {
            "source_text_sha256": hashlib.sha256(
                text.encode("utf-8")
            ).hexdigest(),
            "source_characters": len(text),
            "requests_used": requests_used,
            "chunks": results,
            "events": events,
        }