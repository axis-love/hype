"""SQLite-backed news bot storage.

Three tables:
  - news_items: raw fetched candidates (with engagement signals)
  - seen:       URLs/titles already delivered to Telegram (dedup state)
  - news_digests: posted digests (history)

WAL mode, autocommit, single connection per process — same pattern as
core/settings_store.py. No cross-process coordination needed (cron runs
one-shot), but WAL keeps reads cheap if an admin inspects the DB.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional


log = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class NewsStore:
    """CRUD wrapper for news-bot tables."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(db_path),
            check_same_thread=False,
            isolation_level=None,  # autocommit
        )
        self._conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        cur = self._conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL;")
        cur.execute("PRAGMA synchronous=NORMAL;")
        cur.execute("PRAGMA busy_timeout=5000;")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS news_items(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              source TEXT NOT NULL,
              source_name TEXT NOT NULL,
              title TEXT NOT NULL,
              url TEXT,
              snippet TEXT,
              published_at TEXT,
              fetched_at TEXT NOT NULL,
              upvotes INTEGER,
              comments INTEGER,
              stars INTEGER,
              forks INTEGER,
              reposts INTEGER,
              upvote_ratio REAL,
              score REAL DEFAULT 0,
              category TEXT,
              raw_json TEXT
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS seen(
              url TEXT PRIMARY KEY,
              title TEXT,
              first_seen_at TEXT NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS news_digests(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              created_at TEXT NOT NULL,
              digest_text TEXT NOT NULL,
              model_used TEXT,
              item_count INTEGER
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS ix_news_items_fetched_at ON news_items(fetched_at);")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_news_items_score ON news_items(score DESC);")
        cur.close()

    # --- news_items -----------------------------------------------------

    def insert_items(self, items: list[dict[str, Any]]) -> int:
        """Insert candidates into news_items. Returns the count inserted.

        Dedups on (source, source_name, title, url) — re-fetched items
        update their engagement fields rather than duplicating.
        """
        if not items:
            return 0

        fetched_at = _utc_now_iso()
        persisted = 0
        for item in items:
            source = str(item.get("source") or "").strip()
            source_name = str(item.get("source_name") or "").strip()
            title = str(item.get("title") or "").strip()
            if not source or not source_name or not title:
                continue

            url = str(item.get("url") or "").strip() or None
            raw_json = json.dumps(item.get("raw_json") or None, ensure_ascii=False, default=str)

            existing = self._conn.execute(
                """
                SELECT id FROM news_items
                WHERE source=? AND source_name=? AND title=? AND COALESCE(url,'')=?
                LIMIT 1
                """,
                (source, source_name, title, url or ""),
            ).fetchone()

            if existing is None:
                self._conn.execute(
                    """
                    INSERT INTO news_items(
                      source, source_name, title, url, snippet, published_at, fetched_at,
                      upvotes, comments, stars, forks, reposts, upvote_ratio, score, category, raw_json
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        source, source_name, title, url,
                        str(item.get("snippet") or "").strip() or None,
                        item.get("published_at"),
                        fetched_at,
                        item.get("upvotes"),
                        item.get("comments"),
                        item.get("stars"),
                        item.get("forks"),
                        item.get("reposts"),
                        item.get("upvote_ratio"),
                        float(item.get("score") or 0.0),
                        item.get("category"),
                        raw_json,
                    ),
                )
            else:
                # Refresh engagement + score on re-fetch.
                self._conn.execute(
                    """
                    UPDATE news_items
                    SET snippet=COALESCE(?, snippet),
                        published_at=COALESCE(?, published_at),
                        fetched_at=?,
                        upvotes=COALESCE(?, upvotes),
                        comments=COALESCE(?, comments),
                        stars=COALESCE(?, stars),
                        forks=COALESCE(?, forks),
                        upvote_ratio=COALESCE(?, upvote_ratio),
                        score=?,
                        raw_json=?
                    WHERE id=?
                    """,
                    (
                        str(item.get("snippet") or "").strip() or None,
                        item.get("published_at"),
                        fetched_at,
                        item.get("upvotes"),
                        item.get("comments"),
                        item.get("stars"),
                        item.get("forks"),
                        item.get("upvote_ratio"),
                        float(item.get("score") or 0.0),
                        raw_json,
                        existing["id"],
                    ),
                )
            persisted += 1

        return persisted

    def prune_old_items(self, max_age_hours: int = 48) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=max(1, max_age_hours))).isoformat(timespec="seconds")
        cur = self._conn.execute("DELETE FROM news_items WHERE fetched_at < ?", (cutoff,))
        return int(cur.rowcount or 0)

    # --- seen (dedup state) --------------------------------------------

    def is_seen(self, url: str, title: str) -> bool:
        if url:
            row = self._conn.execute("SELECT 1 FROM seen WHERE url=?", (url,)).fetchone()
            if row is not None:
                return True
        # Title-only dedup for items without a stable URL (e.g. RSS items where
        # the URL is missing). Use normalized title match.
        if title:
            row = self._conn.execute("SELECT 1 FROM seen WHERE title=?", (title.strip().lower(),)).fetchone()
            if row is not None:
                return True
        return False

    def mark_seen(self, items: list[dict[str, Any]]) -> int:
        now = _utc_now_iso()
        count = 0
        for item in items:
            url = str(item.get("url") or "").strip()
            title = str(item.get("title") or "").strip().lower()
            if not url and not title:
                continue
            try:
                self._conn.execute(
                    """
                    INSERT OR IGNORE INTO seen(url, title, first_seen_at) VALUES(?,?,?)
                    """,
                    (url or None, title or None, now),
                )
                count += 1
            except sqlite3.Error as exc:
                log.warning("mark_seen failed url=%s: %s", url, exc)
        return count

    # --- news_digests ---------------------------------------------------

    def insert_digest(self, text: str, model: Optional[str], item_count: Optional[int]) -> Optional[int]:
        try:
            cur = self._conn.execute(
                """
                INSERT INTO news_digests(created_at, digest_text, model_used, item_count)
                VALUES(?,?,?,?)
                """,
                (_utc_now_iso(), str(text or "").strip(), str(model or "").strip() or None,
                 int(item_count) if item_count is not None else None),
            )
        except sqlite3.Error as exc:
            log.warning("insert_digest failed: %s", exc)
            return None
        return int(cur.lastrowid)