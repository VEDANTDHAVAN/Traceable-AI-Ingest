"""Build, validate, and optionally publish the six-tab export.

CSV output is always produced. With ``--upload`` and Google credentials,
the same validated rows are written to a Google Sheet with exactly six
required tabs.

A service account cannot create a new spreadsheet in its own Drive
because it has no Drive storage quota. Provide ``--spreadsheet-key``
for a spreadsheet created by a human Google account and shared with
the service account as Editor.
"""


from __future__ import annotations


import argparse
import csv
import json
import os


from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


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


TAB_SET = set(TABS)


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
                raise ValueError(
                    f"{path}:{line_number}: invalid JSON"
                ) from exc

    return rows



def latest_dir(
    root: Path,
    *,
    require: str | None = None,
) -> Path:
    candidates = [
        path
        for path in root.iterdir()
        if path.is_dir() and path.name[:1].isdigit()
    ]

    if require is not None:
        candidates = [
            path
            for path in candidates
            if (path / require).is_file()
        ]

    if not candidates:
        raise ValueError(
            f"No matching run under {root}"
        )

    return sorted(candidates)[-1]



def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(
        value.replace("Z", "+00:00")
    )

    if parsed.tzinfo is None:
        raise ValueError(
            "Naive timestamp in export."
        )

    return parsed.astimezone(timezone.utc)



def resolve_spreadsheet_id(
    value: str | None,
) -> str | None:
    if value is None:
        return None

    value = value.strip()

    if not value:
        return None

    if "docs.google.com" not in value:
        return value

    parsed = urlsplit(value)
    parts = parsed.path.split("/")

    if "d" in parts:
        index = parts.index("d")

        if index + 1 < len(parts):
            return parts[index + 1]

    query = parse_qs(parsed.query)

    if "id" in query:
        return query["id"][0]

    raise ValueError(
        "Could not extract spreadsheet ID from Google Sheets URL."
    )



def write_csv(
    path: Path,
    headers: list[str],
    rows: list[dict],
) -> None:
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temporary = path.with_name(
        path.name + ".tmp"
    )

    with temporary.open(
        "w",
        encoding="utf-8-sig",
        newline="",
    ) as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=headers,
            extrasaction="ignore",
        )

        writer.writeheader()
        writer.writerows(rows)

    temporary.replace(path)



def build_startups(
    root: Path,
) -> tuple[list[dict], dict]:
    run = latest_dir(
        root,
        require="startups.jsonl",
    )

    records = [
        StartupRecord.model_validate(row)
        for row in read_jsonl(
            run / "startups.jsonl"
        )
    ]

    provenance = read_jsonl(
        run / "provenance.jsonl"
    )

    rows = []

    for record, evidence in zip(
        records,
        provenance,
    ):
        rows.append(
            {
                "entityName": (
                    record.content.entityName
                ),
                "employeeCount": (
                    record.content.data.employeeCount
                ),
                "sourceName": (
                    record.source.name
                ),
                "sourceUrl": str(
                    record.source.url
                ),
                "collectedAt": (
                    record.collectedAt.isoformat()
                ),
                "sourceRecordId": evidence.get(
                    "source_record_id"
                ),
                "rawSha256": evidence.get(
                    "raw_sha256"
                ),
                "sourceBacked": evidence.get(
                    "source_backed",
                    True,
                ),
                "independentlyVerified": evidence.get(
                    "independently_verified",
                    False,
                ),
            }
        )

    return rows, {"run": run.name}



def build_products(
    root: Path,
) -> tuple[list[dict], dict]:
    run = latest_dir(
        root,
        require="provenance.jsonl",
    )

    records = [
        ProductRecord.model_validate(row)
        for row in read_jsonl(
            run / "products.jsonl"
        )
    ]

    provenance = read_jsonl(
        run / "provenance.jsonl"
    )

    metadata = json.loads(
        (run / "metadata.json").read_text(
            encoding="utf-8"
        )
    )

    rows = []

    for record, evidence in zip(
        records,
        provenance,
    ):
        sources = evidence.get(
            "sources",
            [],
        )

        rows.append(
            {
                "productName": (
                    record.content.productName
                ),
                "startupName": (
                    record.content.startupName
                ),
                "pricingModel": (
                    record.content.pricingModel
                ),
                "sourceName": (
                    record.source.name
                ),
                "sourceUrl": str(
                    record.source.url
                ),
                "collectedAt": (
                    record.collectedAt.isoformat()
                ),
                "contributingSources": ";".join(
                    sorted(
                        {
                            source["source"]
                            for source in sources
                        }
                    )
                ),
                "sourceRecordIds": ";".join(
                    str(
                        source.get(
                            "source_record_id"
                        )
                    )
                    for source in sources
                ),
                "rawSha256s": ";".join(
                    str(
                        source.get(
                            "raw_sha256"
                        )
                    )
                    for source in sources
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



def build_papers(
    root: Path,
) -> tuple[list[dict], dict]:
    run = latest_dir(
        root,
        require="associations.jsonl",
    )

    records = [
        ResearchPaperRecord.model_validate(row)
        for row in read_jsonl(
            run / "papers.jsonl"
        )
    ]

    associations = read_jsonl(
        run / "associations.jsonl"
    )

    metadata = json.loads(
        (run / "metadata.json").read_text(
            encoding="utf-8"
        )
    )

    by_paper: dict[str, dict] = {}

    for association in associations:
        key = association["paper_id"]

        current = by_paper.get(key)

        if current is None or (
            association.get("is_official")
            and not current.get("is_official")
        ):
            by_paper[key] = association

    rows = []

    for record in records:
        paper_path = (
            record.content.paper_url.path or ""
        )

        paper_id = paper_path.split("/")[-1]

        if "v" in paper_id:
            paper_id = paper_id.split("v")[0]

        association = by_paper.get(
            f"arxiv:{paper_id}",
            {},
        )

        rows.append(
            {
                "title": record.content.title,
                "authors": "; ".join(
                    record.content.authors
                ),
                "paperUrl": str(
                    record.content.paper_url
                ),
                "publishedDate": (
                    record.content.published_date.isoformat()
                ),
                "githubUrl": (
                    str(record.content.github_url)
                    if record.content.github_url
                    else ""
                ),
                "githubStars": (
                    record.content.github_stars
                    if record.content.github_stars
                    is not None
                    else ""
                ),
                "githubMetricsCollectedAt": (
                    record.content.github_metrics_collected_at.isoformat()
                    if record.content.github_metrics_collected_at
                    else ""
                ),
                "associationType": association.get(
                    "association_type",
                    "",
                ),
                "isOfficial": association.get(
                    "is_official",
                    "",
                ),
                "sourceName": (
                    record.source.name
                ),
                "sourceUrl": str(
                    record.source.url
                ),
                "collectedAt": (
                    record.collectedAt.isoformat()
                ),
            }
        )

    return rows, metadata



def build_jobs(
    phase2: Path,
) -> tuple[list[dict], dict]:
    records = [
        JobRecord.model_validate(row)
        for row in read_jsonl(
            phase2 / "jobs.jsonl"
        )
    ]

    provenance = [
        row
        for row in read_jsonl(
            phase2 / "provenance.jsonl"
        )
        if row["record_type"] == "JOB"
    ]

    by_url = {
        str(
            row.get("job_url", "")
        ).rstrip("/").casefold(): row
        for row in provenance
    }

    rows = []

    for record in records:
        evidence = by_url.get(
            str(
                record.source.url
            ).rstrip("/").casefold(),
            {},
        )

        rows.append(
            {
                "title": record.content.title,
                "company": (
                    record.content.company
                    if record.content.company
                    is not None
                    else ""
                ),
                "date": (
                    record.content.date.isoformat()
                ),
                "isRemote": (
                    record.content.isRemote
                    if record.content.isRemote
                    is not None
                    else ""
                ),
                "roleFamily": (
                    record.content.roleFamily or ""
                ),
                "sourceName": (
                    record.source.name
                ),
                "sourceUrl": str(
                    record.source.url
                ),
                "collectedAt": (
                    record.collectedAt.isoformat()
                ),
                "extractionStatus": (
                    record.content.extraction_status
                ),
                "companyRaw": evidence.get(
                    "company_raw",
                    "",
                ),
                "companyCanonical": (
                    evidence.get(
                        "company_canonical"
                    )
                    or ""
                ),
                "aiMatchTerms": ";".join(
                    evidence.get(
                        "ai_match_terms",
                        [],
                    )
                ),
                "contentSha256": evidence.get(
                    "content_sha256",
                    "",
                ),
            }
        )

    return rows, {}



def build_news(
    phase2: Path,
) -> tuple[list[dict], dict]:
    records = [
        NewsRecord.model_validate(row)
        for row in read_jsonl(
            phase2 / "news.jsonl"
        )
    ]

    rows = []

    for record in records:
        rows.append(
            {
                "title": record.content.title,
                "date": (
                    record.content.date.isoformat()
                ),
                "canonicalUrl": str(
                    record.content.canonical_url
                ),
                "sourceName": (
                    record.source.name
                ),
                "sourceUrl": str(
                    record.source.url
                ),
                "collectedAt": (
                    record.collectedAt.isoformat()
                ),
                "ingestedAt": (
                    record.content.ingested_at.isoformat()
                ),
                "extractionStatus": (
                    record.content.extraction_status
                ),
                "contentSha256": (
                    record.content.content_sha256
                ),
                "freshnessStatus": (
                    record.content.freshness_status
                ),
            }
        )

    return rows, {}



def build_mapping(
    path: Path,
) -> tuple[list[dict], dict]:
    rows = []

    for row in read_jsonl(path):
        record = EntityMappingRecord.model_validate(
            row
        )

        rows.append(
            {
                "rawName": record.raw_name,
                "normalizedKey": record.normalized_key,
                "canonicalEntityId": (
                    record.canonical_entity_id or ""
                ),
                "canonicalName": (
                    record.canonical_name or ""
                ),
                "status": record.status,
                "method": record.method or "",
                "recordType": record.record_type,
                "sourceUrl": str(
                    record.source_url
                ),
                "sourceTextSha256": (
                    record.source_text_sha256
                ),
                "ownershipVerificationStatus": (
                    record.ownership_verification_status
                ),
                "ownershipVerified": (
                    record.ownership_verified
                ),
                "evidence": record.evidence,
            }
        )

    return rows, {}



def validate(
    tables: dict[str, list[dict]],
) -> dict:
    errors: list[str] = []

    counts = {
        name: len(rows)
        for name, rows in tables.items()
    }

    if counts["Startups"] < 1000:
        errors.append(
            "Startups below 1,000."
        )

    if counts["Products"] < 1000:
        errors.append(
            "Products below 1,000."
        )

    if counts["Research Papers"] < 1000:
        errors.append(
            "Research Papers below 1,000."
        )

    if counts["Entity Mapping Log"] == 0:
        errors.append(
            "Entity Mapping Log is empty."
        )

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
            value = str(
                row.get(field, "")
            ).strip()

            if not value.startswith(
                ("https://", "http://")
            ):
                errors.append(
                    f"{tab}:{index} missing source URL."
                )

    for index, row in enumerate(
        tables["Research Papers"]
    ):
        has_stars = row["githubStars"] != ""
        has_url = bool(row["githubUrl"])
        has_observation = bool(
            row["githubMetricsCollectedAt"]
        )
        has_association = bool(
            row["associationType"]
        )

        if has_stars and not (
            has_url and has_observation
        ):
            errors.append(
                f"Research Papers:{index} stars without URL/observation."
            )

        if (
            has_url
            and has_association
            and not has_stars
        ):
            errors.append(
                f"Research Papers:{index} accepted association without GitHub stars."
            )

    for tab, identity in (
        ("News", "canonicalUrl"),
        ("Jobs", "sourceUrl"),
    ):
        seen: set[str] = set()

        for index, row in enumerate(
            tables[tab]
        ):
            key = str(
                row[identity]
            ).rstrip("/").casefold()

            if key in seen:
                errors.append(
                    f"{tab}:{index} duplicate canonical URL."
                )

            seen.add(key)

    for index, row in enumerate(
        tables["News"]
    ):
        ingested = parse_time(
            row["ingestedAt"]
        )

        published = parse_time(
            row["date"]
        )

        age = ingested - published

        if (
            age < -FUTURE_SKEW
            or age > FRESH_WINDOW
        ):
            errors.append(
                f"News:{index} outside 24h at ingestion."
            )

    for index, row in enumerate(
        tables["Jobs"]
    ):
        ingested = parse_time(
            row["collectedAt"]
        )

        published = parse_time(
            row["date"]
        )

        age = ingested - published

        if (
            age < -FUTURE_SKEW
            or age > FRESH_WINDOW
        ):
            errors.append(
                f"Jobs:{index} outside 24h at ingestion."
            )

    papers_with_stars = sum(
        1
        for row in tables["Research Papers"]
        if row["githubStars"] != ""
    )

    return {
        "tabs": TABS,
        "counts": counts,
        "papers_with_github_stars": papers_with_stars,
        "all_rows_have_source_url": not any(
            "missing source URL" in error
            for error in errors
        ),
        "all_exported_news_within_24_hours": not any(
            error.startswith("News:")
            for error in errors
        ),
        "all_exported_jobs_within_24_hours": not any(
            error.startswith("Jobs:")
            for error in errors
        ),
        "no_duplicate_canonical_urls": not any(
            "duplicate canonical URL" in error
            for error in errors
        ),
        "no_fabricated_star_fields": not any(
            "stars without" in error
            or "accepted association without GitHub stars"
            in error
            for error in errors
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
    delete_unexpected_tabs: bool = True,
) -> dict:
    try:
        import gspread
    except ImportError as exc:
        raise RuntimeError(
            "Install gspread and google-auth to upload: "
            "pip install gspread google-auth"
        ) from exc

    credential_data = json.loads(
        credentials_path.read_text(
            encoding="utf-8"
        )
    )

    if credential_data.get("type") == "service_account":
        client = gspread.service_account(
            filename=str(credentials_path)
        )
    else:
        authorized_user = credentials_path.with_name(
            "authorized_user.json"
        )

        client = gspread.oauth(
            credentials_filename=str(
                credentials_path
            ),
            authorized_user_filename=str(
                authorized_user
            ),
        )

    spreadsheet_id = resolve_spreadsheet_id(
        spreadsheet_key
    )

    if spreadsheet_id:
        spreadsheet = client.open_by_key(
            spreadsheet_id
        )

        if delete_unexpected_tabs:
            for existing in spreadsheet.worksheets():
                if existing.title not in TAB_SET:
                    spreadsheet.del_worksheet(
                        existing
                    )
    else:
        raise ValueError(
            "A service account cannot create a new spreadsheet "
            "in its own Drive because it has no Drive storage "
            "quota. Create a Google Sheet with a human Google "
            "account, share it with the service account as "
            "Editor, then provide --spreadsheet-key."
        )

    for tab in TABS:
        try:
            existing = spreadsheet.worksheet(tab)
            spreadsheet.del_worksheet(existing)
        except gspread.WorksheetNotFound:
            pass

        worksheet = spreadsheet.add_worksheet(
            title=tab,
            rows=max(
                len(tables[tab]) + 1,
                2,
            ),
            cols=len(HEADERS[tab]),
        )

        values = [HEADERS[tab]] + [
            [
                row.get(header, "")
                for header in HEADERS[tab]
            ]
            for row in tables[tab]
        ]

        worksheet.update(
            values=values,
            range_name="A1",
        )

    share_error = None
    public = False

    try:
        spreadsheet.share(
            email_address="",
            perm_type="anyone",
            role="reader",
            notify=False,
            with_link=True,
        )

        permissions = spreadsheet.list_permissions()

        public = any(
            permission.get("type") == "anyone"
            and permission.get("role") == "reader"
            for permission in permissions
        )

        if not public:
            raise RuntimeError(
                "Public reader permission was not verified."
            )

    except Exception as exc:
        share_error = str(exc)

    actual_tabs = [
        worksheet.title
        for worksheet in spreadsheet.worksheets()
    ]

    tabs_correct = set(actual_tabs) == TAB_SET

    return {
        "spreadsheet_id": spreadsheet.id,
        "url": (
            "https://docs.google.com/spreadsheets/d/"
            f"{spreadsheet.id}"
        ),
        "title": spreadsheet.title,
        "tabs": actual_tabs,
        "tabs_correct": tabs_correct,
        "public": public,
        "share_verified": public,
        "share_error": share_error,
        "uploaded_row_counts": {
            tab: len(tables[tab])
            for tab in TABS
        },
    }



def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Build, validate, and optionally publish "
            "the six-tab export."
        )
    )

    parser.add_argument(
        "--startups",
        type=Path,
        default=Path(
            "data/startups/ai-market-watch"
        ),
    )

    parser.add_argument(
        "--products",
        type=Path,
        default=Path(
            "data/products/merged"
        ),
    )

    parser.add_argument(
        "--papers",
        type=Path,
        default=Path(
            "data/papers/pwc"
        ),
    )

    parser.add_argument(
        "--phase2",
        type=Path,
        default=Path(
            "data/phase2/merged"
        ),
    )

    parser.add_argument(
        "--mapping",
        type=Path,
        default=Path(
            "data/phase4/entity_mapping.jsonl"
        ),
    )

    parser.add_argument(
        "--output",
        type=Path,
        default=Path(
            "data/exports"
        ),
    )

    parser.add_argument(
        "--upload",
        action="store_true",
    )

    parser.add_argument(
        "--credentials",
        type=Path,
    )

    parser.add_argument(
        "--spreadsheet-key",
        help=(
            "Existing Google Sheet ID or URL. "
            "The Sheet must be shared with the service "
            "account as Editor."
        ),
    )

    parser.add_argument(
        "--title",
        default=(
            "GraphOne / FrontierAtlas AI Intelligence"
        ),
    )

    parser.add_argument(
        "--keep-unexpected-tabs",
        action="store_true",
        help=(
            "Keep worksheets whose titles are not among the six "
            "required tabs. Unexpected tabs are deleted by default "
            "so the workbook contains exactly the six tabs."
        ),
    )

    args = parser.parse_args()

    startup_rows, startup_meta = build_startups(
        args.startups
    )

    product_rows, product_meta = build_products(
        args.products
    )

    paper_rows, paper_meta = build_papers(
        args.papers
    )

    job_rows, _ = build_jobs(
        args.phase2
    )

    news_rows, _ = build_news(
        args.phase2
    )

    mapping_rows, _ = build_mapping(
        args.mapping
    )

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
            args.output
            / f"{tab.lower().replace(' ', '_')}.csv",
            HEADERS[tab],
            tables[tab],
        )

    metadata = {
        "generated_at": datetime.now(
            timezone.utc
        ).isoformat(),
        "sources": {
            "startups_run": startup_meta.get(
                "run"
            ),
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
            raise SystemExit(
                "--upload requires --credentials."
            )

        if args.spreadsheet_key is None:
            raise SystemExit(
                "--upload requires --spreadsheet-key "
                "because a service account cannot create "
                "a new Drive-owned spreadsheet."
            )

        sheet = upload(
            tables,
            credentials_path=args.credentials,
            spreadsheet_key=args.spreadsheet_key,
            title=args.title,
            delete_unexpected_tabs=(
                args.delete_unexpected_tabs
            ),
        )

        metadata["google_sheet"] = sheet

        (args.output / "google_sheet.json").write_text(
            json.dumps(sheet, indent=2) + "\n",
            encoding="utf-8",
        )

    (args.output / "metadata.json").write_text(
        json.dumps(
            metadata,
            indent=2,
            ensure_ascii=False,
        ) + "\n",
        encoding="utf-8",
    )

    print(
        json.dumps(
            metadata,
            indent=2,
        )
    )

    if not validation["validation_passed"]:
        raise SystemExit(1)

    if args.upload:
        sheet = metadata["google_sheet"]

        if not sheet["public"]:
            raise SystemExit(
                "Google Sheet upload completed, but public "
                "reader access was not verified."
            )

        if not sheet["tabs_correct"]:
            raise SystemExit(
                "Google Sheet upload completed, but the "
                "workbook does not contain exactly the six "
                "required tabs."
            )


if __name__ == "__main__":
    main()