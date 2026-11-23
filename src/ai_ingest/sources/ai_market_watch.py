import argparse
import asyncio
import csv
import hashlib
import io
import json

from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from ai_ingest.entity_resolution import normalize_entity_name
from ai_ingest.models import StartupRecord
from ai_ingest.sources.autoventure import download


BASE_URL = (
    "https://raw.githubusercontent.com/"
    "runsmlee/ai-startup-dataset/main"
)

DATA_URL = f"{BASE_URL}/ai-startups.csv"
README_URL = f"{BASE_URL}/README.md"

ATTRIBUTION = (
    "Data: AI Market Watch, AI Startup Dataset, "
    "https://www.ai-market-watch.com. "
    "CC BY 4.0. Fields normalized into the assessment schema."
)

REQUIRED_COLUMNS = {
    "company_name",
    "category",
    "country_code",
    "year_founded",
    "funding_range",
    "profile_url",
}


def normalize_profile_url(value: str) -> str:
    parsed = urlsplit(value.strip())

    if (
        parsed.scheme != "https"
        or parsed.hostname not in {
            "ai-market-watch.com",
            "www.ai-market-watch.com",
        }
        or parsed.username
        or parsed.password
        or parsed.port is not None
        or not parsed.path.strip("/")
    ):
        raise ValueError("Unexpected startup profile URL.")

    return urlunsplit(
        (
            "https",
            "www.ai-market-watch.com",
            parsed.path.rstrip("/"),
            "",
            "",
        )
    )


def parse_csv(raw: bytes) -> list[dict]:
    text = raw.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))

    headers = reader.fieldnames or []

    if len(headers) != len(set(headers)):
        raise ValueError("Duplicate CSV column names.")

    missing = REQUIRED_COLUMNS - set(headers)

    if missing:
        raise ValueError(
            f"Missing CSV columns: {sorted(missing)}. "
            f"Observed headers: {headers}"
        )

    rows = []

    for row in reader:
        if None in row:
            raise ValueError(
                "CSV row contains more values than its header."
            )

        rows.append(row)

    return rows


def normalize_startup(
    row: dict,
    *,
    collected_at: datetime,
) -> tuple[StartupRecord, dict]:
    name = row.get("company_name")

    if not isinstance(name, str) or not name.strip():
        raise ValueError("Missing company name.")

    profile_raw = row.get("profile_url", "")

    if not isinstance(profile_raw, str) or not profile_raw.strip():
        raise ValueError(
            "MISSING_PROFILE_URL"
        )

    try:
        profile = normalize_profile_url(profile_raw)
    except ValueError as exc:
        raise ValueError(
            f"INVALID_PROFILE_URL: {exc}"
        ) from exc

    record = StartupRecord.model_validate(
        {
            "source": {
                "name": "AI Market Watch Startup Dataset",
                "url": profile,
            },
            "collectedAt": collected_at,
            "content": {
                # Preserve the source's company name.
                # Alias-based canonicalization remains a separate step.
                "entityName": name.strip(),
                "data": {
                    "employeeCount": None,
                },
            },
        }
    )

    provenance = {
        "source_record_id": profile,
        "source_company_name": name.strip(),
        "normalized_name_key": normalize_entity_name(name),
        "category": row.get("category"),
        "country_code": row.get("country_code"),
        "year_founded": row.get("year_founded"),
        "funding_range": row.get("funding_range"),
        "profile_url": profile,
        "profile_page_fetched": False,
        "startup_classification_basis": (
            "SECONDARY_STARTUP_DATASET"
        ),
        "attribution": ATTRIBUTION,
    }

    return record, provenance


def write_jsonl(path: Path, rows) -> None:
    temporary = path.with_name(path.name + ".tmp")

    with temporary.open("w", encoding="utf-8") as handle:
        for row in rows:
            if isinstance(row, StartupRecord):
                line = row.model_dump_json()
            else:
                line = json.dumps(row, ensure_ascii=False)

            handle.write(line + "\n")

    temporary.replace(path)


def stage_snapshot(snapshot_dir: Path) -> dict:
    metadata_path = snapshot_dir / "metadata.json"

    metadata = json.loads(
        metadata_path.read_text(encoding="utf-8")
    )

    raw = (snapshot_dir / "ai-startups.csv").read_bytes()
    readme = (snapshot_dir / "README.md").read_bytes()

    if hashlib.sha256(raw).hexdigest() != (
        metadata["dataset_sha256"]
    ):
        raise ValueError("Startup dataset hash mismatch.")

    if hashlib.sha256(readme).hexdigest() != (
        metadata["readme_sha256"]
    ):
        raise ValueError("README hash mismatch.")

    # Verify that the saved document still contains the
    # permission identifier we reviewed.
    if "cc by 4.0" not in readme.decode("utf-8").casefold():
        raise ValueError(
            "Expected CC BY 4.0 notice not found. "
            "Inspect the saved README before proceeding."
        )

    collected_at = datetime.fromisoformat(
        metadata["collected_at"].replace("Z", "+00:00")
    )

    if collected_at.tzinfo is None:
        raise ValueError("Collection time must include a timezone.")

    rows = parse_csv(raw)

    # First group by source profile identity.
    by_profile = {}
    duplicates = []
    quarantine = []

    for index, row in enumerate(rows):
        try:
            record, provenance = normalize_startup(
                row,
                collected_at=collected_at,
            )

            profile = provenance["source_record_id"]

            provenance.update(
                {
                    "csv_record_index": index,
                    "dataset_url": DATA_URL,
                    "raw_path": str(
                        (snapshot_dir / "ai-startups.csv").resolve()
                    ),
                    "raw_sha256": metadata["dataset_sha256"],
                }
            )

            if profile in by_profile:
                previous_record, _ = by_profile[profile]

                if (
                    previous_record.content.entityName
                    != record.content.entityName
                ):
                    raise ValueError(
                        "Conflicting company names for one profile."
                    )

                duplicates.append(
                    {
                        "csv_record_index": index,
                        "reason": "DUPLICATE_PROFILE",
                        "profile_url": profile,
                    }
                )
                continue

            by_profile[profile] = (record, provenance)

        except (ValueError, TypeError) as exc:
            quarantine.append(
                {
                    "csv_record_index": index,
                    "reason": str(exc),
                    "raw_record": row,
                }
            )

    # Do not count distinct profiles with colliding normalized
    # names as distinct companies without further resolution.
    name_groups = {}

    for profile, (_, provenance) in by_profile.items():
        name_groups.setdefault(
            provenance["normalized_name_key"],
            [],
        ).append(profile)

    records = []
    provenance_rows = []

    for profile, (record, provenance) in by_profile.items():
        group = name_groups[
            provenance["normalized_name_key"]
        ]

        if len(group) > 1:
            quarantine.append(
                {
                    "reason": "AMBIGUOUS_NORMALIZED_COMPANY_NAME",
                    "profile_url": profile,
                    "conflicting_profiles": group,
                    "source_company_name": (
                        record.content.entityName
                    ),
                }
            )
            continue

        records.append(record)
        provenance_rows.append(provenance)

    write_jsonl(snapshot_dir / "startups.jsonl", records)
    write_jsonl(
        snapshot_dir / "provenance.jsonl",
        provenance_rows,
    )
    write_jsonl(
        snapshot_dir / "duplicates.jsonl",
        duplicates,
    )
    write_jsonl(
        snapshot_dir / "quarantine.jsonl",
        quarantine,
    )

    metadata.update(
        {
            "status": "staged",
            "parser_version": "ai-market-watch-csv-v1",
            "input_records": len(rows),
            "staged_unique_startup_candidates": len(records),
            "duplicate_rows": len(duplicates),
            "quarantine_entries": len(quarantine),
            "minimum_1000_candidates_reached": (
                len(records) >= 1000
            ),
            "profile_pages_validated": False,
            "final_quality_gate_passed": False,
            "attribution": ATTRIBUTION,
        }
    )

    metadata_path.write_text(
        json.dumps(metadata, indent=2),
        encoding="utf-8",
    )

    return {
        **metadata,
        "output_dir": str(snapshot_dir),
    }


async def ingest(output_dir: Path) -> dict:
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=120),
        headers={
            "User-Agent": "ai-intelligence-ingestion/0.1"
        },
        connector=aiohttp.TCPConnector(limit=2),
    ) as session:
        # Two published files, bounded concurrency.
        raw, readme = await asyncio.gather(
            download(
                session,
                DATA_URL,
                max_bytes=20_000_000,
            ),
            download(
                session,
                README_URL,
                max_bytes=1_000_000,
            ),
        )

    collected_at = datetime.now(timezone.utc)

    snapshot_dir = output_dir / collected_at.strftime(
        "%Y%m%dT%H%M%S%fZ"
    )

    snapshot_dir.mkdir(parents=True, exist_ok=False)

    (snapshot_dir / "ai-startups.csv").write_bytes(raw)
    (snapshot_dir / "README.md").write_bytes(readme)

    metadata = {
        "source": "AI Market Watch Startup Dataset",
        "dataset_url": DATA_URL,
        "readme_url": README_URL,
        "collected_at": collected_at.isoformat(),
        "dataset_sha256": hashlib.sha256(raw).hexdigest(),
        "readme_sha256": hashlib.sha256(readme).hexdigest(),
        "status": "raw_saved",
    }

    (snapshot_dir / "metadata.json").write_text(
        json.dumps(metadata, indent=2),
        encoding="utf-8",
    )

    return stage_snapshot(snapshot_dir)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Acquire the published AI Market Watch dataset."
    )

    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/startups/ai-market-watch"),
    )

    parser.add_argument(
        "--snapshot",
        type=Path,
    )

    args = parser.parse_args()

    try:
        result = (
            stage_snapshot(args.snapshot)
            if args.snapshot is not None
            else asyncio.run(ingest(args.output_dir))
        )
    except Exception as exc:
        parser.exit(
            status=1,
            message=f"Startup ingestion failed: {exc}\n",
        )

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()