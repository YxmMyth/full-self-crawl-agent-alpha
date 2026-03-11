"""
Unit tests for new HistoryDB tables and methods (sections, samples, url_section, sessions).
Uses AsyncMock to mock asyncpg pool — no real DB needed.
"""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from src.tools.database import HistoryDB, _DDL


class TestDDLContents:
    def test_ddl_contains_sections_table(self):
        assert "CREATE TABLE IF NOT EXISTS sections" in _DDL

    def test_ddl_contains_samples_table(self):
        assert "CREATE TABLE IF NOT EXISTS samples" in _DDL

    def test_ddl_contains_url_section_table(self):
        assert "CREATE TABLE IF NOT EXISTS url_section" in _DDL

    def test_ddl_contains_sessions_table(self):
        assert "CREATE TABLE IF NOT EXISTS sessions" in _DDL

    def test_ddl_contains_sections_indexes(self):
        assert "sections_run_id_idx" in _DDL
        assert "sections_sampled_idx" in _DDL

    def test_ddl_contains_sessions_index(self):
        assert "sessions_run_id_idx" in _DDL


class TestHistoryDBNewMethods:
    """Test new HistoryDB methods using mocked asyncpg pool."""

    def _make_db_with_pool(self):
        db = HistoryDB.__new__(HistoryDB)
        db._dsn = "postgresql://test"
        db._available = True
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock(return_value=None)
        mock_conn.fetch = AsyncMock(return_value=[])
        mock_pool = MagicMock()
        mock_pool.acquire = MagicMock(return_value=_AsyncContextManager(mock_conn))
        db._pool = mock_pool
        return db, mock_conn

    @pytest.mark.asyncio
    async def test_upsert_section_calls_execute(self):
        db, conn = self._make_db_with_pool()
        section = {"url": "https://example.com/tag/threejs", "title": "ThreeJS", "agent_type": "listing", "estimated_items": 50}
        result = await db.upsert_section("run-1", section)
        assert result is not None
        assert len(result) == 16  # sha1 hex truncated to 16
        conn.execute.assert_called_once()
        call_args = conn.execute.call_args
        assert "INSERT INTO sections" in call_args[0][0]
        assert "ON CONFLICT" in call_args[0][0]

    @pytest.mark.asyncio
    async def test_upsert_section_returns_none_for_missing_url(self):
        db, conn = self._make_db_with_pool()
        result = await db.upsert_section("run-1", {"title": "No URL"})
        assert result is None
        conn.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_mark_section_sampled_calls_update_and_insert(self):
        db, conn = self._make_db_with_pool()
        samples = [{"title": "Pen A", "code": "x = 1"}, {"title": "Pen B", "code": "y = 2"}]
        await db.mark_section_sampled("run-1", "https://example.com/tag/threejs", samples)
        # Should call UPDATE sections + INSERT samples for each record
        assert conn.execute.call_count == 1 + len(samples)
        first_call = conn.execute.call_args_list[0]
        assert "UPDATE sections SET sampled=TRUE" in first_call[0][0]

    @pytest.mark.asyncio
    async def test_mark_section_sampled_caps_at_3(self):
        db, conn = self._make_db_with_pool()
        samples = [{"title": f"Pen {i}"} for i in range(10)]
        await db.mark_section_sampled("run-1", "https://example.com/page", samples)
        # 1 UPDATE + 3 INSERTs (capped at 3)
        assert conn.execute.call_count == 4

    @pytest.mark.asyncio
    async def test_get_unsampled_sections_uses_sampled_filter(self):
        db, conn = self._make_db_with_pool()
        conn.fetch = AsyncMock(return_value=[])
        result = await db.get_unsampled_sections("run-1")
        assert result == []
        conn.fetch.assert_called_once()
        query = conn.fetch.call_args[0][0]
        assert "sampled=FALSE" in query

    @pytest.mark.asyncio
    async def test_get_all_sections_returns_all(self):
        db, conn = self._make_db_with_pool()
        mock_row = {"id": "abc123", "url": "https://example.com/tag/threejs",
                    "title": "ThreeJS", "agent_type": "listing",
                    "estimated_items": 50, "sampled": True}
        conn.fetch = AsyncMock(return_value=[mock_row])
        result = await db.get_all_sections("run-1")
        assert len(result) == 1
        assert result[0]["url"] == "https://example.com/tag/threejs"

    @pytest.mark.asyncio
    async def test_add_session_returns_id(self):
        db, conn = self._make_db_with_pool()
        session_id = await db.add_session("run-1", "extractor", "https://example.com/pen/abc")
        assert session_id is not None
        assert len(session_id) == 16
        conn.execute.assert_called_once()
        assert "INSERT INTO sessions" in conn.execute.call_args[0][0]

    @pytest.mark.asyncio
    async def test_complete_session_updates_row(self):
        db, conn = self._make_db_with_pool()
        await db.complete_session("sess-1", "success", 5, 3, 0, "Extracted 3 records")
        conn.execute.assert_called_once()
        assert "UPDATE sessions" in conn.execute.call_args[0][0]

    @pytest.mark.asyncio
    async def test_noop_when_unavailable(self):
        db = HistoryDB.__new__(HistoryDB)
        db._dsn = ""
        db._pool = None
        db._available = False
        # All methods should return safely (no exception, no-op)
        assert await db.upsert_section("r", {"url": "x"}) is None
        await db.mark_section_sampled("r", "x", [])
        await db.mark_section_explored("r", "x")
        assert await db.get_unsampled_sections("r") == []
        assert await db.get_all_sections("r") == []
        assert await db.add_session("r", "extractor", "x") is None
        await db.complete_session("s", "success", 0, 0, 0, "")


class _AsyncContextManager:
    """Helper: async context manager that yields a mock connection."""
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *args):
        pass
