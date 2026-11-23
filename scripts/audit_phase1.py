"""Phase I completion audit.

Reads the final staged/merged outputs and verifies every gate in the
assessment's completion definition. Exits non-zero if any gate fails.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from ai_ingest.models import ProductRecord, ResearchPaperRecord, StartupRecord


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


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def audit(
    *,
    startups_snapshot: Path,
    products_merged: Path,
    papers_run: Path,
    output: Path,
) -> dict:
    report: dict = {
        "phase1_complete": False,
        "gates": {},
        "counts": {},
        "errors": [],
    }

    def fail(message: str) -> None:
        report["errors"].append(message)

    # --- Startups ---
    startup_records = []
    startup_provenance = []
    startup_duplicates = []
    startup_quarantine = []
    startup_metadata = {}

    try:
        startup_metadata = json.loads(
            (startups_snapshot / "metadata.json").read_text(encoding="utf-8")
        )
        startup_records = [
            StartupRecord.model_validate(row)
            for row in read_jsonl(startups_snapshot / "startups.jsonl")
        ]
        startup_provenance = read_jsonl(
            startups_snapshot / "provenance.jsonl"
        )
        startup_duplicates = read_jsonl(
            startups_snapshot / "duplicates.jsonl"
        )
        startup_quarantine = read_jsonl(
            startups_snapshot / "quarantine.jsonl"
        )

        if len(startup_records) != len(startup_provenance):
            fail("Startup/provenance count mismatch.")

        if len(startup_records) != startup_metadata.get(
            "staged_unique_startup_candidates"
        ):
            fail("Startup count differs from metadata.")

        if len(startup_duplicates) != startup_metadata.get("duplicate_rows"):
            fail("Startup duplicate count differs from metadata.")

        if len(startup_quarantine) != startup_metadata.get(
            "quarantine_entries"
        ):
            fail("Startup quarantine count differs from metadata.")

        raw_csv = startups_snapshot / "ai-startups.csv"
        readme = startups_snapshot / "README.md"
        if sha256_file(raw_csv) != startup_metadata.get("dataset_sha256"):
            fail("Startup dataset hash mismatch.")
        if sha256_file(readme) != startup_metadata.get("readme_sha256"):
            fail("Startup README hash mismatch.")

        seen_profiles = set()
        seen_names = set()
        for index, (record, evidence) in enumerate(
            zip(startup_records, startup_provenance)
        ):
            if record.source.url is None:
                fail(f"Startup {index}: missing source URL.")
            if not evidence.get("attribution"):
                fail(f"Startup {index}: missing provenance attribution.")
            if not evidence.get("raw_sha256"):
                fail(f"Startup {index}: missing raw hash.")
            profile = evidence.get("source_record_id")
            if profile in seen_profiles:
                fail(f"Startup {index}: duplicate profile identity.")
            seen_profiles.add(profile)
            name_key = evidence.get("normalized_name_key")
            if name_key in seen_names:
                fail(f"Startup {index}: duplicate normalized name.")
            seen_names.add(name_key)

        collected_at = startup_metadata.get("collected_at")
        if not collected_at or "T" not in collected_at:
            fail("Startup collection timestamp missing or naive.")
        else:
            from datetime import datetime

            parsed = datetime.fromisoformat(collected_at.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                fail("Startup collection timestamp has no timezone.")

    except Exception as exc:
        fail(f"Startup audit error: {exc}")

    # --- Products ---
    product_records = []
    product_provenance = []
    product_duplicates = []
    product_metadata = {}

    try:
        product_metadata = json.loads(
            (products_merged / "metadata.json").read_text(encoding="utf-8")
        )
        product_records = [
            ProductRecord.model_validate(row)
            for row in read_jsonl(products_merged / "products.jsonl")
        ]
        product_provenance = read_jsonl(
            products_merged / "provenance.jsonl"
        )
        product_duplicates = read_jsonl(
            products_merged / "duplicates.jsonl"
        )

        if len(product_records) != len(product_provenance):
            fail("Product/provenance count mismatch.")

        seen_urls = set()
        seen_names = set()
        for index, (record, evidence) in enumerate(
            zip(product_records, product_provenance)
        ):
            if record.source.url is None:
                fail(f"Product {index}: missing source URL.")
            if not evidence.get("sources"):
                fail(f"Product {index}: missing provenance sources.")
            for source in evidence["sources"]:
                if not source.get("raw_sha256"):
                    fail(f"Product {index}: missing raw hash.")
                if not source.get("source_record_id"):
                    fail(f"Product {index}: missing source record id.")
            url_key = str(record.source.url).rstrip("/").casefold()
            if url_key in seen_urls:
                fail(f"Product {index}: duplicate normalized URL.")
            seen_urls.add(url_key)
            name_key = record.content.productName.casefold().strip()
            if name_key in seen_names:
                fail(f"Product {index}: duplicate normalized name.")
            seen_names.add(name_key)

    except Exception as exc:
        fail(f"Product audit error: {exc}")

    # --- Papers ---
    paper_records = []
    paper_associations = []
    paper_provenance = []
    paper_quarantine = []
    paper_metadata = {}

    try:
        paper_metadata = json.loads(
            (papers_run / "metadata.json").read_text(encoding="utf-8")
        )
        paper_records = [
            ResearchPaperRecord.model_validate(row)
            for row in read_jsonl(papers_run / "papers.jsonl")
        ]
        paper_associations = read_jsonl(
            papers_run / "associations.jsonl"
        )
        paper_provenance = read_jsonl(
            papers_run / "provenance.jsonl"
        )
        paper_quarantine = read_jsonl(
            papers_run / "quarantine.jsonl"
        )

        if len(paper_records) != len(paper_provenance):
            fail("Paper/provenance count mismatch.")

        seen_paper_urls = set()
        for index, (record, evidence) in enumerate(
            zip(paper_records, paper_provenance)
        ):
            if record.source.url is None:
                fail(f"Paper {index}: missing source URL.")
            if not evidence.get("source_backed", False):
                fail(f"Paper {index}: not marked source-backed.")
            if record.content.github_stars is not None:
                if record.content.github_url is None:
                    fail(f"Paper {index}: stars without github_url.")
                if record.content.github_metrics_collected_at is None:
                    fail(f"Paper {index}: stars without observation time.")
            url_key = str(record.content.paper_url)
            if url_key in seen_paper_urls:
                fail(f"Paper {index}: duplicate paper URL.")
            seen_paper_urls.add(url_key)

        # Association gate: every accepted association must have stars.
        associations_with_stars = sum(
            1
            for a in paper_associations
            if a.get("github_stars") is not None
        )
        if associations_with_stars != len(paper_associations):
            fail(
                "Not every accepted association has GitHub stars: "
                f"{associations_with_stars}/{len(paper_associations)}."
            )

        if not paper_metadata.get("github_metrics_present_for_accepted"):
            fail("Metadata reports GitHub metrics not present for accepted.")

    except Exception as exc:
        fail(f"Paper audit error: {exc}")

    # --- Counts ---
    unique_startups = len(startup_records)
    unique_products = len(product_records)
    unique_papers = len(paper_records)
    paper_repository_associations = len(paper_associations)
    github_metrics_present = (
        paper_repository_associations > 0
        and all(
            a.get("github_stars") is not None
            for a in paper_associations
        )
    )

    report["counts"] = {
        "unique_startups": unique_startups,
        "unique_products": unique_products,
        "unique_research_papers": unique_papers,
        "paper_repository_associations": paper_repository_associations,
        "github_metrics_present_for_accepted": github_metrics_present,
        "startup_duplicates": len(startup_duplicates),
        "startup_quarantine": len(startup_quarantine),
        "product_duplicates": len(product_duplicates),
        "paper_quarantine": len(paper_quarantine),
    }

    # --- Gates ---
    gates = {
        "unique_startups_ge_1000": unique_startups >= 1000,
        "unique_products_ge_1000": unique_products >= 1000,
        "unique_research_papers_ge_1000": unique_papers >= 1000,
        "paper_repository_associations_ge_1000": (
            paper_repository_associations >= 1000
        ),
        "github_metrics_present_for_accepted": github_metrics_present,
        "all_exported_records_have_valid_source_url": len(report["errors"]) == 0
        or all(
            "missing source URL" not in e for e in report["errors"]
        ),
        "all_exported_records_have_provenance": len(report["errors"]) == 0
        or all("provenance" not in e for e in report["errors"]),
        "schema_validation_passed": len(report["errors"]) == 0
        or all("audit error" not in e for e in report["errors"]),
        "source_hash_validation_passed": len(report["errors"]) == 0
        or all("hash mismatch" not in e.lower() for e in report["errors"]),
        "deduplication_audit_passed": len(report["errors"]) == 0
        or all("duplicate" not in e.lower() for e in report["errors"]),
    }
    report["gates"] = gates

    required = [
        "unique_startups_ge_1000",
        "unique_products_ge_1000",
        "unique_research_papers_ge_1000",
        "paper_repository_associations_ge_1000",
        "github_metrics_present_for_accepted",
    ]
    if all(gates[g] for g in required) and not report["errors"]:
        report["phase1_complete"] = True

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase I completion audit.")
    parser.add_argument("--startups", type=Path, required=True)
    parser.add_argument("--products", type=Path, required=True)
    parser.add_argument("--papers", type=Path, required=True)
    parser.add_argument(
        "--output", type=Path, default=Path("data/phase1/audit.json")
    )
    args = parser.parse_args()

    report = audit(
        startups_snapshot=args.startups,
        products_merged=args.products,
        papers_run=args.papers,
        output=args.output,
    )

    print(json.dumps(report, indent=2))

    if not report["phase1_complete"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
