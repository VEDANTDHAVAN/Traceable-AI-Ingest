from pydantic import TypeAdapter

from ai_ingest.import_enrichments import ACCEPTED_RULE
from ai_ingest.models import ResearchPaperRecord
from ai_ingest.reconcile_enrichments import (
    reconciliation_report,
)


PAPER_ID = "arxiv:0000.00000"
REPOSITORY = "https://github.com/fixture/repository"

PAPER_ADAPTER = TypeAdapter(ResearchPaperRecord)


def paper(enriched=False):
    content = {
        "title": "Synthetic Paper",
        "authors": ["Fixture Author"],
        "paper_url": "https://arxiv.org/abs/0000.00000v1",
        "published_date": "2025-12-31T00:00:00Z",
    }

    if enriched:
        content.update(
            {
                "github_url": REPOSITORY,
                "github_stars": 12,
                "github_metrics_collected_at": (
                    "2026-01-01T00:00:00Z"
                ),
            }
        )

    payload = {
        "source": {
            "name": "Synthetic fixture",
            "url": "https://arxiv.org/abs/0000.00000v1",
        },
        "collectedAt": "2026-01-01T00:00:00Z",
        "content": content,
    }

    return PAPER_ADAPTER.validate_python(payload)


def decision(accepted=True):
    return {
        "paper_id": PAPER_ID,
        "candidate_repository_url": REPOSITORY,
        "association_verified": accepted,
        "association_status": (
            "ACCEPTED_BY_RULE" if accepted else "UNRESOLVED"
        ),
        "association_reason": (
            ACCEPTED_RULE
            if accepted
            else "NO_MATCHING_README_REFERENCE"
        ),
        "github": {
            "repository_url": REPOSITORY,
            "github_stars": 15,
            "github_metrics_collected_at": (
                "2026-01-02T00:00:00Z"
            ),
        },
    }


def test_new_enrichment_is_safe():
    result = reconciliation_report(
        {PAPER_ID: paper()},
        [decision()],
    )

    assert result["safe_to_apply"] is True
    assert result["new_enrichments"] == 1


def test_existing_enrichment_reconfirmed():
    result = reconciliation_report(
        {PAPER_ID: paper(enriched=True)},
        [decision()],
    )

    assert result["safe_to_apply"] is True
    assert result["existing_enrichments_reconfirmed"] == 1


def test_unresolved_existing_enrichment_blocks_import():
    result = reconciliation_report(
        {PAPER_ID: paper(enriched=True)},
        [decision(accepted=False)],
    )

    assert result["safe_to_apply"] is False
    assert result["blockers"][0]["reason"] == (
        "EXISTING_ENRICHMENT_NOT_RECONFIRMED"
    )


def test_unresolved_unenriched_paper_is_not_a_conflict():
    result = reconciliation_report(
        {PAPER_ID: paper()},
        [decision(accepted=False)],
    )

    assert result["safe_to_apply"] is True
    assert result[
        "unresolved_papers_without_existing_enrichment"
    ] == 1


def test_unknown_paper_blocks_import():
    result = reconciliation_report(
        {},
        [decision()],
    )

    assert result["safe_to_apply"] is False
    assert result["blockers"][0]["reason"] == (
        "PAPER_NOT_IN_DATABASE"
    )


def test_out_of_scope_enrichment_is_not_revoked():
    result = reconciliation_report(
        {PAPER_ID: paper(enriched=True)},
        [],
    )

    assert result["safe_to_apply"] is True
    assert result[
        "existing_enriched_papers_outside_scope"
    ] == 1