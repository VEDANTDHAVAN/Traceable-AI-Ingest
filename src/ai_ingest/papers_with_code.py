"""Bounded-concurrency paper acquisition from the Papers with Code archive.

Pipeline:
  1. Stream paper/code association rows from the Papers with Code archive
     (CC-BY-SA-4.0) via the Hugging Face datasets-server.
  2. Keep only GitHub repository links, normalized and validated.
  3. Resolve paper metadata from arXiv ``id_list`` batches (<=50/request,
     honoring arXiv's documented request spacing).
  4. Fetch current GitHub stars per unique repository through the REST API.
  5. Persist raw snapshots, provenance and quarantine logs.

Official vs community associations are recorded distinctly, never conflated.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import random
import time
import xml.etree.ElementTree as ET

from datetime import datetime, timezone
from pathlib import Path

import aiohttp

from ai_ingest.models import ResearchPaperRecord
from ai_ingest.repository_links import canonical_repository_url
from ai_ingest.sources.arxiv import ArxivError, required_text


ATOM = "{http://www.w3.org/2005/Atom}"
LINKS_DATASET = "pwc-archive/links-between-paper-and-code"
LINKS_LICENSE = "CC-BY-SA-4.0"
LINKS_ATTRIBUTION = (
    "Data: Papers with Code archive links-between-paper-and-code "
    "(CC-BY-SA-4.0)."
)
HF_ROWS = "https://datasets-server.huggingface.co/rows"
ARXIV_ENDPOINT = "https://export.arxiv.org/api/query"
GITHUB_API = "https://api.github.com/repos"
PARSER_VERSION = "pwc-links-arxiv-github-v1"
DEFAULT_PAGE_SIZE = 100
HF_MAX_PAGE = 100
ARXIV_BATCH = 100
ARXIV_MIN_INTERVAL = 3.1


class AcquisitionError(RuntimeError):
    """A remote response could not be fetched or validated."""


def retry_delay(response: aiohttp.ClientResponse, attempt: int) -> float:
    header = response.headers.get("Retry-After")
    if header:
        try:
            return max(0.0, float(header))
        except ValueError:
            pass
        try:
            from email.utils import parsedate_to_datetime

            target = parsedate_to_datetime(header)
            now = datetime.now(timezone.utc)
            return max(0.0, (target - now).total_seconds())
        except Exception:
            pass
    return min(60.0, 2.0 ** attempt) + random.uniform(0.0, 1.0)


async def get_json(
    session: aiohttp.ClientSession,
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
    max_attempts: int = 6,
) -> dict:
    last_error: Exception | None = None

    for attempt in range(max_attempts):
        try:
            async with session.get(
                url, params=params, headers=headers
            ) as response:
                if response.status == 200:
                    return await response.json()

                if response.status in {429, 500, 502, 503, 504}:
                    await asyncio.sleep(retry_delay(response, attempt))
                    last_error = AcquisitionError(
                        f"HTTP {response.status} for {url}"
                    )
                    continue

                text = await response.text()
                raise AcquisitionError(
                    f"HTTP {response.status} for {url}: {text[:200]}"
                )
        except aiohttp.ClientError as exc:
            last_error = exc
            await asyncio.sleep(
                min(30.0, 2.0 ** attempt) + random.uniform(0.0, 1.0)
            )

    raise AcquisitionError(f"Exhausted retries for {url}: {last_error}")


async def fetch_link_page(
    session: aiohttp.ClientSession,
    *,
    offset: int,
    length: int = DEFAULT_PAGE_SIZE,
) -> dict:
    return await get_json(
        session,
        HF_ROWS,
        params={
            "dataset": LINKS_DATASET,
            "config": "default",
            "split": "train",
            "offset": offset,
            "length": min(length, HF_MAX_PAGE),
        },
    )


def strip_version(arxiv_id: str) -> str:
    identifier = arxiv_id.strip()
    head, separator, tail = identifier.rpartition("v")
    if separator and tail.isdigit() and head:
        return head
    return identifier


def validate_link(row: dict) -> dict:
    """Validate one association row and normalize its repository URL."""
    repo_url = row.get("repo_url")
    arxiv_id = row.get("paper_arxiv_id")

    if not isinstance(repo_url, str) or not repo_url.strip():
        raise ValueError("Missing repo_url.")

    repository_url = canonical_repository_url(repo_url)
    if repository_url is None:
        raise ValueError("Not a canonical GitHub repository URL.")

    if not isinstance(arxiv_id, str) or not arxiv_id.strip():
        raise ValueError("Missing paper_arxiv_id.")

    identifier = strip_version(arxiv_id)
    if not all(
        part.isalnum() or part in {".", "/", "-"}
        for part in identifier
    ):
        raise ValueError("Invalid arXiv identifier characters.")

    is_official = bool(row.get("is_official"))

    return {
        "repository_url": repository_url,
        "arxiv_id": identifier,
        "paper_url": f"https://arxiv.org/abs/{identifier}",
        "source_paper_url": row.get("paper_url"),
        "paper_title": row.get("paper_title"),
        "association_type": "OFFICIAL" if is_official else "COMMUNITY",
        "is_official": is_official,
    }


def parse_arxiv_feed(raw_xml: bytes) -> dict[str, dict]:
    """Parse an arXiv id_list response into metadata keyed by bare id."""
    try:
        root = ET.fromstring(raw_xml)
    except ET.ParseError as exc:
        raise ArxivError("Response is not valid XML.") from exc

    if root.tag != f"{ATOM}feed":
        raise ArxivError("Response is not an Atom feed.")

    papers: dict[str, dict] = {}

    for entry in root.findall(f"{ATOM}entry"):
        entry_id = required_text(entry, "id")

        if "/api/errors" in entry_id:
            raise ArxivError("arXiv returned an error entry.")

        paper_url = entry_id.replace(
            "http://arxiv.org/abs/", "https://arxiv.org/abs/", 1
        )
        if not paper_url.startswith("https://arxiv.org/abs/"):
            raise ArxivError(f"Unexpected identifier: {entry_id}")

        bare = strip_version(
            paper_url.removeprefix("https://arxiv.org/abs/")
        )

        authors = [
            required_text(author, "name")
            for author in entry.findall(f"{ATOM}author")
        ]

        papers[bare] = {
            "paper_url": paper_url,
            "bare_id": bare,
            "title": required_text(entry, "title"),
            "authors": authors,
            "published_date": required_text(entry, "published"),
        }

    return papers


def repo_api_url(repository_url: str) -> str:
    """Map ``https://github.com/owner/repo`` to the REST API URL."""
    parts = repository_url.removeprefix(
        "https://github.com/"
    ).split("/")
    if len(parts) != 2:
        raise ValueError("Not a canonical GitHub repository URL.")
    owner, repository = parts
    return f"{GITHUB_API}/{owner}/{repository}"


def validate_github_metadata(payload: dict, expected_url: str) -> dict:
    full_name = payload.get("full_name")
    html_url = payload.get("html_url")
    stars = payload.get("stargazers_count")

    if not isinstance(full_name, str) or not full_name.strip():
        raise ValueError("Missing repository full_name.")
    if not isinstance(html_url, str):
        raise ValueError("Missing repository html_url.")

    html_canonical = canonical_repository_url(html_url)
    if html_canonical is None:
        raise ValueError("Invalid repository html_url.")
    if html_canonical != expected_url:
        raise ValueError("Repository html_url disagrees with requested URL.")

    expected_name = expected_url.removeprefix(
        "https://github.com/"
    )
    if full_name.lower() != expected_name:
        raise ValueError("Repository full_name disagrees with URL.")

    if type(stars) is not int or stars < 0:
        raise ValueError("Invalid stargazers_count.")

    return {"github_stars": stars, "full_name": full_name}


def github_headers() -> dict:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "ai-intelligence-ingestion/0.1",
    }
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


class ArxivPacer:
    """Serialize arXiv requests to respect the documented spacing."""

    def __init__(self, interval: float = ARXIV_MIN_INTERVAL) -> None:
        self.interval = interval
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def wait(self) -> None:
        async with self._lock:
            elapsed = time.monotonic() - self._last
            if elapsed < self.interval:
                await asyncio.sleep(self.interval - elapsed)
            self._last = time.monotonic()


async def fetch_arxiv_batch(
    session: aiohttp.ClientSession,
    identifiers: list[str],
    raw_dir: Path,
    pacer: ArxivPacer,
    *,
    max_attempts: int = 6,
) -> dict[str, dict]:
    digest = hashlib.sha256(
        ",".join(identifiers).encode("utf-8")
    ).hexdigest()[:16]
    cache_path = raw_dir / f"arxiv_{digest}.xml"

    if cache_path.exists():
        return parse_arxiv_feed(cache_path.read_bytes())

    params = {
        "id_list": ",".join(identifiers),
        "max_results": str(len(identifiers)),
    }

    last_error: str = "unknown error"
    for attempt in range(max_attempts):
        await pacer.wait()
        async with session.get(ARXIV_ENDPOINT, params=params) as response:
            if response.status == 200:
                body = await response.read()
                cache_path.write_bytes(body)
                return parse_arxiv_feed(body)

            body = await response.text()
            last_error = f"arXiv HTTP {response.status}: {body[:150]}"

            if response.status in {429, 500, 502, 503, 504}:
                await asyncio.sleep(retry_delay(response, attempt))
                continue

            raise AcquisitionError(last_error)

    raise AcquisitionError(last_error)


async def run_acquisition(
    *,
    output_dir: Path,
    pages: int,
    page_size: int = DEFAULT_PAGE_SIZE,
    concurrency: int = 4,
    github_concurrency: int = 4,
    cache_dir: Path | None = None,
) -> dict:
    if pages < 1:
        raise ValueError("pages must be at least 1.")
    if not 1 <= concurrency <= 32:
        raise ValueError("concurrency must be between 1 and 32.")
    if not 1 <= github_concurrency <= 16:
        raise ValueError("github_concurrency must be between 1 and 16.")
    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)

    started = time.monotonic()
    collected_at = datetime.now(timezone.utc)
    run_id = collected_at.strftime("%Y%m%dT%H%M%S%fZ")
    run_dir = output_dir / run_id
    raw_dir = run_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    timeout = aiohttp.ClientTimeout(total=120)
    semaphore = asyncio.Semaphore(concurrency)

    async with aiohttp.ClientSession(
        timeout=timeout,
        connector=aiohttp.TCPConnector(limit=concurrency),
    ) as session:
        async def bounded(offset: int) -> dict:
            if cache_dir is not None:
                cache_path = cache_dir / f"links_{offset:08d}.json"
                if cache_path.exists():
                    return json.loads(
                        cache_path.read_text(encoding="utf-8")
                    )
            async with semaphore:
                payload = await fetch_link_page(
                    session, offset=offset, length=page_size
                )
            if cache_dir is not None:
                cache_path = cache_dir / f"links_{offset:08d}.json"
                cache_path.write_text(
                    json.dumps(payload, ensure_ascii=False),
                    encoding="utf-8",
                )
            return payload

        results = await asyncio.gather(
            *[bounded(index * page_size) for index in range(pages)],
            return_exceptions=True,
        )

    page_results: list[dict] = []
    page_quarantine: list[dict] = []
    for index, result in enumerate(results):
        if isinstance(result, Exception):
            page_quarantine.append(
                {"reason": f"PAGE_FETCH_FAILED: {result}", "page": index}
            )
            continue
        (raw_dir / f"links_{index:05d}.json").write_text(
            json.dumps(result, ensure_ascii=False), encoding="utf-8"
        )
        page_results.append(result)

    associations: list[dict] = []
    quarantine: list[dict] = list(page_quarantine)
    seen_pairs: set[tuple[str, str]] = set()

    for payload in page_results:
        for entry in payload.get("rows", []):
            row = entry["row"]
            try:
                association = validate_link(row)
            except ValueError as exc:
                quarantine.append(
                    {
                        "reason": str(exc),
                        "paper_arxiv_id": row.get("paper_arxiv_id"),
                        "repo_url": row.get("repo_url"),
                    }
                )
                continue

            key = (association["arxiv_id"], association["repository_url"])
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            associations.append(association)

    arxiv_ids = sorted({a["arxiv_id"] for a in associations})
    batches = [
        arxiv_ids[i:i + ARXIV_BATCH]
        for i in range(0, len(arxiv_ids), ARXIV_BATCH)
    ]

    papers: dict[str, dict] = {}
    arxiv_quarantine: list[dict] = []
    pacer = ArxivPacer()

    async with aiohttp.ClientSession(
        timeout=timeout,
        connector=aiohttp.TCPConnector(limit=concurrency),
    ) as session:
        arxiv_results = await asyncio.gather(
            *[
                fetch_arxiv_batch(session, batch, raw_dir, pacer)
                for batch in batches
            ],
            return_exceptions=True,
        )

    for batch, result in zip(batches, arxiv_results):
        if isinstance(result, Exception):
            arxiv_quarantine.append(
                {
                    "reason": f"ARXIV_BATCH_FAILED: {result}",
                    "batch_size": len(batch),
                }
            )
            continue
        papers.update(result)

    accepted: list[dict] = []
    for association in associations:
        metadata = papers.get(association["arxiv_id"])
        if metadata is None:
            arxiv_quarantine.append(
                {
                    "reason": "NO_ARXIV_METADATA",
                    "paper_arxiv_id": association["arxiv_id"],
                }
            )
            continue
        accepted.append({**association, "arxiv": metadata})

    repositories = sorted({a["repository_url"] for a in accepted})
    stars: dict[str, int] = {}
    github_quarantine: list[dict] = []

    github_semaphore = asyncio.Semaphore(github_concurrency)
    headers = github_headers()

    async with aiohttp.ClientSession(
        timeout=timeout,
        connector=aiohttp.TCPConnector(limit=github_concurrency),
        headers=headers,
    ) as session:
        async def bounded_github(repository_url: str) -> dict:
            async with github_semaphore:
                payload = await get_json(
                    session, repo_api_url(repository_url)
                )
                return validate_github_metadata(payload, repository_url)

        github_results = await asyncio.gather(
            *[bounded_github(url) for url in repositories],
            return_exceptions=True,
        )

    for repository_url, result in zip(repositories, github_results):
        if isinstance(result, Exception):
            github_quarantine.append(
                {
                    "reason": f"GITHUB_FAILED: {result}",
                    "repository_url": repository_url,
                }
            )
            continue
        stars[repository_url] = result["github_stars"]

    paper_records: list[dict] = []
    provenance: list[dict] = []
    association_rows: list[dict] = []
    seen_papers: set[str] = set()

    # Prefer associations whose GitHub metrics were successfully fetched
    # so that exported papers carry stars whenever any of their
    # associations succeeded.
    accepted_sorted = sorted(
        accepted,
        key=lambda item: stars.get(item["repository_url"]) is None,
    )

    for association in accepted_sorted:
        metadata = association["arxiv"]
        github_stars = stars.get(association["repository_url"])
        github_url = (
            association["repository_url"]
            if github_stars is not None
            else None
        )
        observed_at = collected_at if github_stars is not None else None

        bare = metadata["bare_id"]
        if bare not in seen_papers:
            seen_papers.add(bare)
            record = ResearchPaperRecord.model_validate(
                {
                    "source": {
                        "name": "arXiv (via Papers with Code)",
                        "url": metadata["paper_url"],
                    },
                    "collectedAt": collected_at,
                    "content": {
                        "title": metadata["title"],
                        "authors": metadata["authors"],
                        "paper_url": metadata["paper_url"],
                        "published_date": metadata["published_date"],
                        "github_url": github_url,
                        "github_stars": github_stars,
                        "github_metrics_collected_at": observed_at,
                    },
                }
            )
            paper_records.append(json.loads(record.model_dump_json()))

            provenance.append(
                {
                    "paper_id": f"arxiv:{bare}",
                    "paper_url": metadata["paper_url"],
                    "repository_url": association["repository_url"],
                    "association_type": association["association_type"],
                    "source_paper_url": association["source_paper_url"],
                    "raw_sha256": hashlib.sha256(
                        json.dumps(
                            association, sort_keys=True, default=str
                        ).encode("utf-8")
                    ).hexdigest(),
                    "source_backed": True,
                    "independently_verified": False,
                }
            )

        # Only associations with verified GitHub metrics are exported as
        # accepted paper-repository associations.
        if github_stars is None:
            continue

        association_rows.append(
            {
                "paper_id": f"arxiv:{bare}",
                "paper_url": metadata["paper_url"],
                "repository_url": association["repository_url"],
                "association_type": association["association_type"],
                "is_official": association["is_official"],
                "github_stars": github_stars,
                "github_metrics_collected_at": (
                    observed_at.isoformat() if observed_at else None
                ),
                "source": "paperswithcode-links",
            }
        )

    def write_jsonl(name: str, rows: list[dict]) -> None:
        path = run_dir / name
        temporary = run_dir / f"{name}.tmp"
        with temporary.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        temporary.replace(path)

    all_quarantine = quarantine + arxiv_quarantine + github_quarantine
    write_jsonl("papers.jsonl", paper_records)
    write_jsonl("associations.jsonl", association_rows)
    write_jsonl("provenance.jsonl", provenance)
    write_jsonl("quarantine.jsonl", all_quarantine)

    elapsed = time.monotonic() - started
    repositories_with_metrics = sum(
        1 for a in association_rows if a["github_stars"] is not None
    )

    metadata = {
        "schema_version": "1.0",
        "parser_version": PARSER_VERSION,
        "collected_at": collected_at.isoformat(),
        "sources": {
            "links": {
                "dataset": LINKS_DATASET,
                "license": LINKS_LICENSE,
                "attribution": LINKS_ATTRIBUTION,
            },
            "papers": {"api": ARXIV_ENDPOINT},
            "github": {"api": GITHUB_API},
        },
        "pages_requested": pages,
        "pages_fetched": len(page_results),
        "page_size": page_size,
        "input_association_rows": sum(
            len(p.get("rows", [])) for p in page_results
        ),
        "unique_associations": len(associations),
        "papers_with_metadata": len(papers),
        "unique_papers": len(paper_records),
        "accepted_associations": len(association_rows),
        "official_associations": sum(
            1 for a in association_rows if a["is_official"]
        ),
        "community_associations": sum(
            1 for a in association_rows if not a["is_official"]
        ),
        "unique_repositories": len(repositories),
        "repositories_with_metrics": repositories_with_metrics,
        "github_metrics_present_for_accepted": (
            len(association_rows) > 0
            and repositories_with_metrics == len(association_rows)
        ),
        "concurrency": concurrency,
        "github_concurrency": github_concurrency,
        "quarantine_rows": len(all_quarantine),
        "wall_clock_seconds": round(elapsed, 3),
        "records_per_second": (
            round(len(association_rows) / elapsed, 3) if elapsed else 0.0
        ),
        "resumable": True,
        "output_dir": str(run_dir),
    }

    (run_dir / "metadata.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    return metadata


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Acquire papers + GitHub associations (PwC links)."
    )
    parser.add_argument(
        "--output-dir", type=Path, default=Path("data/pwc")
    )
    parser.add_argument("--pages", type=int, default=20)
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--github-concurrency", type=int, default=4)
    parser.add_argument("--cache-dir", type=Path)
    args = parser.parse_args()

    try:
        from ai_ingest.config import load_environment

        if Path(".env").is_file():
            load_environment(Path(".env"))

        result = asyncio.run(
            run_acquisition(
                output_dir=args.output_dir,
                pages=args.pages,
                page_size=args.page_size,
                concurrency=args.concurrency,
                github_concurrency=args.github_concurrency,
                cache_dir=args.cache_dir,
            )
        )
    except Exception as exc:
        parser.exit(status=1, message=f"Acquisition failed: {exc}\n")

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
