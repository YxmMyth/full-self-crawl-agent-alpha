"""
HistoryDB — persistent PostgreSQL storage for agent exploration results.

Schema:
  runs    — one row per agent run (metadata, timing, status)
  records — arbitrary JSONB records collected by the agent (one row per item)

Multiple agent containers can write concurrently; an orchestrator or UI reads.
Falls back gracefully when DATABASE_URL is not configured.
"""

import json
import logging
import os
import socket
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
