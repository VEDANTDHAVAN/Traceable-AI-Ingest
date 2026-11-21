import hashlib
import json
import re
import sqlite3

from pathlib import Path
from urllib.parse import urlparse

from ai_ingest.models import ResearchPaperRecord


MODERN_ID = re.compile(r"^\d{4}\.\d{4,5}$")
LEGACY_ID = re.compile(r"^[a-zA-Z][a-zA-Z0-9.-]*/\d{7}$")
VERSION_SUFFIX = re.compile(r"v\d+$")


def paper_identity(paper_url: str) -> str:
    """Return an arXiv paper identity independent of its version."""
    parsed = urlparse(paper_url)

    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Expected an HTTP or HTTPS arXiv URL.")

    if parsed.hostname not in {"arxiv.org", "www.arxiv.org"}:
        raise ValueError("Expected an arxiv.org URL.")

    if not parsed.path.startswith("/abs/"):
        raise ValueError("Expected an arXiv abstract URL.")

    identifier = parsed.path.removeprefix("/abs/")
    identifier = VERSION_SUFFIX.sub("", identifier)

    if not (
        MODERN_ID.fullmatch(identifier)
        or LEGACY_ID.fullmatch(identifier)
    ):
        raise ValueError(f"Invalid arXiv identifier: {identifier}")

    return f"arxiv:{identifier}"


def crawl_identity(config: dict) -> str:
    """Create a stable identity from an explicit crawl configuration."""
    serialized = json.dumps(
        config,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )

    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class PaperStore:
    """Single-machine persistence; not a distributed work queue."""

    def __init__(self, database_path: Path) -> None:
        database_path.parent.mkdir(parents=True, exist_ok=True)

        self.connection = sqlite3.connect(
            database_path,
            timeout=30,
        )

        self.connection.execute("PRAGMA foreign_keys = ON")

        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS papers (
                paper_id TEXT PRIMARY KEY,
                record_json TEXT NOT NULL,
                evidence_path TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS crawls (
                crawl_id TEXT PRIMARY KEY,
                config_json TEXT NOT NULL,
                next_start INTEGER NOT NULL DEFAULT 0
                    CHECK (next_start >= 0)
            );

            CREATE TABLE IF NOT EXISTS pages (
                crawl_id TEXT NOT NULL,
                start_offset INTEGER NOT NULL,
                returned_count INTEGER NOT NULL,
                evidence_path TEXT NOT NULL,
                PRIMARY KEY (crawl_id, start_offset),
                FOREIGN KEY (crawl_id)
                    REFERENCES crawls(crawl_id)
            );
            """
        )

    def close(self) -> None:
        self.connection.close()

    def create_crawl(self, config: dict) -> str:
        crawl_id = crawl_identity(config)

        with self.connection:
            self.connection.execute(
                """
                INSERT INTO crawls (
                    crawl_id,
                    config_json,
                    next_start
                )
                VALUES (?, ?, 0)
                ON CONFLICT(crawl_id) DO NOTHING
                """,
                (
                    crawl_id,
                    json.dumps(config, sort_keys=True),
                ),
            )

        return crawl_id

    def next_start(self, crawl_id: str) -> int:
        row = self.connection.execute(
            """
            SELECT next_start
            FROM crawls
            WHERE crawl_id = ?
            """,
            (crawl_id,),
        ).fetchone()

        if row is None:
            raise ValueError("Unknown crawl ID.")

        return int(row[0])

    def paper_count(self) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) FROM papers"
        ).fetchone()

        return int(row[0])

    def commit_page(
        self,
        *,
        crawl_id: str,
        start: int,
        records: list[ResearchPaperRecord],
        evidence_path: Path,
    ) -> bool:
        """
        Commit a sequential page and advance its checkpoint.

        Returns False if this page was already committed.
        """
        if start < 0:
            raise ValueError("start cannot be negative.")

        if not evidence_path.is_file():
            raise ValueError("Raw evidence file does not exist.")

        # Validate/serialize before opening a write transaction.
        rows = [
            (
                paper_identity(str(record.content.paper_url)),
                record.model_dump_json(),
                str(evidence_path.resolve()),
            )
            for record in records
        ]

        try:
            self.connection.execute("BEGIN IMMEDIATE")

            checkpoint = self.next_start(crawl_id)

            previous_page = self.connection.execute(
                """
                SELECT 1
                FROM pages
                WHERE crawl_id = ? AND start_offset = ?
                """,
                (crawl_id, start),
            ).fetchone()

            if previous_page is not None:
                self.connection.rollback()
                return False

            if start != checkpoint:
                raise ValueError(
                    f"Expected page offset {checkpoint}, got {start}."
                )

            # Preserve the first observed record for each paper.
            # Version-history storage will be added separately.
            self.connection.executemany(
                """
                INSERT INTO papers (
                    paper_id,
                    record_json,
                    evidence_path
                )
                VALUES (?, ?, ?)
                ON CONFLICT(paper_id) DO NOTHING
                """,
                rows,
            )

            self.connection.execute(
                """
                INSERT INTO pages (
                    crawl_id,
                    start_offset,
                    returned_count,
                    evidence_path
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    crawl_id,
                    start,
                    len(records),
                    str(evidence_path.resolve()),
                ),
            )

            # Advance by returned entries, not unique insertions.
            self.connection.execute(
                """
                UPDATE crawls
                SET next_start = ?
                WHERE crawl_id = ?
                """,
                (start + len(records), crawl_id),
            )

            self.connection.commit()
            return True

        except BaseException:
            self.connection.rollback()
            raise