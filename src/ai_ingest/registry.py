"""Persistent processed-item registry for cross-run/cross-node deduplication."""

from __future__ import annotations

import sqlite3

from pathlib import Path


class ProcessedRegistry:
    """Idempotency ledger keyed by canonical URL and content hash.

    SQLite is the single-node implementation.  The same schema maps directly to
    a partitioned table in the production design (see architecture.pdf).
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=30)
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS processed (
                identity_kind TEXT NOT NULL
                    CHECK (identity_kind IN ('URL', 'CONTENT')),
                identity TEXT NOT NULL,
                record_type TEXT NOT NULL
                    CHECK (record_type IN ('NEWS', 'JOB')),
                first_seen_at TEXT NOT NULL,
                run_id TEXT NOT NULL,
                PRIMARY KEY (identity_kind, identity, record_type)
            );
            """
        )

    def close(self) -> None:
        self.connection.close()

    def seen(self, *, kind: str, identity: str, record_type: str) -> bool:
        row = self.connection.execute(
            """
            SELECT 1 FROM processed
            WHERE identity_kind = ? AND identity = ? AND record_type = ?
            """,
            (kind, identity, record_type),
        ).fetchone()
        return row is not None

    def claim(
        self,
        *,
        kind: str,
        identity: str,
        record_type: str,
        run_id: str,
        first_seen_at: str,
    ) -> bool:
        """Atomically claim an identity. False means another node won."""
        cursor = self.connection.execute(
            """
            INSERT INTO processed (
                identity_kind, identity, record_type, first_seen_at, run_id
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(identity_kind, identity, record_type) DO NOTHING
            """,
            (kind, identity, record_type, first_seen_at, run_id),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def count(self) -> int:
        row = self.connection.execute("SELECT COUNT(*) FROM processed").fetchone()
        return int(row[0])
