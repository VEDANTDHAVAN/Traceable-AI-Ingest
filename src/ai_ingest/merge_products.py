import argparse
import json

from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from ai_ingest.models import ProductRecord


def normalize_product_url(value) -> str:
    if value is None:
        raise ValueError("Product URL is missing.")

    # Pydantic HttpUrl serializes to a string.
    if not isinstance(value, str):
        value = str(value)

    parsed = urlsplit(value.strip())

    if parsed.scheme != "https" or not parsed.hostname:
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


def normalize_product_name(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Product name must be a string.")

    tokens = "".join(
        char if char.isalnum() else " "
        for char in value.casefold()
    ).split()

    if not tokens:
        raise ValueError(
            "Product name is empty after normalization."
        )

    return " ".join(tokens)

def read_jsonl(path: Path) -> list[dict]:
    rows = []

    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue

            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"{path}:{line_number}: invalid JSON"
                ) from exc

    return rows


def merge_snapshots(
    autoventure: Path,
    awesome: Path,
    output_dir: Path,
) -> dict:
    autoventure_records = [
        ProductRecord.model_validate(row)
        for row in read_jsonl(
            autoventure / "products.jsonl"
        )
    ]

    awesome_records = [
        ProductRecord.model_validate(row)
        for row in read_jsonl(
            awesome / "products.jsonl"
        )
    ]

    autoventure_provenance = read_jsonl(
        autoventure / "provenance.jsonl"
    )

    awesome_provenance = read_jsonl(
        awesome / "provenance.jsonl"
    )

    if len(autoventure_records) != len(
        autoventure_provenance
    ):
        raise ValueError(
            "AutoVenture record/provenance count mismatch."
        )

    if len(awesome_records) != len(
        awesome_provenance
    ):
        raise ValueError(
            "Awesome AI Tools record/provenance count mismatch."
        )

    collected_at = datetime.now(timezone.utc)

    merged = []
    provenance = []
    duplicates = []

    by_url = {}
    by_name = {}

    sources = [
        (
            "AUTOVENTURE",
            autoventure,
            autoventure_records,
            autoventure_provenance,
        ),
        (
            "AWESOME_AI_TOOLS",
            awesome,
            awesome_records,
            awesome_provenance,
        ),
    ]

    for source_name, snapshot, records, evidence in sources:
        for index, (record, row) in enumerate(
            zip(records, evidence)
        ):
            source_url = normalize_product_url(
                record.source.url
            )

            name_key = normalize_product_name(
                record.content.productName
            )

            source_product_name = row.get(
                "source_product_name"
            )

            if source_product_name is None:
                source_product_name = (
                    record.content.productName
                )

            source_ref = {
                "source": source_name,
                "snapshot": str(snapshot),
                "source_record_id": row["source_record_id"],
                "source_product_name": source_product_name,
                "source_pricing": row.get(
                    "source_pricing"
                ),
                "pricing_model": record.content.pricingModel,
                "raw_sha256": row.get("raw_sha256"),
            }

            existing = by_url.get(source_url)

            if existing is not None:
                duplicates.append(
                    {
                        "reason": "DUPLICATE_NORMALIZED_URL",
                        "kept_record_index": existing[
                            "record_index"
                        ],
                        "duplicate_source": source_name,
                        "duplicate_source_record_id": row[
                            "source_record_id"
                        ],
                        "normalized_url": source_url,
                    }
                )
                continue

            existing = by_name.get(name_key)

            if existing is not None:
                duplicates.append(
                    {
                        "reason": "DUPLICATE_NORMALIZED_PRODUCT_NAME",
                        "kept_record_index": existing[
                            "record_index"
                        ],
                        "duplicate_source": source_name,
                        "duplicate_source_record_id": row[
                            "source_record_id"
                        ],
                        "normalized_product_name": name_key,
                    }
                )
                continue

            record_index = len(merged)

            merged.append(record)
            provenance.append(
                {
                    "record_index": record_index,
                    "sources": [source_ref],
                    "company_resolution_status": (
                        "NOT_ATTEMPTED"
                    ),
                    "pricing_resolution_status": (
                        "SOURCE_PROVIDED"
                        if record.content.pricingModel
                        else "UNKNOWN"
                    ),
                }
            )

            by_url[source_url] = {
                "record_index": record_index
            }
            by_name[name_key] = {
                "record_index": record_index
            }

    output_dir.mkdir(parents=True, exist_ok=False)

    for filename, rows in (
        ("products.jsonl", merged),
        ("provenance.jsonl", provenance),
        ("duplicates.jsonl", duplicates),
    ):
        temporary = output_dir / (filename + ".tmp")

        with temporary.open(
            "w",
            encoding="utf-8",
        ) as handle:
            for row in rows:
                if isinstance(row, ProductRecord):
                    line = row.model_dump_json()
                else:
                    line = json.dumps(
                        row,
                        ensure_ascii=False,
                    )

                handle.write(line + "\n")

        temporary.replace(output_dir / filename)

    metadata = {
        "schema_version": "1.0",
        "merged_at": collected_at.isoformat(),
        "input_snapshots": {
            "AUTOVENTURE": str(autoventure),
            "AWESOME_AI_TOOLS": str(awesome),
        },
        "input_records": (
            len(autoventure_records)
            + len(awesome_records)
        ),
        "merged_unique_products": len(merged),
        "duplicate_records": len(duplicates),
        "products_with_known_pricing": sum(
            record.content.pricingModel is not None
            for record in merged
        ),
        "products_with_resolved_company": 0,
        "minimum_1000_candidates_reached": (
            len(merged) >= 1000
        ),
        "company_ownership_verified": False,
        "final_quality_gate_passed": False,
    }

    (output_dir / "metadata.json").write_text(
        json.dumps(metadata, indent=2),
        encoding="utf-8",
    )

    return {
        **metadata,
        "output_dir": str(output_dir),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Merge staged product snapshots with deduplication."
        )
    )

    parser.add_argument(
        "--autoventure",
        type=Path,
        required=True,
    )

    parser.add_argument(
        "--awesome",
        type=Path,
        required=True,
    )

    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(
            "data/products/merged"
        ),
    )

    args = parser.parse_args()

    try:
        result = merge_snapshots(
            args.autoventure,
            args.awesome,
            args.output_dir,
        )
    except Exception as exc:
        parser.exit(
            status=1,
            message=f"Product merge failed: {exc}\n",
        )

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()