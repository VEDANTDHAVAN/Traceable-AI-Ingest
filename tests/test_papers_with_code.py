import asyncio
import json

from datetime import datetime, timezone
from pathlib import Path

import pytest

from ai_ingest import papers_with_code as pwc
from ai_ingest.papers_with_code import (
    AcquisitionError,
    ArxivPacer,
    parse_arxiv_feed,
    repo_api_url,
    retry_delay,
    strip_version,
    validate_github_metadata,
    validate_link,
)


FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/1706.03762v7</id>
    <published>2023-08-02T00:41:18Z</published>
    <title>Attention Is All You Need</title>
    <author><name>Ashish Vaswani</name></author>
    <author><name>Noam Shazeer</name></author>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/1805.10616v1</id>
    <published>2018-05-27T00:00:00Z</published>
    <title>Dynamic Network Model</title>
    <author><name>Elahe Ghalebi</name></author>
  </entry>
</feed>
"""


def test_strip_version_removes_only_trailing_version():
    assert strip_version("1706.03762v7") == "1706.03762"
    assert strip_version("1706.03762") == "1706.03762"
    assert strip_version("hep-th/9901001v2") == "hep-th/9901001"


def test_validate_link_marks_official_vs_community():
    official = validate_link(
        {
            "repo_url": "https://github.com/paperswithcode/paperswithcode",
            "paper_arxiv_id": "1601.02063",
            "is_official": True,
            "paper_url": "https://paperswithcode.com/paper/x",
        }
    )
    assert official["association_type"] == "OFFICIAL"
    assert official["repository_url"] == (
        "https://github.com/paperswithcode/paperswithcode"
    )
    assert official["paper_url"] == "https://arxiv.org/abs/1601.02063"

    community = validate_link(
        {
            "repo_url": "https://github.com/someone/impl",
            "paper_arxiv_id": "1601.02063v2",
            "is_official": False,
        }
    )
    assert community["association_type"] == "COMMUNITY"
    assert community["arxiv_id"] == "1601.02063"


@pytest.mark.parametrize(
    "row",
    [
        {"repo_url": "", "paper_arxiv_id": "x"},
        {"repo_url": "https://gitlab.com/a/b", "paper_arxiv_id": "x"},
        {"repo_url": "https://github.com/a/b", "paper_arxiv_id": ""},
        {"repo_url": "https://github.com/a/b", "paper_arxiv_id": "bad id!"},
    ],
)
def test_validate_link_rejects_invalid(row):
    with pytest.raises(ValueError):
        validate_link(row)


def test_parse_arxiv_feed_keys_by_bare_id():
    papers = parse_arxiv_feed(FEED)

    assert set(papers) == {"1706.03762", "1805.10616"}
    attention = papers["1706.03762"]
    assert attention["title"] == "Attention Is All You Need"
    assert attention["authors"] == ["Ashish Vaswani", "Noam Shazeer"]
    assert attention["paper_url"] == "https://arxiv.org/abs/1706.03762v7"


def test_parse_arxiv_feed_rejects_error_entry():
    error = b"""<feed xmlns="http://www.w3.org/2005/Atom">
      <entry><id>http://arxiv.org/api/errors#bad</id>
      <title>Error</title></entry>
    </feed>"""
    with pytest.raises(Exception):
        parse_arxiv_feed(error)


def test_repo_api_url_maps_canonical_url():
    assert repo_api_url("https://github.com/owner/repo") == (
        "https://api.github.com/repos/owner/repo"
    )
    with pytest.raises(ValueError):
        repo_api_url("https://github.com/owner")


def test_validate_github_metadata_rejects_mismatch_and_bad_stars():
    good = {
        "full_name": "owner/repo",
        "html_url": "https://github.com/owner/repo",
        "stargazers_count": 0,
    }
    assert validate_github_metadata(
        good, "https://github.com/owner/repo"
    )["github_stars"] == 0

    with pytest.raises(ValueError):
        validate_github_metadata(
            {**good, "html_url": "https://github.com/other/repo"},
            "https://github.com/owner/repo",
        )
    with pytest.raises(ValueError):
        validate_github_metadata(
            {**good, "stargazers_count": -1},
            "https://github.com/owner/repo",
        )
    with pytest.raises(ValueError):
        validate_github_metadata(
            {**good, "stargazers_count": True},
            "https://github.com/owner/repo",
        )


def test_retry_delay_prefers_retry_after_seconds():
    class FakeResponse:
        def __init__(self, headers):
            self.headers = headers

    assert retry_delay(FakeResponse({"Retry-After": "4"}), 0) == 4.0
    # Falls back to exponential backoff with jitter in [0, 1).
    delay = retry_delay(FakeResponse({}), 2)
    assert 4.0 <= delay < 5.0


def test_pacer_enforces_minimum_interval():
    async def run():
        pacer = ArxivPacer(interval=0.2)
        import time

        start = time.monotonic()
        await pacer.wait()
        await pacer.wait()
        return time.monotonic() - start

    assert asyncio.run(run()) >= 0.2


def test_run_acquisition_end_to_end(tmp_path, monkeypatch):
    class FakeResponse:
        def __init__(self, status, body):
            self.status = status
            self._body = body

        async def json(self):
            return json.loads(self._body)

        async def text(self):
            return self._body.decode("utf-8")

        async def read(self):
            return self._body

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class FakeSession:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def get(self, url, params=None, headers=None):
            if url == pwc.HF_ROWS:
                offset = int(params["offset"])
                if offset == 0:
                    rows = [
                        {
                            "row": {
                                "repo_url": "https://github.com/owner/repo",
                                "paper_arxiv_id": "1706.03762",
                                "is_official": True,
                                "paper_url": "https://paperswithcode.com/p",
                            }
                        },
                        {
                            "row": {
                                "repo_url": "https://gitlab.com/x/y",
                                "paper_arxiv_id": "1706.03762",
                                "is_official": False,
                            }
                        },
                    ]
                else:
                    rows = []
                return FakeResponse(
                    200, json.dumps({"rows": rows}).encode("utf-8")
                )

            if url == pwc.ARXIV_ENDPOINT:
                return FakeResponse(200, FEED)

            if url.startswith(pwc.GITHUB_API):
                return FakeResponse(
                    200,
                    json.dumps(
                        {
                            "full_name": "owner/repo",
                            "html_url": "https://github.com/owner/repo",
                            "stargazers_count": 42,
                        }
                    ).encode("utf-8"),
                )

            return FakeResponse(404, b"not found")

    monkeypatch.setattr(pwc.aiohttp, "ClientSession", FakeSession)
    monkeypatch.setattr(pwc, "ARXIV_MIN_INTERVAL", 0.0)

    result = asyncio.run(
        pwc.run_acquisition(
            output_dir=tmp_path,
            pages=2,
            page_size=100,
            concurrency=2,
            github_concurrency=2,
        )
    )

    assert result["unique_papers"] == 1
    assert result["accepted_associations"] == 1
    assert result["official_associations"] == 1
    assert result["github_metrics_present_for_accepted"] is True
    assert result["quarantine_rows"] == 1

    run_dir = Path(result["output_dir"])
    paper = json.loads(
        (run_dir / "papers.jsonl").read_text(encoding="utf-8").splitlines()[0]
    )
    assert paper["content"]["github_stars"] == 42
    assert paper["content"]["github_url"] == "https://github.com/owner/repo"
    assert paper["recordType"] == "RESEARCH_PAPER"

    quarantine = json.loads(
        (run_dir / "quarantine.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[0]
    )
    assert quarantine["reason"] == "Not a canonical GitHub repository URL."


def test_run_acquisition_resumes_from_page_cache(tmp_path, monkeypatch):
    calls = {"links": 0, "arxiv": 0, "github": 0}

    class FakeResponse:
        def __init__(self, status, body):
            self.status = status
            self._body = body

        async def json(self):
            return json.loads(self._body)

        async def text(self):
            return self._body.decode("utf-8")

        async def read(self):
            return self._body

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class FakeSession:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def get(self, url, params=None, headers=None):
            if url == pwc.HF_ROWS:
                calls["links"] += 1
                offset = int(params["offset"])
                rows = (
                    [
                        {
                            "row": {
                                "repo_url": "https://github.com/owner/repo",
                                "paper_arxiv_id": "1706.03762",
                                "is_official": True,
                            }
                        }
                    ]
                    if offset == 0
                    else []
                )
                return FakeResponse(
                    200, json.dumps({"rows": rows}).encode("utf-8")
                )
            if url == pwc.ARXIV_ENDPOINT:
                calls["arxiv"] += 1
                return FakeResponse(200, FEED)
            if url.startswith(pwc.GITHUB_API):
                calls["github"] += 1
                return FakeResponse(
                    200,
                    json.dumps(
                        {
                            "full_name": "owner/repo",
                            "html_url": "https://github.com/owner/repo",
                            "stargazers_count": 7,
                        }
                    ).encode("utf-8"),
                )
            return FakeResponse(404, b"not found")

    monkeypatch.setattr(pwc.aiohttp, "ClientSession", FakeSession)
    monkeypatch.setattr(pwc, "ARXIV_MIN_INTERVAL", 0.0)

    cache = tmp_path / "cache"
    asyncio.run(
        pwc.run_acquisition(
            output_dir=tmp_path / "run1",
            pages=1,
            page_size=100,
            concurrency=1,
            github_concurrency=1,
            cache_dir=cache,
        )
    )
    assert calls["links"] == 1

    asyncio.run(
        pwc.run_acquisition(
            output_dir=tmp_path / "run2",
            pages=1,
            page_size=100,
            concurrency=1,
            github_concurrency=1,
            cache_dir=cache,
        )
    )
    assert calls["links"] == 1
    assert (cache / "links_00000000.json").exists()
