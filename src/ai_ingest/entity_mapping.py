"""Phase IV: deterministic entity mapping log.

Only raw names that a source explicitly presents as a company/organization are
mapped. Product names are never treated as owners. A name match produces a
canonical ID but never an ownership, funding or startup-status claim.
"""

from __future__ import annotations

import argparse
import hashlib
import json

from pathlib import Path

from ai_ingest.entity_resolution import EntityResolver
from ai_ingest.models import EntityMappingRecord


DEFAULT_SEEDS = Path("configs/canonical_entities.json")


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
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


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    temporary.replace(path)


def _row(
    resolver: EntityResolver,
    *,
    raw_name: str,
    record_type: str,
    source_url: str,
    source_text_sha256: str,
    evidence: str,
) -> dict:
    resolution = resolver.resolve(raw_name)
    status = resolution["status"]

    row = {
        "raw_name": raw_name,
        "normalized_key": resolution["normalized_key"],
        "canonical_entity_id": resolution["canonical_entity_id"],
        "canonical_name": resolution["canonical_name"],
        "status": status,
        "method": resolution["method"],
        "record_type": record_type,
        "source_url": source_url,
        "source_text_sha256": source_text_sha256,
        "evidence": evidence,
        "ownership_verification_status": (
            "NAME_MATCH_ONLY" if status == "RESOLVED" else "NOT_VERIFIED"
        ),
        "ownership_verified": False,
    }

    EntityMappingRecord.model_validate(row)
    return row


def build_mapping_rows(
    *,
    seeds: Path,
    startups_dir: Path | None,
    phase2_dir: Path | None,
    phase3_dir: Path | None,
) -> list[dict]:
    resolver = EntityResolver.from_file(seeds)
    rows: list[dict] = []
    seen: set[tuple[str, str, str]] = set()

    def add(row: dict) -> None:
        key = (row["raw_name"].casefold(), row["record_type"], row["source_url"])
        if key in seen:
            return
        seen.add(key)
        rows.append(row)

    # 1. Startup records: source presents entityName explicitly.
    if startups_dir is not None:
        records = read_jsonl(startups_dir / "startups.jsonl")
        provenance = read_jsonl(startups_dir / "provenance.jsonl")
        for record, evidence in zip(records, provenance):
            raw_name = record["content"]["entityName"]
            add(
                _row(
                    resolver,
                    raw_name=raw_name,
                    record_type="STARTUP",
                    source_url=str(record["source"]["url"]),
                    source_text_sha256=evidence.get("raw_sha256")
                    or hashlib.sha256(
                        json.dumps(evidence, sort_keys=True).encode("utf-8")
                    ).hexdigest(),
                    evidence=(
                        "Startup dataset entityName field; source profile "
                        f"{evidence.get('source_record_id')}"
                    ),
                )
            )

    # 2. Job records: company field from the source board.
    if phase2_dir is not None:
        for evidence in read_jsonl(phase2_dir / "provenance.jsonl"):
            if evidence.get("record_type") != "JOB":
                continue
            raw_name = evidence.get("company_raw")
            if not raw_name:
                continue
            add(
                _row(
                    resolver,
                    raw_name=raw_name,
                    record_type="JOB",
                    source_url=evidence.get("job_url")
                    or evidence.get("source_url"),
                    source_text_sha256=evidence.get("content_sha256")
                    or hashlib.sha256(
                        json.dumps(evidence, sort_keys=True).encode("utf-8")
                    ).hexdigest(),
                    evidence=(
                        "Job board structured company field; "
                        f"{evidence.get('company_evidence')}"
                    ),
                )
            )

    # 3. Grounded LLM organization claims from news extraction.
    if phase3_dir is not None:
        for run in sorted(
            path for path in phase3_dir.iterdir() if path.is_dir()
        ):
            for record in read_jsonl(run / "extraction.jsonl"):
                for claim in record.get("claims", []):
                    if claim.get("field") not in {
                        "organization_name",
                        "company_name",
                    }:
                        continue
                    add(
                        _row(
                            resolver,
                            raw_name=claim["value"],
                            record_type=record["record_type"],
                            source_url=claim["source_url"],
                            source_text_sha256=claim["source_text_sha256"],
                            evidence=(
                                f"Grounded quote [{claim['quote_start']}:"
                                f"{claim['quote_end']}]: {claim['quote'][:300]}"
                            ),
                        )
                    )

    return rows


def build(
    *,
    seeds: Path,
    startups_dir: Path | None,
    phase2_dir: Path | None,
    phase3_dir: Path | None,
    output: Path,
) -> dict:
    rows = build_mapping_rows(
        seeds=seeds,
        startups_dir=startups_dir,
        phase2_dir=phase2_dir,
        phase3_dir=phase3_dir,
    )

    write_jsonl(output, rows)

    resolved = sum(1 for row in rows if row["status"] == "RESOLVED")
    metadata = {
        "schema_version": "1.0",
        "seed_entities": len(EntityResolver.from_file(seeds).entities),
        "mapping_rows": len(rows),
        "resolved_rows": resolved,
        "unresolved_rows": sum(1 for row in rows if row["status"] == "UNRESOLVED"),
        "ownership_verified_rows": 0,
        "deterministic": True,
        "output": str(output),
    }
    (output.parent / "metadata.json").write_text(
        json.dumps(metadata, indent=2) + "\n",
        encoding="utf-8",
    )
    return metadata


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the entity mapping log.")
    parser.add_argument("--seeds", type=Path, default=DEFAULT_SEEDS)
    parser.add_argument("--startups", type=Path)
    parser.add_argument("--phase2", type=Path, default=Path("data/phase2/merged"))
    parser.add_argument("--phase3", type=Path, default=Path("data/phase3"))
    parser.add_argument("--output", type=Path, default=Path("data/phase4/entity_mapping.jsonl"))
    args = parser.parse_args()

    result = build(
        seeds=args.seeds,
        startups_dir=args.startups,
        phase2_dir=args.phase2,
        phase3_dir=args.phase3,
        output=args.output,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
