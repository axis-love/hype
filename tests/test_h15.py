"""H15: engine story vs Telegram post (flow_001174)."""
from __future__ import annotations

import re
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from newsbot.api import create_api_app
from newsbot.db import NewsStore
from newsbot.main import _recap_input_items
from newsbot.summarizer import FILTER_SYSTEM
from tests.helpers import scored_story
from tests.test_h4_api import NOW, _get_client, _make_app, _seed_store, _story


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _v9_schema_sql() -> str:
    return """
            CREATE TABLE schema_version(
              version INTEGER PRIMARY KEY, description TEXT, applied_at TEXT
            );
            INSERT INTO schema_version VALUES
              (1, 'Initial schema', '2026-01-01T00:00:00+00:00'),
              (2, 'Drop unused tables', '2026-01-01T00:00:00+00:00'),
              (3, 'Score columns', '2026-01-01T00:00:00+00:00'),
              (4, 'Raw-story store', '2026-01-01T00:00:00+00:00'),
              (5, 'message_id', '2026-01-01T00:00:00+00:00'),
              (6, 'origin_topic', '2026-01-01T00:00:00+00:00'),
              (7, 'deliveries', '2026-01-01T00:00:00+00:00'),
              (8, 'external_ref', '2026-01-01T00:00:00+00:00'),
              (9, 'styled_title', '2026-01-01T00:00:00+00:00');

            CREATE TABLE pending_posts(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              title TEXT NOT NULL, body TEXT NOT NULL,
              category TEXT, importance INTEGER, url TEXT,
              created_at TEXT NOT NULL, posted_at TEXT,
              merge_count INTEGER NOT NULL DEFAULT 1,
              merged_urls TEXT, snippet TEXT, source_name TEXT,
              raw_json TEXT, styled_at TEXT, message_id INTEGER,
              origin_topic TEXT, styled_title TEXT,
              source TEXT, published_at TEXT, upvotes INTEGER,
              comments INTEGER, stars INTEGER, reposts INTEGER,
              crosspost_count INTEGER, penalty REAL, lookback_hours REAL,
              score_at_queue REAL, engagement_score REAL,
              recency_at_queue REAL, source_weight REAL,
              topic_bonus REAL, crosspost_bonus REAL,
              matched_topics TEXT, scored_at TEXT
            );
            CREATE INDEX ix_pending_posts_posted ON pending_posts(posted_at);

            CREATE TABLE deliveries(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              post_id INTEGER NOT NULL REFERENCES pending_posts(id),
              channel TEXT NOT NULL,
              delivered_at TEXT NOT NULL,
              message_id INTEGER,
              external_ref TEXT,
              UNIQUE(post_id, channel)
            );
            CREATE TABLE seen(
              url TEXT PRIMARY KEY, title TEXT, first_seen_at TEXT NOT NULL
            );
            """


class TestH15Schema:
    def test_fresh_db_pending_posts_and_deliveries(self, tmp_path: Path) -> None:
        store = NewsStore(tmp_path / "fresh.sqlite")
        try:
            pp = _cols(store._conn, "pending_posts")
            dd = _cols(store._conn, "deliveries")
            assert "summary" in pp
            for gone in ("body", "styled_title", "styled_at", "posted_at", "message_id"):
                assert gone not in pp
            assert "styled_title" in dd
            assert "styled_body" in dd
            assert store.schema_version() == 10
        finally:
            store.close()


class TestH15Migration:
    def test_v9_styled_posted_row_copies_onto_delivery(self, tmp_path: Path) -> None:
        db_path = tmp_path / "v9.sqlite"
        conn = sqlite3.connect(str(db_path))
        conn.executescript(_v9_schema_sql())
        conn.execute(
            "INSERT INTO pending_posts("
            "title, body, url, created_at, posted_at, styled_at, styled_title, message_id"
            ") VALUES(?,?,?,?,?,?,?,?)",
            (
                "English headline",
                "Русское тело",
                "https://ex.example/1",
                "2026-09-13T10:00:00+00:00",
                "2026-09-13T11:00:00+00:00",
                "2026-09-13T10:59:00+00:00",
                "Русский заголовок",
                4242,
            ),
        )
        conn.execute(
            "INSERT INTO deliveries(post_id, channel, delivered_at, message_id) "
            "VALUES(1, 'telegram', '2026-09-13T11:00:00+00:00', 4242)"
        )
        conn.commit()
        conn.close()

        store = NewsStore(db_path)
        try:
            assert store.schema_version() == 10
            pp = store._conn.execute(
                "SELECT title, summary FROM pending_posts WHERE id=1"
            ).fetchone()
            assert pp["title"] == "English headline"
            d = store._conn.execute(
                "SELECT styled_title, styled_body, message_id FROM deliveries "
                "WHERE post_id=1 AND channel='telegram'"
            ).fetchone()
            assert d["styled_title"] == "Русский заголовок"
            assert d["styled_body"] == "Русское тело"
            assert d["message_id"] == 4242
            gone = _cols(store._conn, "pending_posts")
            for name in ("body", "styled_title", "styled_at", "posted_at", "message_id"):
                assert name not in gone

            store2 = NewsStore(db_path)
            try:
                assert store2.schema_version() == 10
                n = store2._conn.execute(
                    "SELECT COUNT(*) AS n FROM schema_version WHERE version=10"
                ).fetchone()["n"]
                assert n == 1
            finally:
                store2.close()
        finally:
            store.close()

    def test_sqlite_older_than_3_35_raises_before_change(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path = tmp_path / "old.sqlite"
        conn = sqlite3.connect(str(db_path))
        conn.executescript(_v9_schema_sql())
        conn.commit()
        conn.close()

        monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 34, 1))
        with pytest.raises(RuntimeError, match="3.35"):
            NewsStore(db_path)

        check = sqlite3.connect(str(db_path))
        try:
            pp = _cols(check, "pending_posts")
            assert "body" in pp
            assert "summary" not in pp
            v = check.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()[0]
            assert v == 9
        finally:
            check.close()


class TestH15SummaryPersistence:
    def test_add_stories_persists_short_summary(self, tmp_path: Path) -> None:
        store = NewsStore(tmp_path / "sum.sqlite")
        try:
            story = scored_story("A Story", 80.0)
            story["short_summary"] = "One factual English sentence."
            store.add_stories_to_store([story], [])
            row = store._conn.execute(
                "SELECT summary, title FROM pending_posts"
            ).fetchone()
            assert row["title"] == "A Story"
            assert row["summary"] == "One factual English sentence."
        finally:
            store.close()

    def test_merge_keeps_stored_summary_fills_null(self, tmp_path: Path) -> None:
        store = NewsStore(tmp_path / "merge.sqlite")
        try:
            first = scored_story("Merge Me", 80.0)
            first["url"] = "https://example.com/merge-me"
            store.add_stories_to_store([first], [])
            rid = store._conn.execute("SELECT id FROM pending_posts").fetchone()["id"]
            cand = scored_story("Merge Me", 90.0)
            cand["url"] = "https://other.example/merge-me"
            cand["short_summary"] = "Filled from candidate."
            store.merge_into_store_row(rid, cand, cand["url"])
            row = store._conn.execute(
                "SELECT summary FROM pending_posts WHERE id=?", (rid,)
            ).fetchone()
            assert row["summary"] == "Filled from candidate."

            cand2 = scored_story("Merge Me", 95.0)
            cand2["short_summary"] = "Must not overwrite."
            store.merge_into_store_row(rid, cand2, "https://third.example/x")
            row = store._conn.execute(
                "SELECT summary FROM pending_posts WHERE id=?", (rid,)
            ).fetchone()
            assert row["summary"] == "Filled from candidate."
        finally:
            store.close()


class TestH15LeakGuard:
    @pytest.mark.asyncio
    async def test_items_never_leak_telegram_copy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from newsbot import api as api_module

        monkeypatch.setattr(api_module, "_now", lambda: NOW)
        store = NewsStore(tmp_path / "leak.sqlite")
        try:
            story = _story(
                title="English headline",
                url="https://example.com/en",
                origin_topic="gaming",
                matched_topics=["gaming"],
            )
            story["short_summary"] = "English one-liner."
            story["score_breakdown"]["origin_topic"] = "gaming"
            story["score_breakdown"]["matched_topics"] = ["gaming"]
            _seed_store(store, [story])
            rid = store._conn.execute("SELECT id FROM pending_posts").fetchone()["id"]
            store._conn.execute(
                "UPDATE pending_posts SET summary=? WHERE id=?",
                ("English one-liner.", rid),
            )
            store.mark_posted(
                rid,
                message_id=99,
                styled_title="Русский заголовок",
                styled_body="Русское тело поста",
            )
            app = _make_app(store)
            client = await _get_client(app)
            try:
                resp = await client.get(
                    "/api/v1/items",
                    headers={"Authorization": "Bearer secret-key"},
                )
                assert resp.status == 200
                data = await resp.json()
            finally:
                await client.close()
            assert data["items"]
            item = data["items"][0]
            assert item["title"] == "English headline"
            assert item["summary"] == "English one-liner."
            blob = str(data)
            assert "Русский" not in blob
            assert "Русское" not in blob
            for key in ("styled_title", "styled_body", "body", "posted_at", "message_id"):
                assert key not in item
        finally:
            store.close()

    def test_api_py_grep_gate(self) -> None:
        result = subprocess.run(
            ["grep", "-nE", r"styled|\bbody\b|posted_at|message_id", "newsbot/api.py"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.stdout.strip() == "", result.stdout


class TestH15Recap:
    def test_recap_uses_delivery_styled_or_falls_back(self, tmp_path: Path) -> None:
        store = NewsStore(tmp_path / "recap.sqlite")
        try:
            styled = scored_story("English A", 80.0)
            store.add_stories_to_store([styled], [])
            rid_a = store._conn.execute(
                "SELECT id FROM pending_posts WHERE title=?", ("English A",)
            ).fetchone()["id"]
            store.mark_posted(
                rid_a,
                message_id=11,
                styled_title="Русский A",
                styled_body="Тело A",
            )

            legacy = scored_story("English B", 70.0)
            legacy["short_summary"] = "Legacy summary."
            store.add_stories_to_store([legacy], [])
            rid_b = store._conn.execute(
                "SELECT id FROM pending_posts WHERE title=?", ("English B",)
            ).fetchone()["id"]
            store.mark_posted(rid_b, message_id=12)

            rows = store.list_posted_since("telegram", "1970-01-01T00:00:00+00:00")
            items = _recap_input_items(rows)
            by_title = {i["title"]: i for i in items}
            assert by_title["Русский A"]["body"] == "Тело A"
            assert by_title["Русский A"]["message_id"] == 11
            assert by_title["English B"]["body"] == "Legacy summary."
            assert by_title["English B"]["message_id"] == 12
        finally:
            store.close()


class TestH15PassAPrompt:
    def test_filter_system_pins_english(self) -> None:
        assert "MUST be in English" in FILTER_SYSTEM
        assert "short_summary is one factual sentence" in FILTER_SYSTEM


class TestH15Docs:
    def test_readme_documents_summary_not_snippet_as_llm(self) -> None:
        text = Path("README.md").read_text()
        assert "one-line summary from the LLM filter pass" not in text
        assert re.search(r'"summary"', text)
        shape = text[text.index("### Item JSON shape"):]
        assert "summary" in shape
