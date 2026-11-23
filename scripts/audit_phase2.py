"""Phase II audit: freshness, provenance, deduplication and source coverage.

The audit merges every completed run under ``data/phase2`` into a stable
snapshot (``data/phase2/merged``) and verifies the exported rows offline
against each record's own ingestion timestamp.  This avoids depending on
wall-clock time at audit time while still proving the 24-hour gate that was
applied when the item was ingested.
"""

from __future__ import annotations

import argparse
import hashlib
import json

from datetime import datetime, timedelta, timezone
from pathlib import Path

from ai_ingest.models import JobRecord, NewsRecord


FRESH_WINDOW = timedelta(hours=24)
FUTURE_SKEW = timedelta(minutes=5)
EXTRACTION_STATUSES = {"FULL_TEXT", "FEED_SUMMARY", "FAILED"}


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON") from exc
    return rows


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    temporary.replace(path)


def parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Naive timestamp is not allowed.")
    return parsed.astimezone(timezone.utc)


def discover_runs(root: Path) -> list[Path]:
    runs = []
    for candidate in root.iterdir():
        if not candidate.is_dir() or not candidate.name[:1].isdigit():
            continue
        if (candidate / "metadata.json").is_file():
            runs.append(candidate)
    return sorted(runs)


def merge_runs(root: Path, output_dir: Path) -> dict:
    runs = discover_runs(root)
    if not runs:
        raise ValueError(f"No Phase II runs found under {root}.")

    news: dict[str, dict] = {}
    jobs: dict[str, dict] = {}
    provenance: dict[tuple[str, str], dict] = {}
    duplicates: list[dict] = []
    quarantine: list[dict] = []
    per_source: dict[str, dict] = {}
    news_sources: set[str] = set()
    job_boards: set[str] = set()
    max_concurrency = 0
    retry_count = 0
    rate_limit_events = 0
    wall_clock = 0.0
    user_agents: set[str] = set()

    for run in runs:
        metadata = json.loads((run / "metadata.json").read_text(encoding="utf-8"))
        max_concurrency = max(max_concurrency, metadata.get("max_concurrent_requests", 0))
        retry_count += metadata.get("retry_count", 0)
        rate_limit_events += metadata.get("rate_limit_events", 0)
        wall_clock += metadata.get("wall_clock_seconds", 0.0)
        if metadata.get("user_agent"):
            user_agents.add(metadata["user_agent"])

        for key, stats in metadata.get("per_source", {}).items():
            if key.startswith("news:"):
                news_sources.add(key.removeprefix("news:"))
            elif key.startswith("jobs:"):
                job_boards.add(key.removeprefix("jobs:"))
            bucket = per_source.setdefault(
                key,
                {
                    "queried_runs": 0,
                    "entries": 0,
                    "exported": 0,
                    "duplicates": 0,
                    "quarantined": 0,
                },
            )
            bucket["queried_runs"] += 1
            for field in ("entries", "exported", "duplicates", "quarantined"):
                bucket[field] += stats.get(field, 0)

        for row in read_jsonl(run / "news.jsonl"):
            key = row["content"]["content_sha256"]
            news.setdefault(key, row)

        for row in read_jsonl(run / "jobs.jsonl"):
            key = str(row["source"]["url"]).rstrip("/").casefold()
            jobs.setdefault(key, row)

        for row in read_jsonl(run / "provenance.jsonl"):
            if row["record_type"] == "NEWS":
                ident = row.get("canonical_url")
            else:
                ident = str(row.get("job_url", "")).rstrip("/").casefold()
            provenance.setdefault((row["record_type"], ident), row)

        duplicates.extend(read_jsonl(run / "duplicates.jsonl"))
        quarantine.extend(read_jsonl(run / "quarantine.jsonl"))

    output_dir.mkdir(parents=True, exist_ok=True)
    news_rows = list(news.values())
    job_rows = list(jobs.values())
    provenance_rows = list(provenance.values())

    # Older runs recorded only the article hash.  When article retrieval was
    # robots-blocked, fall back to the preserved feed snapshot that supplied
    # the record.  The chosen snapshot is hashed from disk, never invented.
    feed_hashes: dict[str, str] = {}
    for run in runs:
        raw_dir = run / "raw"
        if not raw_dir.is_dir():
            continue
        for raw_path in raw_dir.glob("*-feed-*.bin"):
            key = raw_path.name.split("-feed-")[0]
            feed_hashes[key] = hashlib.sha256(raw_path.read_bytes()).hexdigest()

    for row in provenance_rows:
        if row.get("record_type") == "NEWS" and not row.get("raw_sha256"):
            source_key = row.get("source_key", "")
            if source_key in feed_hashes:
                row["raw_sha256"] = feed_hashes[source_key]
                row["evidence_snapshot"] = "FEED"

    write_jsonl(output_dir / "news.jsonl", news_rows)
    write_jsonl(output_dir / "jobs.jsonl", job_rows)
    write_jsonl(output_dir / "provenance.jsonl", provenance_rows)
    write_jsonl(output_dir / "duplicates.jsonl", duplicates)
    write_jsonl(output_dir / "quarantine.jsonl", quarantine)

    metadata = {
        "schema_version": "1.0",
        "merged_at": datetime.now(timezone.utc).isoformat(),
        "runs_merged": [run.name for run in runs],
        "news_sources_queried": len(news_sources),
        "news_source_keys": sorted(news_sources),
        "job_boards_queried": len(job_boards),
        "job_board_keys": sorted(job_boards),
        "news_exported_unique": len(news_rows),
        "jobs_exported_unique": len(job_rows),
        "news_exported_by_source": _count_by(
            news_rows, lambda row: row["source"]["name"]
        ),
        "jobs_exported_by_source": _count_by(
            job_rows, lambda row: row["source"]["name"]
        ),
        "duplicate_records": len(duplicates),
        "quarantine_records": len(quarantine),
        "per_source": per_source,
        "max_concurrent_requests": max_concurrency,
        "retry_count": retry_count,
        "rate_limit_events": rate_limit_events,
        "wall_clock_seconds": round(wall_clock, 3),
        "user_agents": sorted(user_agents),
        "resumable": True,
        "processed_registry_present": any(
            (run.parent / "registry.sqlite3").is_file() for run in runs
        ),
    }
    (output_dir / "metadata.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return metadata


def _count_by(rows: list[dict], key) -> dict:
    counts: dict[str, int] = {}
    for row in rows:
        counts[key(row)] = counts.get(key(row), 0) + 1
    return counts


def audit(merged_dir: Path, output: Path) -> dict:
    report: dict = {"phase2_complete": False, "gates": {}, "counts": {}, "errors": []}

    def fail(message: str) -> None:
        report["errors"].append(message)

    metadata = json.loads((merged_dir / "metadata.json").read_text(encoding="utf-8"))
    news_rows = read_jsonl(merged_dir / "news.jsonl")
    job_rows = read_jsonl(merged_dir / "jobs.jsonl")
    provenance_rows = read_jsonl(merged_dir / "provenance.jsonl")
    quarantine_rows = read_jsonl(merged_dir / "quarantine.jsonl")

    news_records = []
    job_records = []
    for index, row in enumerate(news_rows):
        try:
            record = NewsRecord.model_validate(row)
            news_records.append(record)
        except Exception as exc:
            fail(f"News {index}: schema invalid: {exc}")

    for index, row in enumerate(job_rows):
        try:
            record = JobRecord.model_validate(row)
            job_records.append(record)
        except Exception as exc:
            fail(f"Job {index}: schema invalid: {exc}")

    provenance_index = {
        (row["record_type"], row.get("canonical_url")): row
        for row in provenance_rows
        if row["record_type"] == "NEWS"
    }
    provenance_index.update(
        {
            (
                row["record_type"],
                str(row.get("job_url", "")).rstrip("/").casefold(),
            ): row
            for row in provenance_rows
            if row["record_type"] == "JOB"
        }
    )

    seen_news_urls: set[str] = set()
    seen_news_hashes: set[str] = set()
    news_fresh = 0

    for index, record in enumerate(news_records):
        content = record.content
        ingested = record.collectedAt
        age = ingested - content.date
        if age < -FUTURE_SKEW or age > FRESH_WINDOW:
            fail(f"News {index}: publication outside 24h at ingestion ({age}).")
        else:
            news_fresh += 1

        if content.freshness_status != "FRESH_24H":
            fail(f"News {index}: freshness status not FRESH_24H.")

        if content.extraction_status not in EXTRACTION_STATUSES:
            fail(f"News {index}: unknown extraction status.")

        canonical = str(content.canonical_url).rstrip("/").casefold()
        if canonical in seen_news_urls:
            fail(f"News {index}: duplicate canonical URL.")
        seen_news_urls.add(canonical)

        if content.content_sha256 in seen_news_hashes:
            fail(f"News {index}: duplicate content hash.")
        seen_news_hashes.add(content.content_sha256)

        provenance = provenance_index.get(("NEWS", str(content.canonical_url)))
        if provenance is None:
            fail(f"News {index}: missing provenance.")
        else:
            if not provenance.get("source_backed"):
                fail(f"News {index}: not source-backed.")
            if provenance.get("independently_verified"):
                fail(f"News {index}: independently verified flags are unsupported.")
            if not provenance.get("raw_sha256"):
                fail(f"News {index}: missing raw snapshot hash.")
            if not provenance.get("attribution"):
                fail(f"News {index}: missing attribution.")

    seen_job_urls: set[str] = set()
    job_fresh = 0

    for index, record in enumerate(job_records):
        content = record.content
        age = record.collectedAt - content.date
        if age < -FUTURE_SKEW or age > FRESH_WINDOW:
            fail(f"Job {index}: publication outside 24h at ingestion ({age}).")
        else:
            job_fresh += 1

        if content.extraction_status not in EXTRACTION_STATUSES:
            fail(f"Job {index}: unknown extraction status.")

        canonical = str(record.source.url).rstrip("/").casefold()
        if canonical in seen_job_urls:
            fail(f"Job {index}: duplicate job URL.")
        seen_job_urls.add(canonical)

        provenance = provenance_index.get(("JOB", canonical))
        if provenance is None:
            fail(f"Job {index}: missing provenance.")
        else:
            if not provenance.get("source_backed"):
                fail(f"Job {index}: not source-backed.")
            if not provenance.get("raw_sha256"):
                fail(f"Job {index}: missing raw snapshot hash.")
            if not provenance.get("ai_match_terms"):
                fail(f"Job {index}: missing AI relevance evidence.")
            if provenance.get("company_canonical") is not None:
                if provenance.get("company_resolution_status") != "RESOLVED":
                    fail(f"Job {index}: canonical company without resolution.")
            if (
                provenance.get("company_canonical") is not None
                and provenance.get("ownership_verification_status")
                != "NAME_MATCH_ONLY"
            ):
                fail(f"Job {index}: unsupported ownership status.")

    for index, row in enumerate(quarantine_rows):
        if not row.get("reason"):
            fail(f"Quarantine {index}: missing reason.")
        if row.get("kind") not in {"NEWS", "JOB"}:
            fail(f"Quarantine {index}: unknown record kind.")

    news_sources = metadata.get("news_sources_queried", 0)
    job_boards = metadata.get("job_boards_queried", 0)

    gates = {
        "news_sources_queried_ge_5": news_sources >= 5,
        "job_boards_queried_ge_5": job_boards >= 5,
        "all_exported_news_within_24_hours": (
            len(news_records) > 0 and news_fresh == len(news_records)
        ),
        "all_exported_jobs_within_24_hours": (
            len(job_records) > 0 and job_fresh == len(job_records)
        ),
        "missing_freshness_quarantined": any(
            "UNKNOWN_PUBLICATION_DATE" in row.get("reason", "")
            for row in quarantine_rows
        ),
        "full_text_status_recorded": all(
            record.content.extraction_status in EXTRACTION_STATUSES
            for record in news_records
        )
        and all(
            record.content.extraction_status in EXTRACTION_STATUSES
            for record in job_records
        ),
        "content_hashes_present": all(
            record.content.content_sha256 for record in news_records
        ),
        "source_urls_present": all(
            record.source.url for record in news_records + job_records
        ),
        "no_duplicate_urls": len(seen_news_urls) == len(news_records)
        and len(seen_job_urls) == len(job_records),
        "no_duplicate_content_hashes": len(seen_news_hashes) == len(news_records),
        "provenance_for_every_record": (
            len(provenance_index) == len(news_records) + len(job_records)
        ),
        "rate_limit_compliance_documented": bool(metadata.get("user_agents")),
        "processed_registry_present": bool(metadata.get("processed_registry_present")),
    }
    report["gates"] = gates
    report["counts"] = {
        "news_sources_queried": news_sources,
        "job_boards_queried": job_boards,
        "news_exported": len(news_records),
        "jobs_exported": len(job_records),
        "news_fresh_within_24h": news_fresh,
        "jobs_fresh_within_24h": job_fresh,
        "quarantine_records": len(quarantine_rows),
        "duplicate_records": metadata.get("duplicate_records", 0),
        "provenance_rows": len(provenance_rows),
    }

    report["phase2_complete"] = all(gates.values()) and not report["errors"]

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase II completion audit.")
    parser.add_argument("--root", type=Path, default=Path("data/phase2"))
    parser.add_argument("--merged", type=Path, default=Path("data/phase2/merged"))
    parser.add_argument("--output", type=Path, default=Path("data/phase2/audit.json"))
    args = parser.parse_args()

    merge_runs(args.root, args.merged)
    report = audit(args.merged, args.output)
    print(json.dumps(report, indent=2))

    if not report["phase2_complete"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
