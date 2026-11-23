"""Stage the AIToolBuzz AI tools directory as product observations.

Source: https://huggingface.co/datasets/aitoolbuzz/ai-tools-database-25k-aitoolbuzz
License: CC-BY-SA-4.0 (dataset card).
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import io
import json
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

from ai_ingest.models import ProductRecord

DATASET_URL = (
    "https://huggingface.co/datasets/aitoolbuzz/"
    "ai-tools-database-25k-aitoolbuzz/resolve/main/"
    "ai_tools_database_25k_aitoolbuzz.csv"
)
SOURCE_NAME = "AIToolBuzz AI Tools Database"
LICENSE = "CC-BY-SA-4.0"
ATTRIBUTION = "Data: AIToolBuzz AI Tools Database (CC-BY-SA-4.0)."
PARSER_VERSION = "aitoolbuzz-csv-v1"
REQUIRED_COLUMNS = {
    "No.",
    "Name",
    "Link",
    "Website",
    "Status",
    "Task",
    "Short Description",
}


def normalize_product_url(value: str) -> str:
    value = value.strip()

    if not value.startswith("https://"):
        raise ValueError("Product URL must use https.")

    return value.rstrip("/")


def normalize_pricing(value: object) -> str | None:
    if not isinstance(value, str):
        return None

    normalized = value.strip().casefold()
    mapping = {
        "free": "FREE",
        "freemium": "FREEMIUM",
        "paid": "PAID",
        "enterprise": "ENTERPRISE",
    }
    return mapping.get(normalized)


def normalize_tool(
    row: dict[str, str],
    *,
    collected_at: datetime,
) -> tuple[ProductRecord, dict]:
    name = (row.get("Name") or "").strip()
    if not name:
        raise ValueError("Missing product name.")

    website = normalize_product_url(row.get("Website") or "")
    source_page = (row.get("Link") or "").strip()
    if source_page and not source_page.startswith("https://"):
        source_page = ""

    record = ProductRecord.model_validate(
        {
            "source": {
                "name": SOURCE_NAME,
                "url": website,
            },
            "collectedAt": collected_at,
            "content": {
                "productName": name,
                "startupName": None,
                "pricingModel": normalize_pricing(row.get("Pricing")),
            },
        }
    )

    raw = json.dumps(row, sort_keys=True, ensure_ascii=False)
    provenance = {
        "source_name": SOURCE_NAME,
        "source_url": website,
        "source_page": source_page or None,
        "source_record_id": (row.get("No.") or "").strip() or None,
        "source_product_name": name,
        "source_pricing": (row.get("Pricing") or "").strip() or None,
        "source_task": (row.get("Task") or "").strip() or None,
        "raw_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
    }

    return record, provenance


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


async def download(output_dir: Path) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "ai_tools_database_25k_aitoolbuzz.csv"

    timeout = aiohttp.ClientTimeout(total=180)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(DATASET_URL) as response:
            response.raise_for_status()
            body = await response.read()

    csv_path.write_bytes(body)

    return {
        "csv_path": str(csv_path),
        "sha256": hashlib.sha256(body).hexdigest(),
        "bytes": len(body),
    }


def stage_snapshot(snapshot_dir: Path) -> dict:
    snapshot_dir = snapshot_dir.resolve()
    csv_path = snapshot_dir / "ai_tools_database_25k_aitoolbuzz.csv"
    if not csv_path.exists():
        raise ValueError(f"Missing snapshot CSV: {csv_path}")

    raw = csv_path.read_bytes()
    text = raw.decode("utf-8-sig", "replace")
    reader = csv.DictReader(io.StringIO(text))

    columns = set(reader.fieldnames or [])
    missing = sorted(REQUIRED_COLUMNS - columns)
    if missing:
        raise ValueError(f"Unexpected CSV schema; missing {missing}")

    collected_at = datetime.now(timezone.utc)
    records = []
    provenance = []
    duplicates = []
    quarantine = []
    seen_urls: set[str] = set()

    for index, row in enumerate(reader, start=2):
        try:
            record, evidence = normalize_tool(
                row, collected_at=collected_at
            )
        except Exception as exc:
            quarantine.append(
                {
                    "row_number": index,
                    "reason": str(exc),
                    "name": (row.get("Name") or "").strip() or None,
                    "website": (row.get("Website") or "").strip() or None,
                }
            )
            continue

        url = str(record.source.url).rstrip("/")
        if url in seen_urls:
            duplicates.append(
                {
                    "row_number": index,
                    "reason": "DUPLICATE_PRODUCT_URL",
                    "product_url": url,
                    "name": record.content.productName,
                }
            )
            continue

        seen_urls.add(url)
        records.append(json.loads(record.model_dump_json()))
        provenance.append(evidence)

    write_jsonl(snapshot_dir / "products.jsonl", records)
    write_jsonl(snapshot_dir / "provenance.jsonl", provenance)
    write_jsonl(snapshot_dir / "duplicates.jsonl", duplicates)
    write_jsonl(snapshot_dir / "quarantine.jsonl", quarantine)

    metadata = {
        "schema_version": "1.0",
        "source_name": SOURCE_NAME,
        "dataset_url": DATASET_URL,
        "license": LICENSE,
        "attribution": ATTRIBUTION,
        "parser_version": PARSER_VERSION,
        "collected_at": collected_at.isoformat(),
        "csv_sha256": hashlib.sha256(raw).hexdigest(),
        "csv_bytes": len(raw),
        "input_rows": index - 1 if "index" in locals() else 0,
        "staged_products": len(records),
        "duplicate_rows": len(duplicates),
        "quarantine_rows": len(quarantine),
        "products_with_known_pricing": sum(
            1 for r in records if r["content"]["pricingModel"] is not None
        ),
        "products_with_resolved_company": 0,
        "company_ownership_verified": False,
        "final_quality_gate_passed": False,
    }

    (snapshot_dir / "metadata.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    return metadata


async def ingest(output_dir: Path) -> dict:
    collected_at = datetime.now(timezone.utc)
    run_id = collected_at.strftime("%Y%m%dT%H%M%S%fZ")
    snapshot_dir = output_dir / run_id
    download_info = await download(snapshot_dir)
    metadata = stage_snapshot(snapshot_dir)
    metadata["download"] = download_info
    (snapshot_dir / "metadata.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return metadata


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Stage AIToolBuzz product snapshot."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/products/aitoolbuzz"),
    )
    parser.add_argument("--snapshot", type=Path)
    args = parser.parse_args()

    try:
        if args.snapshot is not None:
            result = stage_snapshot(args.snapshot)
        else:
            result = asyncio.run(ingest(args.output_dir))
    except Exception as exc:
        parser.exit(status=1, message=f"AIToolBuzz staging failed: {exc}\n")

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
