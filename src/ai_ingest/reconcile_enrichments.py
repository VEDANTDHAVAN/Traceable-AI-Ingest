import argparse
import json
import sqlite3

from collections import defaultdict
from pathlib import Path

from ai_ingest.import_enrichments import (
    build_enriched_record,
    import_enrichments,
)
from ai_ingest.models import ResearchPaperRecord
from ai_ingest.repository_links import canonical_repository_url


def reconciliation_report(
    current_records: dict[str, ResearchPaperRecord],
    decisions: list[dict],
) -> dict:
    grouped = defaultdict(list)
    seen = set()

    for row in decisions:
        key = (
            row["paper_id"],
            row["candidate_repository_url"],
        )

        if key in seen:
            raise ValueError("Duplicate association decision.")

        seen.add(key)

        if type(row["association_verified"]) is not bool:
            raise ValueError(
                "association_verified must be a JSON boolean."
            )

        grouped[row["paper_id"]].append(row)

    new_enrichments = 0
    existing_enrichments_reconfirmed = 0
    unresolved_without_existing_enrichment = 0
    blockers = []

    for paper_id, rows in grouped.items():
        current = current_records.get(paper_id)

        if current is None:
            blockers.append(
                {
                    "paper_id": paper_id,
                    "reason": "PAPER_NOT_IN_DATABASE",
                }
            )
            continue

        accepted = [
            row
            for row in rows
            if row["association_verified"] is True
        ]

        if len(accepted) > 1:
            blockers.append(
                {
                    "paper_id": paper_id,
                    "reason": "MULTIPLE_ACCEPTED_ASSOCIATIONS",
                }
            )
            continue

        has_existing_enrichment = (
            current.content.github_url is not None
            and current.content.github_stars is not None
        )

        if not accepted:
            if has_existing_enrichment:
                blockers.append(
                    {
                        "paper_id": paper_id,
                        "reason": (
                            "EXISTING_ENRICHMENT_NOT_RECONFIRMED"
                        ),
                        "existing_repository": str(
                            current.content.github_url
                        ),
                        "latest_reasons": sorted(
                            {
                                row["association_reason"]
                                for row in rows
                            }
                        ),
                    }
                )
            else:
                unresolved_without_existing_enrichment += 1

            continue

        try:
            # Reuse the importer's identity, schema, repository,
            # and observation-time checks.
            enriched = build_enriched_record(
                current,
                accepted[0],
            )

        except ValueError as exc:
            blockers.append(
                {
                    "paper_id": paper_id,
                    "reason": "ENRICHMENT_VALIDATION_FAILED",
                    "detail": str(exc),
                }
            )
            continue

        if has_existing_enrichment:
            existing_url = canonical_repository_url(
                str(current.content.github_url)
            )

            incoming_url = canonical_repository_url(
                str(enriched.content.github_url)
            )

            if existing_url != incoming_url:
                blockers.append(
                    {
                        "paper_id": paper_id,
                        "reason": "REPOSITORY_CONFLICT",
                    }
                )
                continue

            existing_enrichments_reconfirmed += 1
        else:
            new_enrichments += 1

    existing_outside_scope = sum(
        record.content.github_url is not None
        and record.content.github_stars is not None
        and paper_id not in grouped
        for paper_id, record in current_records.items()
    )

    return {
        "candidate_pairs": len(decisions),
        "papers_in_decision_scope": len(grouped),
        "new_enrichments": new_enrichments,
        "existing_enrichments_reconfirmed": (
            existing_enrichments_reconfirmed
        ),
        "unresolved_papers_without_existing_enrichment": (
            unresolved_without_existing_enrichment
        ),
        "existing_enriched_papers_outside_scope": (
            existing_outside_scope
        ),
        "safe_to_apply": not blockers,
        "blocker_count": len(blockers),
        "blockers": blockers,
    }


def load_current_records(
    database_path: Path,
) -> dict[str, ResearchPaperRecord]:
    uri = database_path.resolve().as_uri() + "?mode=ro"

    connection = sqlite3.connect(
        uri,
        uri=True,
        timeout=30,
    )

    try:
        return {
            paper_id: ResearchPaperRecord.model_validate_json(
                record_json
            )
            for paper_id, record_json in connection.execute(
                "SELECT paper_id, record_json FROM papers"
            )
        }
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Reconcile association decisions with canonical papers "
            "before optionally applying enrichments."
        )
    )

    parser.add_argument(
        "--input",
        required=True,
        type=Path,
    )

    parser.add_argument(
        "--database",
        type=Path,
        default=Path("data/arxiv/papers.sqlite3"),
    )

    parser.add_argument(
        "--apply",
        action="store_true",
    )

    args = parser.parse_args()

    try:
        # Read once for the comparison.
        with args.input.open(encoding="utf-8") as handle:
            decisions = [
                json.loads(line)
                for line in handle
                if line.strip()
            ]

        report = reconciliation_report(
            load_current_records(args.database),
            decisions,
        )

        result = {"reconciliation": report}

        if args.apply:
            if not report["safe_to_apply"]:
                print(json.dumps(result, indent=2))
                parser.exit(
                    status=1,
                    message=(
                        "Import blocked. Resolve the reported "
                        "canonical-association conflicts first.\n"
                    ),
                )

            result["import"] = import_enrichments(
                input_path=args.input,
                database_path=args.database,
                apply=True,
            )

    except Exception as exc:
        parser.exit(
            status=1,
            message=f"Reconciliation failed: {exc}\n",
        )

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()