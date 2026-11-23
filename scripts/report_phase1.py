"""Summarize the executed Phase I acquisition, staging and merge."""

from __future__ import annotations

import argparse
import json

from pathlib import Path


def read(path: Path) -> dict:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def latest(root: Path, require: str | None = None) -> Path | None:
    if not root.is_dir():
        return None
    candidates = [
        path
        for path in root.iterdir()
        if path.is_dir() and path.name[:1].isdigit()
    ]
    if require:
        candidates = [path for path in candidates if (path / require).is_file()]
    return sorted(candidates)[-1] if candidates else None


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase I acquisition report.")
    parser.add_argument("--output", type=Path, default=Path("data/phase1/acquisition_report.json"))
    args = parser.parse_args()

    startup_run = latest(Path("data/startups/ai-market-watch"), "metadata.json")
    product_run = latest(Path("data/products/merged"), "metadata.json")
    paper_run = latest(Path("data/papers/pwc"), "metadata.json")

    startup = read(startup_run / "metadata.json") if startup_run else {}
    products = read(product_run / "metadata.json") if product_run else {}
    papers = read(paper_run / "metadata.json") if paper_run else {}

    product_sources = {}
    for name, snapshot in products.get("input_snapshots", {}).items():
        metadata = read(Path(snapshot) / "metadata.json")
        product_sources[name] = {
            "snapshot": snapshot,
            "input_rows": metadata.get(
                "input_rows",
                metadata.get("input_records", metadata.get("status")),
            ),
            "staged": metadata.get(
                "staged_products",
                metadata.get("staged_unique_products"),
            ),
            "duplicates": metadata.get(
                "duplicate_rows", metadata.get("duplicate_records")
            ),
            "quarantine": metadata.get(
                "quarantine_rows", metadata.get("quarantine_entries")
            ),
            "license": metadata.get("license"),
            "collected_at": metadata.get("collected_at"),
            "telemetry": (
                "NOT_INSTRUMENTED"
                if "wall_clock_seconds" not in metadata
                else "PRESENT"
            ),
        }

    report = {
        "schema_version": "1.0",
        "startups": {
            "run": startup_run.name if startup_run else None,
            "input_csv_rows": startup.get("input_rows"),
            "staged_unique_startup_candidates": startup.get(
                "staged_unique_startup_candidates"
            ),
            "duplicate_rows": startup.get("duplicate_rows"),
            "quarantine_entries": startup.get("quarantine_entries"),
            "employee_counts_present": False,
            "profile_pages_independently_validated": False,
        },
        "products": {
            "merge_run": product_run.name if product_run else None,
            "sources": product_sources,
            "input_records": products.get("input_records"),
            "merged_unique_products": products.get("merged_unique_products"),
            "duplicate_records": products.get("duplicate_records"),
            "cross_source_duplicates": products.get("cross_source_duplicates"),
            "products_with_multiple_sources": products.get(
                "merged_products_with_multiple_sources"
            ),
            "products_with_known_pricing": products.get(
                "products_with_known_pricing"
            ),
            "products_with_resolved_company": 0,
            "minimum_1000_candidates_reached": products.get(
                "minimum_1000_candidates_reached"
            ),
            "telemetry": (
                "PRODUCT_RUNS_NOT_INSTRUMENTED; merge is deterministic and "
                "local, so wall-clock/retry counters were not recorded"
            ),
        },
        "papers": {
            "run": paper_run.name if paper_run else None,
            "input_association_rows": papers.get("input_association_rows"),
            "unique_associations": papers.get("unique_associations"),
            "unique_papers": papers.get("unique_papers"),
            "accepted_associations": papers.get("accepted_associations"),
            "official_associations": papers.get("official_associations"),
            "community_associations": papers.get("community_associations"),
            "repositories_with_metrics": papers.get("repositories_with_metrics"),
            "github_metrics_present_for_accepted": papers.get(
                "github_metrics_present_for_accepted"
            ),
            "quarantine_rows": papers.get("quarantine_rows"),
            "concurrency": papers.get("concurrency"),
            "github_concurrency": papers.get("github_concurrency"),
            "wall_clock_seconds": papers.get("wall_clock_seconds"),
            "records_per_second": papers.get("records_per_second"),
            "resumable": papers.get("resumable"),
        },
        "gates": {
            "unique_startups_ge_1000": (
                startup.get("staged_unique_startup_candidates", 0) >= 1000
            ),
            "unique_products_ge_1000": (
                products.get("merged_unique_products", 0) >= 1000
            ),
            "unique_papers_ge_1000": papers.get("unique_papers", 0) >= 1000,
            "associations_ge_1000": (
                papers.get("accepted_associations", 0) >= 1000
            ),
        },
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
