import argparse
import asyncio
import hashlib
import json
import random

from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import aiohttp

from ai_ingest.models import ProductRecord


BASE_URL = (
    "https://raw.githubusercontent.com/"
    "autoventure-projects/ai-tools-dataset/main"
)

DATA_URL = f"{BASE_URL}/ai-tools.json"
LICENSE_URL = f"{BASE_URL}/LICENSE"

ATTRIBUTION = (
    "AutoVenture (autoventure.live), "
    "https://autoventure.live — CC BY 4.0. "
    "Fields normalized into the assessment schema."
)


def http_url(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Expected a URL string.")

    value = value.strip()

    try:
        parsed = urlparse(value)

        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError("Expected a public HTTP(S) URL.")

        # Validate malformed port syntax without using the port.
        _ = parsed.port

    except ValueError as exc:
        raise ValueError(f"Invalid URL: {value}") from exc

    return value


def normalize_pricing(value: object) -> str | None:
    if not isinstance(value, str):
        return None

    mapping = {
        "free": "FREE",
        "freemium": "FREEMIUM",
    }

    # Free Trial deliberately remains unknown in our enum.
    return mapping.get(value.strip().casefold())


def normalize_tool(
    row: dict,
    *,
    collected_at: datetime,
) -> tuple[ProductRecord, dict]:
    if not isinstance(row, dict):
        raise ValueError("Tool entry must be an object.")

    slug = row.get("slug")

    if not isinstance(slug, str) or not slug.strip():
        raise ValueError("Missing tool slug.")

    name = row.get("name")

    if not isinstance(name, str) or not name.strip():
        raise ValueError("Missing product name.")

    website = http_url(row.get("website"))
    review_url = http_url(row.get("review_url"))

    if urlparse(review_url).hostname not in {
        "autoventure.live",
        "www.autoventure.live",
    }:
        raise ValueError("Unexpected review URL host.")

    record = ProductRecord.model_validate(
        {
            "source": {
                "name": "AutoVenture AI Tools Dataset",
                "url": review_url,
            },
            "collectedAt": collected_at,
            "content": {
                "productName": name,
                # Do not infer a company from a product name or domain.
                "startupName": None,
                "pricingModel": normalize_pricing(
                    row.get("pricing")
                ),
            },
        }
    )

    provenance = {
        "source_record_id": slug.strip(),
        "website": website,
        "review_url": review_url,
        "original_pricing": row.get("pricing"),
        "attribution": ATTRIBUTION,
        "company_resolution_status": "UNRESOLVED",
        "pricing_evidence_level": "SECONDARY_DATASET",
    }

    return record, provenance


async def download(
    session: aiohttp.ClientSession,
    url: str,
    *,
    max_bytes: int,
) -> bytes:
    for attempt in range(4):
        try:
            async with session.get(
                url,
                allow_redirects=False,
            ) as response:
                if response.status != 200:
                    if (
                        response.status in {500, 502, 503, 504}
                        and attempt < 3
                    ):
                        await response.read()
                    else:
                        raise RuntimeError(
                            f"HTTP {response.status} for {url}. "
                            "No redirects or access blocks "
                            "will be bypassed."
                        )
                else:
                    chunks = []
                    size = 0

                    async for chunk in response.content.iter_chunked(
                        65536
                    ):
                        size += len(chunk)

                        if size > max_bytes:
                            raise ValueError(
                                "Download exceeds the safety limit."
                            )

                        chunks.append(chunk)

                    return b"".join(chunks)

        except (aiohttp.ClientError, asyncio.TimeoutError):
            if attempt == 3:
                raise

        await asyncio.sleep(
            random.uniform(1.0, 2.0 ** (attempt + 1))
        )

    raise RuntimeError("Download retries exhausted.")

def extract_tools(payload: object) -> tuple[list, dict]:
    """Validate the observed dataset envelope."""
    if not isinstance(payload, dict):
        raise ValueError(
            "Expected a dataset envelope object."
        )

    tools = payload.get("tools")

    if not isinstance(tools, list):
        raise ValueError(
            "Expected envelope.tools to be an array."
        )

    declared_count = payload.get("count")

    if type(declared_count) is not int or declared_count < 0:
        raise ValueError(
            "Expected envelope.count to be a nonnegative integer."
        )

    if declared_count != len(tools):
        raise ValueError(
            f"Declared count {declared_count} does not match "
            f"tools length {len(tools)}."
        )

    source_metadata = {
        key: value
        for key, value in payload.items()
        if key != "tools"
    }

    return tools, source_metadata


def stage_snapshot(run_dir: Path) -> dict:
    """Replay saved evidence without making network requests."""
    dataset_path = run_dir / "dataset.json"
    license_path = run_dir / "LICENSE.txt"
    metadata_path = run_dir / "metadata.json"

    metadata = json.loads(
        metadata_path.read_text(encoding="utf-8")
    )

    dataset_raw = dataset_path.read_bytes()
    license_raw = license_path.read_bytes()

    dataset_hash = hashlib.sha256(dataset_raw).hexdigest()
    license_hash = hashlib.sha256(license_raw).hexdigest()

    if dataset_hash != metadata["dataset_sha256"]:
        raise ValueError("Dataset snapshot hash mismatch.")

    if license_hash != metadata["license_sha256"]:
        raise ValueError("License snapshot hash mismatch.")

    license_text = license_raw.decode("utf-8").casefold()

    if not (
        "attribution 4.0 international" in license_text
        or "creativecommons.org/licenses/by/4.0" in license_text
        or "cc by 4.0" in license_text
    ):
        raise ValueError(
            "Saved license lacks the expected CC BY 4.0 identifier."
        )

    collected_at = datetime.fromisoformat(
        metadata["collected_at"].replace("Z", "+00:00")
    )

    if collected_at.tzinfo is None:
        raise ValueError(
            "Saved collection timestamp must include a timezone."
        )

    payload = json.loads(dataset_raw)
    tools, source_metadata = extract_tools(payload)

    staged = []
    provenance_rows = []
    rejected = []
    seen = set()

    for index, row in enumerate(tools):
        try:
            record, provenance = normalize_tool(
                row,
                collected_at=collected_at,
            )

            source_id = provenance["source_record_id"]

            if source_id in seen:
                raise ValueError("Duplicate source slug.")

            seen.add(source_id)

            provenance.update(
                {
                    "record_index": index,
                    "dataset_url": metadata["dataset_url"],
                    "raw_path": str(dataset_path.resolve()),
                    "raw_sha256": dataset_hash,
                    "source_updated": source_metadata.get(
                        "updated"
                    ),
                    "source_license_notice": source_metadata.get(
                        "license"
                    ),
                }
            )

            staged.append(record)
            provenance_rows.append(provenance)

        except (ValueError, TypeError) as exc:
            rejected.append(
                {
                    "record_index": index,
                    "reason": str(exc),
                    "raw_record": row,
                }
            )

    def write_atomic(path: Path, text: str) -> None:
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(path)

    # Mark derived outputs as incomplete until all are written.
    metadata["status"] = "staging"

    write_atomic(
        metadata_path,
        json.dumps(metadata, indent=2),
    )

    write_atomic(
        run_dir / "products.jsonl",
        "".join(
            record.model_dump_json() + "\n"
            for record in staged
        ),
    )

    for filename, rows in (
        ("provenance.jsonl", provenance_rows),
        ("quarantine.jsonl", rejected),
    ):
        write_atomic(
            run_dir / filename,
            "".join(
                json.dumps(row, ensure_ascii=False) + "\n"
                for row in rows
            ),
        )

    metadata.update(
        {
            "status": "staged",
            "parser_version": "autoventure-envelope-v1",
            "dataset_metadata": source_metadata,
            "input_records": len(tools),
            "staged_products": len(staged),
            "quarantined_records": len(rejected),
            "products_with_known_pricing": sum(
                record.content.pricingModel is not None
                for record in staged
            ),
            "products_with_resolved_company": 0,
            "final_export_eligible_products": 0,
            "last_staged_at": datetime.now(
                timezone.utc
            ).isoformat(),
        }
    )

    write_atomic(
        metadata_path,
        json.dumps(metadata, indent=2),
    )

    return {
        **metadata,
        "output_dir": str(run_dir),
    }

async def ingest(output_dir: Path) -> dict:
    timeout = aiohttp.ClientTimeout(total=90)

    async with aiohttp.ClientSession(
        timeout=timeout,
        headers={
            "User-Agent": "ai-intelligence-ingestion/0.1"
        },
        connector=aiohttp.TCPConnector(limit=1),
    ) as session:
        license_raw = await download(
            session,
            LICENSE_URL,
            max_bytes=200_000,
        )

        dataset_raw = await download(
            session,
            DATA_URL,
            max_bytes=20_000_000,
        )

    collected_at = datetime.now(timezone.utc)

    run_dir = output_dir / collected_at.strftime(
        "%Y%m%dT%H%M%S%fZ"
    )

    run_dir.mkdir(parents=True, exist_ok=False)

    # Preserve evidence before parsing or normalization.
    (run_dir / "dataset.json").write_bytes(dataset_raw)
    (run_dir / "LICENSE.txt").write_bytes(license_raw)

    metadata = {
        "source": "AutoVenture",
        "dataset_url": DATA_URL,
        "license_url": LICENSE_URL,
        "collected_at": collected_at.isoformat(),
        "dataset_sha256": hashlib.sha256(
            dataset_raw
        ).hexdigest(),
        "license_sha256": hashlib.sha256(
            license_raw
        ).hexdigest(),
        "attribution": ATTRIBUTION,
        "status": "raw_saved",
    }

    metadata_path = run_dir / "metadata.json"

    metadata_path.write_text(
        json.dumps(metadata, indent=2),
        encoding="utf-8",
    )

    return stage_snapshot(run_dir)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Stage the published AutoVenture product dataset."
    )

    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/products/autoventure"),
    )

    parser.add_argument(
        "--snapshot",
        type=Path,
        help=(
            "Replay an existing snapshot directory "
            "without downloading."
        ),
    )

    args = parser.parse_args()

    try:
        if args.snapshot is not None:
            result = stage_snapshot(args.snapshot)
        else:
            result = asyncio.run(
                ingest(args.output_dir)
            )

    except Exception as exc:
        parser.exit(
            status=1,
            message=f"Product ingestion failed: {exc}\n",
        )

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()