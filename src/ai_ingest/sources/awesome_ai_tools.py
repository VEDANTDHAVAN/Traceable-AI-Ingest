import argparse
import asyncio
import hashlib
import json

from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from ai_ingest.models import ProductRecord


REPO_API = (
    "https://api.github.com/repos/"
    "aliammari1/awesome-ai-tools/git/trees/main"
    "?recursive=1"
)

RAW_BASE = (
    "https://raw.githubusercontent.com/"
    "aliammari1/awesome-ai-tools/main/"
)

LICENSE_URL = f"{RAW_BASE}LICENSE"

ATTRIBUTION = (
    "Data: Awesome AI Tools, "
    "https://github.com/aliammari1/awesome-ai-tools. "
    "CC0 1.0. Fields normalized into the assessment schema."
)

ALLOWED_PRICING = {
    "free": "FREE",
    "freemium": "FREEMIUM",
    "paid": "PAID",
    "enterprise": "ENTERPRISE",
}

def normalize_product_url(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Product URL must be a string.")

    parsed = urlsplit(value.strip())

    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.port is not None
    ):
        raise ValueError("Unexpected product URL.")

    return urlunsplit(
        (
            "https",
            parsed.hostname,
            parsed.path.rstrip("/"),
            "",
            "",
        )
    )

def normalize_pricing(value) -> str | None:
    if value is None:
        return None

    if not isinstance(value, str):
        raise ValueError("Pricing must be a string or null.")

    key = value.strip().casefold()

    if key not in ALLOWED_PRICING:
        return None

    return ALLOWED_PRICING[key]


def normalize_tool(
    payload: dict,
    *,
    source_path: str,
    collected_at: datetime,
) -> tuple[ProductRecord, dict]:
    if not isinstance(payload, dict):
        raise ValueError("Tool record must be an object.")

    name = payload.get("name")

    if (
        not isinstance(name, str)
        or not name.strip()
    ):
        raise ValueError("Missing product name.")

    url = normalize_product_url(
        payload.get("url", "")
    )

    if payload.get("status") != "listed":
        raise ValueError(
            f"Tool status is not listed: {payload.get('status')}"
        )

    record = ProductRecord.model_validate(
        {
            "source": {
                "name": "Awesome AI Tools",
                "url": url,
            },
            "collectedAt": collected_at,
            "content": {
                "productName": name.strip(),
                "startupName": None,
                "pricingModel": normalize_pricing(
                    payload.get("pricing")
                ),
            },
        }
    )

    provenance = {
        "source_record_id": url,
        "source_path": source_path,
        "source_product_name": name.strip(),
        "description": payload.get("description"),
        "category": payload.get("category"),
        "subcategory": payload.get("subcategory"),
        "open_source": payload.get("openSource"),
        "source_pricing": payload.get("pricing"),
        "pricing_model": record.content.pricingModel,
        "startup_name": None,
        "company_resolution_status": "NOT_ATTEMPTED",
        "attribution": ATTRIBUTION,
    }

    return record, provenance


def write_jsonl(path: Path, rows) -> None:
    temporary = path.with_name(path.name + ".tmp")

    with temporary.open("w", encoding="utf-8") as handle:
        for row in rows:
            if isinstance(row, ProductRecord):
                line = row.model_dump_json()
            else:
                line = json.dumps(row, ensure_ascii=False)

            handle.write(line + "\n")

    temporary.replace(path)


def stage_snapshot(snapshot_dir: Path) -> dict:
    metadata = json.loads(
        (snapshot_dir / "metadata.json").read_text(
            encoding="utf-8"
        )
    )

    tool_paths = metadata["tool_paths"]

    if not tool_paths:
        raise ValueError("No tool catalog paths found.")

    collected_at = datetime.fromisoformat(
        metadata["collected_at"].replace("Z", "+00:00")
    )

    if collected_at.tzinfo is None:
        raise ValueError("Collection time must include a timezone.")

    records = []
    provenance = []
    duplicates = []
    quarantine = []

    seen_urls = set()
    seen_names = set()

    for index, source_path in enumerate(tool_paths):
        raw_path = snapshot_dir / "tools" / (
            source_path.removeprefix("catalog/tools/")
        )

        raw = raw_path.read_bytes()

        actual_hash = hashlib.sha256(raw).hexdigest()

        if actual_hash != metadata["tool_sha256"][source_path]:
            quarantine.append(
                {
                    "source_path": source_path,
                    "reason": "SOURCE_HASH_MISMATCH",
                }
            )
            continue

        try:
            payload = json.loads(raw)

            record, evidence = normalize_tool(
                payload,
                source_path=source_path,
                collected_at=collected_at,
            )

            evidence.update(
                {
                    "catalog_index": index,
                    "repository_tree_url": REPO_API,
                    "raw_path": str(raw_path.resolve()),
                    "raw_sha256": actual_hash,
                }
            )

            url = evidence["source_record_id"]
            name_key = (
                record.content.productName.casefold().strip()
            )

            if url in seen_urls:
                duplicates.append(
                    {
                        "source_path": source_path,
                        "reason": "DUPLICATE_PRODUCT_URL",
                        "product_url": url,
                    }
                )
                continue

            if name_key in seen_names:
                duplicates.append(
                    {
                        "source_path": source_path,
                        "reason": "DUPLICATE_NORMALIZED_PRODUCT_NAME",
                        "product_name": (
                            record.content.productName
                        ),
                    }
                )
                continue

            seen_urls.add(url)
            seen_names.add(name_key)

            records.append(record)
            provenance.append(evidence)

        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            quarantine.append(
                {
                    "source_path": source_path,
                    "reason": str(exc),
                    "raw_sha256": actual_hash,
                }
            )

    write_jsonl(snapshot_dir / "products.jsonl", records)
    write_jsonl(snapshot_dir / "provenance.jsonl", provenance)
    write_jsonl(snapshot_dir / "duplicates.jsonl", duplicates)
    write_jsonl(snapshot_dir / "quarantine.jsonl", quarantine)

    metadata.update(
        {
            "status": "staged",
            "parser_version": "awesome-ai-tools-catalog-v1",
            "input_records": len(tool_paths),
            "staged_unique_products": len(records),
            "duplicate_records": len(duplicates),
            "quarantine_entries": len(quarantine),
            "products_with_known_pricing": sum(
                record.content.pricingModel is not None
                for record in records
            ),
            "products_with_resolved_company": 0,
            "minimum_1000_candidates_reached": (
                len(records) >= 1000
            ),
            "company_ownership_verified": False,
            "final_quality_gate_passed": False,
            "attribution": ATTRIBUTION,
        }
    )

    (snapshot_dir / "metadata.json").write_text(
        json.dumps(metadata, indent=2),
        encoding="utf-8",
    )

    return {
        **metadata,
        "output_dir": str(snapshot_dir),
    }


async def fetch_json(
    session: aiohttp.ClientSession,
    url: str,
    *,
    max_bytes: int,
) -> bytes:
    async with session.get(url) as response:
        response.raise_for_status()

        raw = await response.content.read(max_bytes + 1)

        if len(raw) > max_bytes:
            raise ValueError(
                f"Response exceeds {max_bytes} bytes: {url}"
            )

        return raw


async def ingest(output_dir: Path) -> dict:
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=120),
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "ai-intelligence-ingestion/0.1",
        },
        connector=aiohttp.TCPConnector(limit=10),
    ) as session:
        async with session.get(REPO_API) as response:
            response.raise_for_status()

            tree = await response.json()

        if tree.get("truncated"):
            raise ValueError(
                "GitHub tree response was truncated."
            )

        tool_paths = sorted(
            item["path"]
            for item in tree["tree"]
            if (
                item["type"] == "blob"
                and item["path"].startswith("catalog/tools/")
                and item["path"].endswith(".json")
            )
        )

        if not tool_paths:
            raise ValueError("No tool catalog files found.")

        collected_at = datetime.now(timezone.utc)

        snapshot_dir = output_dir / collected_at.strftime(
            "%Y%m%dT%H%M%S%fZ"
        )

        snapshot_dir.mkdir(parents=True, exist_ok=False)

        tool_dir = snapshot_dir / "tools"
        tool_dir.mkdir()

        semaphore = asyncio.Semaphore(10)
        hashes = {}

        async def download_one(path: str) -> None:
            async with semaphore:
                raw = await fetch_json(
                    session,
                    f"{RAW_BASE}{path}",
                    max_bytes=100_000,
                )

                target = tool_dir / path.removeprefix(
                    "catalog/tools/"
                )

                target.parent.mkdir(
                    parents=True,
                    exist_ok=True,
                )
                target.write_bytes(raw)

                hashes[path] = hashlib.sha256(
                    raw
                ).hexdigest()

        await asyncio.gather(
            *(download_one(path) for path in tool_paths)
        )

        license_raw = await fetch_json(
            session,
            LICENSE_URL,
            max_bytes=100_000,
        )

        (snapshot_dir / "LICENSE").write_bytes(license_raw)

        metadata = {
            "source": "Awesome AI Tools",
            "repository_tree_url": REPO_API,
            "license_url": LICENSE_URL,
            "collected_at": collected_at.isoformat(),
            "tool_paths": tool_paths,
            "tool_sha256": hashes,
            "license_sha256": hashlib.sha256(
                license_raw
            ).hexdigest(),
            "status": "raw_saved",
        }

        (snapshot_dir / "metadata.json").write_text(
            json.dumps(metadata, indent=2),
            encoding="utf-8",
        )

        return stage_snapshot(snapshot_dir)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Acquire the Awesome AI Tools structured catalog."
        )
    )

    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/products/awesome-ai-tools"),
    )

    parser.add_argument(
        "--snapshot",
        type=Path,
    )

    args = parser.parse_args()

    try:
        result = (
            stage_snapshot(args.snapshot)
            if args.snapshot is not None
            else asyncio.run(ingest(args.output_dir))
        )
    except Exception as exc:
        parser.exit(
            status=1,
            message=f"Product ingestion failed: {exc}\n",
        )

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()