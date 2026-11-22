import argparse
import hashlib
import json
import sqlite3

from datetime import date, datetime, timezone
from pathlib import Path

from ai_ingest.models import ProductRecord


SOURCE_KEY = "autoventure"


def read_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as handle:
        return [
            json.loads(line)
            for line in handle
            if line.strip()
        ]


def load_snapshot(
    snapshot_dir: Path,
) -> tuple[list[tuple], dict]:
    metadata = json.loads(
        (snapshot_dir / "metadata.json").read_text(
            encoding="utf-8"
        )
    )

    if metadata["status"] != "staged":
        raise ValueError("Snapshot is not fully staged.")

    if metadata["parser_version"] != "autoventure-envelope-v1":
        raise ValueError("Unsupported product parser version.")

    dataset_raw = (snapshot_dir / "dataset.json").read_bytes()
    license_raw = (snapshot_dir / "LICENSE.txt").read_bytes()

    if hashlib.sha256(dataset_raw).hexdigest() != (
        metadata["dataset_sha256"]
    ):
        raise ValueError("Dataset evidence hash mismatch.")

    if hashlib.sha256(license_raw).hexdigest() != (
        metadata["license_sha256"]
    ):
        raise ValueError("License evidence hash mismatch.")

    products_path = snapshot_dir / "products.jsonl"
    provenance_path = snapshot_dir / "provenance.jsonl"

    products = read_jsonl(products_path)
    provenance_rows = read_jsonl(provenance_path)

    if len(products) != len(provenance_rows):
        raise ValueError(
            "Product and provenance row counts disagree."
        )

    if len(products) != metadata["staged_products"]:
        raise ValueError(
            "Staged product count does not match metadata."
        )

    dataset = json.loads(dataset_raw)
    source_tools = dataset["tools"]

    source_updated = metadata["dataset_metadata"]["updated"]

    # Validate the source-version date without inventing a timestamp.
    date.fromisoformat(source_updated)

    collected_at = datetime.fromisoformat(
        metadata["collected_at"].replace("Z", "+00:00")
    )

    if collected_at.tzinfo is None:
        raise ValueError("Collection time must include a timezone.")

    prepared = []
    seen = set()

    for product_payload, provenance in zip(
        products,
        provenance_rows,
        strict=True,
    ):
        product = ProductRecord.model_validate(
            product_payload
        )

        if product.collectedAt != collected_at:
            raise ValueError("Collection timestamp mismatch.")

        if product.content.startupName is not None:
            raise ValueError(
                "Source import must not assign company ownership."
            )

        source_id = provenance["source_record_id"]
        record_index = provenance["record_index"]

        if (
            not isinstance(source_id, str)
            or not source_id.strip()
        ):
            raise ValueError("Invalid source record ID.")

        if source_id in seen:
            raise ValueError("Duplicate product source ID.")

        seen.add(source_id)

        if (
            type(record_index) is not int
            or not 0 <= record_index < len(source_tools)
        ):
            raise ValueError("Invalid raw record index.")

        raw_tool = source_tools[record_index]

        if raw_tool["slug"] != source_id:
            raise ValueError("Source ID does not match raw evidence.")

        if raw_tool["name"].strip() != (
            product.content.productName
        ):
            raise ValueError(
                "Product name does not match raw evidence."
            )

        if provenance["raw_sha256"] != (
            metadata["dataset_sha256"]
        ):
            raise ValueError("Provenance hash mismatch.")

        raw_path = Path(provenance["raw_path"])

        if raw_path.resolve() != (
            snapshot_dir / "dataset.json"
        ).resolve():
            raise ValueError("Unexpected provenance evidence path.")

        attribution = provenance.get("attribution")

        if not isinstance(attribution, str) or not attribution.strip():
            raise ValueError("Missing dataset attribution.")

        prepared.append(
            (
                source_id,
                product,
                provenance,
            )
        )

    # A receipt covers the raw source, parser, and derived outputs.
    # Replaying an unchanged snapshot produces the same receipt.
    receipt_payload = {
        "source": SOURCE_KEY,
        "dataset_sha256": metadata["dataset_sha256"],
        "license_sha256": metadata["license_sha256"],
        "parser_version": metadata["parser_version"],
        "products_sha256": hashlib.sha256(
            products_path.read_bytes()
        ).hexdigest(),
        "provenance_sha256": hashlib.sha256(
            provenance_path.read_bytes()
        ).hexdigest(),
    }

    import_id = hashlib.sha256(
        json.dumps(
            receipt_payload,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()

    return prepared, {
        "import_id": import_id,
        "source_updated": source_updated,
        "collected_at": collected_at.isoformat(),
        "dataset_sha256": metadata["dataset_sha256"],
        "snapshot_dir": str(snapshot_dir.resolve()),
    }


def import_products(
    *,
    snapshot_dir: Path,
    database_path: Path,
    apply: bool = False,
) -> dict:
    prepared, snapshot = load_snapshot(snapshot_dir)

    database_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    connection = sqlite3.connect(
        database_path,
        timeout=30,
    )

    try:
        # A dry run may initialize these empty tables.
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS product_source_records (
                source_key TEXT NOT NULL,
                source_record_id TEXT NOT NULL,
                record_json TEXT NOT NULL,
                provenance_json TEXT NOT NULL,
                source_updated TEXT NOT NULL,
                collected_at TEXT NOT NULL,
                dataset_sha256 TEXT NOT NULL,
                PRIMARY KEY (source_key, source_record_id)
            );

            CREATE TABLE IF NOT EXISTS product_imports (
                import_id TEXT PRIMARY KEY,
                source_key TEXT NOT NULL,
                snapshot_dir TEXT NOT NULL,
                imported_at TEXT NOT NULL,
                product_count INTEGER NOT NULL
            );
            """
        )

        connection.execute("BEGIN IMMEDIATE")

        exists = connection.execute(
            """
            SELECT 1
            FROM product_imports
            WHERE import_id = ?
            """,
            (snapshot["import_id"],),
        ).fetchone()

        if exists is not None:
            connection.rollback()

            return {
                "mode": "apply" if apply else "dry_run",
                "already_applied": True,
                "products_written": 0,
                "import_id": snapshot["import_id"],
            }

        new_records = 0
        existing_records = 0

        for source_id, product, provenance in prepared:
            previous = connection.execute(
                """
                SELECT source_updated, collected_at
                FROM product_source_records
                WHERE source_key = ?
                  AND source_record_id = ?
                """,
                (SOURCE_KEY, source_id),
            ).fetchone()

            if previous is None:
                new_records += 1
            else:
                previous_updated, previous_collected = previous

                if (
                    date.fromisoformat(snapshot["source_updated"])
                    < date.fromisoformat(previous_updated)
                ):
                    raise ValueError(
                        "Refusing to replace a newer source version."
                    )

                if (
                    datetime.fromisoformat(snapshot["collected_at"])
                    < datetime.fromisoformat(previous_collected)
                ):
                    raise ValueError(
                        "Refusing an older collection observation."
                    )

                existing_records += 1

            connection.execute(
                """
                INSERT INTO product_source_records (
                    source_key,
                    source_record_id,
                    record_json,
                    provenance_json,
                    source_updated,
                    collected_at,
                    dataset_sha256
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_key, source_record_id)
                DO UPDATE SET
                    record_json = excluded.record_json,
                    provenance_json = excluded.provenance_json,
                    source_updated = excluded.source_updated,
                    collected_at = excluded.collected_at,
                    dataset_sha256 = excluded.dataset_sha256
                """,
                (
                    SOURCE_KEY,
                    source_id,
                    product.model_dump_json(),
                    json.dumps(
                        provenance,
                        ensure_ascii=False,
                    ),
                    snapshot["source_updated"],
                    snapshot["collected_at"],
                    snapshot["dataset_sha256"],
                ),
            )

        connection.execute(
            """
            INSERT INTO product_imports (
                import_id,
                source_key,
                snapshot_dir,
                imported_at,
                product_count
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                snapshot["import_id"],
                SOURCE_KEY,
                snapshot["snapshot_dir"],
                datetime.now(timezone.utc).isoformat(),
                len(prepared),
            ),
        )

        if apply:
            connection.commit()
        else:
            connection.rollback()

        return {
            "mode": "apply" if apply else "dry_run",
            "already_applied": False,
            "new_source_records": new_records,
            "existing_source_records": existing_records,
            "products_would_write": len(prepared),
            "products_written": len(prepared) if apply else 0,
            "import_id": snapshot["import_id"],
            "final_export_eligibility_checked": False,
        }

    except BaseException:
        connection.rollback()
        raise

    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Persist staged product source records."
    )

    parser.add_argument(
        "--snapshot",
        required=True,
        type=Path,
    )

    parser.add_argument(
        "--database",
        type=Path,
        default=Path("data/products/products.sqlite3"),
    )

    parser.add_argument(
        "--apply",
        action="store_true",
    )

    args = parser.parse_args()

    try:
        result = import_products(
            snapshot_dir=args.snapshot,
            database_path=args.database,
            apply=args.apply,
        )
    except Exception as exc:
        parser.exit(
            status=1,
            message=f"Product import failed: {exc}\n",
        )

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()