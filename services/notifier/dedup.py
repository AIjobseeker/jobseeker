"""SQLite-backed dedup store for scored jobs.

Keys are sha256(company.lower() + "|" + source_id) — UUIDs are NOT used because
the scrapers regenerate them on every run, so the same posting would otherwise
trigger duplicate notifications forever.

Single-writer (the notifier process), but the WAL journal makes reads from
other processes (e.g. healthcheck.py) safe and lock-free.
"""
from __future__ import annotations

import hashlib
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger("notifier.dedup")


SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_jobs (
    key TEXT PRIMARY KEY,
    first_seen_at TEXT NOT NULL,
    company TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    score REAL NOT NULL,
    notified INTEGER DEFAULT 0,
    status TEXT DEFAULT 'NEW',
    status_updated_at TEXT,
    telegram_message_id INTEGER,
    -- Artifact tracking (resume, cover letter, study guide produced via Ollama)
    tailor_status TEXT DEFAULT 'NONE',     -- NONE | TAILORING | DONE | FAILED
    tailored_at TEXT,
    resume_path TEXT,                       -- local: <dedup_id>/resume.docx OR minio: bucket/key OR https://...
    cover_letter_path TEXT,
    study_guide TEXT,                       -- markdown content, small enough to inline
    resume_url TEXT,                        -- presigned URL when available
    cover_letter_url TEXT
);
CREATE INDEX IF NOT EXISTS idx_company ON seen_jobs(company);
CREATE INDEX IF NOT EXISTS idx_first_seen ON seen_jobs(first_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_status ON seen_jobs(status);
CREATE INDEX IF NOT EXISTS idx_tailor_status ON seen_jobs(tailor_status);
"""

# Migration shim — adds the columns to existing dbs that pre-date this schema.
MIGRATIONS = [
    "ALTER TABLE seen_jobs ADD COLUMN status TEXT DEFAULT 'NEW'",
    "ALTER TABLE seen_jobs ADD COLUMN status_updated_at TEXT",
    "ALTER TABLE seen_jobs ADD COLUMN telegram_message_id INTEGER",
    "ALTER TABLE seen_jobs ADD COLUMN tailor_status TEXT DEFAULT 'NONE'",
    "ALTER TABLE seen_jobs ADD COLUMN tailored_at TEXT",
    "ALTER TABLE seen_jobs ADD COLUMN resume_path TEXT",
    "ALTER TABLE seen_jobs ADD COLUMN cover_letter_path TEXT",
    "ALTER TABLE seen_jobs ADD COLUMN study_guide TEXT",
    "ALTER TABLE seen_jobs ADD COLUMN resume_url TEXT",
    "ALTER TABLE seen_jobs ADD COLUMN cover_letter_url TEXT",
]


# Status values used in inline-button callbacks. Keep short & uppercase so
# they read well in CSV exports.
STATUS_NEW = "NEW"
STATUS_APPLIED = "APPLIED"
STATUS_SKIPPED = "SKIPPED"
STATUS_INTERVIEW = "INTERVIEW"
STATUS_OFFER = "OFFER"
STATUS_REJECTED = "REJECTED"

# Tailor status values
TAILOR_NONE = "NONE"
TAILOR_RUNNING = "TAILORING"
TAILOR_DONE = "DONE"
TAILOR_FAILED = "FAILED"


def compute_key(company: str, source_id: str) -> str:
    raw = f"{company.lower()}|{source_id}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class DedupStore:
    """Thin SQLite wrapper. Synchronous on purpose — sqlite is fast enough
    for this workload (thousands of inserts/min) and avoids the complexity
    of an async driver.
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None
        self._open()

    def _open(self) -> None:
        # check_same_thread=False because asyncio may dispatch us from a
        # thread executor; we serialize ourselves through a single conn.
        self._conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            isolation_level=None,  # autocommit; we manage transactions explicitly
        )
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._conn.execute("PRAGMA busy_timeout=5000;")
        # Order matters on a pre-existing db that's missing the newer columns:
        #   1. CREATE TABLE IF NOT EXISTS (idempotent — old shape is left alone)
        #   2. Run additive migrations (ADD COLUMN) so newer cols exist
        #   3. THEN create indexes — some reference cols added in step 2
        statements = [s.strip() for s in SCHEMA.strip().split(";") if s.strip()]
        creates_table = [s for s in statements if s.upper().startswith("CREATE TABLE")]
        creates_index = [s for s in statements if s.upper().startswith("CREATE INDEX")]

        for stmt in creates_table:
            self._conn.execute(stmt)
        for stmt in MIGRATIONS:
            try:
                self._conn.execute(stmt)
            except sqlite3.OperationalError:
                # Column already exists — fine.
                pass
        for stmt in creates_index:
            try:
                self._conn.execute(stmt)
            except sqlite3.OperationalError:
                # Column for the index doesn't exist (shouldn't happen after
                # migrations) — log and continue. Indexes are non-critical.
                pass

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._open()
        assert self._conn is not None
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "DedupStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def has_seen(self, key: str) -> bool:
        cur = self.conn.execute("SELECT 1 FROM seen_jobs WHERE key = ?", (key,))
        return cur.fetchone() is not None

    def insert_if_new(
        self,
        key: str,
        company: str,
        title: str,
        url: str,
        score: float,
    ) -> bool:
        """Atomic check-and-insert. Returns True if inserted (new), False if dup."""
        try:
            self.conn.execute(
                """
                INSERT INTO seen_jobs (key, first_seen_at, company, title, url, score)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (key, _utcnow_iso(), company, title, url, score),
            )
            return True
        except sqlite3.IntegrityError:
            return False

    def mark_notified(self, key: str, message_id: Optional[int] = None) -> None:
        if message_id is not None:
            self.conn.execute(
                "UPDATE seen_jobs SET notified = 1, telegram_message_id = ? WHERE key = ?",
                (message_id, key),
            )
        else:
            self.conn.execute(
                "UPDATE seen_jobs SET notified = 1 WHERE key = ?",
                (key,),
            )

    def update_status(self, key: str, status: str) -> bool:
        """Update status by key. Returns True if a row was updated."""
        cur = self.conn.execute(
            "UPDATE seen_jobs SET status = ?, status_updated_at = ? WHERE key = ?",
            (status, _utcnow_iso(), key),
        )
        return cur.rowcount > 0

    def get_by_dedup_id_short(self, short_id: str) -> Optional[tuple]:
        """Lookup a row by the 16-char dedup_id used in Telegram callbacks.

        We use sha256[:16] in callbacks because Telegram caps callback_data
        at 64 bytes total. Doing a LIKE prefix match on the full sha256 key.
        """
        if len(short_id) != 16:
            return None
        cur = self.conn.execute(
            """
            SELECT key, company, title, url, status
            FROM seen_jobs
            WHERE substr(key, 1, 16) = ?
            LIMIT 1
            """,
            (short_id,),
        )
        return cur.fetchone()

    def record_artifacts(
        self,
        key: str,
        *,
        resume_path: Optional[str] = None,
        cover_letter_path: Optional[str] = None,
        study_guide: Optional[str] = None,
        resume_url: Optional[str] = None,
        cover_letter_url: Optional[str] = None,
        tailor_status: str = "DONE",
    ) -> None:
        """Persist tailored artifacts (resume DOCX path, cover letter, etc.)
        against the dedup row. Idempotent — running it twice overwrites with
        the latest values."""
        self.conn.execute(
            """
            UPDATE seen_jobs
            SET tailor_status = ?,
                tailored_at = ?,
                resume_path = COALESCE(?, resume_path),
                cover_letter_path = COALESCE(?, cover_letter_path),
                study_guide = COALESCE(?, study_guide),
                resume_url = COALESCE(?, resume_url),
                cover_letter_url = COALESCE(?, cover_letter_url)
            WHERE key = ?
            """,
            (
                tailor_status,
                _utcnow_iso(),
                resume_path,
                cover_letter_path,
                study_guide,
                resume_url,
                cover_letter_url,
                key,
            ),
        )

    def get_artifacts(self, short_id: str) -> Optional[dict]:
        """Read all tailored artifact metadata for a dedup_id."""
        if len(short_id) != 16:
            return None
        cur = self.conn.execute(
            """
            SELECT key, company, title, url, score, status,
                   tailor_status, tailored_at,
                   resume_path, cover_letter_path, study_guide,
                   resume_url, cover_letter_url, telegram_message_id
            FROM seen_jobs
            WHERE substr(key, 1, 16) = ?
            LIMIT 1
            """,
            (short_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [
            "key", "company", "title", "url", "score", "status",
            "tailor_status", "tailored_at",
            "resume_path", "cover_letter_path", "study_guide",
            "resume_url", "cover_letter_url", "telegram_message_id",
        ]
        return dict(zip(cols, row))

    def count(self) -> int:
        cur = self.conn.execute("SELECT COUNT(*) FROM seen_jobs")
        row = cur.fetchone()
        return int(row[0]) if row else 0

    def recent(self, limit: int = 10) -> list[tuple]:
        cur = self.conn.execute(
            """
            SELECT first_seen_at, company, title, score, notified, url
            FROM seen_jobs
            ORDER BY first_seen_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        return list(cur.fetchall())
