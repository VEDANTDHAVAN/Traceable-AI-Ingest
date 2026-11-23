"""Phase II acquisition: 24-hour news and AI job ingestion.

Run outputs (one directory per run):
  raw/            original feed/API bodies
  news.jsonl      exported, freshness-verified news records
  jobs.jsonl      exported, freshness-verified AI job records
  provenance.jsonl
  duplicates.jsonl
  quarantine.jsonl
  metadata.json   per-source accounting and bounded-concurrency telemetry
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time

from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup

from ai_ingest.content import (
    canonical_url,
    content_identity,
    extract_article,
    normalize_whitespace,
    sha256_text,
)
from ai_ingest.entity_resolution import EntityResolver
from ai_ingest.freshness import classify_freshness, parse_publication
from ai_ingest.models import JobRecord, NewsRecord
from ai_ingest.net import AsyncFetcher, SourcePolicy
from ai_ingest.registry import ProcessedRegistry
from ai_ingest.sources.news_jobs import (
    JOB_BOARDS,
    NEWS_FEEDS,
    ai_match_terms,
    board_company,
    discount_duplicate_titles,
    feed_entries,
    parse_job_payload,
    role_family,
)


USER_AGENT = (
    "ai-intelligence-ingestion/0.2 "
    "(source-traceable research crawler; contact: project maintainer)"
)

DEFAULT_REGISTRY = Path("data/phase2/registry.sqlite3")
DEFAULT_ENTITY_SEEDS = Path("configs/canonical_entities.json")


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    temporary.replace(path)


def html_to_text(value: str) -> str:
    if not value:
        return ""
    soup = BeautifulSoup(value, "html.parser")
    text = normalize_whitespace(soup.get_text(" ", strip=True))
    return text


def _policies() -> dict[str, SourcePolicy]:
    policies: dict[str, SourcePolicy] = {}
    for feed in NEWS_FEEDS.values():
        policies.setdefault(
            feed["policy"],
            SourcePolicy(
                name=feed["policy"],
                domain=feed["domain"],
                max_concurrency=2,
                min_interval=0.6,
                max_attempts=3,
                retry_budget=2,
            ),
        )
    for board in JOB_BOARDS.values():
        policies.setdefault(
            board["policy"],
            SourcePolicy(
                name=board["policy"],
                domain=board["domain"],
                max_concurrency=2,
                min_interval=0.4,
                max_attempts=3,
                retry_budget=2,
                # Large ATS boards (OpenAI ~850 postings) exceed 4 MB.
                max_bytes=16_000_000,
            ),
        )
    return policies


def _news_record(
    *,
    feed: dict,
    title: str,
    article_url: str,
    canonical: str,
    published: datetime,
    text: str,
    extraction_status: str,
    collected_at: datetime,
) -> dict:
    record = NewsRecord.model_validate(
        {
            "source": {"name": feed["name"], "url": article_url},
            "collectedAt": collected_at,
            "content": {
                "title": title,
                "date": published,
                "full_text": text,
                "canonical_url": canonical,
                "content_sha256": sha256_text(text),
                "freshness_status": "FRESH_24H",
                "extraction_status": extraction_status,
                "ingested_at": collected_at,
            },
        }
    )
    return json.loads(record.model_dump_json())


def _job_record(
    *,
    board: dict,
    job: dict,
    company_canonical: str | None,
    published: datetime,
    text: str,
    extraction_status: str,
    collected_at: datetime,
) -> dict:
    record = JobRecord.model_validate(
        {
            "source": {"name": board["name"], "url": job["url"]},
            "collectedAt": collected_at,
            "content": {
                "title": job["title"],
                "company": company_canonical,
                "date": published,
                "isRemote": job["is_remote"],
                "roleFamily": role_family(job.get("department"), job["title"]),
                "full_text": text,
                "extraction_status": extraction_status,
            },
        }
    )
    return json.loads(record.model_dump_json())


async def run_acquisition(
    *,
    output_dir: Path,
    registry_path: Path = DEFAULT_REGISTRY,
    entity_seeds: Path = DEFAULT_ENTITY_SEEDS,
    news_keys: list[str] | None = None,
    job_keys: list[str] | None = None,
    max_per_feed: int = 100,
    max_per_board: int = 400,
    article_concurrency: int = 4,
) -> dict:
    started = time.monotonic()
    collected_at = datetime.now(timezone.utc)
    run_id = collected_at.strftime("%Y%m%dT%H%M%S%fZ")
    run_dir = output_dir / run_id
    raw_dir = run_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=False)

    news_selection = list(news_keys or NEWS_FEEDS)
    job_selection = list(job_keys or JOB_BOARDS)

    resolver = EntityResolver.from_file(entity_seeds)
    registry = ProcessedRegistry(registry_path)

    policies = _policies()
    news: list[dict] = []
    jobs: list[dict] = []
    provenance: list[dict] = []
    duplicates: list[dict] = []
    quarantine: list[dict] = []
    filtered: list[dict] = []

    per_source: dict[str, dict] = {}
    news_sources_queried = 0
    job_boards_queried = 0

    fetcher = AsyncFetcher(
        policies=policies,
        user_agent=USER_AGENT,
        raw_dir=raw_dir,
    )

    async with fetcher:
        feed_results = await asyncio.gather(
            *[
                fetcher.fetch(
                    NEWS_FEEDS[key]["policy"],
                    NEWS_FEEDS[key]["feed_url"],
                    cache_key=f"feed-{key}",
                    check_robots=False,
                )
                for key in news_selection
            ],
            return_exceptions=True,
        )

        board_results = await asyncio.gather(
            *[
                fetcher.fetch(
                    JOB_BOARDS[key]["policy"],
                    JOB_BOARDS[key]["url"],
                    cache_key=f"board-{key}",
                    check_robots=False,
                )
                for key in job_selection
            ],
            return_exceptions=True,
        )

        # ---- News ----
        pending_articles: list[tuple[str, dict, dict]] = []
        for key, result in zip(news_selection, feed_results):
            feed = NEWS_FEEDS[key]
            stats = {
                "name": feed["name"],
                "feed_url": feed["feed_url"],
                "queried": True,
                "entries": 0,
                "exported": 0,
                "duplicates": 0,
                "quarantined": 0,
            }
            per_source[f"news:{key}"] = stats
            news_sources_queried += 1

            if isinstance(result, Exception):
                quarantine.append(
                    {"source": key, "kind": "NEWS", "reason": f"FEED_ERROR: {result}"}
                )
                stats["quarantined"] += 1
                continue

            if not result.ok:
                quarantine.append(
                    {
                        "source": key,
                        "kind": "NEWS",
                        "reason": f"FEED_UNAVAILABLE: {result.blocked_reason}",
                    }
                )
                stats["quarantined"] += 1
                continue

            try:
                entries = feed_entries(result.body)
            except ValueError as exc:
                quarantine.append(
                    {"source": key, "kind": "NEWS", "reason": str(exc)}
                )
                stats["quarantined"] += 1
                continue

            stats["entries"] = len(entries)

            for entry in entries[:max_per_feed]:
                title = entry["title"]
                link = entry["link"]

                if not title or not link.startswith(("https://", "http://")):
                    quarantine.append(
                        {
                            "source": key,
                            "kind": "NEWS",
                            "reason": "MISSING_TITLE_OR_URL",
                            "title": title or None,
                            "link": link or None,
                        }
                    )
                    stats["quarantined"] += 1
                    continue

                published = entry["published"]
                if published is None and entry.get("published_raw"):
                    published = parse_publication(entry["published_raw"])

                try:
                    canonical = canonical_url(link)
                except ValueError:
                    quarantine.append(
                        {
                            "source": key,
                            "kind": "NEWS",
                            "reason": "INVALID_ARTICLE_URL",
                            "link": link,
                        }
                    )
                    stats["quarantined"] += 1
                    continue

                if registry.seen(
                    kind="URL", identity=canonical, record_type="NEWS"
                ):
                    duplicates.append(
                        {
                            "source": key,
                            "kind": "NEWS",
                            "reason": "DUPLICATE_CANONICAL_URL",
                            "canonical_url": canonical,
                        }
                    )
                    stats["duplicates"] += 1
                    continue

                pending_articles.append(
                    (
                        key,
                        feed,
                        {
                            **entry,
                            "canonical": canonical,
                            "published": published,
                            "feed_raw_sha256": result.raw_sha256,
                        },
                    )
                )

        async def enrich_article(item: tuple[str, dict, dict]) -> dict:
            key, feed, entry = item
            article_result = await fetcher.fetch(
                feed["policy"],
                entry["link"],
                cache_key=f"article-{hashlib.sha256(entry['link'].encode()).hexdigest()[:16]}",
                check_robots=True,
            )

            extracted = None
            if article_result.ok:
                extracted = extract_article(
                    article_result.text, fallback_title=entry["title"]
                )

            published = entry["published"]
            if published is None and extracted is not None:
                published = extracted["published_at"]

            return {
                "key": key,
                "feed": feed,
                "entry": entry,
                "result": article_result,
                "extracted": extracted,
                "published": published,
            }

        article_semaphore = asyncio.Semaphore(max(1, article_concurrency))

        async def bounded_article(item: tuple[str, dict, dict]) -> dict:
            async with article_semaphore:
                return await enrich_article(item)

        enriched = await asyncio.gather(
            *[bounded_article(item) for item in pending_articles],
            return_exceptions=True,
        )

        for outcome in enriched:
            if isinstance(outcome, Exception):
                quarantine.append(
                    {"kind": "NEWS", "reason": f"ARTICLE_ENRICH_ERROR: {outcome}"}
                )
                continue

            key = outcome["key"]
            feed = outcome["feed"]
            entry = outcome["entry"]
            extracted = outcome["extracted"]
            stats = per_source[f"news:{key}"]

            published = outcome["published"]
            freshness = classify_freshness(published, now=collected_at)
            if freshness != "FRESH_24H":
                quarantine.append(
                    {
                        "source": key,
                        "kind": "NEWS",
                        "reason": (
                            "UNKNOWN_PUBLICATION_DATE"
                            if freshness == "UNKNOWN"
                            else f"NOT_FRESH_24H: {freshness}"
                        ),
                        "link": entry["link"],
                        "published": published.isoformat() if published else None,
                    }
                )
                stats["quarantined"] += 1
                continue

            canonical = outcome["entry"]["canonical"]
            if extracted and extracted.get("canonical_url"):
                try:
                    candidate = canonical_url(extracted["canonical_url"])
                    if candidate:
                        canonical = candidate
                except ValueError:
                    pass

            summary_text = max(
                (
                    html_to_text(entry["summary"] or ""),
                    html_to_text(entry["content_text"] or ""),
                ),
                key=len,
            )
            full_text = (extracted or {}).get("text") or ""
            article_status = (extracted or {}).get("extraction_status") or "FEED_SUMMARY"

            if len(full_text) >= 200:
                text = full_text
                extraction_status = "FULL_TEXT"
            elif len(summary_text) >= 80:
                text = summary_text
                extraction_status = "FEED_SUMMARY"
                article_status = "FEED_SUMMARY"
            else:
                quarantine.append(
                    {
                        "source": key,
                        "kind": "NEWS",
                        "reason": "INSUFFICIENT_TEXT",
                        "link": entry["link"],
                    }
                )
                stats["quarantined"] += 1
                continue

            identity = content_identity(text)
            if registry.seen(kind="CONTENT", identity=identity, record_type="NEWS"):
                duplicates.append(
                    {
                        "source": key,
                        "kind": "NEWS",
                        "reason": "DUPLICATE_CONTENT_HASH",
                        "canonical_url": canonical,
                        "content_identity": identity,
                    }
                )
                stats["duplicates"] += 1
                continue

            claimed_url = registry.claim(
                kind="URL",
                identity=canonical,
                record_type="NEWS",
                run_id=run_id,
                first_seen_at=collected_at.isoformat(),
            )
            claimed_content = registry.claim(
                kind="CONTENT",
                identity=identity,
                record_type="NEWS",
                run_id=run_id,
                first_seen_at=collected_at.isoformat(),
            )
            if not claimed_url or not claimed_content:
                duplicates.append(
                    {
                        "source": key,
                        "kind": "NEWS",
                        "reason": "REGISTRY_ALREADY_CLAIMED",
                        "canonical_url": canonical,
                    }
                )
                stats["duplicates"] += 1
                continue

            try:
                record = _news_record(
                    feed=feed,
                    title=entry["title"],
                    article_url=entry["link"],
                    canonical=canonical,
                    published=published,
                    text=text,
                    extraction_status=extraction_status,
                    collected_at=collected_at,
                )
            except Exception as exc:
                quarantine.append(
                    {
                        "source": key,
                        "kind": "NEWS",
                        "reason": f"SCHEMA_INVALID: {exc}",
                        "link": entry["link"],
                    }
                )
                stats["quarantined"] += 1
                continue

            news.append(record)
            stats["exported"] += 1

            provenance.append(
                {
                    "record_type": "NEWS",
                    "source_key": key,
                    "source_name": feed["name"],
                    "feed_url": feed["feed_url"],
                    "article_url": entry["link"],
                    "canonical_url": canonical,
                    "feed_published_raw": entry.get("published_raw"),
                    "published_at": published.isoformat(),
                    "ingested_at": collected_at.isoformat(),
                    "content_sha256": record["content"]["content_sha256"],
                    "content_identity": identity,
                    "extraction_status": extraction_status,
                    "article_fetch_status": (
                        outcome["result"].status
                        if outcome["result"] is not None
                        else None
                    ),
                    "article_blocked_reason": (
                        outcome["result"].blocked_reason
                        if outcome["result"] is not None
                        else None
                    ),
                    "raw_sha256": (
                        outcome["result"].raw_sha256
                        if outcome["result"] is not None
                        and outcome["result"].raw_sha256
                        else entry.get("feed_raw_sha256")
                    ),
                    "article_raw_sha256": (
                        outcome["result"].raw_sha256
                        if outcome["result"] is not None
                        else None
                    ),
                    "feed_raw_sha256": entry.get("feed_raw_sha256"),
                    "evidence_snapshot": (
                        "ARTICLE"
                        if outcome["result"] is not None
                        and outcome["result"].raw_sha256
                        else "FEED"
                    ),
                    "attribution": feed["license_note"],
                    "source_backed": True,
                    "independently_verified": False,
                }
            )

        # ---- Jobs ----
        for key, result in zip(job_selection, board_results):
            board = JOB_BOARDS[key]
            stats = {
                "name": board["name"],
                "url": board["url"],
                "queried": True,
                "entries": 0,
                "exported": 0,
                "duplicates": 0,
                "quarantined": 0,
                "filtered_non_ai": 0,
            }
            per_source[f"jobs:{key}"] = stats
            job_boards_queried += 1

            if isinstance(result, Exception):
                quarantine.append(
                    {"source": key, "kind": "JOB", "reason": f"BOARD_ERROR: {result}"}
                )
                stats["quarantined"] += 1
                continue

            if not result.ok:
                quarantine.append(
                    {
                        "source": key,
                        "kind": "JOB",
                        "reason": f"BOARD_UNAVAILABLE: {result.blocked_reason}",
                    }
                )
                stats["quarantined"] += 1
                continue

            try:
                parsed = parse_job_payload(board["kind"], result.body)
            except (ValueError, json.JSONDecodeError) as exc:
                quarantine.append(
                    {"source": key, "kind": "JOB", "reason": f"PARSER_ERROR: {exc}"}
                )
                stats["quarantined"] += 1
                continue

            parsed = discount_duplicate_titles(parsed)[:max_per_board]
            stats["entries"] = len(parsed)

            for job in parsed:
                published = job["published_at"]
                freshness = classify_freshness(published, now=collected_at)
                if freshness != "FRESH_24H":
                    quarantine.append(
                        {
                            "source": key,
                            "kind": "JOB",
                            "reason": (
                                "UNKNOWN_PUBLICATION_DATE"
                                if freshness == "UNKNOWN"
                                else f"NOT_FRESH_24H: {freshness}"
                            ),
                            "url": job["url"],
                            "published": (
                                published.isoformat() if published else None
                            ),
                        }
                    )
                    stats["quarantined"] += 1
                    continue

                try:
                    canonical = canonical_url(job["url"])
                except ValueError:
                    quarantine.append(
                        {
                            "source": key,
                            "kind": "JOB",
                            "reason": "INVALID_JOB_URL",
                            "url": job["url"],
                        }
                    )
                    stats["quarantined"] += 1
                    continue

                description_text = html_to_text(job["description"])
                haystack = " ".join(
                    [
                        job["title"] or "",
                        job.get("company") or "",
                        job.get("department") or "",
                        description_text[:1500],
                    ]
                )
                terms = ai_match_terms(haystack)
                if not terms:
                    filtered.append(
                        {
                            "source": key,
                            "url": job["url"],
                            "title": job["title"],
                            "reason": "NOT_AI_RELATED",
                        }
                    )
                    stats["filtered_non_ai"] += 1
                    continue

                if registry.seen(kind="URL", identity=canonical, record_type="JOB"):
                    duplicates.append(
                        {
                            "source": key,
                            "kind": "JOB",
                            "reason": "DUPLICATE_CANONICAL_URL",
                            "canonical_url": canonical,
                        }
                    )
                    stats["duplicates"] += 1
                    continue

                raw_company = board_company(board["kind"], {}, board, job)
                company_evidence = "OFFICIAL_BOARD_OWNER" if board.get("company") else "SOURCE_FIELD"
                resolution = resolver.resolve(raw_company)
                canonical_company = resolution.get("canonical_name")

                text = description_text or job["title"]
                extraction_status = (
                    "FULL_TEXT"
                    if len(description_text) >= 200
                    else "FEED_SUMMARY"
                )
                job_identity = content_identity(
                    " ".join(
                        [
                            job["title"],
                            raw_company or "",
                            job["description"] or "",
                        ]
                    )
                )

                if registry.seen(
                    kind="CONTENT", identity=job_identity, record_type="JOB"
                ):
                    duplicates.append(
                        {
                            "source": key,
                            "kind": "JOB",
                            "reason": "DUPLICATE_CONTENT_HASH",
                            "canonical_url": canonical,
                        }
                    )
                    stats["duplicates"] += 1
                    continue

                claimed = registry.claim(
                    kind="URL",
                    identity=canonical,
                    record_type="JOB",
                    run_id=run_id,
                    first_seen_at=collected_at.isoformat(),
                )
                if not claimed:
                    duplicates.append(
                        {
                            "source": key,
                            "kind": "JOB",
                            "reason": "REGISTRY_ALREADY_CLAIMED",
                            "canonical_url": canonical,
                        }
                    )
                    stats["duplicates"] += 1
                    continue

                try:
                    record = _job_record(
                        board=board,
                        job=job,
                        company_canonical=canonical_company,
                        published=published,
                        text=text,
                        extraction_status=extraction_status,
                        collected_at=collected_at,
                    )
                except Exception as exc:
                    quarantine.append(
                        {
                            "source": key,
                            "kind": "JOB",
                            "reason": f"SCHEMA_INVALID: {exc}",
                            "url": job["url"],
                        }
                    )
                    stats["quarantined"] += 1
                    continue

                registry.claim(
                    kind="CONTENT",
                    identity=job_identity,
                    record_type="JOB",
                    run_id=run_id,
                    first_seen_at=collected_at.isoformat(),
                )
                jobs.append(record)
                stats["exported"] += 1

                provenance.append(
                    {
                        "record_type": "JOB",
                        "source_key": key,
                        "source_name": board["name"],
                        "source_url": board["url"],
                        "job_url": job["url"],
                        "canonical_url": canonical,
                        "published_at": published.isoformat(),
                        "ingested_at": collected_at.isoformat(),
                        "company_raw": raw_company,
                        "company_canonical": canonical_company,
                        "company_resolution_status": resolution.get("status"),
                        "company_evidence": company_evidence,
                        "ownership_verification_status": "NAME_MATCH_ONLY",
                        "role_family": record["content"]["roleFamily"],
                        "is_remote": record["content"]["isRemote"],
                        "ai_match_terms": terms,
                        "content_identity": job_identity,
                        "content_sha256": sha256_text(text),
                        "extraction_status": extraction_status,
                        "raw_sha256": result.raw_sha256,
                        "source_backed": True,
                        "independently_verified": False,
                    }
                )

    registry.close()

    write_jsonl(run_dir / "news.jsonl", news)
    write_jsonl(run_dir / "jobs.jsonl", jobs)
    write_jsonl(run_dir / "provenance.jsonl", provenance)
    write_jsonl(run_dir / "duplicates.jsonl", duplicates)
    write_jsonl(run_dir / "quarantine.jsonl", quarantine)
    write_jsonl(run_dir / "filtered.jsonl", filtered)

    elapsed = time.monotonic() - started
    report = fetcher.report()
    report_path = run_dir / "http_report.json"
    report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    registry_count = ProcessedRegistry(registry_path).count()

    metadata = {
        "schema_version": "1.0",
        "parser_version": "news-jobs-v1",
        "run_id": run_id,
        "collected_at": collected_at.isoformat(),
        "freshness_window_hours": 24,
        "news_sources_queried": news_sources_queried,
        "job_boards_queried": job_boards_queried,
        "news_exported": len(news),
        "jobs_exported": len(jobs),
        "news_duplicate_rows": sum(
            1 for row in duplicates if row.get("kind") == "NEWS"
        ),
        "job_duplicate_rows": sum(
            1 for row in duplicates if row.get("kind") == "JOB"
        ),
        "news_quarantine_rows": sum(
            1 for row in quarantine if row.get("kind") == "NEWS"
        ),
        "job_quarantine_rows": sum(
            1 for row in quarantine if row.get("kind") == "JOB"
        ),
        "jobs_filtered_non_ai": len(filtered),
        "per_source": per_source,
        "wall_clock_seconds": round(elapsed, 3),
        "effective_records_per_second": round(
            (len(news) + len(jobs)) / elapsed, 3
        ) if elapsed else 0.0,
        "max_concurrent_requests": report["max_concurrent_requests"],
        "user_agent": USER_AGENT,
        "rate_limit_events": sum(
            metric["rate_limit_events"] for metric in report["metrics"].values()
        ),
        "retry_count": sum(
            metric["retries"] for metric in report["metrics"].values()
        ),
        "resumable": True,
        "registry_path": str(registry_path),
        "registry_identities": registry_count,
        "output_dir": str(run_dir),
    }
    (run_dir / "metadata.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    return metadata


def latest_run(root: Path) -> Path:
    runs = sorted(path for path in root.iterdir() if path.is_dir() and path.name[0].isdigit())
    if not runs:
        raise ValueError(f"No runs found in {root}")
    return runs[-1]


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase II news and AI job ingestion.")
    parser.add_argument("--output-dir", type=Path, default=Path("data/phase2"))
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--entity-seeds", type=Path, default=DEFAULT_ENTITY_SEEDS)
    parser.add_argument("--max-per-feed", type=int, default=100)
    parser.add_argument("--max-per-board", type=int, default=400)
    parser.add_argument("--article-concurrency", type=int, default=4)
    args = parser.parse_args()

    result = asyncio.run(
        run_acquisition(
            output_dir=args.output_dir,
            registry_path=args.registry,
            entity_seeds=args.entity_seeds,
            max_per_feed=args.max_per_feed,
            max_per_board=args.max_per_board,
            article_concurrency=args.article_concurrency,
        )
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
