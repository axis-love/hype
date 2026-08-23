"""Tests for scripts/migrate_news_sources.py — news.sources → news.topics migration."""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

# Ensure scripts/ is importable
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

# Ensure repo root is importable for newsbot.topics
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _make_synth_db(db_path: Path, sources: dict | None = None) -> None:
    """Create a synthetic settings DB with optional news.sources."""
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings(
          namespace   TEXT NOT NULL,
          key         TEXT NOT NULL,
          value_json  TEXT NOT NULL,
          updated_at  TEXT NOT NULL,
          PRIMARY KEY(namespace, key)
        )
    """)
    if sources is not None:
        conn.execute(
            "INSERT INTO settings(namespace, key, value_json, updated_at) VALUES(?,?,?,?)",
            ("news", "sources", json.dumps(sources), "2026-01-01T00:00:00+00:00"),
        )
    conn.execute(
        "INSERT INTO settings(namespace, key, value_json, updated_at) VALUES(?,?,?,?)",
        ("news", "style_prompt", json.dumps("test"), "2026-01-01T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()


def _read_settings(db_path: str) -> dict:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rows = conn.execute(
        "SELECT key, value_json FROM settings WHERE namespace='news'"
    ).fetchall()
    conn.close()
    return {r[0]: json.loads(r[1]) for r in rows}


SYNTH_FEEDS = [
    {"name": "OpenAI", "url": "https://openai.com/news/rss.xml", "weight": 1.3},
    {"name": "Google DeepMind", "url": "https://deepmind.google/discover/blog/rss.xml", "weight": 1.3},
    {"name": "IGN", "url": "https://feeds.ign.com/ign/all", "weight": 1.1},
    {"name": "Eurogamer", "url": "https://www.eurogamer.net/feed", "weight": 1.1},
    {"name": "TechCrunch AI", "url": "https://techcrunch.com/category/artificial-intelligence/feed/", "weight": 1.0},
    {"name": "VentureBeat AI", "url": "https://venturebeat.com/category/ai/feed/", "weight": 1.0},
    {"name": "The Verge", "url": "https://www.theverge.com/rss/index.xml", "weight": 1.0},
    {"name": "Kotaku", "url": "https://kotaku.com/rss", "weight": 1.1},
]


class TestMigrateNewsSources:
    def test_noop_when_sources_absent(self, tmp_path):
        """Live DB has no news.sources — migration is a no-op."""
        from migrate_news_sources import build_topics_override
        db = tmp_path / "test.sqlite"
        _make_synth_db(db, sources=None)

        import migrate_news_sources as m
        sources = m._load_sources(str(db))
        assert sources is None
        # build_topics_override with None sources returns empty dict
        result = build_topics_override({}, {}, None)
        assert result == {}

    def test_dry_run_does_not_write(self, tmp_path, capsys):
        """Dry-run mode must not modify the DB."""
        db = tmp_path / "test.sqlite"
        _make_synth_db(db, sources={"rss": {"feeds": SYNTH_FEEDS}})

        from migrate_news_sources import main
        exit_code = main(["--db", str(db)])
        assert exit_code == 0

        # Verify news.sources still exists and no news.topics was written
        settings = _read_settings(str(db))
        assert "sources" in settings
        assert "topics" not in settings

    def test_apply_migrates_feeds_to_topics(self, tmp_path):
        """--apply writes news.topics and deletes news.sources."""
        db = tmp_path / "test.sqlite"
        _make_synth_db(db, sources={"rss": {"feeds": SYNTH_FEEDS}})

        from migrate_news_sources import main
        exit_code = main(["--db", str(db), "--apply"])
        assert exit_code == 0

        settings = _read_settings(str(db))
        # news.sources must be deleted
        assert "sources" not in settings
        # news.topics must be written
        assert "topics" in settings
        topics = settings["topics"]
        # AI feeds should be in the ai pack override
        assert "ai" in topics
        ai_feeds = topics["ai"]["feeds"]
        ai_urls = {f["url"] for f in ai_feeds}
        assert "https://openai.com/news/rss.xml" in ai_urls
        assert "https://deepmind.google/discover/blog/rss.xml" in ai_urls
        # Gaming feeds should be in the gaming pack override
        assert "gaming" in topics
        gaming_feeds = topics["gaming"]["feeds"]
        gaming_urls = {f["url"] for f in gaming_feeds}
        assert "https://feeds.ign.com/ign/all" in gaming_urls
        assert "https://www.eurogamer.net/feed" in gaming_urls

    def test_apply_is_idempotent(self, tmp_path):
        """Re-running on an already-migrated DB is a no-op."""
        db = tmp_path / "test.sqlite"
        _make_synth_db(db, sources={"rss": {"feeds": SYNTH_FEEDS}})

        from migrate_news_sources import main
        main(["--db", str(db), "--apply"])
        # Second run should find no news.sources
        exit_code = main(["--db", str(db)])
        assert exit_code == 0

        settings = _read_settings(str(db))
        assert "sources" not in settings
        assert "topics" in settings

    def test_word_boundary_matching_prevents_false_positives(self, tmp_path):
        """'intel' inside 'artificial-intelligence' must NOT match hardware."""
        from migrate_news_sources import _match_topic_for_feed
        from newsbot.topics import DEFAULT_TOPIC_PACKS

        feed = {"name": "TechCrunch AI", "url": "https://techcrunch.com/category/artificial-intelligence/feed/"}
        topic = _match_topic_for_feed(feed, DEFAULT_TOPIC_PACKS)
        # Should match "ai" via keyword "ai" (word boundary), NOT "hardware" via "intel"
        assert topic == "ai"

    def test_preserves_existing_topics_override(self, tmp_path):
        """Existing news.topics overrides must be preserved."""
        db = tmp_path / "test.sqlite"
        _make_synth_db(db, sources={"rss": {"feeds": SYNTH_FEEDS[:2]}})

        # Pre-set a topics override
        conn = sqlite3.connect(str(db))
        existing = {"science": {"enabled": False}}
        conn.execute(
            "INSERT INTO settings(namespace, key, value_json, updated_at) VALUES(?,?,?,?)",
            ("news", "topics", json.dumps(existing), "2026-01-01T00:00:00+00:00"),
        )
        conn.commit()
        conn.close()

        from migrate_news_sources import main
        exit_code = main(["--db", str(db), "--apply"])
        assert exit_code == 0

        settings = _read_settings(str(db))
        topics = settings["topics"]
        # science override must be preserved
        assert "science" in topics
        assert topics["science"]["enabled"] is False
        # ai feeds must be added
        assert "ai" in topics
