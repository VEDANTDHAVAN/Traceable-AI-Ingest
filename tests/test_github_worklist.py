import hashlib
import json

from datetime import datetime, timedelta, timezone

import pytest

from ai_ingest.github_worklist import (
    build_worklist,
    merge_rows,
    reusable_metrics,
)


REPOSITORY = "https://github.com/fixture/repository"

NOW = datetime(
    2026, 1, 2,
    tzinfo=timezone.utc,
)


def candidate(paper_id="arxiv:0000.00000"):
    return {
        "paper_id": paper_id,
        "repository_url": REPOSITORY,
        "evidence": [{"kind": "synthetic-current-evidence"}],
    }


def test_uncached_candidate_is_pending():
    cached, pending = build_worklist(
        [candidate()],
        {},
    )

    assert cached == []
    assert len(pending) == 1


def test_cached_metrics_use_current_paper_evidence():
    cached, pending = build_worklist(
        [candidate()],
        {REPOSITORY: {"github_stars": 10}},
    )

    assert pending == []
    assert cached[0]["paper_link_evidence"] == (
        candidate()["evidence"]
    )
    assert cached[0]["association_verified"] is False


def test_repository_cache_can_serve_another_paper():
    candidates = [
        candidate("arxiv:0000.00000"),
        candidate("arxiv:0000.00001"),
    ]

    cached, pending = build_worklist(
        candidates,
        {REPOSITORY: {"github_stars": 10}},
    )

    assert len(cached) == 2
    assert pending == []


def test_merge_preserves_cached_and_pending_associations():
    cached = [
        {
            "paper_id": "arxiv:0000.00000",
            "candidate_repository_url": REPOSITORY,
        }
    ]

    pending = [candidate("arxiv:0000.00001")]

    fetched = [
        {
            "paper_id": "arxiv:0000.00001",
            "candidate_repository_url": REPOSITORY,
            "github": {"github_stars": 10},
        }
    ]

    merged = merge_rows(cached, fetched, pending)

    assert len(merged) == 2


def test_incomplete_fetch_rejected():
    with pytest.raises(ValueError, match="exactly match"):
        merge_rows([], [], [candidate()])


def test_recent_evidence_can_be_reused(tmp_path):
    payload = {
        "full_name": "fixture/repository",
        "html_url": REPOSITORY,
        "stargazers_count": 10,
    }

    raw = json.dumps(payload).encode()
    path = tmp_path / "response.json"
    path.write_bytes(raw)

    github = {
        "repository_url": REPOSITORY,
        "github_stars": 10,
        "github_metrics_collected_at": (
            "2026-01-01T23:00:00Z"
        ),
        "raw_path": str(path),
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
    }

    assert reusable_metrics(
        github,
        now=NOW,
        max_age=timedelta(hours=24),
    )


def test_old_metrics_not_reused(tmp_path):
    github = {
        "github_metrics_collected_at": (
            "2025-12-01T00:00:00Z"
        )
    }

    assert not reusable_metrics(
        github,
        now=NOW,
        max_age=timedelta(hours=24),
    )