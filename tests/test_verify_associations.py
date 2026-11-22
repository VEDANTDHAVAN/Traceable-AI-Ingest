from ai_ingest.verify_associations import (
    assess_association,
    referenced_papers,
)


def observation():
    return {
        "paper_id": "arxiv:0000.00000",
        "paper_link_evidence": [
            {"kind": "synthetic-test-evidence"}
        ],
        "github": {
            "github_stars": 0,
            "fork": False,
        },
    }


def assess(row, readme, **overrides):
    arguments = {
        "paper_candidate_count": 1,
        "repository_candidate_count": 1,
        **overrides,
    }

    return assess_association(
        row,
        readme,
        **arguments,
    )


def test_versioned_arxiv_link_normalized():
    assert referenced_papers(
        "Paper: https://arxiv.org/abs/0000.00000v2"
    ) == {"arxiv:0000.00000"}


def test_pdf_reference_normalized():
    assert referenced_papers(
        "[PDF](https://arxiv.org/pdf/0000.00000.pdf)"
    ) == {"arxiv:0000.00000"}


def test_reciprocal_link_accepted():
    accepted, reason = assess(
        observation(),
        "Paper: https://arxiv.org/abs/0000.00000",
    )

    assert accepted is True
    assert reason == (
        "RECIPROCAL_ARXIV_METADATA_README_LINK_V1"
    )


def test_wrong_paper_rejected():
    accepted, reason = assess(
        observation(),
        "Paper: https://arxiv.org/abs/0000.00001",
    )

    assert accepted is False
    assert reason == "NO_MATCHING_README_REFERENCE"


def test_multiple_paper_references_unresolved():
    accepted, reason = assess(
        observation(),
        (
            "https://arxiv.org/abs/0000.00000 "
            "https://arxiv.org/abs/0000.00001"
        ),
    )

    assert accepted is False
    assert reason == "README_REFERENCES_MULTIPLE_PAPERS"


def test_fork_unresolved():
    row = observation()
    row["github"]["fork"] = True

    accepted, reason = assess(
        row,
        "https://arxiv.org/abs/0000.00000",
    )

    assert accepted is False
    assert reason == "FORK_OR_UNKNOWN_FORK_STATUS"


def test_multiple_repositories_unresolved():
    accepted, reason = assess(
        observation(),
        "https://arxiv.org/abs/0000.00000",
        paper_candidate_count=2,
    )

    assert accepted is False
    assert reason == "MULTIPLE_REPOSITORIES_FOR_PAPER"


def test_missing_paper_evidence_unresolved():
    row = observation()
    row["paper_link_evidence"] = []

    accepted, reason = assess(
        row,
        "https://arxiv.org/abs/0000.00000",
    )

    assert accepted is False
    assert reason == "PAPER_LINK_EVIDENCE_MISSING"


def test_fake_arxiv_host_not_accepted():
    assert referenced_papers(
        "https://arxiv.org.evil.example/abs/0000.00000"
    ) == set()