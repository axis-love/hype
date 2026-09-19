"""H13 migration 11: persist external_url / is_self / feed_weight."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from newsbot.db import NewsStore, _backfill_payload_columns

_SCHEMA10_PENDING_POSTS = """
CREATE TABLE pending_posts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  category TEXT,
  importance INTEGER,
  url TEXT,
  created_at TEXT NOT NULL,
  source TEXT,
  published_at TEXT,
  upvotes INTEGER,
  comments INTEGER,
  stars INTEGER,
  reposts INTEGER,
  crosspost_count INTEGER,
  penalty REAL,
  lookback_hours REAL,
  score_at_queue REAL,
  engagement_score REAL,
  recency_at_queue REAL,
  source_weight REAL,
  topic_bonus REAL,
  crosspost_bonus REAL,
  matched_topics TEXT,
  scored_at TEXT,
  merge_count INTEGER NOT NULL DEFAULT 1,
  merged_urls TEXT,
  snippet TEXT,
  source_name TEXT,
  raw_json TEXT,
  origin_topic TEXT,
  summary TEXT
);
"""


def test_migration_11_backfills_and_is_noop(tmp_path: Path):
    store = NewsStore(tmp_path / "m.sqlite")
    assert store.schema_version() == 11
    payload = {
        "external_url": "https://example.com/article",
        "is_self": True,
        "weight": 0.4,
    }
    store._conn.execute(
        "INSERT INTO pending_posts(title, url, created_at, raw_json) VALUES(?,?,?,?)",
        ("Story", "https://reddit.com/r/x", "2026-01-01T00:00:00+00:00", json.dumps(payload)),
    )
    cur = store._conn.cursor()
    _backfill_payload_columns(cur)
    row = store._conn.execute(
        "SELECT external_url, is_self, feed_weight FROM pending_posts"
    ).fetchone()
    assert row["external_url"] == "https://example.com/article"
    assert row["is_self"] == 1
    assert row["feed_weight"] == 0.4

    _backfill_payload_columns(cur)
    row2 = store._conn.execute(
        "SELECT external_url, is_self, feed_weight FROM pending_posts"
    ).fetchone()
    assert dict(row2) == dict(row)

    store.close()
    again = NewsStore(tmp_path / "m.sqlite")
    assert again.schema_version() == 11
    again.close()


def test_migration_11_upgrades_schema_10_raw_json_rows(tmp_path: Path):
    """Upgrade path: schema-10 DB with mixed raw_json rows, then reopen no-op."""
    db_path = tmp_path / "v10.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE schema_version(
          version INTEGER PRIMARY KEY,
          description TEXT NOT NULL,
          applied_at TEXT NOT NULL
        )
        """
    )
    for version in range(1, 11):
        conn.execute(
            "INSERT INTO schema_version(version, description, applied_at) VALUES(?,?,?)",
            (version, f"m{version}", "2026-01-01T00:00:00+00:00"),
        )
    conn.executescript(_SCHEMA10_PENDING_POSTS)
    rows = [
        (
            "full payload",
            json.dumps({
                "external_url": "https://example.com/article",
                "is_self": True,
                "weight": 0.4,
            }),
        ),
        (
            "is_self string weight",
            json.dumps({"is_self": True, "weight": "0.75"}),
        ),
        ("no raw_json", None),
        ("malformed json", "{not-json"),
        (
            "blank url non-numeric weight",
            json.dumps({"external_url": "  ", "weight": "nope"}),
        ),
    ]
    for title, raw_json in rows:
        conn.execute(
            "INSERT INTO pending_posts(title, url, created_at, raw_json) VALUES(?,?,?,?)",
            (title, "https://reddit.com/r/x", "2026-01-01T00:00:00+00:00", raw_json),
        )
    conn.commit()
    conn.close()

    store = NewsStore(db_path)
    assert store.schema_version() == 11
    n11 = store._conn.execute(
        "SELECT COUNT(*) AS n FROM schema_version WHERE version=11"
    ).fetchone()["n"]
    assert n11 == 1

    by_title = {
        r["title"]: r
        for r in store._conn.execute(
            "SELECT title, external_url, is_self, feed_weight FROM pending_posts"
        ).fetchall()
    }
    full = by_title["full payload"]
    assert full["external_url"] == "https://example.com/article"
    assert full["is_self"] == 1
    assert full["feed_weight"] == 0.4

    self_only = by_title["is_self string weight"]
    assert self_only["external_url"] is None
    assert self_only["is_self"] == 1
    assert self_only["feed_weight"] == 0.75

    missing = by_title["no raw_json"]
    assert missing["external_url"] is None
    assert missing["is_self"] == 0
    assert missing["feed_weight"] is None

    malformed = by_title["malformed json"]
    assert malformed["external_url"] is None
    assert malformed["is_self"] == 0
    assert malformed["feed_weight"] is None

    blank = by_title["blank url non-numeric weight"]
    assert blank["external_url"] is None
    assert blank["is_self"] == 0
    assert blank["feed_weight"] is None

    snapshot = [
        dict(r)
        for r in store._conn.execute(
            "SELECT title, external_url, is_self, feed_weight FROM pending_posts ORDER BY id"
        ).fetchall()
    ]
    store.close()

    again = NewsStore(db_path)
    assert again.schema_version() == 11
    n11_again = again._conn.execute(
        "SELECT COUNT(*) AS n FROM schema_version WHERE version=11"
    ).fetchone()["n"]
    assert n11_again == 1
    snapshot2 = [
        dict(r)
        for r in again._conn.execute(
            "SELECT title, external_url, is_self, feed_weight FROM pending_posts ORDER BY id"
        ).fetchall()
    ]
    assert snapshot2 == snapshot
    again.close()


def test_list_store_rows_omits_raw_json(tmp_path: Path):
    store = NewsStore(tmp_path / "s.sqlite")
    store.add_stories_to_store(
        [{
            "title": "T",
            "url": "https://example.com/t",
            "source": "rss",
            "source_name": "X",
            "raw_json": {"external_url": "https://example.com/article", "is_self": False, "weight": 0.5},
            "score_breakdown": {},
        }],
        [],
    )
    rows = store.list_store_rows("telegram")
    assert "raw_json" not in rows[0]
    assert rows[0]["external_url"] == "https://example.com/article"
    story = store.get_story(rows[0]["id"])
    assert story is not None
    assert "raw_json" in story
    store.close()
