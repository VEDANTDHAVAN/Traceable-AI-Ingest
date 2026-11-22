import hashlib
import json
import sqlite3

import pytest

from ai_ingest.sources.autoventure import stage_snapshot
from ai_ingest.storage.products import import_products


@pytest.fixture
def snapshot(tmp_path):
    directory = tmp_path / "snapshot"
    directory.mkdir()

    tools = []

    for number in (1, 2):
        tools.append(
            {
                "name": f"Synthetic Product {number}",
                "slug": f"synthetic-product-{number}",
                "pricing": "Free Trial",
                # Shared domain must not merge different products.
                "website": "https://example.org",
                "review_url": (
                    "https://autoventure.live/reviews/"
                    f"synthetic-product-{number}.html"
                ),
            }
        )

    payload = {
        "name": "Synthetic Dataset",
        "source": "https://autoventure.live",
        "license": "Synthetic attribution notice",
        "updated": "2026-09-22",
        "count": len(tools),
        "tools": tools,
    }

    raw = json.dumps(payload).encode("utf-8")
    license_raw = b"Synthetic fixture: CC BY 4.0"

    (directory / "dataset.json").write_bytes(raw)
    (directory / "LICENSE.txt").write_bytes(license_raw)

    metadata = {
        "dataset_url": "https://example.org/dataset.json",
        "collected_at": "2026-11-22T00:00:00Z",
        "dataset_sha256": hashlib.sha256(raw).hexdigest(),
        "license_sha256": hashlib.sha256(
            license_raw
        ).hexdigest(),
        "status": "raw_saved",
    }

    (directory / "metadata.json").write_text(
        json.dumps(metadata),
        encoding="utf-8",
    )

    stage_snapshot(directory)

    return directory


def product_count(database):
    connection = sqlite3.connect(database)

    try:
        return connection.execute(
            "SELECT COUNT(*) FROM product_source_records"
        ).fetchone()[0]
    finally:
        connection.close()


def test_dry_run_does_not_insert(snapshot, tmp_path):
    database = tmp_path / "products.sqlite3"

    result = import_products(
        snapshot_dir=snapshot,
        database_path=database,
    )

    assert result["products_would_write"] == 2
    assert result["products_written"] == 0
    assert product_count(database) == 0


def test_apply_persists_products(snapshot, tmp_path):
    database = tmp_path / "products.sqlite3"

    result = import_products(
        snapshot_dir=snapshot,
        database_path=database,
        apply=True,
    )

    assert result["products_written"] == 2
    assert product_count(database) == 2


def test_repeated_import_is_noop(snapshot, tmp_path):
    database = tmp_path / "products.sqlite3"

    arguments = {
        "snapshot_dir": snapshot,
        "database_path": database,
        "apply": True,
    }

    import_products(**arguments)
    result = import_products(**arguments)

    assert result["already_applied"] is True
    assert product_count(database) == 2


def test_shared_vendor_domain_does_not_merge_products(
    snapshot,
    tmp_path,
):
    database = tmp_path / "products.sqlite3"

    import_products(
        snapshot_dir=snapshot,
        database_path=database,
        apply=True,
    )

    assert product_count(database) == 2


def test_changed_raw_evidence_rejected(snapshot, tmp_path):
    (snapshot / "dataset.json").write_text(
        "{}",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="hash mismatch"):
        import_products(
            snapshot_dir=snapshot,
            database_path=tmp_path / "products.sqlite3",
            apply=True,
        )


def test_product_provenance_is_retained(snapshot, tmp_path):
    database = tmp_path / "products.sqlite3"

    import_products(
        snapshot_dir=snapshot,
        database_path=database,
        apply=True,
    )

    connection = sqlite3.connect(database)

    try:
        value = connection.execute(
            """
            SELECT provenance_json
            FROM product_source_records
            LIMIT 1
            """
        ).fetchone()[0]
    finally:
        connection.close()

    provenance = json.loads(value)

    assert provenance["attribution"]
    assert provenance["raw_sha256"]
    assert provenance["company_resolution_status"] == "UNRESOLVED"