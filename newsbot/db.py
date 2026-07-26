"""SQLite-backed news bot storage.

Tables:
  - pending_posts: individual posts waiting to be sent to Telegram
  - seen:          URLs/titles already delivered (dedup state)
  - news_items:    raw fetched candidates (retained for prune window)
  - news_digests:  posted digest history
  - schema_version: migration tracking

WAL mode, autocommit, single connection per process — same pattern as
core/settings_store.py. Schema evolution is handled by a lightweight
migration mechanism that records applied versions in schema_version.
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


# --- Migrations ----------------------------------------------------------
# Each migration is a function that takes a sqlite3 cursor and is safe to
# rerun (idempotent). Migrations are applied in order, tracking the
# applied version in the schema_version table.

_MIGRATIONS: list[tuple[int, str]] = []


def _migration(version: int, description: str):
    """Decorator to register a migration."""
    def decorator(fn):
        _MIGRATIONS.append((version, description))
        globals()[f"_migration_{version}"] = fn
        return fn
    return decorator


@_migration(1, "Initial schema: news_items, seen, news_digests, pending_posts")
def _migration_1(cur: sqlite3.Cursor) -> None:
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
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS pending_posts(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          title TEXT NOT NULL,
          body TEXT NOT NULL,
          category TEXT,
          importance INTEGER,
          url TEXT,
          created_at TEXT NOT NULL,
          posted_at TEXT
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS ix_news_items_fetched_at ON news_items(fetched_at);")
    cur.execute("CREATE INDEX IF NOT EXISTS ix_news_items_score ON news_items(score DESC);")
    cur.execute("CREATE INDEX IF NOT EXISTS ix_pending_posts_posted ON pending_posts(posted_at);")


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
        cur.close()

        # Run migrations.
        self._run_migrations()

    def _run_migrations(self) -> None:
        """Apply pending migrations in order, tracking in schema_version."""
        cur = self._conn.cursor()
        # Create the version tracking table first.
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_version(
              version INTEGER PRIMARY KEY,
              description TEXT NOT NULL,
              applied_at TEXT NOT NULL
            )
            """
        )
        # Determine current version.
        row = cur.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
        current_version = int(row["v"]) if row and row["v"] is not None else 0

        for version, description in _MIGRATIONS:
            if version <= current_version:
                continue
            fn = globals().get(f"_migration_{version}")
            if fn is None:
                log.error("Migration %d not found: %s", version, description)
                continue
            try:
                cur.execute("BEGIN IMMEDIATE")
                fn(cur)
                cur.execute(
                    "INSERT INTO schema_version(version, description, applied_at) VALUES(?,?,?)",
                    (version, description, _utc_now_iso()),
                )
                cur.execute("COMMIT")
                log.info("Applied migration %d: %s", version, description)
            except Exception as exc:
                log.error("Migration %d failed: %s", version, exc)
                try:
                    cur.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            finally:
                cur.close()
                cur = self._conn.cursor()

        cur.close()

    def close(self) -> None:
        """Explicitly close the database connection."""
        try:
            self._conn.close()
        except Exception:
            pass

    def __enter__(self) -> "NewsStore":
        return self

    def __exit__(self, *args) -> None:
        self.close()

    # --- news_items -----------------------------------------------------
    # Note: insert_items() was removed — the current pipeline does not
    # persist candidates to news_items. prune_old_items() remains because
    # it is called from the generation cycle and is harmless on an empty
    # table. The table is created by migration 1 for backward compatibility.

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

    def is_seen_batch(self, items: list[dict[str, Any]]) -> set[int]:
        """Check which items are seen, returning a set of indices that ARE seen.

        Uses set-based SQL instead of per-item queries: at most 2 queries
        total for the entire batch, regardless of batch size.
        Returns a set of indices into the input list.
        """
        if not items:
            return set()

        urls: list[str] = []
        titles: list[str] = []
        url_to_idx: dict[str, int] = {}
        title_to_idx: dict[str, int] = {}

        for i, item in enumerate(items):
            url = str(item.get("url") or "").strip()
            title = str(item.get("title") or "").strip().lower()
            if url and url not in url_to_idx:
                url_to_idx[url] = i
                urls.append(url)
            if title and title not in title_to_idx:
                title_to_idx[title] = i
                titles.append(title)

        seen_indices: set[int] = set()

        # Batch URL lookup: SELECT WHERE url IN (?,?,...).
        if urls:
            # SQLite parameter limit is 999; chunk if needed.
            for chunk_start in range(0, len(urls), 500):
                chunk = urls[chunk_start:chunk_start + 500]
                placeholders = ",".join("?" * len(chunk))
                rows = self._conn.execute(
                    f"SELECT url FROM seen WHERE url IN ({placeholders})", chunk
                ).fetchall()
                for row in rows:
                    seen_indices.add(url_to_idx[row["url"]])

        # Batch title lookup.
        if titles:
            for chunk_start in range(0, len(titles), 500):
                chunk = titles[chunk_start:chunk_start + 500]
                placeholders = ",".join("?" * len(chunk))
                rows = self._conn.execute(
                    f"SELECT title FROM seen WHERE title IN ({placeholders})", chunk
                ).fetchall()
                for row in rows:
                    seen_indices.add(title_to_idx[row["title"]])

        return seen_indices

    def mark_seen(self, items: list[dict[str, Any]]) -> int:
        """Mark items as seen using batched executemany."""
        now = _utc_now_iso()
        rows: list[tuple] = []
        for item in items:
            url = str(item.get("url") or "").strip()
            title = str(item.get("title") or "").strip().lower()
            if not url and not title:
                continue
            rows.append((url or None, title or None, now))
        if not rows:
            return 0
        # Use executemany for batched insertion (much faster than per-row).
        # INSERT OR IGNORE handles duplicates silently.
        try:
            cur = self._conn.executemany(
                "INSERT OR IGNORE INTO seen(url, title, first_seen_at) VALUES(?,?,?)",
                rows,
            )
            return len(rows)
        except sqlite3.Error as exc:
            log.warning("mark_seen batch failed: %s", exc)
            return 0

    # --- news_digests ---------------------------------------------------
    # Note: insert_digest() was removed — the current pipeline does not
    # persist digests to news_digests. prune_digests() remains for cleanup.

    # --- pending_posts (individual posts waiting to be sent) -----------

    def add_pending_post(self, post: dict[str, Any]) -> Optional[int]:
        """Insert a styled post into the pending queue. Returns row id."""
        try:
            cur = self._conn.execute(
                """
                INSERT INTO pending_posts(title, body, category, importance, url, created_at)
                VALUES(?,?,?,?,?,?)
                """,
                (
                    str(post.get("title") or "").strip(),
                    str(post.get("body") or "").strip(),
                    str(post.get("category") or "").strip() or None,
                    post.get("importance"),
                    str(post.get("url") or "").strip() or None,
                    _utc_now_iso(),
                ),
            )
        except sqlite3.Error as exc:
            log.warning("add_pending_post failed: %s", exc)
            return None
        return int(cur.lastrowid)

    def get_next_pending_post(self) -> Optional[dict[str, Any]]:
        """Get the oldest unposted post from the queue."""
        row = self._conn.execute(
            """
            SELECT * FROM pending_posts
            WHERE posted_at IS NULL
            ORDER BY created_at ASC, id ASC
            LIMIT 1
            """
        ).fetchone()
        return dict(row) if row else None

    def mark_posted(self, post_id: int) -> None:
        """Mark a pending post as posted."""
        self._conn.execute(
            "UPDATE pending_posts SET posted_at=? WHERE id=?",
            (_utc_now_iso(), post_id),
        )

    def clear_unposted(self) -> int:
        """Delete all unposted posts (called before a new generation cycle)."""
        cur = self._conn.execute("DELETE FROM pending_posts WHERE posted_at IS NULL")
        return int(cur.rowcount or 0)

    def replace_unposted_batch(
        self, new_posts: list[dict[str, Any]], seen_items: list[dict[str, Any]]
    ) -> tuple[int, int]:
        """Atomically replace unposted posts with a new batch and mark items as seen.

        This performs clear + insert + mark-seen in a single transaction.
        If any insertion fails, the transaction is rolled back and the
        previous queue remains intact.

        Returns (posts_inserted, seen_marked).
        Raises sqlite3.Error on failure (transaction rolled back).
        """
        now = _utc_now_iso()
        posts_inserted = 0
        seen_marked = 0

        cur = self._conn.cursor()
        try:
            # Use explicit transaction (autocommit is normally on).
            cur.execute("BEGIN IMMEDIATE")

            # 1. Clear old unposted posts.
            cur.execute("DELETE FROM pending_posts WHERE posted_at IS NULL")

            # 2. Insert new posts.
            for post in new_posts:
                cur.execute(
                    """
                    INSERT INTO pending_posts(title, body, category, importance, url, created_at)
                    VALUES(?,?,?,?,?,?)
                    """,
                    (
                        str(post.get("title") or "").strip(),
                        str(post.get("body") or "").strip(),
                        str(post.get("category") or "").strip() or None,
                        post.get("importance"),
                        str(post.get("url") or "").strip() or None,
                        now,
                    ),
                )
                posts_inserted += 1

            # 3. Mark source items as seen.
            for item in seen_items:
                url = str(item.get("url") or "").strip()
                title = str(item.get("title") or "").strip().lower()
                if not url and not title:
                    continue
                cur.execute(
                    "INSERT OR IGNORE INTO seen(url, title, first_seen_at) VALUES(?,?,?)",
                    (url or None, title or None, now),
                )
                seen_marked += 1

            cur.execute("COMMIT")
        except sqlite3.Error as exc:
            log.error("replace_unposted_batch failed, rolling back: %s", exc)
            try:
                cur.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            cur.close()

        return posts_inserted, seen_marked

    def count_pending(self) -> int:
        """Count unposted posts in the queue."""
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM pending_posts WHERE posted_at IS NULL"
        ).fetchone()
        return int(row["n"] if row else 0)

    # --- Retention / cleanup --------------------------------------------

    def prune_posted_posts(self, max_age_days: int = 30, batch_size: int = 500) -> int:
        """Delete posted posts older than max_age_days, in bounded batches.

        Never removes unposted posts (posted_at IS NULL).
        Returns the total count of deleted rows.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, max_age_days))).isoformat(timespec="seconds")
        total_deleted = 0
        while True:
            cur = self._conn.execute(
                "DELETE FROM pending_posts WHERE posted_at IS NOT NULL AND posted_at < ? AND id IN "
                "(SELECT id FROM pending_posts WHERE posted_at IS NOT NULL AND posted_at < ? LIMIT ?)",
                (cutoff, cutoff, batch_size),
            )
            deleted = int(cur.rowcount or 0)
            total_deleted += deleted
            if deleted < batch_size:
                break
        if total_deleted:
            log.info("Pruned %d posted posts older than %d days", total_deleted, max_age_days)
        return total_deleted

    def prune_seen(self, max_age_days: int = 14, batch_size: int = 500) -> int:
        """Delete seen entries older than max_age_days, in bounded batches.

        Never removes entries within the deduplication window.
        Returns the total count of deleted rows.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, max_age_days))).isoformat(timespec="seconds")
        total_deleted = 0
        while True:
            cur = self._conn.execute(
                "DELETE FROM seen WHERE first_seen_at < ? AND url IN "
                "(SELECT url FROM seen WHERE first_seen_at < ? LIMIT ?)",
                (cutoff, cutoff, batch_size),
            )
            deleted = int(cur.rowcount or 0)
            total_deleted += deleted
            if deleted < batch_size:
                break
        if total_deleted:
            log.info("Pruned %d seen entries older than %d days", total_deleted, max_age_days)
        return total_deleted

    def prune_digests(self, max_age_days: int = 90, batch_size: int = 100) -> int:
        """Delete old digest history entries older than max_age_days."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, max_age_days))).isoformat(timespec="seconds")
        total_deleted = 0
        while True:
            cur = self._conn.execute(
                "DELETE FROM news_digests WHERE created_at < ? AND id IN "
                "(SELECT id FROM news_digests WHERE created_at < ? LIMIT ?)",
                (cutoff, cutoff, batch_size),
            )
            deleted = int(cur.rowcount or 0)
            total_deleted += deleted
            if deleted < batch_size:
                break
        if total_deleted:
            log.info("Pruned %d digests older than %d days", total_deleted, max_age_days)
        return total_deleted