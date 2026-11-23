"""Phase III: run grounded multi-provider extraction over Phase II records.

Extraction never mutates canonical records. Outputs are evidence artifacts:
each accepted claim carries its quote, offsets, provider, model, source URL and
source-text hash. Failed records are recorded with their attempt log.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import time

from datetime import datetime, timezone
from pathlib import Path

import aiohttp

from ai_ingest.config import load_environment
from ai_ingest.llm.engine import ExtractionEngine, ExtractionFailed, FACT_MODELS
from ai_ingest.llm.providers import HTTPProvider


PROVIDER_ORDER = ("gemini", "groq", "deepseek")


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
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    temporary.replace(path)


def configured_providers() -> list[tuple[str, str, str]]:
    configured = []

    for kind in PROVIDER_ORDER:
        prefix = kind.upper()
        key = os.environ.get(f"{prefix}_API_KEY", "").strip()
        model = os.environ.get(f"{prefix}_MODEL", "").strip()

        if bool(key) != bool(model):
            raise ValueError(
                f"Configure both {prefix}_API_KEY and {prefix}_MODEL, or neither."
            )

        if key and model:
            configured.append((kind, model, key))

    if not configured:
        raise ValueError("No provider is configured in the environment.")

    return configured


def select_records(input_dir: Path, kinds: list[str], limit: int) -> list[dict]:
    selected = []

    if "news" in kinds:
        for row in read_jsonl(input_dir / "news.jsonl"):
            if row["content"]["extraction_status"] != "FULL_TEXT":
                continue
            selected.append(
                {
                    "kind": "news",
                    "record_type": "NEWS",
                    "record_id": row["content"]["content_sha256"],
                    "source_url": str(row["source"]["url"]),
                    "published_at": row["content"]["date"],
                    "text": row["content"]["full_text"],
                    "title": row["content"]["title"],
                }
            )

    if "job" in kinds:
        for row in read_jsonl(input_dir / "jobs.jsonl"):
            if row["content"]["extraction_status"] != "FULL_TEXT":
                continue
            selected.append(
                {
                    "kind": "job",
                    "record_type": "JOB",
                    "record_id": hashlib.sha256(
                        str(row["source"]["url"]).encode("utf-8")
                    ).hexdigest(),
                    "source_url": str(row["source"]["url"]),
                    "published_at": row["content"]["date"],
                    "text": row["content"]["full_text"],
                    "title": row["content"]["title"],
                }
            )

    # Interleave kinds so a small limit covers both news and jobs.
    selected.sort(key=lambda item: (item["kind"], item["record_id"]))
    if len(kinds) > 1:
        interleaved = []
        by_kind = {kind: [r for r in selected if r["kind"] == kind] for kind in kinds}
        while any(by_kind.values()):
            for kind in kinds:
                if by_kind[kind]:
                    interleaved.append(by_kind[kind].pop(0))
        selected = interleaved

    return selected[:limit]


async def run_extraction(
    *,
    input_dir: Path,
    output_dir: Path,
    kinds: list[str],
    limit: int,
    chunk_bytes: int,
    timeout_seconds: float,
) -> dict:
    providers_config = configured_providers()
    records = select_records(input_dir, kinds, limit)

    if not records:
        raise ValueError("No FULL_TEXT records matched the selection.")

    started = time.monotonic()
    collected_at = datetime.now(timezone.utc)
    run_dir = output_dir / collected_at.strftime("%Y%m%dT%H%M%S%fZ")
    run_dir.mkdir(parents=True, exist_ok=False)

    extracted: list[dict] = []
    failures: list[dict] = []

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=max(90, timeout_seconds + 30)),
    ) as session:
        async def process(record: dict) -> None:
            providers = [
                HTTPProvider(
                    kind=kind,
                    model=model,
                    api_key=key,
                    session=session,
                    evidence_dir=run_dir / "provider_responses",
                )
                for kind, model, key in providers_config
            ]
            engine = ExtractionEngine(
                providers,
                schema=record["kind"],
                chunk_bytes=chunk_bytes,
                attempts=2,
                timeout_seconds=timeout_seconds,
                max_wait_seconds=20,
                max_requests=6,
            )

            try:
                result = await engine.extract(record["text"])
            except ExtractionFailed as exc:
                failures.append(
                    {
                        **{key: record[key] for key in (
                            "kind",
                            "record_type",
                            "record_id",
                            "source_url",
                            "published_at",
                            "title",
                        )},
                        "error": str(exc),
                        "events": exc.events,
                        "canonical_records_updated": False,
                    }
                )
                return

            claims = []
            null_fields = []
            for chunk in result["chunks"]:
                for field, claim in chunk["facts"].items():
                    if claim is None:
                        null_fields.append(
                            {
                                "field": field,
                                "reason": "NOT_ESTABLISHED_BY_SOURCE",
                                "chunk_start": chunk["chunk_start"],
                            }
                        )
                        continue
                    claims.append(
                        {
                            "field": field,
                            **claim,
                            "provider": chunk["provider"],
                            "model": chunk["model"],
                            "chunk_start": chunk["chunk_start"],
                            "chunk_end": chunk["chunk_end"],
                            "source_url": record["source_url"],
                            "source_text_sha256": result["source_text_sha256"],
                        }
                    )

            extracted.append(
                {
                    "record_type": record["record_type"],
                    "record_id": record["record_id"],
                    "source_url": record["source_url"],
                    "published_at": record["published_at"],
                    "title": record["title"],
                    "schema": result["schema"],
                    "schema_version": "1.0",
                    "source_text_sha256": result["source_text_sha256"],
                    "source_characters": result["source_characters"],
                    "chunk_bytes": result["chunk_bytes"],
                    "chunks": result["chunks"],
                    "coverage_complete": result["coverage_complete"],
                    "requests_used": result["requests_used"],
                    "provider_attempts": result["provider_attempts"],
                    "provider_calls": result["provider_calls"],
                    "events": result["events"],
                    "claims": claims,
                    "null_fields": null_fields,
                    "extracted_at": datetime.now(timezone.utc).isoformat(),
                    "canonical_records_updated": False,
                    "source_backed": True,
                    "independently_verified": False,
                }
            )

        for record in records:
            await process(record)

    write_jsonl(run_dir / "extraction.jsonl", extracted)
    write_jsonl(run_dir / "failures.jsonl", failures)

    elapsed = time.monotonic() - started
    attempts = sum(len(row["provider_attempts"]) for row in extracted) + sum(
        len(row.get("events") or []) for row in failures
    )
    metadata = {
        "schema_version": "1.0",
        "purpose": "NEWS_JOB_GROUNDED_EXTRACTION",
        "provider_order": [
            {"provider": kind, "model": model}
            for kind, model, _key in providers_config
        ],
        "provider_fallback_implemented": len(providers_config) > 1,
        "chunk_bytes": chunk_bytes,
        "chunking_implemented": True,
        "rate_limit_backoff_implemented": True,
        "payload_too_large_handling_implemented": True,
        "records_selected": len(records),
        "records_extracted": len(extracted),
        "records_failed": len(failures),
        "claims_total": sum(len(row["claims"]) for row in extracted),
        "non_null_claims": sum(
            1
            for row in extracted
            for claim in row["claims"]
        ),
        "all_accepted_chunks_covered": all(
            row["coverage_complete"] for row in extracted
        ),
        "provider_attempts_logged": attempts,
        "wall_clock_seconds": round(elapsed, 3),
        "canonical_records_updated": False,
        "output_dir": str(run_dir),
        "collected_at": collected_at.isoformat(),
    }
    (run_dir / "metadata.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    return metadata


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Grounded LLM extraction for news and jobs."
    )
    parser.add_argument("--input", type=Path, default=Path("data/phase2/merged"))
    parser.add_argument("--output-dir", type=Path, default=Path("data/phase3"))
    parser.add_argument(
        "--kinds",
        default="news,job",
        help="Comma-separated subset of: news,job",
    )
    parser.add_argument("--limit", type=int, default=4)
    parser.add_argument("--chunk-bytes", type=int, default=6000)
    parser.add_argument("--timeout", type=float, default=75.0)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument(
        "--allow-api-calls",
        action="store_true",
        help="Explicitly permit potentially billable provider requests.",
    )
    args = parser.parse_args()

    if not args.allow_api_calls:
        parser.error("Live requests require --allow-api-calls.")

    kinds = [kind.strip() for kind in args.kinds.split(",") if kind.strip()]
    for kind in kinds:
        if kind not in FACT_MODELS:
            parser.error(f"Unknown kind: {kind}")

    load_environment(args.env_file)

    result = asyncio.run(
        run_extraction(
            input_dir=args.input,
            output_dir=args.output_dir,
            kinds=kinds,
            limit=args.limit,
            chunk_bytes=args.chunk_bytes,
            timeout_seconds=args.timeout,
        )
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
