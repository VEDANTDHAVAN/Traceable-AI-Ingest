import hashlib
import json
import sqlite3

from datetime import datetime, timezone

import pytest

from ai_ingest.import_enrichments import (
    ACCEPTED_RULE,
    import_enrichments,
)
from ai_ingest.models import ResearchPaperRecord
from ai_ingest.storage.sqlite import PaperStore


@pytest.fixture
def setup_import(tmp_path):
    database = tmp_path / "papers.sqlite3"

    paper_url = "https://arxiv.org/abs/0000.00000v1"

    record = ResearchPaperRecord.model_validate(
        {
            "source": {
                "name": "Synthetic fixture",
                "url": paper_url,
            },
            "collectedAt": "2026-01-01T00:00:00Z",
            "content": {
                "title": "Synthetic Paper",
                "authors": ["Fixture Author"],
                "paper_url": paper_url,
                "published_date": "2025-12-31T00:00:00Z",
            },
        }
    )

    raw_path = tmp_path / "synthetic-evidence.txt"
    raw_path.write_bytes(b"synthetic test evidence")

    evidence = {
        "raw_path": str(raw_path),
        "raw_sha256": hashlib.sha256(
            raw_path.read_bytes()
        ).hexdigest(),
    }

    store = PaperStore(database)

    try:
        crawl_id = store.create_crawl(
            {"query": "synthetic-test"}
        )

        store.commit_page(
            crawl_id=crawl_id,
            start=0,
            records=[record],
            evidence_path=raw_path,
        )
    finally:
        store.close()

    row = {
        "paper_id": "arxiv:0000.00000",
        "candidate_repository_url": (
            "https://github.com/fixture/repository"
        ),
        "association_verified": True,
        "association_status": "ACCEPTED_BY_RULE",
        "association_reason": ACCEPTED_RULE,
        "paper_link_evidence": [evidence],
        "readme_evidence": evidence,
        "github": {
            **evidence,
            "repository_url": (
                "https://github.com/fixture/repository"
            ),
            "github_stars": 12,
            "github_metrics_collected_at": (
                "2026-01-02T00:00:00Z"
            ),
        },
    }

    input_path = tmp_path / "associations.jsonl"
    input_path.write_text(
        json.dumps(row) + "\n",
        encoding="utf-8",
    )

    return database, input_path, raw_path, row


def stored_record(database):
    connection = sqlite3.connect(database)

    try:
        value = connection.execute(
            "SELECT record_json FROM papers"
        ).fetchone()[0]

        return ResearchPaperRecord.model_validate_json(value)
    finally:
        connection.close()


def test_dry_run_does_not_update_paper(setup_import):
    database, input_path, _, _ = setup_import

    result = import_enrichments(
        input_path=input_path,
        database_path=database,
    )

    assert result["papers_would_update"] == 1
    assert result["papers_updated"] == 0
    assert stored_record(database).content.github_stars is None


def test_apply_updates_only_enrichment_fields(setup_import):
    database, input_path, _, _ = setup_import

    before = stored_record(database)

    result = import_enrichments(
        input_path=input_path,
        database_path=database,
        apply=True,
    )

    after = stored_record(database)

    assert result["papers_updated"] == 1
    assert after.content.github_stars == 12
    assert after.content.title == before.content.title
    assert after.content.authors == before.content.authors
    assert after.content.published_date == before.content.published_date
    assert after.collectedAt == before.collectedAt
    assert after.source == before.source


def test_reapplying_same_file_is_noop(setup_import):
    database, input_path, _, _ = setup_import

    import_enrichments(
        input_path=input_path,
        database_path=database,
        apply=True,
    )

    result = import_enrichments(
        input_path=input_path,
        database_path=database,
        apply=True,
    )

    assert result["already_applied"] is True
    assert result["papers_updated"] == 0


def test_changed_evidence_rejected(setup_import):
    database, input_path, raw_path, _ = setup_import
    raw_path.write_bytes(b"changed")

    with pytest.raises(ValueError, match="hash mismatch"):
        import_enrichments(
            input_path=input_path,
            database_path=database,
            apply=True,
        )

    assert stored_record(database).content.github_stars is None


def test_unresolved_association_not_applied(setup_import):
    database, input_path, _, row = setup_import

    row["association_verified"] = False
    row["association_status"] = "UNRESOLVED"
    row["association_reason"] = "NO_MATCHING_README_REFERENCE"

    input_path.write_text(
        json.dumps(row) + "\n",
        encoding="utf-8",
    )

    result = import_enrichments(
        input_path=input_path,
        database_path=database,
        apply=True,
    )

    assert result["accepted_associations"] == 0
    assert result["unresolved_associations"] == 1
    assert stored_record(database).content.github_stars is None


def test_older_metrics_rejected(setup_import):
    database, input_path, _, row = setup_import

    import_enrichments(
        input_path=input_path,
        database_path=database,
        apply=True,
    )

    row["github"]["github_metrics_collected_at"] = (
        "2026-01-01T00:00:00Z"
    )

    input_path.write_text(
        json.dumps(row) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="older observation"):
        import_enrichments(
            input_path=input_path,
            database_path=database,
            apply=True,
        )

    assert stored_record(database).content.github_metrics_collected_at == (
        datetime(2026, 1, 2, tzinfo=timezone.utc)
    )