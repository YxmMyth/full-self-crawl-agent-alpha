"""
HistoryDB — persistent PostgreSQL storage for agent exploration results.

Schema:
  runs    — one row per agent run (metadata, timing, status)
  records — arbitrary JSONB records collected by the agent (one row per item)

Multiple agent containers can write concurrently; an orchestrator or UI reads.
Falls back gracefully when DATABASE_URL is not configured.
"""

import hashlib
import json
import logging
import os
import socket
import uuid
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("tools.database")

_DDL = """
CREATE TABLE IF NOT EXISTS runs (
    id          TEXT PRIMARY KEY,
    start_url   TEXT NOT NULL,
    mode        TEXT,
    model       TEXT,
    requirement TEXT,
    agent_id    TEXT,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    status      TEXT NOT NULL DEFAULT 'running',
    record_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS records (
    id          BIGSERIAL PRIMARY KEY,
    run_id      TEXT REFERENCES runs(id),
    source_url  TEXT,
    data        JSONB NOT NULL,
    crawled_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS records_run_id_idx    ON records(run_id);
CREATE INDEX IF NOT EXISTS records_source_url_idx ON records(source_url);
CREATE INDEX IF NOT EXISTS records_crawled_at_idx ON records(crawled_at);
CREATE INDEX IF NOT EXISTS records_data_gin_idx   ON records USING GIN(data);

CREATE TABLE IF NOT EXISTS sections (
    id                   TEXT PRIMARY KEY,
    run_id               TEXT REFERENCES runs(id),
    url                  TEXT NOT NULL,
    title                TEXT,
    agent_type           TEXT,
    estimated_items      INTEGER,
    estimation_confidence TEXT,
    structure_explored   BOOLEAN DEFAULT FALSE,
    sampled              BOOLEAN DEFAULT FALSE,
    discovered_at        TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(run_id, url)
);

CREATE TABLE IF NOT EXISTS samples (
    id          BIGSERIAL PRIMARY KEY,
    section_id  TEXT REFERENCES sections(id),
    run_id      TEXT REFERENCES runs(id),
    data        JSONB NOT NULL,
    sampled_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS url_section (
    url         TEXT NOT NULL,
    section_id  TEXT REFERENCES sections(id),
    run_id      TEXT REFERENCES runs(id),
    PRIMARY KEY (url, section_id)
);

CREATE TABLE IF NOT EXISTS sessions (
    id                  TEXT PRIMARY KEY,
    run_id              TEXT REFERENCES runs(id),
    role                TEXT,
    assigned_url        TEXT,
    started_at          TIMESTAMPTZ DEFAULT NOW(),
    ended_at            TIMESTAMPTZ,
    outcome             TEXT,
    steps_taken         INTEGER,
    records_count       INTEGER DEFAULT 0,
    sections_found      INTEGER DEFAULT 0,
    trajectory_summary  TEXT
);

CREATE INDEX IF NOT EXISTS sections_run_id_idx ON sections(run_id);
CREATE INDEX IF NOT EXISTS sections_sampled_idx ON sections(run_id, sampled);
CREATE INDEX IF NOT EXISTS sessions_run_id_idx ON sessions(run_id);
"""


class HistoryDB:
    """Async PostgreSQL client for agent exploration history."""

    def __init__(self, dsn: str | None = None):
        self._dsn = dsn or os.environ.get("DATABASE_URL", "")
        self._pool = None
        self._available = bool(self._dsn)

    @property
    def available(self) -> bool:
        return self._available and self._pool is not None

    async def connect(self) -> bool:
        """Open connection pool and ensure schema exists. Returns True if successful."""
        if not self._dsn:
            logger.debug("DATABASE_URL not set — history DB disabled")
            return False
        try:
            import asyncpg
            self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=5,
                                                   command_timeout=10)
            async with self._pool.acquire() as conn:
                await conn.execute(_DDL)
            logger.info("HistoryDB connected and schema ready")
            return True
        except Exception as e:
            logger.warning(f"HistoryDB unavailable (continuing without history): {e}")
            self._pool = None
            self._available = False
            return False

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
            self._pool = None

    async def begin_run(self, run_id: str, start_url: str, mode: str,
                        model: str, requirement: str = "") -> None:
        """Record that a run has started."""
        if not self.available:
            return
        try:
            agent_id = socket.gethostname()
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO runs (id, start_url, mode, model, requirement, agent_id, status)
                    VALUES ($1, $2, $3, $4, $5, $6, 'running')
                    ON CONFLICT (id) DO NOTHING
                    """,
                    run_id, start_url, mode, model, requirement, agent_id,
                )
        except Exception as e:
            logger.warning(f"HistoryDB.begin_run failed: {e}")

    async def complete_run(self, run_id: str, success: bool,
                           record_count: int) -> None:
        """Update run status on completion."""
        if not self.available:
            return
        try:
            status = "success" if success else "failed"
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE runs
                    SET completed_at = NOW(), status = $1, record_count = $2
                    WHERE id = $3
                    """,
                    status, record_count, run_id,
                )
        except Exception as e:
            logger.warning(f"HistoryDB.complete_run failed: {e}")

    async def save_records(self, run_id: str,
                           records: list[dict[str, Any]]) -> int:
        """Append records to history. Returns number of rows inserted."""
        if not self.available or not records:
            return 0
        inserted = 0
        try:
            async with self._pool.acquire() as conn:
                for rec in records:
                    source_url = (
                        rec.get("url")
                        or rec.get("source_url")
                        or rec.get("link")
                        or rec.get("href")
                    )
                    await conn.execute(
                        """
                        INSERT INTO records (run_id, source_url, data)
                        VALUES ($1, $2, $3::jsonb)
                        """,
                        run_id,
                        source_url,
                        json.dumps(rec, ensure_ascii=False, default=str),
                    )
                    inserted += 1
            logger.info(f"HistoryDB: saved {inserted} records for run {run_id[:8]}")
        except Exception as e:
            logger.warning(f"HistoryDB.save_records failed: {e}")
        return inserted

    async def upsert_section(self, run_id: str, section_dict: dict) -> str | None:
        """Insert or update a discovered section. Returns section id."""
        if not self.available:
            return None
        url = section_dict.get("url", "")
        if not url:
            return None
        section_id = hashlib.sha1(f"{run_id}:{url}".encode()).hexdigest()[:16]
        try:
            async with self._pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO sections (id, run_id, url, title, agent_type, estimated_items, estimation_confidence)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    ON CONFLICT (run_id, url) DO UPDATE SET
                        title = EXCLUDED.title,
                        agent_type = EXCLUDED.agent_type,
                        estimated_items = EXCLUDED.estimated_items,
                        estimation_confidence = EXCLUDED.estimation_confidence
                """, section_id, run_id, url,
                    section_dict.get("title"), section_dict.get("agent_type"),
                    section_dict.get("estimated_items"), section_dict.get("estimation_confidence"))
            return section_id
        except Exception as e:
            logger.warning(f"HistoryDB.upsert_section failed: {e}")
            return None

    async def mark_section_sampled(self, run_id: str, url: str,
                                    sample_records: list[dict]) -> None:
        """Mark section sampled and persist up to 3 sample records."""
        if not self.available:
            return
        section_id = hashlib.sha1(f"{run_id}:{url}".encode()).hexdigest()[:16]
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "UPDATE sections SET sampled=TRUE WHERE run_id=$1 AND url=$2",
                    run_id, url)
                for rec in sample_records[:3]:
                    await conn.execute(
                        "INSERT INTO samples (section_id, run_id, data) VALUES ($1, $2, $3::jsonb)",
                        section_id, run_id,
                        json.dumps(rec, ensure_ascii=False, default=str))
        except Exception as e:
            logger.warning(f"HistoryDB.mark_section_sampled failed: {e}")

    async def mark_section_explored(self, run_id: str, url: str) -> None:
        if not self.available:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "UPDATE sections SET structure_explored=TRUE WHERE run_id=$1 AND url=$2",
                    run_id, url)
        except Exception as e:
            logger.warning(f"HistoryDB.mark_section_explored failed: {e}")

    async def get_unsampled_sections(self, run_id: str) -> list[dict]:
        if not self.available:
            return []
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT id, url, title, agent_type, estimated_items "
                    "FROM sections WHERE run_id=$1 AND sampled=FALSE ORDER BY discovered_at",
                    run_id)
                return [dict(r) for r in rows]
        except Exception as e:
            logger.warning(f"HistoryDB.get_unsampled_sections failed: {e}")
            return []

    async def get_all_sections(self, run_id: str) -> list[dict]:
        if not self.available:
            return []
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT id, url, title, agent_type, estimated_items, sampled "
                    "FROM sections WHERE run_id=$1 ORDER BY discovered_at",
                    run_id)
                return [dict(r) for r in rows]
        except Exception as e:
            logger.warning(f"HistoryDB.get_all_sections failed: {e}")
            return []

    async def add_session(self, run_id: str, role: str, url: str) -> str | None:
        if not self.available:
            return None
        session_id = str(uuid.uuid4())[:16]
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO sessions (id, run_id, role, assigned_url) VALUES ($1,$2,$3,$4)",
                    session_id, run_id, role, url)
            return session_id
        except Exception as e:
            logger.warning(f"HistoryDB.add_session failed: {e}")
            return None

    async def complete_session(self, session_id: str, outcome: str, steps: int,
                                records: int, sections: int, summary: str) -> None:
        if not self.available or not session_id:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute("""
                    UPDATE sessions SET ended_at=NOW(), outcome=$2, steps_taken=$3,
                        records_count=$4, sections_found=$5, trajectory_summary=$6
                    WHERE id=$1
                """, session_id, outcome, steps, records, sections, summary)
        except Exception as e:
            logger.warning(f"HistoryDB.complete_session failed: {e}")
