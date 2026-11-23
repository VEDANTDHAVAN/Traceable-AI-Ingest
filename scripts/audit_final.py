"""Final end-to-end audit against the assessment's completion definition."""

from __future__ import annotations

import argparse
import json
import re

from pathlib import Path


TABS = {
    "Startups",
    "Products",
    "Research Papers",
    "Jobs",
    "News",
    "Entity Mapping Log",
}


def load(path: Path) -> dict:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def pdf_page_count(path: Path) -> int:
    if not path.is_file():
        return 0
    payload = path.read_bytes()
    return len(re.findall(rb"/Type /Page\b", payload))


def audit(*, root: Path, output: Path) -> dict:
    phase1 = load(root / "data/phase1/audit.json")
    phase2 = load(root / "data/phase2/audit.json")
    phase3 = load(root / "data/phase3/audit.json")
    phase4 = load(root / "data/phase4/audit.json")
    phase5 = load(root / "data/phase5/audit.json")
    export = load(root / "data/exports/metadata.json")
    sheet = load(root / "data/exports/google_sheet.json")

    counts1 = phase1.get("counts", {})
    counts2 = phase2.get("counts", {})
    gates3 = phase3.get("gates", {})
    counts4 = phase4.get("counts", {})

    pages = pdf_page_count(root / "architecture.pdf")
    validation = export.get("validation", {})
    csv_tabs = {
        path.stem.replace("_", " ").title()
        for path in (root / "data/exports").glob("*.csv")
    }

    sheet_tabs = set(sheet.get("tabs", []))
    sheet_public = sheet.get("public") is True
    sheet_created = bool(sheet.get("spreadsheet_id"))

    gates = {
        "phase1_audit_passed": phase1.get("phase1_complete") is True,
        "unique_startups_ge_1000": counts1.get("unique_startups", 0) >= 1000,
        "unique_products_ge_1000": counts1.get("unique_products", 0) >= 1000,
        "unique_research_papers_ge_1000": counts1.get("unique_research_papers", 0)
        >= 1000,
        "paper_repository_associations_ge_1000": counts1.get(
            "paper_repository_associations", 0
        )
        >= 1000,
        "github_metrics_present_for_accepted": counts1.get(
            "github_metrics_present_for_accepted"
        )
        is True,
        "phase2_audit_passed": phase2.get("phase2_complete") is True,
        "news_sources_queried_ge_5": counts2.get("news_sources_queried", 0) >= 5,
        "job_boards_queried_ge_5": counts2.get("job_boards_queried", 0) >= 5,
        "all_exported_news_within_24_hours": phase2.get("gates", {}).get(
            "all_exported_news_within_24_hours"
        )
        is True,
        "all_exported_jobs_within_24_hours": phase2.get("gates", {}).get(
            "all_exported_jobs_within_24_hours"
        )
        is True,
        "phase3_audit_passed": phase3.get("phase3_complete") is True,
        "all_non_null_extractions_grounded": gates3.get(
            "all_non_null_extractions_grounded"
        )
        is True,
        "provider_fallback_implemented": gates3.get(
            "provider_fallback_implemented"
        )
        is True,
        "chunking_implemented": gates3.get("chunking_implemented") is True,
        "rate_limit_backoff_implemented": gates3.get(
            "rate_limit_429_backoff_implemented"
        )
        is True,
        "phase4_audit_passed": phase4.get("phase4_complete") is True,
        "seed_entities_ge_50": counts4.get("seed_entities", 0) >= 50,
        "entity_mapping_log_complete": phase4.get("gates", {}).get(
            "mapping_log_complete"
        )
        is True,
        "no_unsupported_ownership_inference": phase4.get("gates", {}).get(
            "no_unsupported_ownership_inference"
        )
        is True,
        "phase5_audit_passed": phase5.get("phase5_complete") is True,
        "async_operation_implemented": phase5.get("gates", {}).get(
            "async_operation_implemented"
        )
        is True,
        "no_anti_bot_bypass_claims": phase5.get("gates", {}).get(
            "no_anti_bot_bypass_claims"
        )
        is True,
        "phase6_architecture_pdf_created": pages > 0,
        "architecture_pdf_page_count_le_3": 0 < pages <= 3,
        "google_sheet_created": sheet_created,
        "google_sheet_public": sheet_public,
        "sheet_has_six_required_tabs": sheet_created and sheet_tabs == TABS,
        "all_sheet_rows_schema_valid": validation.get("validation_passed") is True,
        "all_sheet_rows_source_backed": validation.get("all_rows_have_source_url")
        is True,
        "six_csv_exports_present": csv_tabs == TABS,
    }

    report = {
        "final_end_to_end_audit_passed": all(gates.values()),
        "gates": gates,
        "counts": {
            "unique_startups": counts1.get("unique_startups", 0),
            "unique_products": counts1.get("unique_products", 0),
            "unique_research_papers": counts1.get("unique_research_papers", 0),
            "paper_repository_associations": counts1.get(
                "paper_repository_associations", 0
            ),
            "news_sources_queried": counts2.get("news_sources_queried", 0),
            "job_boards_queried": counts2.get("job_boards_queried", 0),
            "seed_entities": counts4.get("seed_entities", 0),
            "entity_mapping_rows": counts4.get("mapping_rows", 0),
            "architecture_pdf_pages": pages,
            "export_counts": validation.get("counts", {}),
            "google_sheet_url": sheet.get("url"),
        },
        "failed_gates": sorted(name for name, value in gates.items() if not value),
        "distinction": {
            "source_backed": [
                "startups",
                "products",
                "research papers and associations",
                "news and jobs",
            ],
            "independently_verified": [
                "GitHub stars fetched live from the GitHub API for accepted "
                "associations",
            ],
            "implemented_behavior": [
                "async bounded acquisition",
                "24-hour freshness gate",
                "grounded multi-provider extraction",
                "deterministic entity resolution",
                "robots/retry/circuit-breaker controls",
            ],
            "designed_not_executed": [
                "distributed queue and workers",
                "500k-record load test",
                "managed storage partitioning and production alerting",
            ],
        },
    }

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Final end-to-end audit.")
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--output", type=Path, default=Path("data/final_audit.json"))
    args = parser.parse_args()

    report = audit(root=args.root, output=args.output)
    print(json.dumps(report, indent=2))

    if not report["final_end_to_end_audit_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
