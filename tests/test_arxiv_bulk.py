import asyncio

import pytest

from ai_ingest.arxiv_bulk import bounded_query, crawl
from ai_ingest.models import ResearchPaperRecord


def test_bounded_query():
    result = bounded_query(
        "cat:cs.AI",
        "202501010000",
    )

    assert result == (
        "(cat:cs.AI) AND "
        "submittedDate:[199101010000 TO 202501010000]"
    )


def test_invalid_cutoff_rejected():
    with pytest.raises(ValueError):
        bounded_query("cat:cs.AI", "2025-01-01")


def test_empty_query_rejected():
    with pytest.raises(ValueError):
        bounded_query(" ", "202501010000")


def install_fake_fetch(monkeypatch, tmp_path, *, empty=False):
    offsets = []

    async def fake_ingest_page(**kwargs):
        start = kwargs["start"]
        offsets.append(start)

        run_dir = tmp_path / f"fixture-page-{len(offsets)}"
        run_dir.mkdir()

        (run_dir / "response.xml").write_text(
            "<synthetic-fixture/>",
            encoding="utf-8",
        )

        lines = ""

        if not empty:
            url = (
                "https://arxiv.org/abs/"
                f"0000.{start:05d}v1"
            )

            record = ResearchPaperRecord.model_validate(
                {
                    "source": {
                        "name": "Synthetic fixture",
                        "url": url,
                    },
                    "collectedAt": "2025-01-01T00:00:00Z",
                    "content": {
                        "title": f"Synthetic paper {start}",
                        "authors": ["Fixture Author"],
                        "paper_url": url,
                        "published_date": "2024-12-31T00:00:00Z",
                    },
                }
            )

            lines = record.model_dump_json() + "\n"

        (run_dir / "papers.jsonl").write_text(
            lines,
            encoding="utf-8",
        )

        return run_dir

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(
        "ai_ingest.arxiv_bulk.ingest_page",
        fake_ingest_page,
    )

    monkeypatch.setattr(
        "ai_ingest.arxiv_bulk.asyncio.sleep",
        no_sleep,
    )

    return offsets


def crawl_arguments(tmp_path):
    return {
        "query": "cat:cs.AI",
        "before": "202501010000",
        "page_size": 1,
        "max_pages": 1,
        "database": tmp_path / "papers.sqlite3",
        "output_dir": tmp_path / "raw",
        "user_agent": "synthetic-offline-test",
    }


def test_resume_uses_next_offset(monkeypatch, tmp_path):
    offsets = install_fake_fetch(monkeypatch, tmp_path)
    arguments = crawl_arguments(tmp_path)

    first = asyncio.run(crawl(**arguments))
    second = asyncio.run(crawl(**arguments))

    assert offsets == [0, 1]
    assert first["next_start"] == 1
    assert second["next_start"] == 2
    assert second["database_unique_papers"] == 2
    assert first["crawl_id"] == second["crawl_id"]


def test_empty_page_does_not_advance(monkeypatch, tmp_path):
    install_fake_fetch(monkeypatch, tmp_path, empty=True)

    result = asyncio.run(crawl(**crawl_arguments(tmp_path)))

    assert result["next_start"] == 0
    assert result["committed_pages_this_run"] == 0
    assert result["stop_reason"] == "empty_page"


def test_page_budget_is_enforced(monkeypatch, tmp_path):
    offsets = install_fake_fetch(monkeypatch, tmp_path)

    arguments = {
        **crawl_arguments(tmp_path),
        "max_pages": 2,
    }

    result = asyncio.run(crawl(**arguments))

    assert offsets == [0, 1]
    assert result["committed_pages_this_run"] == 2
    assert result["stop_reason"] == "page_budget_reached"