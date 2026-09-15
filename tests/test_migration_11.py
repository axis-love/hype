"""H13 migration 11: persist external_url / is_self / feed_weight."""
from __future__ import annotations

import json
from pathlib import Path

from newsbot.db import NewsStore, _backfill_payload_columns


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
