import argparse
import asyncio
import json
import re

from datetime import datetime, timezone
from pathlib import Path

from ai_ingest.models import ResearchPaperRecord
from ai_ingest.sources.arxiv import ingest_page
from ai_ingest.storage.sqlite import PaperStore


def bounded_query(query: str, before: str) -> str:
    """Build a publication-date-bounded arXiv query."""
    if not query.strip():
        raise ValueError("query cannot be empty.")

    if not re.fullmatch(r"\d{12}", before):
        raise ValueError(
            "before must use YYYYMMDDHHMM format in UTC."
        )

    cutoff = datetime.strptime(
        before,
        "%Y%m%d%H%M",
    ).replace(tzinfo=timezone.utc)

    if cutoff > datetime.now(timezone.utc):
        raise ValueError("before cannot be in the future.")

    return (
        f"({query}) AND "
        f"submittedDate:[199101010000 TO {before}]"
    )


def load_page(run_dir: Path) -> list[ResearchPaperRecord]:
    """Validate every staged record before committing the page."""
    records = []

    with (run_dir / "papers.jsonl").open(
        encoding="utf-8"
    ) as handle:
        for line in handle:
            if line.strip():
                records.append(
                    ResearchPaperRecord.model_validate_json(line)
                )

    return records


async def crawl(
    *,
    query: str,
    before: str,
    page_size: int,
    max_pages: int,
    database: Path,
    output_dir: Path,
    user_agent: str,
) -> dict:
    if not 1 <= page_size <= 100:
        raise ValueError("page_size must be between 1 and 100.")

    if max_pages < 1:
        raise ValueError("max_pages must be positive.")

    full_query = bounded_query(query, before)

    # Changing a setting creates a different checkpoint identity.
    config = {
        "adapter_version": "arxiv-offset-v1",
        "query": full_query,
        "page_size": page_size,
        "sort_by": "submittedDate",
        "sort_order": "descending",
    }

    store = PaperStore(database)

    try:
        crawl_id = store.create_crawl(config)
        committed_pages = 0
        returned_records = 0
        stop_reason = "page_budget_reached"

        for _ in range(max_pages):
            start = store.next_start(crawl_id)

            # ingest_page creates a new client for each page.
            # Its own limiter handles retries; this delay handles
            # spacing between separate page clients.
            # This is NOT a cross-process rate limiter.
            await asyncio.sleep(3.1)

            run_dir = await ingest_page(
                query=full_query,
                start=start,
                page_size=page_size,
                output_dir=output_dir,
                user_agent=user_agent,
            )

            records = load_page(run_dir)

            if not records:
                # Do not advance the offset on an empty response.
                # A future invocation can recheck this position.
                stop_reason = "empty_page"
                break

            if len(records) > page_size:
                raise ValueError(
                    "Response contains more records than requested."
                )

            committed = store.commit_page(
                crawl_id=crawl_id,
                start=start,
                records=records,
                evidence_path=run_dir / "response.xml",
            )

            if committed:
                committed_pages += 1
                returned_records += len(records)

            # A short nonempty page is not treated as definitive
            # proof that the result set is exhausted.
            print(
                f"offset={start} "
                f"returned={len(records)} "
                f"next_start={store.next_start(crawl_id)} "
                f"database_unique_papers={store.paper_count()}",
                flush=True,
            )

        return {
            "crawl_id": crawl_id,
            "committed_pages_this_run": committed_pages,
            "returned_records_this_run": returned_records,
            "next_start": store.next_start(crawl_id),
            "database_unique_papers": store.paper_count(),
            "stop_reason": stop_reason,
        }

    finally:
        store.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bounded, resumable local arXiv ingestion."
    )

    parser.add_argument("--query", default="cat:cs.AI")

    parser.add_argument(
        "--before",
        required=True,
        help="Fixed UTC publication cutoff: YYYYMMDDHHMM.",
    )

    parser.add_argument(
        "--page-size",
        type=int,
        default=25,
    )

    parser.add_argument(
        "--max-pages",
        type=int,
        default=2,
        help="Maximum pages to fetch during this invocation.",
    )

    parser.add_argument(
        "--database",
        type=Path,
        default=Path("data/arxiv/papers.sqlite3"),
    )

    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/arxiv"),
    )

    parser.add_argument(
        "--user-agent",
        required=True,
        help="Application name and a real contact address.",
    )

    args = parser.parse_args()

    try:
        result = asyncio.run(
            crawl(
                query=args.query,
                before=args.before,
                page_size=args.page_size,
                max_pages=args.max_pages,
                database=args.database,
                output_dir=args.output_dir,
                user_agent=args.user_agent,
            )
        )
    except Exception as exc:
        parser.exit(
            status=1,
            message=f"Crawl failed: {exc}\n",
        )

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()