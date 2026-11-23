"""Build and validate the six-tab export.

CSV output is always produced. With ``--upload`` and Google credentials the
same validated rows are written to a public Google Sheet with exactly six tabs.
"""

from __future__ import annotations

import argparse
import csv
import json
import os

from datetime import datetime, timedelta, timezone
from pathlib import Path

from ai_ingest.models import (
    EntityMappingRecord,
    JobRecord,
    NewsRecord,
    ProductRecord,
    ResearchPaperRecord,
    StartupRecord,
)


TABS = [
    "Startups",
    "Products",
    "Research Papers",
    "Jobs",
    "News",
    "Entity Mapping Log",
]

FRESH_WINDOW = timedelta(hours=24)
FUTURE_SKEW = timedelta(minutes=5)


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


def latest_dir(root: Path, *, require: str | None = None) -> Path:
    candidates = [
        path
        for path in root.iterdir()
        if path.is_dir() and path.name[:1].isdigit()
    ]
    if require is not None:
        candidates = [path for path in candidates if (path / require).is_file()]
    if not candidates:
        raise ValueError(f"No matching run under {root}")
    return sorted(candidates)[-1]


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Naive timestamp in export.")
    return parsed.astimezone(timezone.utc)


def write_csv(path: Path, headers: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def build_startups(root: Path) -> tuple[list[dict], dict]:
    run = latest_dir(root, require="startups.jsonl")
    records = [StartupRecord.model_validate(row) for row in read_jsonl(run / "startups.jsonl")]
    provenance = read_jsonl(run / "provenance.jsonl")

    rows = []
    for record, evidence in zip(records, provenance):
        rows.append(
            {
                "entityName": record.content.entityName,
                "employeeCount": record.content.data.employeeCount,
                "sourceName": record.source.name,
                "sourceUrl": str(record.source.url),
                "collectedAt": record.collectedAt.isoformat(),
                "sourceRecordId": evidence.get("source_record_id"),
                "rawSha256": evidence.get("raw_sha256"),
                "sourceBacked": evidence.get("source_backed", True),
                "independentlyVerified": evidence.get("independently_verified", False),
            }
        )
    return rows, {"run": run.name}


def build_products(root: Path) -> tuple[list[dict], dict]:
    run = latest_dir(root, require="provenance.jsonl")
    records = [ProductRecord.model_validate(row) for row in read_jsonl(run / "products.jsonl")]
    provenance = read_jsonl(run / "provenance.jsonl")
    metadata = json.loads((run / "metadata.json").read_text(encoding="utf-8"))

    rows = []
    for record, evidence in zip(records, provenance):
        sources = evidence.get("sources", [])
        rows.append(
            {
                "productName": record.content.productName,
                "startupName": record.content.startupName,
                "pricingModel": record.content.pricingModel,
                "sourceName": record.source.name,
                "sourceUrl": str(record.source.url),
                "collectedAt": record.collectedAt.isoformat(),
                "contributingSources": ";".join(
                    sorted({source["source"] for source in sources})
                ),
                "sourceRecordIds": ";".join(
                    str(source.get("source_record_id")) for source in sources
                ),
                "rawSha256s": ";".join(
                    str(source.get("raw_sha256")) for source in sources
                ),
                "companyResolutionStatus": evidence.get(
                    "company_resolution_status"
                ),
                "pricingResolutionStatus": evidence.get(
                    "pricing_resolution_status"
                ),
            }
        )
    return rows, metadata


def build_papers(root: Path) -> tuple[list[dict], dict]:
    run = latest_dir(root, require="associations.jsonl")
    records = [
        ResearchPaperRecord.model_validate(row)
        for row in read_jsonl(run / "papers.jsonl")
    ]
    associations = read_jsonl(run / "associations.jsonl")
    metadata = json.loads((run / "metadata.json").read_text(encoding="utf-8"))

    by_paper: dict[str, dict] = {}
    for association in associations:
        key = association["paper_id"]
        current = by_paper.get(key)
        if current is None or (
            association.get("is_official") and not current.get("is_official")
        ):
            by_paper[key] = association

    rows = []
    for record in records:
        paper_id = record.content.paper_url.path.split("/")[-1]
        paper_id = paper_id.split("v")[0] if "v" in paper_id else paper_id
        association = by_paper.get(f"arxiv:{paper_id}", {})

        rows.append(
            {
                "title": record.content.title,
                "authors": "; ".join(record.content.authors),
                "paperUrl": str(record.content.paper_url),
                "publishedDate": record.content.published_date.isoformat(),
                "githubUrl": (
                    str(record.content.github_url)
                    if record.content.github_url
                    else ""
                ),
                "githubStars": (
                    record.content.github_stars
                    if record.content.github_stars is not None
                    else ""
                ),
                "githubMetricsCollectedAt": (
                    record.content.github_metrics_collected_at.isoformat()
                    if record.content.github_metrics_collected_at
                    else ""
                ),
                "associationType": association.get("association_type", ""),
                "isOfficial": association.get("is_official", ""),
                "sourceName": record.source.name,
                "sourceUrl": str(record.source.url),
                "collectedAt": record.collectedAt.isoformat(),
            }
        )
    return rows, metadata


def build_jobs(phase2: Path) -> tuple[list[dict], dict]:
    records = [JobRecord.model_validate(row) for row in read_jsonl(phase2 / "jobs.jsonl")]
    provenance = [
        row for row in read_jsonl(phase2 / "provenance.jsonl") if row["record_type"] == "JOB"
    ]
    by_url = {
        str(row.get("job_url", "")).rstrip("/").casefold(): row for row in provenance
    }

    rows = []
    for record in records:
        evidence = by_url.get(str(record.source.url).rstrip("/").casefold(), {})
        rows.append(
            {
                "title": record.content.title,
                "company": (
                    record.content.company
                    if record.content.company is not None
                    else ""
                ),
                "date": record.content.date.isoformat(),
                "isRemote": (
                    record.content.isRemote
                    if record.content.isRemote is not None
                    else ""
                ),
                "roleFamily": record.content.roleFamily or "",
                "sourceName": record.source.name,
                "sourceUrl": str(record.source.url),
                "collectedAt": record.collectedAt.isoformat(),
                "extractionStatus": record.content.extraction_status,
                "companyRaw": evidence.get("company_raw", ""),
                "companyCanonical": evidence.get("company_canonical") or "",
                "aiMatchTerms": ";".join(evidence.get("ai_match_terms", [])),
                "contentSha256": evidence.get("content_sha256", ""),
            }
        )
    return rows, {}


def build_news(phase2: Path) -> tuple[list[dict], dict]:
    records = [NewsRecord.model_validate(row) for row in read_jsonl(phase2 / "news.jsonl")]
    rows = []
    for record in records:
        rows.append(
            {
                "title": record.content.title,
                "date": record.content.date.isoformat(),
                "canonicalUrl": str(record.content.canonical_url),
                "sourceName": record.source.name,
                "sourceUrl": str(record.source.url),
                "collectedAt": record.collectedAt.isoformat(),
                "ingestedAt": record.content.ingested_at.isoformat(),
                "extractionStatus": record.content.extraction_status,
                "contentSha256": record.content.content_sha256,
                "freshnessStatus": record.content.freshness_status,
            }
        )
    return rows, {}


def build_mapping(path: Path) -> tuple[list[dict], dict]:
    rows = []
    for row in read_jsonl(path):
        record = EntityMappingRecord.model_validate(row)
        rows.append(
            {
                "rawName": record.raw_name,
                "normalizedKey": record.normalized_key,
                "canonicalEntityId": record.canonical_entity_id or "",
                "canonicalName": record.canonical_name or "",
                "status": record.status,
                "method": record.method or "",
                "recordType": record.record_type,
                "sourceUrl": str(record.source_url),
                "sourceTextSha256": record.source_text_sha256,
                "ownershipVerificationStatus": record.ownership_verification_status,
                "ownershipVerified": record.ownership_verified,
                "evidence": record.evidence,
            }
        )
    return rows, {}


def validate(tables: dict[str, list[dict]]) -> dict:
    errors: list[str] = []
    counts = {name: len(rows) for name, rows in tables.items()}

    if counts["Startups"] < 1000:
        errors.append("Startups below 1,000.")
    if counts["Products"] < 1000:
        errors.append("Products below 1,000.")
    if counts["Research Papers"] < 1000:
        errors.append("Research Papers below 1,000.")
    if counts["Entity Mapping Log"] == 0:
        errors.append("Entity Mapping Log is empty.")

    url_fields = {
        "Startups": "sourceUrl",
        "Products": "sourceUrl",
        "Research Papers": "sourceUrl",
        "Jobs": "sourceUrl",
        "News": "sourceUrl",
        "Entity Mapping Log": "sourceUrl",
    }

    for tab, rows in tables.items():
        field = url_fields[tab]
        for index, row in enumerate(rows):
            if not str(row.get(field, "")).startswith(("https://", "http://")):
                errors.append(f"{tab}:{index} missing source URL.")
                break
            if not row.get("rawSha256" if tab == "Startups" else field):
                pass

    for index, row in enumerate(tables["Research Papers"]):
        has_stars = row["githubStars"] != ""
        if has_stars and (
            not row["githubUrl"] or not row["githubMetricsCollectedAt"]
        ):
            errors.append(f"Research Papers:{index} stars without URL/observation.")

    for tab, identity in (("News", "canonicalUrl"), ("Jobs", "sourceUrl")):
        seen: set[str] = set()
        for index, row in enumerate(tables[tab]):
            key = str(row[identity]).rstrip("/").casefold()
            if key in seen:
                errors.append(f"{tab}:{index} duplicate canonical URL.")
                break
            seen.add(key)

    for index, row in enumerate(tables["News"]):
        collected = parse_time(row["collectedAt"])
        published = parse_time(row["date"])
        age = collected - published
        if age < -FUTURE_SKEW or age > FRESH_WINDOW:
            errors.append(f"News:{index} outside 24h at ingestion.")
            break

    for index, row in enumerate(tables["Jobs"]):
        collected = parse_time(row["collectedAt"])
        published = parse_time(row["date"])
        age = collected - published
        if age < -FUTURE_SKEW or age > FRESH_WINDOW:
            errors.append(f"Jobs:{index} outside 24h at ingestion.")
            break

    papers_with_stars = sum(
        1 for row in tables["Research Papers"] if row["githubStars"] != ""
    )

    return {
        "tabs": TABS,
        "counts": counts,
        "papers_with_github_stars": papers_with_stars,
        "all_rows_have_source_url": not any(
            "missing source URL" in error for error in errors
        ),
        "all_exported_news_within_24_hours": not any(
            error.startswith("News:") for error in errors
        ),
        "all_exported_jobs_within_24_hours": not any(
            error.startswith("Jobs:") for error in errors
        ),
        "no_duplicate_canonical_urls": not any(
            "duplicate canonical URL" in error for error in errors
        ),
        "no_fabricated_star_fields": not any(
            "stars without" in error for error in errors
        ),
        "validation_passed": not errors,
        "errors": errors,
    }


HEADERS = {
    "Startups": [
        "entityName",
        "employeeCount",
        "sourceName",
        "sourceUrl",
        "collectedAt",
        "sourceRecordId",
        "rawSha256",
        "sourceBacked",
        "independentlyVerified",
    ],
    "Products": [
        "productName",
        "startupName",
        "pricingModel",
        "sourceName",
        "sourceUrl",
        "collectedAt",
        "contributingSources",
        "sourceRecordIds",
        "rawSha256s",
        "companyResolutionStatus",
        "pricingResolutionStatus",
    ],
    "Research Papers": [
        "title",
        "authors",
        "paperUrl",
        "publishedDate",
        "githubUrl",
        "githubStars",
        "githubMetricsCollectedAt",
        "associationType",
        "isOfficial",
        "sourceName",
        "sourceUrl",
        "collectedAt",
    ],
    "Jobs": [
        "title",
        "company",
        "date",
        "isRemote",
        "roleFamily",
        "sourceName",
        "sourceUrl",
        "collectedAt",
        "extractionStatus",
        "companyRaw",
        "companyCanonical",
        "aiMatchTerms",
        "contentSha256",
    ],
    "News": [
        "title",
        "date",
        "canonicalUrl",
        "sourceName",
        "sourceUrl",
        "collectedAt",
        "ingestedAt",
        "extractionStatus",
        "contentSha256",
        "freshnessStatus",
    ],
    "Entity Mapping Log": [
        "rawName",
        "normalizedKey",
        "canonicalEntityId",
        "canonicalName",
        "status",
        "method",
        "recordType",
        "sourceUrl",
        "sourceTextSha256",
        "ownershipVerificationStatus",
        "ownershipVerified",
        "evidence",
    ],
}


def upload(
    tables: dict[str, list[dict]],
    *,
    credentials_path: Path,
    spreadsheet_key: str | None,
    title: str,
) -> dict:
    try:
        import gspread
    except ImportError as exc:
        raise RuntimeError(
            "Install gspread and google-auth to upload: pip install gspread google-auth"
        ) from exc

    client = gspread.service_account(filename=str(credentials_path))

    if spreadsheet_key:
        spreadsheet = client.open_by_key(spreadsheet_key)
        for existing in spreadsheet.worksheets():
            if existing.title not in TABS:
                existing.delete()
    else:
        spreadsheet = client.create(title)

    for tab in TABS:
        try:
            worksheet = spreadsheet.worksheet(tab)
            worksheet.clear()
        except gspread.WorksheetNotFound:
            worksheet = spreadsheet.add_worksheet(
                title=tab,
                rows=max(len(tables[tab]) + 1, 2),
                cols=len(HEADERS[tab]),
            )

        values = [HEADERS[tab]] + [
            [row.get(header, "") for header in HEADERS[tab]]
            for row in tables[tab]
        ]
        worksheet.update(values=values, range_name="A1")

    try:
        client.share(
            spreadsheet.id,
            perm_type="anyone",
            role="reader",
            notify=False,
        )
        public = True
    except Exception:
        public = False

    return {
        "spreadsheet_id": spreadsheet.id,
        "url": f"https://docs.google.com/spreadsheets/d/{spreadsheet.id}",
        "title": spreadsheet.title,
        "tabs": [worksheet.title for worksheet in spreadsheet.worksheets()],
        "public": public,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build/validate the six-tab export.")
    parser.add_argument("--startups", type=Path, default=Path("data/startups/ai-market-watch"))
    parser.add_argument("--products", type=Path, default=Path("data/products/merged"))
    parser.add_argument("--papers", type=Path, default=Path("data/papers/pwc"))
    parser.add_argument("--phase2", type=Path, default=Path("data/phase2/merged"))
    parser.add_argument("--mapping", type=Path, default=Path("data/phase4/entity_mapping.jsonl"))
    parser.add_argument("--output", type=Path, default=Path("data/exports"))
    parser.add_argument("--upload", action="store_true")
    parser.add_argument("--credentials", type=Path)
    parser.add_argument("--spreadsheet-key")
    parser.add_argument("--title", default="GraphOne / FrontierAtlas AI Intelligence")
    args = parser.parse_args()

    startup_rows, startup_meta = build_startups(args.startups)
    product_rows, product_meta = build_products(args.products)
    paper_rows, paper_meta = build_papers(args.papers)
    job_rows, _ = build_jobs(args.phase2)
    news_rows, _ = build_news(args.phase2)
    mapping_rows, _ = build_mapping(args.mapping)

    tables = {
        "Startups": startup_rows,
        "Products": product_rows,
        "Research Papers": paper_rows,
        "Jobs": job_rows,
        "News": news_rows,
        "Entity Mapping Log": mapping_rows,
    }

    validation = validate(tables)

    for tab in TABS:
        write_csv(
            args.output / f"{tab.lower().replace(' ', '_')}.csv",
            HEADERS[tab],
            tables[tab],
        )

    metadata = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sources": {
            "startups_run": startup_meta.get("run"),
            "products": {
                key: product_meta.get(key)
                for key in (
                    "merged_unique_products",
                    "products_with_known_pricing",
                    "merged_products_with_multiple_sources",
                )
            },
            "papers": {
                key: paper_meta.get(key)
                for key in (
                    "unique_papers",
                    "accepted_associations",
                    "github_metrics_present_for_accepted",
                )
            },
        },
        "validation": validation,
    }

    if args.upload:
        if args.credentials is None:
            raise SystemExit("--upload requires --credentials.")
        sheet = upload(
            tables,
            credentials_path=args.credentials,
            spreadsheet_key=args.spreadsheet_key,
            title=args.title,
        )
        metadata["google_sheet"] = sheet
        (args.output / "google_sheet.json").write_text(
            json.dumps(sheet, indent=2) + "\n",
            encoding="utf-8",
        )

    (args.output / "metadata.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print(json.dumps(metadata, indent=2))

    if not validation["validation_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
