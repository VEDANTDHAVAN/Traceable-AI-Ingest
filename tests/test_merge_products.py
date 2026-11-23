import json
from pathlib import Path

import pytest

from ai_ingest.merge_products import merge_snapshots, normalize_product_url
from ai_ingest.models import SourceRef, ProductRecord

def write_jsonl(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            if isinstance(row, ProductRecord):
                handle.write(row.model_dump_json() + "\n")
            else:
                handle.write(
                    json.dumps(row, ensure_ascii=False)
                    + "\n"
                )


def product_record(
    name: str,
    url: str,
):
    return ProductRecord.model_validate(
        {
            "source": {
                "name": "Synthetic Source",
                "url": url,
            },
            "collectedAt": "2026-01-01T00:00:00Z",
            "content": {
                "productName": name,
                "startupName": None,
                "pricingModel": None,
            },
        }
    )


def test_autoventure_provenance_without_source_product_name(
    tmp_path: Path,
):
    autoventure = tmp_path / "autoventure"
    awesome = tmp_path / "awesome"
    output = tmp_path / "merged"

    autoventure_record = product_record(
        "Synthetic Product",
        "https://example.com/synthetic-product",
    )

    awesome_record = product_record(
        "Another Product",
        "https://example.org/another-product",
    )

    write_jsonl(
        autoventure / "products.jsonl",
        [autoventure_record],
    )

    write_jsonl(
        autoventure / "provenance.jsonl",
        [
            {
                "source_record_id": (
                    "https://example.com/synthetic-product"
                ),
                "source_pricing": None,
                "raw_sha256": (
                    "0" * 64
                ),
            }
        ],
    )

    write_jsonl(
        awesome / "products.jsonl",
        [awesome_record],
    )

    write_jsonl(
        awesome / "provenance.jsonl",
        [
            {
                "source_record_id": (
                    "https://example.org/another-product"
                ),
                "source_product_name": "Another Product",
                "source_pricing": None,
                "raw_sha256": "1" * 64,
            }
        ],
    )

    result = merge_snapshots(
        {
            "AUTOVENTURE": autoventure,
            "AWESOME_AI_TOOLS": awesome,
        },
        output,
    )

    assert result["merged_unique_products"] == 2

    provenance = [
        json.loads(line)
        for line in (
            output / "provenance.jsonl"
        ).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert provenance[0]["sources"][0][
        "source_product_name"
    ] == "Synthetic Product"

    assert provenance[1]["sources"][0][
        "source_product_name"
    ] == "Another Product"

def test_http_url_object_is_accepted():
    source = SourceRef.model_validate(
        {
            "name": "Synthetic Source",
            "url": "https://example.com/product/",
        }
    )

    assert normalize_product_url(
        source.url
    ) == "https://example.com/product"


def test_plain_https_url_is_accepted():
    assert normalize_product_url(
        "https://example.com/product?utm_source=x"
    ) == "https://example.com/product"


def test_non_https_url_rejected():
    with pytest.raises(ValueError):
        normalize_product_url(
            "http://example.com/product"
        )


def test_cross_source_duplicate_retains_both_sources(tmp_path: Path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    output = tmp_path / "merged"

    shared_url = "https://example.com/shared-product"

    def record(name: str, pricing: str | None):
        return ProductRecord.model_validate(
            {
                "source": {"name": "Fixture", "url": shared_url},
                "collectedAt": "2026-01-01T00:00:00Z",
                "content": {"productName": name, "pricingModel": pricing},
            }
        )

    write_jsonl(first / "products.jsonl", [record("Shared Product", None)])
    write_jsonl(
        first / "provenance.jsonl",
        [
            {
                "source_record_id": "a-1",
                "source_product_name": "Shared Product",
                "raw_sha256": "a" * 64,
            }
        ],
    )
    write_jsonl(second / "products.jsonl", [record("Shared Product", "FREE")])
    write_jsonl(
        second / "provenance.jsonl",
        [
            {
                "source_record_id": "b-1",
                "source_product_name": "Shared Product",
                "raw_sha256": "b" * 64,
            }
        ],
    )

    result = merge_snapshots(
        {"FIRST": first, "SECOND": second},
        output,
    )

    assert result["merged_unique_products"] == 1
    assert result["cross_source_duplicates"] == 1
    assert result["merged_products_with_multiple_sources"] == 1
    assert result["products_with_known_pricing"] == 1

    provenance = json.loads(
        (output / "provenance.jsonl").read_text(encoding="utf-8")
    )
    assert [ref["source"] for ref in provenance["sources"]] == [
        "FIRST",
        "SECOND",
    ]
    assert provenance["pricing_resolution_status"] == "SOURCE_PROVIDED"