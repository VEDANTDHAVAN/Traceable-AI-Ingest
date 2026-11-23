import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ai_ingest.sources.aitoolbuzz import normalize_tool, stage_snapshot


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def row(**overrides):
    base = {
        "No.": "1",
        "Name": "Synthetic Tool",
        "Link": "https://aitoolbuzz.com/synthetic-tool",
        "Logo": "https://aitoolbuzz.com/logo.png",
        "Website": "https://example.com/tool",
        "Status": "OK",
        "Task": "Writing",
        "Short Description": "A synthetic tool.",
    }
    base.update(overrides)
    return base


def test_normalize_tool_schema_and_provenance():
    record, evidence = normalize_tool(row(), collected_at=NOW)

    assert record.recordType == "PRODUCT"
    assert record.content.productName == "Synthetic Tool"
    assert str(record.source.url) == "https://example.com/tool"
    assert record.content.startupName is None
    assert record.content.pricingModel is None
    assert evidence["source_name"] == "AIToolBuzz AI Tools Database"
    assert evidence["source_record_id"] == "1"
    assert evidence["source_page"] == "https://aitoolbuzz.com/synthetic-tool"
    assert len(evidence["raw_sha256"]) == 64


def test_non_https_website_rejected():
    with pytest.raises(ValueError, match="https"):
        normalize_tool(row(Website="http://example.com/tool"), collected_at=NOW)


def test_missing_name_rejected():
    with pytest.raises(ValueError, match="name"):
        normalize_tool(row(Name=" "), collected_at=NOW)


def test_stage_snapshot_dedupes_and_quarantines(tmp_path: Path):
    csv_path = tmp_path / "ai_tools_database_25k_aitoolbuzz.csv"
    csv_path.write_text(
        "No.,Name,Link,Logo,Website,Status,Task,Short Description\n"
        "1,Tool A,https://aitoolbuzz.com/a,,https://a.example,OK,Task,Desc\n"
        "2,Tool A dup,https://aitoolbuzz.com/a2,,https://a.example/,OK,Task,Desc\n"
        "3,,https://aitoolbuzz.com/b,,https://b.example,OK,Task,Desc\n",
        encoding="utf-8",
    )

    metadata = stage_snapshot(tmp_path)

    assert metadata["staged_products"] == 1
    assert metadata["duplicate_rows"] == 1
    assert metadata["quarantine_rows"] == 1

    products = [
        json.loads(line)
        for line in (tmp_path / "products.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert products[0]["content"]["productName"] == "Tool A"

    quarantine = [
        json.loads(line)
        for line in (tmp_path / "quarantine.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert quarantine[0]["reason"] == "Missing product name."
