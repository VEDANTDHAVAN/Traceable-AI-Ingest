import pytest

from ai_ingest.entity_resolution import (
    CanonicalEntity,
    EntityResolver,
    normalize_entity_name,
)


@pytest.fixture
def resolver():
    return EntityResolver(
        [
            CanonicalEntity(
                entity_id="org:openai",
                canonical_name="OpenAI",
                aliases=(
                    "Open AI",
                    "OpenAI, Inc.",
                ),
            )
        ]
    )


@pytest.mark.parametrize(
    "raw_name",
    [
        "OpenAI",
        "Open AI",
        "OpenAI, Inc.",
        "  OPENAI  ",
    ],
)
def test_assessment_variants_resolve(resolver, raw_name):
    result = resolver.resolve(raw_name)

    assert result["status"] == "RESOLVED"
    assert result["canonical_entity_id"] == "org:openai"
    assert result["canonical_name"] == "OpenAI"


def test_unknown_name_not_forced_to_known_entity(resolver):
    result = resolver.resolve("OpenAI Research Fixtures")

    assert result["status"] == "UNRESOLVED"
    assert result["canonical_name"] is None


def test_missing_name_stays_missing(resolver):
    result = resolver.resolve(None)

    assert result["status"] == "MISSING"
    assert result["canonical_name"] is None


def test_legal_suffix_removed_only_at_end():
    assert normalize_entity_name(
        "Fixture Labs, LLC"
    ) == "fixturelabs"

    assert normalize_entity_name(
        "LLC Research"
    ) == "llcresearch"


def test_conflicting_seed_alias_rejected():
    with pytest.raises(ValueError, match="Conflicting"):
        EntityResolver(
            [
                CanonicalEntity(
                    entity_id="org:first",
                    canonical_name="Fixture AI",
                ),
                CanonicalEntity(
                    entity_id="org:second",
                    canonical_name="FixtureAI",
                ),
            ]
        )


def test_duplicate_entity_id_rejected():
    with pytest.raises(ValueError, match="Duplicate entity ID"):
        EntityResolver(
            [
                CanonicalEntity(
                    entity_id="org:fixture",
                    canonical_name="First Fixture",
                ),
                CanonicalEntity(
                    entity_id="org:fixture",
                    canonical_name="Second Fixture",
                ),
            ]
        )


def test_punctuation_only_name_rejected():
    with pytest.raises(ValueError, match="empty"):
        normalize_entity_name(" , . ")