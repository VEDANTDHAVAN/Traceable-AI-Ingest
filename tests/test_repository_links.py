import pytest

from ai_ingest.repository_links import (
    candidates_from_response,
    canonical_repository_url,
    scan_responses,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (
            "https://github.com/Fixture/Repository",
            "https://github.com/fixture/repository",
        ),
        (
            "https://github.com/Fixture/Repository.git",
            "https://github.com/fixture/repository",
        ),
        (
            "https://github.com/Fixture/Repository/tree/main",
            "https://github.com/fixture/repository",
        ),
        (
            "https://github.com/Fixture/Repository).",
            "https://github.com/fixture/repository",
        ),
        (
            "https://github.com/topics/machine-learning",
            None,
        ),
        (
            "https://github.com/Fixture",
            None,
        ),
        (
            "https://github.com.evil.example/Fixture/Repository",
            None,
        ),
        (
            "https://user:password@github.com/Fixture/Repository",
            None,
        ),
    ],
)
def test_repository_normalization(value, expected):
    assert canonical_repository_url(value) == expected


FEED = b"""
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/0000.00000v1</id>
    <summary>
      Synthetic code link:
      https://github.com/Fixture/Repository
    </summary>
  </entry>
</feed>
"""


def test_candidate_preserves_evidence(tmp_path):
    raw_path = tmp_path / "response.xml"
    raw_path.write_bytes(FEED)

    candidates = candidates_from_response(raw_path)

    assert len(candidates) == 1

    candidate = candidates[0]

    assert candidate["paper_id"] == "arxiv:0000.00000"
    assert candidate["association_verified"] is False
    assert candidate["evidence"]["field"] == "summary"
    assert len(candidate["evidence"]["raw_sha256"]) == 64


def test_repeated_pages_do_not_inflate_candidates(tmp_path):
    for directory in ("first", "second"):
        page = tmp_path / directory
        page.mkdir()
        (page / "response.xml").write_bytes(FEED)

    candidates, scanned_files = scan_responses(tmp_path)

    assert scanned_files == 2
    assert len(candidates) == 1
    assert len(candidates[0]["evidence"]) == 2


def test_no_github_link_produces_no_candidates(tmp_path):
    raw_path = tmp_path / "response.xml"
    raw_path.write_bytes(
        FEED.replace(
            b"https://github.com/Fixture/Repository",
            b"https://example.org/project",
        )
    )

    assert candidates_from_response(raw_path) == []