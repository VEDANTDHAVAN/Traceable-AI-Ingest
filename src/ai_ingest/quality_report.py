import argparse
import json
import sqlite3

from datetime import datetime, timedelta, timezone
from pathlib import Path

from ai_ingest.models import ResearchPaperRecord
from ai_ingest.repository_links import canonical_repository_url
from ai_ingest.storage.sqlite import paper_identity


def decision_matches(
    record: ResearchPaperRecord,
    decision: dict,
) -> bool:
    if decision.get("association_verified") is not True:
        return False

    if decision.get("association_status") != "ACCEPTED_BY_RULE":
        return False

    if decision.get("association_reason") != (
        "RECIPROCAL_ARXIV_METADATA_README_LINK_V1"
    ):
        return False

    try:
        github = decision["github"]

        observed_at = datetime.fromisoformat(
            github["github_metrics_collected_at"].replace(
                "Z", "+00:00"
            )
        )

        return (
            decision["paper_id"]
            == paper_identity(str(record.content.paper_url))
            and canonical_repository_url(
                github["repository_url"]
            )
            == canonical_repository_url(
                str(record.content.github_url)
            )
            and type(github["github_stars"]) is int
            and github["github_stars"]
            == record.content.github_stars
            and observed_at.tzinfo is not None
            and observed_at
            == record.content.github_metrics_collected_at
        )

    except (KeyError, TypeError, ValueError, AttributeError):
        return False


def assess_record(
    paper_id: str,
    record: ResearchPaperRecord,
    decisions: list[dict],
    *,
    now: datetime,
    max_metrics_age: timedelta,
) -> dict:
    identity_valid = (
        paper_identity(str(record.content.paper_url)) == paper_id
    )

    metrics_complete = (
        record.content.github_url is not None
        and record.content.github_stars is not None
        and record.content.github_metrics_collected_at is not None
        and canonical_repository_url(
            str(record.content.github_url)
        ) is not None
    )

    accepted_decision_matches = (
        metrics_complete
        and any(
            decision_matches(record, decision)
            for decision in decisions
        )
    )

    metrics_recent = False
    observed_at = record.content.github_metrics_collected_at

    if observed_at is not None:
        age = now - observed_at

        metrics_recent = (
            timedelta(0) <= age <= max_metrics_age
        )

    return {
        "identity_valid": identity_valid,
        "metrics_complete": metrics_complete,
        "accepted_decision_matches": accepted_decision_matches,
        "metrics_recent": metrics_recent,
        "enriched_with_applied_decision": (
            identity_valid
            and metrics_complete
            and accepted_decision_matches
        ),
    }


def build_report(
    database_path: Path,
    *,
    max_metrics_age_hours: float = 24,
) -> dict:
    if max_metrics_age_hours <= 0:
        raise ValueError(
            "max_metrics_age_hours must be positive."
        )

    now = datetime.now(timezone.utc)

    uri = database_path.resolve().as_uri() + "?mode=ro"

    connection = sqlite3.connect(
        uri,
        uri=True,
        timeout=30,
    )

    try:
        # Keep the reads within one database snapshot.
        connection.execute("BEGIN")

        paper_rows = connection.execute(
            "SELECT paper_id, record_json FROM papers"
        ).fetchall()

        tables = {
            row[0]
            for row in connection.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table'
                """
            )
        }

        decisions_by_paper = {}

        if {
            "association_decisions",
            "enrichment_imports",
        }.issubset(tables):
            for paper_id, decision_json in connection.execute(
                """
                SELECT d.paper_id, d.decision_json
                FROM association_decisions AS d
                INNER JOIN enrichment_imports AS i
                    ON i.import_id = d.import_id
                """
            ):
                decisions_by_paper.setdefault(
                    paper_id, []
                ).append(json.loads(decision_json))

        counts = {
            "stored_papers": len(paper_rows),
            "schema_valid_papers": 0,
            "identity_valid_papers": 0,
            "papers_with_complete_metrics": 0,
            "papers_with_matching_applied_decision": 0,
            "papers_with_recent_metrics": 0,
            "enriched_papers": 0,
            "enriched_papers_with_recent_metrics": 0,
        }

        failures = []

        for paper_id, record_json in paper_rows:
            try:
                record = (
                    ResearchPaperRecord.model_validate_json(
                        record_json
                    )
                )

                counts["schema_valid_papers"] += 1

                status = assess_record(
                    paper_id,
                    record,
                    decisions_by_paper.get(paper_id, []),
                    now=now,
                    max_metrics_age=timedelta(
                        hours=max_metrics_age_hours
                    ),
                )

                counts["identity_valid_papers"] += int(
                    status["identity_valid"]
                )

                counts["papers_with_complete_metrics"] += int(
                    status["metrics_complete"]
                )

                counts[
                    "papers_with_matching_applied_decision"
                ] += int(
                    status["accepted_decision_matches"]
                )

                counts["papers_with_recent_metrics"] += int(
                    status["metrics_recent"]
                )

                counts["enriched_papers"] += int(
                    status["enriched_with_applied_decision"]
                )

                counts[
                    "enriched_papers_with_recent_metrics"
                ] += int(
                    status["enriched_with_applied_decision"]
                    and status["metrics_recent"]
                )

                if not status["identity_valid"]:
                    failures.append(
                        {
                            "paper_id": paper_id,
                            "reason": "IDENTITY_MISMATCH",
                        }
                    )

            except (ValueError, TypeError, KeyError) as exc:
                failures.append(
                    {
                        "paper_id": paper_id,
                        "reason": "INVALID_RECORD",
                        "detail": str(exc),
                    }
                )

        target = 1000

        return {
            "generated_at": now.isoformat(),
            "database": str(database_path.resolve()),
            "metrics_age_policy_hours": max_metrics_age_hours,
            "research_papers": {
                **counts,
                "target": target,
                "remaining_enriched_papers": max(
                    0,
                    target - counts["enriched_papers"],
                ),
                "target_reached": (
                    counts["enriched_papers"] >= target
                ),
            },
            "record_failures": failures,
            "limitations": [
                (
                    "Matches canonical metrics to an accepted "
                    "decision from an applied import."
                ),
                (
                    "Does not rehash raw evidence files or rerun "
                    "association verification."
                ),
                (
                    "Does not establish that code is official "
                    "or scientifically correct."
                ),
                (
                    "Does not report startup, product, news, "
                    "or job progress."
                ),
            ],
        }

    finally:
        connection.rollback()
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Report canonical research-paper data quality."
    )

    parser.add_argument(
        "--database",
        type=Path,
        default=Path("data/arxiv/papers.sqlite3"),
    )

    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/reports/research-quality.json"),
    )

    parser.add_argument(
        "--max-metrics-age-hours",
        type=float,
        default=24,
    )

    args = parser.parse_args()

    try:
        report = build_report(
            args.database,
            max_metrics_age_hours=args.max_metrics_age_hours,
        )

        args.output.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        temporary = args.output.with_name(
            args.output.name + ".tmp"
        )

        temporary.write_text(
            json.dumps(report, indent=2),
            encoding="utf-8",
        )

        temporary.replace(args.output)

    except Exception as exc:
        parser.exit(
            status=1,
            message=f"Quality report failed: {exc}\n",
        )

    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()