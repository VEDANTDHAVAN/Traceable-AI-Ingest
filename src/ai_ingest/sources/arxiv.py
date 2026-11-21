import asyncio
import hashlib
import json
import random
import time
import xml.etree.ElementTree as ET

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import aiohttp

from ai_ingest.models import ResearchPaperRecord


ENDPOINT = "https://export.arxiv.org/api/query"

ATOM = "{http://www.w3.org/2005/Atom}"

RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


class ArxivError(RuntimeError):
    """An arXiv response could not be fetched or interpreted."""


def clean_text(value: str) -> str:
    return " ".join(value.split())


def required_text(element: ET.Element, field: str) -> str:
    value = element.findtext(f"{ATOM}{field}")

    if not value or not value.strip():
        raise ArxivError(f"Missing required Atom field: {field}")

    return clean_text(value)


def parse_feed(
    raw_xml: bytes,
    *,
    collected_at: datetime,
) -> list[ResearchPaperRecord]:
    """Parse a complete page; fail rather than silently discard bad entries."""
    try:
        root = ET.fromstring(raw_xml)
    except ET.ParseError as exc:
        raise ArxivError("Response is not valid XML.") from exc

    if root.tag != f"{ATOM}feed":
        raise ArxivError("Response is not an Atom feed.")

    records = []

    for entry in root.findall(f"{ATOM}entry"):
        entry_id = required_text(entry, "id")

        if "/api/errors" in entry_id:
            message = entry.findtext(f"{ATOM}summary", default="")
            raise ArxivError(f"arXiv API error: {clean_text(message)}")

        # Normalize only the known arXiv HTTP identifier prefix.
        paper_url = entry_id.replace(
            "http://arxiv.org/abs/",
            "https://arxiv.org/abs/",
            1,
        )

        if not paper_url.startswith("https://arxiv.org/abs/"):
            raise ArxivError(f"Unexpected paper identifier: {entry_id}")

        authors = [
            required_text(author, "name")
            for author in entry.findall(f"{ATOM}author")
        ]

        record = ResearchPaperRecord.model_validate(
            {
                "source": {
                    "name": "arXiv",
                    "url": paper_url,
                },
                "collectedAt": collected_at,
                "content": {
                    "title": required_text(entry, "title"),
                    "authors": authors,
                    "paper_url": paper_url,
                    "published_date": required_text(entry, "published"),
                    # Deliberately unknown until evidence-backed enrichment.
                    "github_url": None,
                    "github_stars": None,
                    "github_metrics_collected_at": None,
                },
            }
        )

        records.append(record)

    return records


def retry_after_seconds(
    value: str | None,
    *,
    now: datetime,
) -> float | None:
    if not value:
        return None

    try:
        return max(0.0, float(value))
    except ValueError:
        pass

    try:
        retry_at = parsedate_to_datetime(value)

        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)

        return max(0.0, (retry_at - now).total_seconds())
    except (ValueError, TypeError, OverflowError):
        return None


class ArxivClient:
    """Single-process client; do not run competing instances."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        min_interval: float = 3.1,
        max_attempts: int = 4,
    ) -> None:
        if min_interval < 3.0:
            raise ValueError("min_interval must be at least 3 seconds.")

        if max_attempts < 1:
            raise ValueError("max_attempts must be positive.")

        self.session = session
        self.min_interval = min_interval
        self.max_attempts = max_attempts
        self._lock = asyncio.Lock()
        self._last_finished: float | None = None

    async def _request(
        self,
        params: dict,
    ) -> tuple[int, dict, bytes]:
        async with self._lock:
            if self._last_finished is not None:
                elapsed = time.monotonic() - self._last_finished
                await asyncio.sleep(
                    max(0.0, self.min_interval - elapsed)
                )

            try:
                async with self.session.get(
                    ENDPOINT,
                    params=params,
                    allow_redirects=False,
                ) as response:
                    body = await response.read()
                    return (
                        response.status,
                        dict(response.headers),
                        body,
                    )
            finally:
                self._last_finished = time.monotonic()

    async def fetch_page(
        self,
        *,
        query: str,
        start: int = 0,
        page_size: int = 10,
    ) -> bytes:
        if not query.strip():
            raise ValueError("query cannot be empty.")

        if start < 0:
            raise ValueError("start cannot be negative.")

        if not 1 <= page_size <= 100:
            raise ValueError("This adapter supports page sizes 1–100.")

        params = {
            "search_query": query,
            "start": start,
            "max_results": page_size,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }

        for attempt in range(self.max_attempts):
            server_delay = None

            try:
                status, headers, body = await self._request(params)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                if attempt == self.max_attempts - 1:
                    raise ArxivError(
                        "Request failed after bounded retries."
                    ) from exc
            else:
                if status == 200:
                    return body

                if status not in RETRYABLE_STATUSES:
                    raise ArxivError(
                        f"Non-retryable HTTP status: {status}"
                    )

                if attempt == self.max_attempts - 1:
                    raise ArxivError(
                        f"HTTP {status} after bounded retries."
                    )

                # Header names are case-insensitive.
                normalized = {
                    key.lower(): value
                    for key, value in headers.items()
                }

                server_delay = retry_after_seconds(
                    normalized.get("retry-after"),
                    now=datetime.now(timezone.utc),
                )

            jittered_backoff = random.uniform(
                0.0,
                min(60.0, 2.0 ** (attempt + 1)),
            )

            await asyncio.sleep(
                max(server_delay or 0.0, jittered_backoff)
            )

        raise ArxivError("Retry loop ended unexpectedly.")


async def ingest_page(
    *,
    query: str,
    start: int,
    page_size: int,
    output_dir: Path,
    user_agent: str,
) -> Path:
    """Fetch, preserve evidence, validate, and write one staging page."""
    timeout = aiohttp.ClientTimeout(total=90)

    async with aiohttp.ClientSession(
        timeout=timeout,
        headers={"User-Agent": user_agent},
        connector=aiohttp.TCPConnector(limit=1),
    ) as session:
        client = ArxivClient(session)

        raw = await client.fetch_page(
            query=query,
            start=start,
            page_size=page_size,
        )

    collected_at = datetime.now(timezone.utc)
    digest = hashlib.sha256(raw).hexdigest()

    run_id = (
        collected_at.strftime("%Y%m%dT%H%M%S%fZ")
        + "-"
        + digest[:12]
    )

    run_dir = output_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=False)

    # Keep the response even if parsing or validation subsequently fails.
    await asyncio.to_thread(
        (run_dir / "response.xml").write_bytes,
        raw,
    )

    metadata = {
        "endpoint": ENDPOINT,
        "query": query,
        "start": start,
        "page_size": page_size,
        "collected_at": collected_at.isoformat(),
        "raw_sha256": digest,
        "status": "raw_saved",
    }

    metadata_path = run_dir / "metadata.json"

    await asyncio.to_thread(
        metadata_path.write_text,
        json.dumps(metadata, indent=2),
        encoding="utf-8",
    )

    records = parse_feed(
        raw,
        collected_at=collected_at,
    )

    jsonl = "".join(
        record.model_dump_json() + "\n"
        for record in records
    )

    await asyncio.to_thread(
        (run_dir / "papers.jsonl").write_text,
        jsonl,
        encoding="utf-8",
    )

    metadata["status"] = "parsed"
    metadata["record_count"] = len(records)

    await asyncio.to_thread(
        metadata_path.write_text,
        json.dumps(metadata, indent=2),
        encoding="utf-8",
    )

    return run_dir