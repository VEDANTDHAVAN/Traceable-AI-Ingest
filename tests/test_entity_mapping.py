import json

from pathlib import Path

import pytest

from ai_ingest.entity_mapping import build_mapping_rows


SEEDS = [
    {
        "entity_id": "org:openai",
        "canonical_name": "OpenAI",
        "aliases": ["Open AI", "OpenAI, Inc."],
    }
]


def write_jsonl(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_unknown_startup_preserved_unresolved(tmp_path: Path):
    seeds = tmp_path / "seeds.json"
    seeds.write_text(json.dumps(SEEDS), encoding="utf-8")

    startups = tmp_path / "startups"
    write_jsonl(
        startups / "startups.jsonl",
        [
            {
                "recordType": "STARTUP",
                "source": {"name": "Fixture", "url": "https://example.com/a"},
                "collectedAt": "2026-01-01T00:00:00Z",
                "content": {"entityName": "Unknown Fixture Labs"},
            },
            {
                "recordType": "STARTUP",
                "source": {"name": "Fixture", "url": "https://example.com/b"},
                "collectedAt": "2026-01-01T00:00:00Z",
                "content": {"entityName": "OpenAI, Inc."},
            },
        ],
    )
    write_jsonl(
        startups / "provenance.jsonl",
        [
            {"source_record_id": "a", "raw_sha256": "a" * 64},
            {"source_record_id": "b", "raw_sha256": "b" * 64},
        ],
    )

    rows = build_mapping_rows(
        seeds=seeds,
        startups_dir=startups,
        phase2_dir=None,
        phase3_dir=None,
    )

    assert len(rows) == 2

    unresolved = next(row for row in rows if row["raw_name"] == "Unknown Fixture Labs")
    assert unresolved["status"] == "UNRESOLVED"
    assert unresolved["canonical_entity_id"] is None
    assert unresolved["ownership_verified"] is False

    resolved = next(row for row in rows if row["raw_name"] == "OpenAI, Inc.")
    assert resolved["status"] == "RESOLVED"
    assert resolved["canonical_entity_id"] == "org:openai"
    assert resolved["method"] == "NORMALIZED_EXACT_ALIAS_V1"
    assert resolved["ownership_verification_status"] == "NAME_MATCH_ONLY"
    assert resolved["ownership_verified"] is False


def test_conflicting_seed_alias_rejected(tmp_path: Path):
    seeds = tmp_path / "seeds.json"
    seeds.write_text(
        json.dumps(
            [
                {"entity_id": "org:a", "canonical_name": "Fixture AI"},
                {"entity_id": "org:b", "canonical_name": "FixtureAI"},
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Conflicting"):
        build_mapping_rows(
            seeds=seeds,
            startups_dir=None,
            phase2_dir=None,
            phase3_dir=None,
        )
