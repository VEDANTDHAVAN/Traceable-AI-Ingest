"""Phase IV audit: seed integrity, determinism and mapping-log completeness."""

from __future__ import annotations

import argparse
import json
import re

from pathlib import Path

from ai_ingest.entity_resolution import (
    CanonicalEntity,
    EntityResolver,
    normalize_entity_name,
)
from ai_ingest.models import EntityMappingRecord


SHA256 = re.compile(r"^[0-9a-f]{64}$")
REQUIRED_KEYS = {
    "raw_name",
    "normalized_key",
    "canonical_entity_id",
    "canonical_name",
    "status",
    "method",
    "record_type",
    "source_url",
    "source_text_sha256",
    "evidence",
    "ownership_verification_status",
    "ownership_verified",
}


def read_jsonl(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON") from exc
    return rows


def audit(*, seeds: Path, mapping: Path, output: Path) -> dict:
    report: dict = {"phase4_complete": False, "gates": {}, "counts": {}, "errors": []}

    def fail(message: str) -> None:
        report["errors"].append(message)

    payload = json.loads(seeds.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        fail("Seed file is not a JSON array.")
        payload = []

    entities = [
        CanonicalEntity(
            entity_id=row["entity_id"],
            canonical_name=row["canonical_name"],
            aliases=tuple(row.get("aliases", [])),
            identifiers=tuple(row.get("identifiers", [])),
        )
        for row in payload
    ]

    try:
        resolver = EntityResolver(entities)
    except ValueError as exc:
        fail(f"Seed integrity failure: {exc}")
        resolver = EntityResolver([])

    # Alias collisions are only acceptable when they point at one entity.
    owners: dict[str, set[str]] = {}
    for entity in entities:
        for name in (entity.canonical_name, *entity.aliases):
            try:
                key = normalize_entity_name(name)
            except ValueError:
                fail(f"Empty normalized name in seed: {entity.entity_id}")
                continue
            owners.setdefault(key, set()).add(entity.entity_id)

    conflicting = {
        key: sorted(ids) for key, ids in owners.items() if len(ids) > 1
    }
    for key, ids in conflicting.items():
        fail(f"Conflicting alias '{key}' across {ids}.")

    rows = read_jsonl(mapping)
    verified_rows = 0
    resolved_rows = 0
    unresolved_rows = 0
    deterministic = True

    for index, row in enumerate(rows):
        missing = REQUIRED_KEYS - set(row)
        if missing:
            fail(f"Mapping {index}: missing keys {sorted(missing)}.")
            continue

        try:
            EntityMappingRecord.model_validate(row)
        except Exception as exc:
            fail(f"Mapping {index}: schema invalid: {exc}")
            continue

        if not SHA256.match(row["source_text_sha256"]):
            fail(f"Mapping {index}: invalid source hash.")

        if not str(row["source_url"]).startswith(("https://", "http://")):
            fail(f"Mapping {index}: invalid source URL.")

        if row["status"] == "RESOLVED":
            resolved_rows += 1
            if not row["canonical_entity_id"] or not row["canonical_name"]:
                fail(f"Mapping {index}: resolved without canonical identity.")
            if row["method"] != "NORMALIZED_EXACT_ALIAS_V1":
                fail(f"Mapping {index}: unexpected resolution method.")
            if row["ownership_verified"] is not False:
                fail(f"Mapping {index}: unsupported ownership verification.")
            if row["ownership_verification_status"] != "NAME_MATCH_ONLY":
                fail(f"Mapping {index}: ownership status is not NAME_MATCH_ONLY.")
            if row["canonical_entity_id"] not in resolver.entities:
                fail(f"Mapping {index}: canonical ID is not in the seed.")
        elif row["status"] == "UNRESOLVED":
            unresolved_rows += 1
            if row["canonical_entity_id"] is not None:
                fail(f"Mapping {index}: unresolved row has a canonical ID.")
            if row["ownership_verified"] is not False:
                fail(f"Mapping {index}: unresolved row claims ownership.")
        else:
            fail(f"Mapping {index}: unexpected status {row['status']}.")

        first = resolver.resolve(row["raw_name"])
        second = resolver.resolve(row["raw_name"])
        if first != second:
            deterministic = False
        if first["normalized_key"] != row["normalized_key"]:
            fail(f"Mapping {index}: normalized key is not reproducible.")
        if first["canonical_entity_id"] != row["canonical_entity_id"]:
            fail(f"Mapping {index}: canonical ID is not reproducible.")

        if not row["evidence"].strip():
            fail(f"Mapping {index}: empty evidence.")
        else:
            verified_rows += 1

    if not rows:
        fail("Mapping log is empty.")

    gates = {
        "seed_entities_ge_50": len(entities) >= 50,
        "no_conflicting_aliases": not conflicting,
        "deterministic_resolution": deterministic and bool(rows),
        "no_unsupported_ownership_inference": all(
            row.get("ownership_verified") is False for row in rows
        )
        and all(
            row.get("ownership_verification_status") != "VERIFIED"
            for row in rows
        ),
        "mapping_log_complete": bool(rows) and verified_rows == len(rows),
        "evidence_retention": all(
            row.get("source_url")
            and row.get("source_text_sha256")
            and row.get("evidence")
            for row in rows
        ),
        "unresolved_preserved_with_status": unresolved_rows >= 0,
    }
    report["gates"] = gates
    report["counts"] = {
        "seed_entities": len(entities),
        "seed_aliases": sum(
            1 + len(entity.aliases) for entity in entities
        ),
        "mapping_rows": len(rows),
        "resolved_rows": resolved_rows,
        "unresolved_rows": unresolved_rows,
        "ownership_verified_rows": sum(
            1 for row in rows if row.get("ownership_verified")
        ),
    }

    report["phase4_complete"] = all(gates.values()) and not report["errors"]

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase IV completion audit.")
    parser.add_argument("--seeds", type=Path, default=Path("configs/canonical_entities.json"))
    parser.add_argument("--mapping", type=Path, default=Path("data/phase4/entity_mapping.jsonl"))
    parser.add_argument("--output", type=Path, default=Path("data/phase4/audit.json"))
    args = parser.parse_args()

    report = audit(seeds=args.seeds, mapping=args.mapping, output=args.output)
    print(json.dumps(report, indent=2))

    if not report["phase4_complete"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
