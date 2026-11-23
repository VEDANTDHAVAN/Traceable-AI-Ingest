import json

from pathlib import Path

import pytest

from ai_ingest.registry import ProcessedRegistry
from ai_ingest.sources.news_jobs import (
    ai_match_terms,
    board_company,
    discount_duplicate_titles,
    feed_entries,
    parse_job_payload,
    role_family,
)


RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>AI Feed</title>
<item>
  <title>Model ships</title>
  <link>https://example.com/news/model</link>
  <pubDate>Mon, 23 Nov 2026 09:00:00 GMT</pubDate>
  <description><![CDATA[<p>A model was released.</p>]]></description>
</item>
<item>
  <title>Undated item</title>
  <link>https://example.com/news/undated</link>
</item>
</channel></rss>
"""


def test_feed_entries_parses_dates_and_keeps_undated():
    entries = feed_entries(RSS)
    assert entries[0]["title"] == "Model ships"
    assert entries[0]["published"].isoformat() == "2026-11-23T09:00:00+00:00"
    assert entries[1]["published"] is None


def test_feed_rejects_unparseable_body():
    with pytest.raises(ValueError):
        feed_entries(b"<not-a-feed")


def test_ashby_job_parsing():
    payload = json.dumps(
        {
            "jobs": [
                {
                    "title": "Research Engineer",
                    "jobUrl": "https://jobs.ashbyhq.com/openai/123",
                    "publishedAt": "2026-11-23T08:00:00Z",
                    "isRemote": False,
                    "department": "Research",
                    "descriptionPlain": "Work on machine learning systems.",
                }
            ]
        }
    ).encode()

    jobs = parse_job_payload("ashby", payload)
    assert len(jobs) == 1
    job = jobs[0]
    assert job["published_at"].isoformat() == "2026-11-23T08:00:00+00:00"
    assert job["is_remote"] is False
    assert job["company"] is None
    assert role_family(job["department"], job["title"]) == "Research"


def test_greenhouse_missing_remote_is_unknown():
    payload = json.dumps(
        {
            "jobs": [
                {
                    "title": "ML Engineer",
                    "absolute_url": "https://boards.greenhouse.io/anthropic/jobs/1",
                    "first_published": "2026-11-23T07:00:00Z",
                    "company_name": "Anthropic",
                }
            ]
        }
    ).encode()

    job = parse_job_payload("greenhouse", payload)[0]
    assert job["is_remote"] is None


def test_remoteok_skips_legal_notice_and_marks_remote():
    payload = json.dumps(
        [
            {"legal": "link back please"},
            {
                "id": "1",
                "position": "AI Engineer",
                "company": "Acme",
                "date": "2026-11-23T01:00:00Z",
                "url": "https://remoteok.com/remote-jobs/1",
                "description": "LLM work",
                "tags": ["ai"],
            },
        ]
    ).encode()

    jobs = parse_job_payload("remoteok", payload)
    assert len(jobs) == 1
    assert jobs[0]["is_remote"] is True


def test_jobic_payload_and_company_from_board():
    payload = json.dumps(
        {
            "jobs": [
                {
                    "jobTitle": "GenAI Engineer",
                    "companyName": "Fixture",
                    "url": "https://jobicy.com/jobs/1",
                    "pubDate": "2026-11-23T03:00:00+00:00",
                    "jobGeo": "Remote",
                    "jobIndustry": ["Engineering"],
                }
            ]
        }
    ).encode()

    job = parse_job_payload("jobicy", payload)[0]
    board = {"company": None}
    assert board_company("jobicy", {}, board, job) == "Fixture"
    assert role_family(job["department"], job["title"]) == "Engineering"


def test_ai_filter_requires_evidence():
    assert ai_match_terms("Senior Machine Learning Engineer")
    assert not ai_match_terms("Regional sales manager, retail")
    assert ai_match_terms("We build LLM tooling")


def test_duplicate_titles_collapse():
    jobs = [
        {"url": "https://example.com/a"},
        {"url": "https://example.com/a/"},
        {"url": "https://example.com/b"},
    ]
    assert len(discount_duplicate_titles(jobs)) == 2


def test_registry_claim_is_atomic(tmp_path: Path):
    registry = ProcessedRegistry(tmp_path / "registry.sqlite3")

    first = registry.claim(
        kind="URL",
        identity="https://example.com/x",
        record_type="NEWS",
        run_id="run-1",
        first_seen_at="2026-11-23T00:00:00+00:00",
    )
    second = registry.claim(
        kind="URL",
        identity="https://example.com/x",
        record_type="NEWS",
        run_id="run-2",
        first_seen_at="2026-11-23T00:00:00+00:00",
    )

    assert first is True
    assert second is False
    assert registry.seen(
        kind="URL", identity="https://example.com/x", record_type="NEWS"
    )
    registry.close()
