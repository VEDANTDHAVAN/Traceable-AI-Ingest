import argparse
import hashlib
import json
import sqlite3

from datetime import datetime, timezone
from pathlib import Path

from ai_ingest.models import ResearchPaperRecord
from ai_ingest.repository_links import canonical_repository_url
from ai_ingest.storage.sqlite import paper_identity


ACCEPTED_RULE = (
    "RECIPROCAL_ARXIV_METADATA_README_LINK_V1"
)


def verify_evidence(evidence: dict) -> None:
    path = Path(evidence["raw_path"])
    expected = evidence["raw_sha256"]

    if not path.is_file():
        raise ValueError(f"Evidence file is missing: {path}")

    actual = hashlib.sha256(path.read_bytes()).hexdigest()

    if actual != expected:
        raise ValueError(f"Evidence hash mismatch: {path}")


def build_enriched_record(
    current: ResearchPaperRecord,
    row: dict,
) -> ResearchPaperRecord:
    if row["association_verified"] is not True:
        raise ValueError("Association is not accepted.")

    if row["association_status"] != "ACCEPTED_BY_RULE":
        raise ValueError("Unexpected association status.")

    if row["association_reason"] != ACCEPTED_RULE:
        raise ValueError("Unsupported association rule.")

    if paper_identity(
        str(current.content.paper_url)
    ) != row["paper_id"]:
        raise ValueError("Paper identity mismatch.")

    github = row["github"]

    candidate_url = canonical_repository_url(
        row["candidate_repository_url"]
    )

    repository_url = canonical_repository_url(
        github["repository_url"]
    )

    if candidate_url is None or repository_url != candidate_url:
        raise ValueError("Candidate and repository URLs disagree.")

    payload = current.model_dump(mode="json")

    payload["content"]["github_url"] = repository_url
    payload["content"]["github_stars"] = github["github_stars"]
    payload["content"]["github_metrics_collected_at"] = (
        github["github_metrics_collected_at"]
    )

    enriched = ResearchPaperRecord.model_validate(payload)

    if current.content.github_url is not None:
        previous_repository = canonical_repository_url(
            str(current.content.github_url)
        )

        if previous_repository != repository_url:
            raise ValueError(
                "Paper already has a different repository."
            )

    previous_time = current.content.github_metrics_collected_at
    incoming_time = enriched.content.github_metrics_collected_at

    if previous_time is not None and (
        incoming_time is None or incoming_time < previous_time
    ):
        raise ValueError(
            "Refusing to overwrite metrics with an older observation."
        )

    return enriched


def import_enrichments(
    *,
    input_path: Path,
    database_path: Path,
    apply: bool = False,
) -> dict:
    if not database_path.is_file():
        raise ValueError(
            "Database does not exist. Check the database path."
        )

    raw_input = input_path.read_bytes()
    import_id = hashlib.sha256(raw_input).hexdigest()

    rows = [
        json.loads(line)
        for line in raw_input.decode("utf-8").splitlines()
        if line.strip()
    ]

    pairs = [
        (row["paper_id"], row["candidate_repository_url"])
        for row in rows
    ]

    if len(set(pairs)) != len(pairs):
        raise ValueError("Duplicate association rows in input.")

    connection = sqlite3.connect(
        database_path,
        timeout=30,
    )

    try:
        # Creating the audit tables is harmless on subsequent runs.
        # Dry runs may create these empty tables, but never commit
        # paper updates, decisions, or import receipts.
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS enrichment_imports (
                import_id TEXT PRIMARY KEY,
                imported_at TEXT NOT NULL,
                input_path TEXT NOT NULL,
                accepted_count INTEGER NOT NULL,
                unresolved_count INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS association_decisions (
                import_id TEXT NOT NULL,
                row_number INTEGER NOT NULL,
                paper_id TEXT NOT NULL,
                decision_json TEXT NOT NULL,
                PRIMARY KEY (import_id, row_number)
            );
            """
        )

        connection.execute("BEGIN IMMEDIATE")

        previous_import = connection.execute(
            """
            SELECT 1
            FROM enrichment_imports
            WHERE import_id = ?
            """,
            (import_id,),
        ).fetchone()

        if previous_import is not None:
            connection.rollback()

            return {
                "mode": "apply" if apply else "dry_run",
                "already_applied": True,
                "papers_updated": 0,
                "import_id": import_id,
            }

        accepted_count = 0
        unresolved_count = 0
        accepted_papers = set()

        for index, row in enumerate(rows):
            paper_id = row["paper_id"]

            stored = connection.execute(
                """
                SELECT record_json
                FROM papers
                WHERE paper_id = ?
                """,
                (paper_id,),
            ).fetchone()

            if stored is None:
                raise ValueError(
                    f"Paper not found in canonical database: {paper_id}"
                )

            if row["association_verified"] is True:
                if paper_id in accepted_papers:
                    raise ValueError(
                        "Multiple accepted repositories for one paper."
                    )

                accepted_papers.add(paper_id)

                verify_evidence(row["github"])
                verify_evidence(row["readme_evidence"])

                paper_evidence = row.get(
                    "paper_link_evidence",
                    [],
                )

                if not paper_evidence:
                    raise ValueError(
                        "Accepted association lacks paper evidence."
                    )

                for evidence in paper_evidence:
                    verify_evidence(evidence)

                current = (
                    ResearchPaperRecord.model_validate_json(
                        stored[0]
                    )
                )

                enriched = build_enriched_record(
                    current,
                    row,
                )

                connection.execute(
                    """
                    UPDATE papers
                    SET record_json = ?
                    WHERE paper_id = ?
                    """,
                    (
                        enriched.model_dump_json(),
                        paper_id,
                    ),
                )

                accepted_count += 1

            elif row["association_verified"] is False:
                unresolved_count += 1

            else:
                raise ValueError(
                    "association_verified must be a JSON boolean."
                )

            connection.execute(
                """
                INSERT INTO association_decisions (
                    import_id,
                    row_number,
                    paper_id,
                    decision_json
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    import_id,
                    index,
                    paper_id,
                    json.dumps(row, ensure_ascii=False),
                ),
            )

        connection.execute(
            """
            INSERT INTO enrichment_imports (
                import_id,
                imported_at,
                input_path,
                accepted_count,
                unresolved_count
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                import_id,
                datetime.now(timezone.utc).isoformat(),
                str(input_path.resolve()),
                accepted_count,
                unresolved_count,
            ),
        )

        if apply:
            connection.commit()
        else:
            connection.rollback()

        return {
            "mode": "apply" if apply else "dry_run",
            "already_applied": False,
            "accepted_associations": accepted_count,
            "unresolved_associations": unresolved_count,
            "papers_updated": accepted_count if apply else 0,
            "papers_would_update": accepted_count,
            "import_id": import_id,
        }

    except BaseException:
        connection.rollback()
        raise

    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Import accepted repository enrichments. "
            "Defaults to a dry run."
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
        help="Commit updates after validation.",
    )

    args = parser.parse_args()

    try:
        result = import_enrichments(
            input_path=args.input,
            database_path=args.database,
            apply=args.apply,
        )
    except Exception as exc:
        parser.exit(
            status=1,
            message=f"Enrichment import failed: {exc}\n",
        )

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()