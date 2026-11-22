from datetime import datetime, timedelta, timezone

from ai_ingest.models import ResearchPaperRecord
from ai_ingest.quality_report import (
    assess_record,
    decision_matches,
)


NOW = datetime(
    2026, 1, 2,
    tzinfo=timezone.utc,
)

PAPER_ID = "arxiv:0000.00000"
REPOSITORY = "https://github.com/fixture/repository"


def paper(enriched=True):
    content = {
        "title": "Synthetic Paper",
        "authors": ["Fixture Author"],
        "paper_url": "https://arxiv.org/abs/0000.00000v1",
        "published_date": "2025-12-30T00:00:00Z",
    }

    if enriched:
        content.update(
            {
                "github_url": REPOSITORY,
                "github_stars": 0,
                "github_metrics_collected_at": (
                    "2026-01-01T23:00:00Z"
                ),
            }
        )

    return ResearchPaperRecord.model_validate(
        {
            "source": {
                "name": "Synthetic fixture",
                "url": "https://arxiv.org/abs/0000.00000v1",
            },
            "collectedAt": "2026-01-01T00:00:00Z",
            "content": content,
        }
    )


def decision():
    return {
        "paper_id": PAPER_ID,
        "association_verified": True,
        "association_status": "ACCEPTED_BY_RULE",
        "association_reason": (
            "RECIPROCAL_ARXIV_METADATA_README_LINK_V1"
        ),
        "github": {
            "repository_url": REPOSITORY,
            "github_stars": 0,
            "github_metrics_collected_at": (
                "2026-01-01T23:00:00Z"
            ),
        },
    }


def assess(record, decisions):
    return assess_record(
        PAPER_ID,
        record,
        decisions,
        now=NOW,
        max_metrics_age=timedelta(hours=24),
    )


def test_matching_decision_recognized():
    assert decision_matches(paper(), decision())


def test_zero_stars_is_complete_metrics():
    result = assess(paper(), [decision()])

    assert result["metrics_complete"] is True
    assert result["enriched_with_applied_decision"] is True


def test_metrics_without_decision_not_counted_as_enriched():
    result = assess(paper(), [])

    assert result["metrics_complete"] is True
    assert result["enriched_with_applied_decision"] is False


def test_unenriched_paper_not_counted():
    result = assess(paper(enriched=False), [])

    assert result["metrics_complete"] is False
    assert result["metrics_recent"] is False


def test_different_star_observation_does_not_match():
    value = decision()
    value["github"]["github_stars"] = 10

    assert decision_matches(paper(), value) is False


def test_old_metrics_reported_separately():
    record = paper()
    record.content.github_metrics_collected_at = datetime(
        2025, 12, 1,
        tzinfo=timezone.utc,
    )

    value = decision()
    value["github"]["github_metrics_collected_at"] = (
        "2025-12-01T00:00:00Z"
    )

    result = assess(record, [value])

    assert result["enriched_with_applied_decision"] is True
    assert result["metrics_recent"] is False