import argparse
import hashlib
import json

from datetime import datetime
from pathlib import Path

from ai_ingest.models import StartupRecord
from ai_ingest.sources.ai_market_watch import (
    DATA_URL,
    normalize_startup,
    parse_csv,
)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read_jsonl(path):
    rows = []

    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue

            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"{path.name}:{line_number}: invalid JSON"
                ) from exc

    return rows


def audit(snapshot):
    metadata = json.loads(
        (snapshot / "metadata.json").read_text(
            encoding="utf-8"
        )
    )

    require(
        metadata["dataset_url"] == DATA_URL,
        "Unexpected dataset URL.",
    )

    for filename, hash_field in (
        ("ai-startups.csv", "dataset_sha256"),
        ("README.md", "readme_sha256"),
    ):
        actual = hashlib.sha256(
            (snapshot / filename).read_bytes()
        ).hexdigest()

        require(
            actual == metadata[hash_field],
            f"Hash mismatch: {filename}",
        )

    raw_rows = parse_csv(
        (snapshot / "ai-startups.csv").read_bytes()
    )

    records = [
        StartupRecord.model_validate(row)
        for row in read_jsonl(snapshot / "startups.jsonl")
    ]

    provenance = read_jsonl(
        snapshot / "provenance.jsonl"
    )
    duplicates = read_jsonl(
        snapshot / "duplicates.jsonl"
    )
    quarantine = read_jsonl(
        snapshot / "quarantine.jsonl"
    )

    require(
        len(raw_rows) == metadata["input_records"],
        "Input count differs from metadata.",
    )
    require(
        len(records)
        == metadata["staged_unique_startup_candidates"],
        "Startup count differs from metadata.",
    )
    require(
        len(duplicates) == metadata["duplicate_rows"],
        "Duplicate count differs from metadata.",
    )
    require(
        len(quarantine) == metadata["quarantine_entries"],
        "Quarantine count differs from metadata.",
    )
    require(
        len(records) == len(provenance),
        "Startup/provenance count mismatch.",
    )
    require(
        len(raw_rows)
        == len(records) + len(duplicates) + len(quarantine),
        "Input rows are not fully accounted for.",
    )

    collected_at = datetime.fromisoformat(
        metadata["collected_at"].replace("Z", "+00:00")
    )

    require(
        collected_at.tzinfo is not None,
        "Collection timestamp is missing its timezone.",
    )

    seen_profiles = set()
    seen_names = set()
    seen_csv_indexes = set()
    unknown_employee_counts = 0

    for position, (record, evidence) in enumerate(
        zip(records, provenance)
    ):
        prefix = f"Record {position}"

        require(
            record.recordType == "STARTUP",
            f"{prefix}: wrong record type.",
        )
        require(
            evidence["dataset_url"] == DATA_URL,
            f"{prefix}: unexpected provenance dataset.",
        )
        require(
            evidence["raw_sha256"]
            == metadata["dataset_sha256"],
            f"{prefix}: provenance hash mismatch.",
        )

        csv_index = evidence["csv_record_index"]

        require(
            type(csv_index) is int
            and 0 <= csv_index < len(raw_rows),
            f"{prefix}: invalid CSV record index.",
        )
        require(
            csv_index not in seen_csv_indexes,
            f"{prefix}: reused CSV record index.",
        )

        expected_record, expected_evidence = normalize_startup(
            raw_rows[csv_index],
            collected_at=collected_at,
        )

        require(
            record.model_dump(mode="json")
            == expected_record.model_dump(mode="json"),
            f"{prefix}: record differs from its source row.",
        )

        for key, expected_value in expected_evidence.items():
            require(
                evidence.get(key) == expected_value,
                f"{prefix}: provenance mismatch for {key}.",
            )

        profile = evidence["source_record_id"]
        name_key = evidence["normalized_name_key"]

        require(
            profile not in seen_profiles,
            f"{prefix}: duplicate profile identity.",
        )
        require(
            name_key not in seen_names,
            f"{prefix}: unresolved normalized-name collision.",
        )

        seen_profiles.add(profile)
        seen_names.add(name_key)
        seen_csv_indexes.add(csv_index)

        if record.content.data.employeeCount is None:
            unknown_employee_counts += 1

    require(
        bool(records),
        "Startup export is empty.",
    )

    return {
        "automated_checks_passed": True,
        "input_records": len(raw_rows),
        "schema_valid_startup_records": len(records),
        "unique_source_profiles": len(seen_profiles),
        "unique_normalized_company_names": len(seen_names),
        "records_with_verified_csv_lineage": len(records),
        "duplicate_rows": len(duplicates),
        "quarantine_entries": len(quarantine),
        "unknown_employee_counts": unknown_employee_counts,
        "minimum_1000_source_backed_records_reached": (
            len(records) >= 1000
        ),
        "classification_basis": (
            "Publisher-curated startup dataset; "
            "not independently verified company-by-company."
        ),
        "profile_pages_validated": False,
        "independent_semantic_review_completed": False,
        "final_quality_gate_passed": False,
        "dataset_sha256": metadata["dataset_sha256"],
        "readme_sha256": metadata["readme_sha256"],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("snapshot", type=Path)
    args = parser.parse_args()

    report_path = args.snapshot / "audit.json"

    try:
        report = audit(args.snapshot)
    except Exception as exc:
        report = {
            "automated_checks_passed": False,
            "error": str(exc),
        }

        report_path.write_text(
            json.dumps(report, indent=2),
            encoding="utf-8",
        )

        parser.exit(
            status=1,
            message=f"Startup audit failed: {exc}\n",
        )

    temporary = report_path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(report, indent=2),
        encoding="utf-8",
    )
    temporary.replace(report_path)

    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()