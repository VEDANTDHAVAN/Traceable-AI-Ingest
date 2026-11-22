import base64
import hashlib
import json

from datetime import datetime, timedelta, timezone

import pytest

from ai_ingest.readme_cache import (
    load_readme_cache,
    read_cached_readme,
)


NOW = datetime(
    2026, 1, 2,
    tzinfo=timezone.utc,
)

REPOSITORY = "https://github.com/fixture/repository"


@pytest.fixture
def evidence(tmp_path):
    text = "Paper: https://arxiv.org/abs/0000.00000"

    payload = {
        "encoding": "base64",
        "content": base64.b64encode(
            text.encode("utf-8")
        ).decode("ascii"),
    }

    raw = json.dumps(payload).encode("utf-8")
    raw_path = tmp_path / "readme.json"
    raw_path.write_bytes(raw)

    return {
        "endpoint": (
            "https://api.github.com/repos/"
            "fixture/repository/readme"
        ),
        "http_status": 200,
        "observed_at": "2026-01-01T23:00:00Z",
        "raw_path": str(raw_path),
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
    }


def read(evidence):
    return read_cached_readme(
        evidence,
        repository_url=REPOSITORY,
        now=NOW,
        max_age=timedelta(hours=24),
    )


def test_recent_readme_reused(evidence):
    result = read(evidence)

    assert result is not None
    assert "0000.00000" in result["text"]
    assert result["evidence"]["observed_at"] == (
        "2026-01-01T23:00:00Z"
    )


def test_stale_readme_not_reused(evidence):
    evidence["observed_at"] = "2025-12-01T00:00:00Z"

    assert read(evidence) is None


def test_changed_raw_response_not_reused(evidence):
    from pathlib import Path

    Path(evidence["raw_path"]).write_bytes(b"changed")

    assert read(evidence) is None


def test_wrong_repository_endpoint_not_reused(evidence):
    evidence["endpoint"] = (
        "https://api.github.com/repos/other/repository/readme"
    )

    assert read(evidence) is None


def test_future_observation_not_reused(evidence):
    evidence["observed_at"] = "2026-01-03T00:00:00Z"

    assert read(evidence) is None


def test_unresolved_decision_can_supply_readme_cache(
    evidence,
    tmp_path,
):
    history = tmp_path / "history"
    history.mkdir()

    row = {
        "candidate_repository_url": REPOSITORY,
        "association_verified": False,
        "association_reason": "NO_MATCHING_README_REFERENCE",
        "readme_evidence": evidence,
    }

    (history / "associations.jsonl").write_text(
        json.dumps(row) + "\n",
        encoding="utf-8",
    )

    cache = load_readme_cache(
        history,
        now=NOW,
    )

    assert REPOSITORY in cache