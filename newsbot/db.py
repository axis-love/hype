"""SQLite-backed news bot storage.

Tables:
  - pending_posts:   the STORY STORE — raw scored news waiting to be styled
                     and delivered (and, after delivery, the posted archive)
  - daily_summaries: one recap post per local day (migration 4)
  - seen:            URLs/titles already delivered (dedup state)
  - schema_version:  migration tracking

WAL mode, autocommit, single connection per process — same pattern as
core/settings_store.py. Schema evolution is handled by a lightweight
migration mechanism that records applied versions in schema_version.

Store semantics (migration 4 / v2):
  The digest APPENDS raw scored stories (add_stories_to_store); it never
  clears the queue. Duplicates merge into existing rows (merge_into_store_row),
  and the poster styles a single winner at pick time (set_styled_content).

  ``body`` stays NOT NULL (SQLite cannot relax NOT NULL without a table
  rebuild), so new raw rows insert ``body=''``. The poster fills ``body`` +
  ``styled_at`` when it styles the winner. The marker for "raw, not yet
  styled" is therefore ``body='' AND styled_at IS NULL``.

  ``posted_at`` means "delivered to the Telegram channel" — nothing else.
  The ``deliveries`` table (migration 7) is the per-consumer delivery
  marker: each row records a (post_id, channel, delivered_at) delivery.
  ``posted_at`` is preserved during the transition (dual-write in
  ``mark_delivered``); read paths migrate to ``deliveries`` in H2.
  Do not overload ``posted_at`` as a general consumption marker.
  ``message_id`` (migration 5) stores the Telegram channel message_id for
  recap linking — it is NOT a general consumption marker.

  Legacy rows (pre-migration-4) carry merge_count=1 and NULL in the new
  columns; all reads must be NULL-safe.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from collections.abc import Sequence

from newsbot.collectors.base import Candidate
from newsbot.scoring import engagement

log = logging.getLogger(__name__)

#: Story/candidate input: plain dicts or collector Candidate dataclasses
#: (same union shape used by summarizer.py).
_StoryLike = dict[str, Any] | Candidate


def _as_dict(item: _StoryLike) -> dict[str, Any]:
    """Normalize a story/candidate to a plain dict (Candidate.to_dict())."""
    if isinstance(item, Candidate):
        return item.to_dict()
    return item


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


@_migration(2, "Drop unused news_items and news_digests tables")
def _migration_2(cur: sqlite3.Cursor) -> None:
    """Drop the unused news_items and news_digests tables and their indexes.

    These tables are not used by the current pipeline. Keeping them
    wastes disk and complicates schema evolution.
    """
    cur.execute("DROP INDEX IF EXISTS ix_news_items_fetched_at;")
    cur.execute("DROP INDEX IF EXISTS ix_news_items_score;")
    cur.execute("DROP TABLE IF EXISTS news_items;")
    cur.execute("DROP TABLE IF EXISTS news_digests;")


@_migration(3, "Add hype score columns to pending_posts")
def _migration_3(cur: sqlite3.Cursor) -> None:
    """Add score component and raw engagement columns to pending_posts.

    All new columns are nullable so existing rows survive the migration
    with NULL score data (legacy rows).
    """
    # Raw scoring inputs (for recalculation).
    cur.execute("ALTER TABLE pending_posts ADD COLUMN source TEXT")
    cur.execute("ALTER TABLE pending_posts ADD COLUMN published_at TEXT")
    cur.execute("ALTER TABLE pending_posts ADD COLUMN upvotes INTEGER")
    cur.execute("ALTER TABLE pending_posts ADD COLUMN comments INTEGER")
    cur.execute("ALTER TABLE pending_posts ADD COLUMN stars INTEGER")
    cur.execute("ALTER TABLE pending_posts ADD COLUMN reposts INTEGER")
    cur.execute("ALTER TABLE pending_posts ADD COLUMN crosspost_count INTEGER")
    cur.execute("ALTER TABLE pending_posts ADD COLUMN penalty REAL")
    cur.execute("ALTER TABLE pending_posts ADD COLUMN lookback_hours REAL")
    # Queue-time breakdown (historical snapshot).
    cur.execute("ALTER TABLE pending_posts ADD COLUMN score_at_queue REAL")
    cur.execute("ALTER TABLE pending_posts ADD COLUMN engagement_score REAL")
    cur.execute("ALTER TABLE pending_posts ADD COLUMN recency_at_queue REAL")
    cur.execute("ALTER TABLE pending_posts ADD COLUMN source_weight REAL")
    cur.execute("ALTER TABLE pending_posts ADD COLUMN topic_bonus REAL")
    cur.execute("ALTER TABLE pending_posts ADD COLUMN crosspost_bonus REAL")
    cur.execute("ALTER TABLE pending_posts ADD COLUMN matched_topics TEXT")
    cur.execute("ALTER TABLE pending_posts ADD COLUMN scored_at TEXT")


@_migration(4, "Additive raw-story store: merge/raw-material columns + daily_summaries")
def _migration_4(cur: sqlite3.Cursor) -> None:
    """Turn pending_posts into an additive raw-story store.

    Additive-only: ALTER ADD columns (existing rows get merge_count=1 and
    NULL elsewhere) plus the daily_summaries table. No rebuild — ``body``
    keeps its NOT NULL constraint, so raw rows insert ``body=''``.
    """
    cur.execute("ALTER TABLE pending_posts ADD COLUMN merge_count INTEGER NOT NULL DEFAULT 1")
    cur.execute("ALTER TABLE pending_posts ADD COLUMN merged_urls TEXT")  # JSON list, audit trail
    # Raw material for style-at-pick and for future consumers (girllm, blog).
    cur.execute("ALTER TABLE pending_posts ADD COLUMN snippet TEXT")
    cur.execute("ALTER TABLE pending_posts ADD COLUMN source_name TEXT")
    cur.execute("ALTER TABLE pending_posts ADD COLUMN raw_json TEXT")  # JSON, collector payload
    cur.execute("ALTER TABLE pending_posts ADD COLUMN styled_at TEXT")  # set when the styler ran
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_summaries(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          day TEXT NOT NULL UNIQUE,
          posted_at TEXT NOT NULL,
          summary_text TEXT NOT NULL,
          model_used TEXT,
          item_count INTEGER
        )
        """
    )


@_migration(5, "Add message_id to pending_posts for channel-post linking")
def _migration_5(cur: sqlite3.Cursor) -> None:
    """Add message_id column to pending_posts.

    Stores the Telegram channel message_id returned by sendMessage, so
    the daily recap can link each item to its actual channel post via
    a t.me link. Existing rows get NULL (legacy — no link, plain text).
    """
    cur.execute("ALTER TABLE pending_posts ADD COLUMN message_id INTEGER")


@_migration(6, "Add origin_topic to pending_posts (topic-pack acceptance criteria)")
def _migration_6(cur: sqlite3.Cursor) -> None:
    """Add origin_topic column to pending_posts.

    The topic pack whose source first surfaced the story (e.g. the pack
    the Reddit subreddit or RSS feed belongs to). matched_topics already
    stores every pack that matched; origin_topic pins the pack of
    origin so /scores can show where a story came from and the week
    watch (H-6) can attribute candidates to packs. Existing rows get
    NULL (legacy).
    """
    cur.execute("ALTER TABLE pending_posts ADD COLUMN origin_topic TEXT")


@_migration(7, "Add deliveries table for per-consumer delivery tracking")
def _migration_7(cur: sqlite3.Cursor) -> None:
    """Create the deliveries table and backfill from posted_at.

    The deliveries table is the per-consumer delivery marker designed in
    .hermes/plans/2026-08-27-multi-consumer-hype.md (§2). Each row records
    that a post was delivered to a specific channel (e.g. 'telegram',
    'girllm:gaming'). UNIQUE(post_id, channel) prevents duplicate
    deliveries per (post, channel) pair.

    Backfill: every pending_posts row with posted_at IS NOT NULL gets a
    'telegram' delivery row, preserving the original posted_at timestamp
    and message_id. This makes the deliveries table a superset of the
    existing posted_at data — read paths migrate one at a time in H2.

    posted_at on pending_posts stays untouched (db.py:24-27 contract).
    """
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS deliveries(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          post_id INTEGER NOT NULL REFERENCES pending_posts(id),
          channel TEXT NOT NULL,
          delivered_at TEXT NOT NULL,
          message_id INTEGER,
          UNIQUE(post_id, channel)
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS ix_deliveries_post ON deliveries(post_id);")
    cur.execute("CREATE INDEX IF NOT EXISTS ix_deliveries_channel ON deliveries(channel);")
    cur.execute("CREATE INDEX IF NOT EXISTS ix_deliveries_delivered ON deliveries(delivered_at);")
    # Backfill: one delivery row per posted pending_posts row.
    cur.execute(
        """
        INSERT OR IGNORE INTO deliveries(post_id, channel, delivered_at, message_id)
        SELECT id, 'telegram', posted_at, message_id
        FROM pending_posts
        WHERE posted_at IS NOT NULL
        """
    )


@_migration(8, "Add external_ref to deliveries for consumer delivery tracking")
def _migration_8(cur: sqlite3.Cursor) -> None:
    """Add the ``external_ref`` column to the deliveries table.

    Stores an opaque caller-supplied reference (e.g. a message ID or
    post URL from the consumer's own system) for each delivery. Nullable
    because existing rows pre-date the column and the API only started
    requiring it in H4. The first POST wins (INSERT OR IGNORE) — a
    repeat delivery for the same (post_id, channel) does NOT overwrite
    the original ref.
    """
    cur.execute("ALTER TABLE deliveries ADD COLUMN external_ref TEXT")


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
                log.error("Migration %d not found: %s — ABORTING migrations", version, description)
                raise RuntimeError(f"Migration {version} function not found: {description}")
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

    # --- news_items (dropped in migration 2) -----------------------------
    # The news_items table was dropped in migration 2. No pruning needed.

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
        per chunk (one URL, one title). Batches exceeding the SQLite parameter
        limit (999) are split into bounded 500-item chunks.
        Returns a set of indices into the input list.
        """
        if not items:
            return set()

        urls: list[str] = []
        titles: list[str] = []
        url_to_indices: dict[str, list[int]] = {}
        title_to_indices: dict[str, list[int]] = {}

        for i, item in enumerate(items):
            url = str(item.get("url") or "").strip()
            title = str(item.get("title") or "").strip().lower()
            if url:
                if url not in url_to_indices:
                    url_to_indices[url] = []
                    urls.append(url)
                url_to_indices[url].append(i)
            if title:
                if title not in title_to_indices:
                    title_to_indices[title] = []
                    titles.append(title)
                title_to_indices[title].append(i)

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
                    seen_indices.update(url_to_indices[row["url"]])

        # Batch title lookup.
        if titles:
            for chunk_start in range(0, len(titles), 500):
                chunk = titles[chunk_start:chunk_start + 500]
                placeholders = ",".join("?" * len(chunk))
                rows = self._conn.execute(
                    f"SELECT title FROM seen WHERE title IN ({placeholders})", chunk
                ).fetchall()
                for row in rows:
                    seen_indices.update(title_to_indices[row["title"]])

        return seen_indices

    def mark_seen(self, items: list[dict[str, Any]]) -> int:
        """Mark items as seen using batched executemany.

        Returns the number of rows actually inserted (not attempted).
        Uses INSERT OR IGNORE so duplicates are silently skipped.
        Wrapped in an explicit transaction for atomicity.
        """
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
        # Wrap in explicit transaction for atomicity.
        cur = self._conn.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            cur.executemany(
                "INSERT OR IGNORE INTO seen(url, title, first_seen_at) VALUES(?,?,?)",
                rows,
            )
            rowcount = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            cur.execute("COMMIT")
            return rowcount
        except sqlite3.Error as exc:
            try:
                cur.execute("ROLLBACK")
            except Exception:
                pass
            log.warning("mark_seen batch failed: %s", exc)
            return 0
        finally:
            cur.close()

    # --- news_digests (dropped in migration 2) -----------------------------
    # The news_digests table was dropped in migration 2. No pruning needed.

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

    def mark_delivered(
        self,
        post_id: int,
        channel: str,
        message_id: int | None = None,
        external_ref: str | None = None,
    ) -> bool:
        """Record a delivery of a post to a specific channel (idempotent).

        Raises ``ValueError`` if post_id does not exist in
        pending_posts. This is the per-consumer delivery marker — callers
        pass their own channel name (e.g. 'telegram', 'girllm:gaming').
        ``external_ref`` is an opaque caller-supplied reference persisted
        on first insert only (INSERT OR IGNORE — a repeat call does NOT
        overwrite the original ref).
        """
        row = self._conn.execute(
            "SELECT 1 FROM pending_posts WHERE id=?", (post_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"mark_delivered: post_id {post_id} does not exist")
        self._conn.execute(
            "INSERT OR IGNORE INTO deliveries(post_id, channel, delivered_at, message_id, external_ref) "
            "VALUES(?,?,?,?,?)",
            (post_id, channel, _utc_now_iso(), message_id, external_ref),
        )
        return True

    def is_delivered(self, post_id: int, channel: str) -> bool:
        """Return True if a delivery row exists for (post_id, channel)."""
        row = self._conn.execute(
            "SELECT 1 FROM deliveries WHERE post_id=? AND channel=?",
            (post_id, channel),
        ).fetchone()
        return row is not None

    def schema_version(self) -> int:
        """Return the highest applied migration version (0 if none)."""
        row = self._conn.execute(
            "SELECT MAX(version) AS v FROM schema_version"
        ).fetchone()
        return int(row["v"]) if row and row["v"] is not None else 0

    def mark_posted(self, post_id: int, message_id: int | None = None) -> None:
        """Mark a pending post as posted (Telegram delivery, atomic dual-write).

        Sets posted_at on pending_posts AND records a 'telegram' delivery
        in a single transaction (BEGIN IMMEDIATE / COMMIT). If the
        delivery INSERT fails, posted_at is rolled back — the row stays
        undelivered and will be re-posted on the next cycle.
        """
        now = _utc_now_iso()
        cur = self._conn.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            if message_id is not None:
                cur.execute(
                    "UPDATE pending_posts SET posted_at=?, message_id=? WHERE id=?",
                    (now, message_id, post_id),
                )
            else:
                cur.execute(
                    "UPDATE pending_posts SET posted_at=? WHERE id=?",
                    (now, post_id),
                )
            cur.execute(
                "INSERT OR IGNORE INTO deliveries(post_id, channel, delivered_at, message_id, external_ref) "
                "VALUES(?,?,?,?,?)",
                (post_id, "telegram", now, message_id, None),
            )
            cur.execute("COMMIT")
        except Exception:
            try:
                cur.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            cur.close()

    # Note: clear_unposted(), replace_unposted_batch(), and
    # get_next_pending_post() were removed in the v2 store redesign —
    # the store is now additive (see module docstring).

    # --- pending_posts as raw-story store (migration 4 / v2) -----------

    def add_stories_to_store(
        self, stories: Sequence[_StoryLike], seen_items: Sequence[_StoryLike]
    ) -> int:
        """Append RAW stories to the store and mark seen_items, atomically.

        Accepts plain dicts or collector Candidate dataclasses (both carry
        the same fields; Candidates are normalized via to_dict()).

        Raw rows insert body='' (NOT NULL cannot be relaxed); the poster
        fills body + styled_at at pick time. Every score-component column
        is persisted from story['score_breakdown']. NO delete of unposted
        rows — the store is additive.

        Returns the number of rows inserted.
        Raises sqlite3.Error on failure (transaction rolled back).
        """
        now = _utc_now_iso()
        cur = self._conn.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")

            post_rows = []
            for raw_story in stories:
                story = _as_dict(raw_story)
                bd = story.get("score_breakdown") or {}
                raw_json = story.get("raw_json")
                if raw_json is not None and not isinstance(raw_json, str):
                    raw_json = json.dumps(raw_json)
                # Seed merged_urls from contributing_urls (minus the row's
                # own url) so in-cycle merge audit trails persist (flow_001123).
                row_url = str(story.get("url") or "").strip()
                contributing = story.get("contributing_urls") or []
                seed_urls = [u for u in contributing if u and u != row_url]
                merged_urls_json = json.dumps(seed_urls) if seed_urls else None
                post_rows.append((
                    str(story.get("title") or "").strip(),
                    "",  # body — raw, not yet styled
                    str(story.get("category") or "").strip() or None,
                    row_url or None,
                    now,
                    str(story.get("snippet") or "").strip() or None,
                    str(story.get("source_name") or "").strip() or None,
                    raw_json,
                    # Score columns (from score_breakdown).
                    bd.get("source"),
                    bd.get("published_at"),
                    bd.get("upvotes"),
                    bd.get("comments"),
                    bd.get("stars"),
                    bd.get("reposts"),
                    bd.get("crosspost_count"),
                    bd.get("penalty"),
                    bd.get("lookback_hours"),
                    bd.get("score"),
                    bd.get("engagement"),
                    bd.get("recency"),
                    bd.get("source_weight"),
                    bd.get("topic_bonus"),
                    bd.get("crosspost_bonus"),
                    json.dumps(bd.get("matched_topics") or []) if bd else None,
                    bd.get("scored_at"),
                    bd.get("origin_topic"),
                    merged_urls_json,
                ))
            if post_rows:
                cur.executemany(
                    """
                    INSERT INTO pending_posts(
                        title, body, category, url, created_at,
                        snippet, source_name, raw_json,
                        source, published_at, upvotes, comments, stars, reposts,
                        crosspost_count, penalty, lookback_hours,
                        score_at_queue, engagement_score, recency_at_queue,
                        source_weight, topic_bonus, crosspost_bonus,
                        matched_topics, scored_at, origin_topic, merged_urls
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    post_rows,
                )

            seen_rows = [
                (str(item.get("url") or "").strip() or None,
                 str(item.get("title") or "").strip().lower() or None,
                 now)
                for item in map(_as_dict, seen_items)
                if str(item.get("url") or "").strip() or str(item.get("title") or "").strip()
            ]
            if seen_rows:
                cur.executemany(
                    "INSERT OR IGNORE INTO seen(url, title, first_seen_at) VALUES(?,?,?)",
                    seen_rows,
                )

            cur.execute("COMMIT")
        except Exception as exc:
            log.error("add_stories_to_store failed, rolling back: %s", exc)
            try:
                cur.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            cur.close()

        return len(post_rows)

    _STORE_SELECT = (
        "id, title, url, snippet, source_name, raw_json, category, "
        "source, published_at, upvotes, comments, stars, reposts, "
        "crosspost_count, penalty, lookback_hours, "
        "score_at_queue, engagement_score, recency_at_queue, "
        "source_weight, topic_bonus, crosspost_bonus, "
        "matched_topics, scored_at, origin_topic, merge_count, merged_urls, "
        "styled_at, message_id"
    )

    def list_store_rows(self, channel: str) -> list[dict]:
        """Return store rows not yet delivered to *channel*.

        Each consumer sees only its own undelivered rows: a row delivered
        to 'girllm:gaming' is still eligible for 'telegram' and vice versa.
        Uses the deliveries table (H2) — no posted_at filter.
        """
        rows = self._conn.execute(
            f"SELECT {self._STORE_SELECT} FROM pending_posts "
            "WHERE id NOT IN (SELECT post_id FROM deliveries WHERE channel=?) "
            "ORDER BY created_at ASC, id ASC",
            (channel,),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_merge_target_rows(self, channel: str, days: int) -> list[dict]:
        """Return rows eligible as merge targets for *channel*.

        Includes all rows not yet delivered to *channel* (no delivery
        row for this channel) plus rows delivered to *channel* within
        the *days* window. A row delivered to a DIFFERENT channel only
        is NOT a merge target.

        Used ONLY at classification (main.py step 8) so that a story
        arriving from a different source can merge into a recently-delivered
        row instead of being inserted as a duplicate. list_store_rows()
        stays the sole method for pick_hottest, eviction, /scores, /store
        — those must see undelivered rows only.
        """
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=max(1, days))
        ).isoformat(timespec="seconds")
        rows = self._conn.execute(
            f"SELECT {self._STORE_SELECT}, posted_at FROM pending_posts "
            "WHERE id NOT IN (SELECT post_id FROM deliveries) "
            "OR id IN (SELECT post_id FROM deliveries WHERE channel=? AND delivered_at >= ?) "
            "ORDER BY created_at ASC, id ASC",
            (channel, cutoff),
        ).fetchall()
        return [dict(row) for row in rows]

    def merge_into_store_row(
        self, row_id: int, candidate: _StoryLike, extra_urls: str | list[str]
    ) -> None:
        """Merge a duplicate candidate into an existing store row.

        merge_count += 1 (exactly once, regardless of how many URLs are
        appended); raw engagement fields take per-field max(stored,
        candidate); published_at takes the max; each URL in *extra_urls*
        is appended to merged_urls (deduped). engagement_score is
        RECOMPUTED via scoring.engagement() from the merged raw fields —
        never copied from either side — so a hotter stored row can never
        lose temperature to a colder candidate. The remaining components
        (source_weight, topic_bonus, crosspost_bonus, penalty,
        lookback_hours) refresh from candidate['score_breakdown'], and
        score_at_queue is recomputed from the rebuilt components using
        the queue-time formula:
        (engagement * recency * source_weight + topic_bonus + crosspost_bonus)
        * penalty.

        *extra_urls* accepts a single URL string (backward-compatible)
        or a list of URLs. All are appended to merged_urls (deduped) in
        a single UPDATE — merge_count increments exactly once per call.
        """
        row = self._conn.execute(
            "SELECT * FROM pending_posts WHERE id=?", (row_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"merge_into_store_row: no such row id={row_id}")
        candidate = _as_dict(candidate)
        bd = candidate.get("score_breakdown") or {}

        def _field_max(column: str, candidate_key: str) -> int:
            stored = row[column]
            cand = (bd.get(candidate_key) if candidate_key in bd else candidate.get(candidate_key))
            stored_v = int(stored) if stored is not None else 0
            cand_v = int(cand) if cand is not None else 0
            return max(stored_v, cand_v)

        upvotes = _field_max("upvotes", "upvotes")
        comments = _field_max("comments", "comments")
        stars = _field_max("stars", "stars")
        reposts = _field_max("reposts", "reposts")

        # published_at: max of stored vs candidate (ISO strings compare
        # chronologically when formats match; fall back to whichever exists).
        stored_pub = row["published_at"] or ""
        cand_pub = str(bd.get("published_at") or candidate.get("published_at") or "")
        published_at = max(stored_pub, cand_pub) or None

        merged_urls: list[str] = []
        if row["merged_urls"]:
            try:
                merged_urls = list(json.loads(row["merged_urls"]))
            except (json.JSONDecodeError, TypeError):
                merged_urls = []
        # Normalize extra_urls to a list (backward-compatible with str).
        if isinstance(extra_urls, str):
            url_list = [extra_urls]
        else:
            url_list = list(extra_urls) if extra_urls else []
        for url in url_list:
            url = str(url or "").strip()
            if url and url not in merged_urls:
                merged_urls.append(url)

        # Recompute engagement from the merged raw fields — never copy.
        merged_engagement = engagement({
            "upvotes": upvotes, "comments": comments,
            "stars": stars, "reposts": reposts,
        })

        # Refresh the non-engagement components from the candidate breakdown.
        source_weight = bd.get("source_weight")
        if source_weight is None:
            source_weight = row["source_weight"]
        topic_bonus_v = bd.get("topic_bonus")
        if topic_bonus_v is None:
            topic_bonus_v = row["topic_bonus"]
        crosspost_bonus = bd.get("crosspost_bonus")
        if crosspost_bonus is None:
            crosspost_bonus = row["crosspost_bonus"]
        penalty = bd.get("penalty")
        if penalty is None:
            penalty = row["penalty"]
        lookback_hours = bd.get("lookback_hours")
        if lookback_hours is None:
            lookback_hours = row["lookback_hours"]

        # Rebuild score_at_queue from the merged components (queue-time
        # formula; recency_at_queue is the stored snapshot).
        recency = row["recency_at_queue"]
        recency_v = float(recency) if recency is not None else 0.0
        weight_v = float(source_weight) if source_weight is not None else 1.0
        penalty_v = float(penalty) if penalty is not None else 1.0
        score_at_queue = (
            merged_engagement * recency_v * weight_v
            + float(topic_bonus_v or 0) + float(crosspost_bonus or 0)
        ) * penalty_v

        # Fill origin_topic if the stored row has none (the candidate's
        # source may be pack-attributable when the original wasn't).
        origin_topic = row["origin_topic"] or bd.get("origin_topic")

        self._conn.execute(
            """
            UPDATE pending_posts SET
                merge_count = merge_count + 1,
                merged_urls = ?,
                upvotes = ?, comments = ?, stars = ?, reposts = ?,
                published_at = ?,
                engagement_score = ?,
                source_weight = ?, topic_bonus = ?, crosspost_bonus = ?,
                penalty = ?, lookback_hours = ?,
                score_at_queue = ?,
                origin_topic = ?
            WHERE id = ?
            """,
            (
                json.dumps(merged_urls),
                upvotes, comments, stars, reposts,
                published_at,
                merged_engagement,
                source_weight, topic_bonus_v, crosspost_bonus,
                penalty, lookback_hours,
                score_at_queue,
                origin_topic,
                row_id,
            ),
        )

    def set_styled_content(self, row_id: int, title: str, body: str) -> None:
        """Fill the styled title/body and stamp styled_at (UTC ISO).

        Called by the poster after styling the picked winner. Clears the
        raw-not-yet-styled marker (body='' AND styled_at IS NULL).
        """
        self._conn.execute(
            "UPDATE pending_posts SET title=?, body=?, styled_at=? WHERE id=?",
            (title, body, _utc_now_iso(), row_id),
        )

    def evict_coldest(self, temps: dict[int, float], cap: int) -> int:
        """Delete undelivered rows with the lowest temperatures until count <= cap.

        temps maps row_id -> raw current temperature (computed by the caller).
        Rows missing from temps are treated as coldest-first by id.

        Global eviction (decision 2026-09-03): a row with ANY delivery
        (to ANY channel) is never evicted. This protects delivered rows
        even when they are the coldest — a row delivered to girllm must
        survive a telegram eviction pass.
        """
        # Only evict rows with no deliveries at all (global protection).
        rows = self._conn.execute(
            "SELECT id FROM pending_posts "
            "WHERE id NOT IN (SELECT post_id FROM deliveries)"
        ).fetchall()
        count = len(rows)
        if count <= cap:
            return 0
        # Sort by temperature ascending; unknown ids sort coldest (stable by id).
        ids_sorted = sorted(
            (r["id"] for r in rows),
            key=lambda rid: (temps.get(rid, float("-inf")), rid),
        )
        to_evict = ids_sorted[:count - cap]
        if not to_evict:
            return 0
        placeholders = ",".join("?" * len(to_evict))
        cur = self._conn.execute(
            f"DELETE FROM pending_posts WHERE id IN ({placeholders}) "
            "AND id NOT IN (SELECT post_id FROM deliveries)",
            to_evict,
        )
        evicted = int(cur.rowcount or 0)
        if evicted:
            log.info("evicted %d coldest store rows (cap=%d)", evicted, cap)
        return evicted

    def list_posted_since(self, channel: str, since_iso: str) -> list[dict]:
        """Return rows delivered to *channel* with delivered_at >= since_iso, oldest first.

        Boundary is inclusive. Source rows for the daily summary and
        topic cooldown. Each consumer reads its own deliveries.
        """
        # Prefix _STORE_SELECT columns with pp. to disambiguate from
        # deliveries columns in the JOIN.
        select_cols = ", ".join(
            f"pp.{c.strip()}" for c in self._STORE_SELECT.split(",")
        )
        rows = self._conn.execute(
            f"SELECT {select_cols}, pp.body, d.delivered_at AS posted_at "
            "FROM pending_posts pp "
            "JOIN deliveries d ON d.post_id = pp.id "
            "WHERE d.channel = ? AND d.delivered_at >= ? "
            "ORDER BY d.delivered_at ASC",
            (channel, since_iso),
        ).fetchall()
        return [dict(row) for row in rows]

    # --- daily_summaries -------------------------------------------------

    def add_summary(self, day: str, text: str, model: str, item_count: int) -> None:
        """Record a delivered daily summary. day UNIQUE guards against dupes."""
        self._conn.execute(
            """
            INSERT INTO daily_summaries(day, posted_at, summary_text, model_used, item_count)
            VALUES(?,?,?,?,?)
            """,
            (day, _utc_now_iso(), text, model, item_count),
        )

    def get_summary_for_day(self, day: str) -> Optional[dict]:
        """Return the summary row for a local day ('YYYY-MM-DD'), or None."""
        row = self._conn.execute(
            "SELECT * FROM daily_summaries WHERE day=?", (day,)
        ).fetchone()
        return dict(row) if row else None

    def count_pending(self, channel: str) -> int:
        """Count rows not yet delivered to *channel*."""
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM pending_posts "
            "WHERE id NOT IN (SELECT post_id FROM deliveries WHERE channel=?)",
            (channel,),
        ).fetchone()
        return int(row["n"] if row else 0)

    def list_unposted_posts(self, channel: str) -> list[dict[str, Any]]:
        """Return all rows not yet delivered to *channel*, ordered by created_at, id.

        Used by the /scores command to show queued posts with score breakdown.
        """
        rows = self._conn.execute(
            """
            SELECT * FROM pending_posts
            WHERE id NOT IN (SELECT post_id FROM deliveries WHERE channel=?)
            ORDER BY created_at ASC, id ASC
            """,
            (channel,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_store_row(self, row_id: int, channel: str) -> dict[str, Any] | None:
        """Return a single row by id if not yet delivered to *channel*, or None."""
        row = self._conn.execute(
            f"SELECT {self._STORE_SELECT}, body FROM pending_posts "
            "WHERE id=? AND id NOT IN (SELECT post_id FROM deliveries WHERE channel=?)",
            (row_id, channel),
        ).fetchone()
        return dict(row) if row else None

    def list_store_ids(self, channel: str) -> list[int]:
        """Return row ids not yet delivered to *channel* (for /store error hints)."""
        rows = self._conn.execute(
            "SELECT id FROM pending_posts "
            "WHERE id NOT IN (SELECT post_id FROM deliveries WHERE channel=?) "
            "ORDER BY id ASC",
            (channel,),
        ).fetchall()
        return [int(row["id"]) for row in rows]

    # --- Retention / cleanup --------------------------------------------

    def prune_delivered(self, max_age_days: int = 30, batch_size: int = 500) -> int:
        """Delete rows whose newest delivery is older than max_age_days.

        Also cleans up orphaned delivery rows (post_id no longer in
        pending_posts) in the same batch loop. A row with no deliveries
        is never pruned (still in the active store). A row with a recent
        delivery to any channel survives — the newest delivery
        determines the row's age. Returns the total count of deleted
        pending_posts rows.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, max_age_days))).isoformat(timespec="seconds")
        total_deleted = 0
        while True:
            cur = self._conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            # Delete pending_posts rows with old deliveries.
            cur.execute(
                "DELETE FROM pending_posts WHERE id IN ("
                "  SELECT pp.id FROM pending_posts pp"
                "  WHERE EXISTS (SELECT 1 FROM deliveries d WHERE d.post_id = pp.id)"
                "    AND (SELECT MAX(d2.delivered_at) FROM deliveries d2 WHERE d2.post_id = pp.id) < ?"
                "  LIMIT ?"
                ")",
                (cutoff, batch_size),
            )
            deleted = int(cur.rowcount or 0)
            # Delete orphaned deliveries (post_id not in pending_posts).
            cur.execute(
                "DELETE FROM deliveries WHERE post_id NOT IN "
                "(SELECT id FROM pending_posts)"
            )
            cur.execute("COMMIT")
            cur.close()
            total_deleted += deleted
            if deleted < batch_size:
                break
        if total_deleted:
            log.info("Pruned %d posts with deliveries older than %d days", total_deleted, max_age_days)
        return total_deleted

    def prune_seen(self, max_age_days: int = 14, batch_size: int = 500) -> int:
        """Delete seen entries older than max_age_days, in bounded batches.

        Never removes entries within the deduplication window.
        Prunes by rowid (not url) so title-only entries (url IS NULL)
        are also pruned. Returns the total count of deleted rows.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, max_age_days))).isoformat(timespec="seconds")
        total_deleted = 0
        while True:
            cur = self._conn.execute(
                "DELETE FROM seen WHERE rowid IN "
                "(SELECT rowid FROM seen WHERE first_seen_at < ? LIMIT ?)",
                (cutoff, batch_size),
            )
            deleted = int(cur.rowcount or 0)
            total_deleted += deleted
            if deleted < batch_size:
                break
        if total_deleted:
            log.info("Pruned %d seen entries older than %d days", total_deleted, max_age_days)
        return total_deleted