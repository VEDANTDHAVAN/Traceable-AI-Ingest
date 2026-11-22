import hashlib
import json

import pytest

from ai_ingest.sources.autoventure import (
    extract_tools,
    stage_snapshot,
)


def test_observed_envelope_supported():
    tools, metadata = extract_tools(
        {
            "name": "Synthetic Dataset",
            "updated": "2026-09-22",
            "count": 1,
            "tools": [{"name": "Synthetic Product"}],
        }
    )

    assert len(tools) == 1
    assert metadata["updated"] == "2026-09-22"
    assert "tools" not in metadata


def test_root_array_rejected():
    with pytest.raises(ValueError, match="envelope object"):
        extract_tools([])


def test_missing_tools_rejected():
    with pytest.raises(ValueError, match="tools"):
        extract_tools({"count": 0})


def test_count_mismatch_rejected():
    with pytest.raises(ValueError, match="does not match"):
        extract_tools(
            {
                "count": 2,
                "tools": [],
            }
        )


def test_non_array_tools_rejected():
    with pytest.raises(ValueError, match="array"):
        extract_tools(
            {
                "count": 1,
                "tools": {"name": "Synthetic Product"},
            }
        )


def test_saved_snapshot_replayed_without_new_collection_time(
    tmp_path,
):
    payload = {
        "name": "Synthetic Dataset",
        "source": "https://autoventure.live",
        "license": "Synthetic attribution notice",
        "updated": "2026-09-22",
        "count": 1,
        "tools": [
            {
                "name": "Synthetic Product",
                "slug": "synthetic-product",
                "pricing": "Free Trial",
                "website": "https://example.org",
                "review_url": (
                    "https://autoventure.live/"
                    "reviews/synthetic-product.html"
                ),
            }
        ],
    }

    dataset_raw = json.dumps(payload).encode("utf-8")
    license_raw = b"Synthetic fixture: CC BY 4.0"

    (tmp_path / "dataset.json").write_bytes(dataset_raw)
    (tmp_path / "LICENSE.txt").write_bytes(license_raw)

    metadata = {
        "dataset_url": "https://example.org/dataset.json",
        "collected_at": "2026-01-01T12:00:00Z",
        "dataset_sha256": hashlib.sha256(
            dataset_raw
        ).hexdigest(),
        "license_sha256": hashlib.sha256(
            license_raw
        ).hexdigest(),
        "status": "raw_saved",
    }

    (tmp_path / "metadata.json").write_text(
        json.dumps(metadata),
        encoding="utf-8",
    )

    result = stage_snapshot(tmp_path)

    record = json.loads(
        (tmp_path / "products.jsonl").read_text(
            encoding="utf-8"
        ).strip()
    )

    assert result["staged_products"] == 1
    assert result["quarantined_records"] == 0
    assert result["dataset_metadata"]["updated"] == "2026-09-22"
    assert record["collectedAt"].startswith("2026-01-01T12:00:00")
    assert record["content"]["pricingModel"] is None
    assert record["content"]["startupName"] is None

    # Replaying again replaces derived outputs, not appends to them.
    stage_snapshot(tmp_path)

    lines = (
        tmp_path / "products.jsonl"
    ).read_text(encoding="utf-8").splitlines()

    assert len(lines) == 1