import pytest

from ai_ingest.models import ResearchPaperRecord
from ai_ingest.storage.sqlite import (
    PaperStore,
    crawl_identity,
    paper_identity,
)


def make_paper(version: int = 1) -> ResearchPaperRecord:
    url = f"https://arxiv.org/abs/0000.00000v{version}"

    return ResearchPaperRecord.model_validate(
        {
            "source": {
                "name": "Synthetic fixture",
                "url": url,
            },
            "collectedAt": "2026-01-01T00:00:00Z",
            "content": {
                "title": "Synthetic Paper",
                "authors": ["Fixture Author"],
                "paper_url": url,
                "published_date": "2025-12-31T00:00:00Z",
            },
        }
    )


@pytest.fixture
def store(tmp_path):
    instance = PaperStore(tmp_path / "papers.sqlite3")

    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture
def evidence(tmp_path):
    path = tmp_path / "response.xml"
    path.write_text("<synthetic-fixture/>", encoding="utf-8")
    return path


def test_modern_identity_ignores_version():
    assert paper_identity(
        "https://arxiv.org/abs/0000.00000v2"
    ) == "arxiv:0000.00000"


def test_legacy_identity_ignores_version():
    assert paper_identity(
        "https://arxiv.org/abs/cs/0000000v3"
    ) == "arxiv:cs/0000000"


def test_non_arxiv_url_rejected():
    with pytest.raises(ValueError):
        paper_identity("https://example.org/abs/0000.00000")


def test_crawl_identity_ignores_dictionary_order():
    assert crawl_identity(
        {"query": "cat:cs.AI", "page_size": 10}
    ) == crawl_identity(
        {"page_size": 10, "query": "cat:cs.AI"}
    )


def test_page_commit_advances_checkpoint(store, evidence):
    crawl_id = store.create_crawl({"query": "fixture"})

    committed = store.commit_page(
        crawl_id=crawl_id,
        start=0,
        records=[make_paper()],
        evidence_path=evidence,
    )

    assert committed is True
    assert store.paper_count() == 1
    assert store.next_start(crawl_id) == 1


def test_replaying_page_is_idempotent(store, evidence):
    crawl_id = store.create_crawl({"query": "fixture"})

    arguments = {
        "crawl_id": crawl_id,
        "start": 0,
        "records": [make_paper()],
        "evidence_path": evidence,
    }

    assert store.commit_page(**arguments) is True
    assert store.commit_page(**arguments) is False
    assert store.paper_count() == 1
    assert store.next_start(crawl_id) == 1


def test_versions_do_not_inflate_count(store, evidence):
    crawl_id = store.create_crawl({"query": "fixture"})

    store.commit_page(
        crawl_id=crawl_id,
        start=0,
        records=[make_paper(version=1)],
        evidence_path=evidence,
    )

    store.commit_page(
        crawl_id=crawl_id,
        start=1,
        records=[make_paper(version=2)],
        evidence_path=evidence,
    )

    assert store.paper_count() == 1
    assert store.next_start(crawl_id) == 2


def test_out_of_order_page_does_not_change_state(store, evidence):
    crawl_id = store.create_crawl({"query": "fixture"})

    with pytest.raises(ValueError, match="Expected page offset"):
        store.commit_page(
            crawl_id=crawl_id,
            start=10,
            records=[make_paper()],
            evidence_path=evidence,
        )

    assert store.paper_count() == 0
    assert store.next_start(crawl_id) == 0


def test_missing_evidence_rejected(store, tmp_path):
    crawl_id = store.create_crawl({"query": "fixture"})

    with pytest.raises(ValueError, match="does not exist"):
        store.commit_page(
            crawl_id=crawl_id,
            start=0,
            records=[make_paper()],
            evidence_path=tmp_path / "missing.xml",
        )

    assert store.paper_count() == 0


def test_checkpoint_survives_reopen(tmp_path, evidence):
    database = tmp_path / "persistent.sqlite3"

    first = PaperStore(database)

    try:
        crawl_id = first.create_crawl({"query": "fixture"})

        first.commit_page(
            crawl_id=crawl_id,
            start=0,
            records=[make_paper()],
            evidence_path=evidence,
        )
    finally:
        first.close()

    second = PaperStore(database)

    try:
        assert second.next_start(crawl_id) == 1
        assert second.paper_count() == 1
    finally:
        second.close()