"""Tests for scripts/migrate_news_sources.py — news.sources → news.topics migration.

H-8 regression tests cover:
  1. Exact-URL matching (empty URL, "Design Weekly"-style false positive)
  2. Full merged feeds list (pack defaults + migrated, deduped by URL)
  3. Refuse --apply on unhandled blocks/feeds
  4. SettingsStore reuse + validate_topic_overrides
"""
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

# A feed whose URL exactly matches an existing pack feed
_OPENAI_FEED = {"name": "OpenAI", "url": "https://openai.com/news/rss.xml", "weight": 1.3}

# A feed with no URL (empty string)
_EMPTY_URL_FEED = {"name": "Broken Feed", "url": "", "weight": 1.0}

# A feed whose name contains "ign" as a substring of "design"
_DESIGN_WEEKLY_FEED = {"name": "Design Weekly", "url": "https://example.com/design/feed"}


class TestMigrateNewsSources:
    def test_noop_when_sources_absent(self, tmp_path):
        """Live DB has no news.sources — migration is a no-op."""
        from migrate_news_sources import build_topics_override
        db = tmp_path / "test.sqlite"
        _make_synth_db(db, sources=None)

        import migrate_news_sources as m
        store = m.SettingsStore(m.SettingsStoreConfig(db_path=db))
        sources = store.get("news", "sources", default=None)
        store.close()
        assert sources is None
        # build_topics_override with empty sources returns empty dict
        result, unmatched, unhandled = build_topics_override({}, {}, None)
        assert result == {}
        assert unmatched == []
        assert unhandled == []

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
        _make_synth_db(db, sources={"rss": {"feeds": [_OPENAI_FEED]}})

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

    def test_apply_is_idempotent(self, tmp_path):
        """Re-running on an already-migrated DB is a no-op."""
        db = tmp_path / "test.sqlite"
        _make_synth_db(db, sources={"rss": {"feeds": [_OPENAI_FEED]}})

        from migrate_news_sources import main
        main(["--db", str(db), "--apply"])
        # Second run should find no news.sources
        exit_code = main(["--db", str(db)])
        assert exit_code == 0

        settings = _read_settings(str(db))
        assert "sources" not in settings
        assert "topics" in settings

    def test_preserves_existing_topics_override(self, tmp_path):
        """Existing news.topics overrides must be preserved."""
        db = tmp_path / "test.sqlite"
        _make_synth_db(db, sources={"rss": {"feeds": [_OPENAI_FEED]}})

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

    # ─── H-8 Regression: exact URL matching ───────────────────────────

    @pytest.mark.parametrize("label,feed", [
        ("empty URL", _EMPTY_URL_FEED),
        ("missing URL key", {"name": "No URL Feed"}),
        ("None URL", {"name": "None URL Feed", "url": None}),
    ])
    def test_exact_url_matching_empty_url_does_not_match(self, label, feed):
        """Empty/missing URL must not match any pack (was: '' in x → True)."""
        from migrate_news_sources import _match_topic_for_feed
        from newsbot.topics import DEFAULT_TOPIC_PACKS
        topic = _match_topic_for_feed(feed, DEFAULT_TOPIC_PACKS)
        assert topic is None, f"Empty/missing URL should not match, got {topic} ({label})"

    def test_exact_url_matching_design_weekly_does_not_match_gaming(self):
        """'Design Weekly' must NOT match gaming via 'ign' inside 'design'.

        Old substring matcher: pack_name 'IGN' (lowered 'ign') is a substring
        of 'design weekly' → false positive. Exact URL matching eliminates this.
        """
        from migrate_news_sources import _match_topic_for_feed
        from newsbot.topics import DEFAULT_TOPIC_PACKS
        topic = _match_topic_for_feed(_DESIGN_WEEKLY_FEED, DEFAULT_TOPIC_PACKS)
        assert topic is None, f"Design Weekly should not match any pack, got {topic}"

    def test_exact_url_matching_case_insensitive(self):
        """URL matching must be case-insensitive but exact (not substring)."""
        from migrate_news_sources import _match_topic_for_feed
        from newsbot.topics import DEFAULT_TOPIC_PACKS
        feed = {"name": "OpenAI", "url": "HTTPS://OPENAI.COM/news/RSS.xml"}
        topic = _match_topic_for_feed(feed, DEFAULT_TOPIC_PACKS)
        assert topic == "ai"

    def test_exact_url_matching_no_substring_false_positive(self):
        """A URL that merely contains a pack URL as substring must not match."""
        from migrate_news_sources import _match_topic_for_feed
        from newsbot.topics import DEFAULT_TOPIC_PACKS
        # 'https://openai.com/news/rss.xml' is a pack URL.
        # A feed with a URL that *contains* that string but is different:
        feed = {"name": "Evil", "url": "https://evil.com/https://openai.com/news/rss.xml"}
        topic = _match_topic_for_feed(feed, DEFAULT_TOPIC_PACKS)
        assert topic is None, f"Substring URL should not match, got {topic}"

    def test_no_keyword_matching_tier(self):
        """Keyword matching tier was deleted — keywords must not match feeds."""
        from migrate_news_sources import _match_topic_for_feed
        from newsbot.topics import DEFAULT_TOPIC_PACKS
        # A feed whose URL contains 'ai' (an ai-pack keyword) but doesn't
        # exactly match any pack feed URL should NOT match.
        feed = {"name": "Random AI Blog", "url": "https://random-ai-blog.example.com/feed"}
        topic = _match_topic_for_feed(feed, DEFAULT_TOPIC_PACKS)
        assert topic is None, f"Keyword matching should not fire, got {topic}"

    # ─── H-8 Regression: full merged feeds list ────────────────────────

    def test_full_merged_feeds_preserves_pack_defaults(self, tmp_path):
        """Migrating OpenAI must NOT drop Google DeepMind from the ai pack.

        Old behavior: override wrote {ai: {feeds: [OpenAI only]}} and
        merge_packs REPLACED the list → DeepMind lost.
        New behavior: override writes full merged list (DeepMind + OpenAI).
        """
        db = tmp_path / "test.sqlite"
        _make_synth_db(db, sources={"rss": {"feeds": [_OPENAI_FEED]}})

        from migrate_news_sources import main
        exit_code = main(["--db", str(db), "--apply"])
        assert exit_code == 0

        settings = _read_settings(str(db))
        ai_feeds = settings["topics"]["ai"]["feeds"]
        ai_urls = {f["url"] for f in ai_feeds}
        # Both the migrated feed AND the pack default must be present
        assert "https://openai.com/news/rss.xml" in ai_urls
        assert "https://deepmind.google/discover/blog/rss.xml" in ai_urls
        # No duplicates
        assert len(ai_urls) == len(ai_feeds), "Duplicate URLs in feeds list"

    def test_full_merged_feeds_deduped_by_url(self, tmp_path):
        """If a feed's URL already exists in the pack defaults, no duplicate."""
        # OpenAI is already in the ai pack defaults — migrating it should
        # produce exactly one entry for that URL, not two.
        db = tmp_path / "test.sqlite"
        _make_synth_db(db, sources={"rss": {"feeds": [_OPENAI_FEED]}})

        from migrate_news_sources import main
        main(["--db", str(db), "--apply"])

        settings = _read_settings(str(db))
        ai_feeds = settings["topics"]["ai"]["feeds"]
        openai_feeds = [f for f in ai_feeds if f["url"] == "https://openai.com/news/rss.xml"]
        assert len(openai_feeds) == 1, f"Expected 1 OpenAI feed, got {len(openai_feeds)}"

    def test_full_merged_feeds_preserves_other_topics(self, tmp_path):
        """Migrating only AI feeds must not affect gaming pack feeds."""
        db = tmp_path / "test.sqlite"
        _make_synth_db(db, sources={"rss": {"feeds": [_OPENAI_FEED]}})

        from migrate_news_sources import main
        main(["--db", str(db), "--apply"])

        settings = _read_settings(str(db))
        topics = settings["topics"]
        # Gaming was not touched by migration — no override should exist for it
        # (the override only contains topics that got feeds migrated into them)
        assert "gaming" not in topics

    def test_build_topics_override_returns_unmatched_feeds(self):
        """Unmatched feeds must be returned for reporting."""
        from migrate_news_sources import build_topics_override
        # A feed with a URL that doesn't match any pack feed exactly
        unmatched_feed = {"name": "Custom Blog", "url": "https://custom-blog.example.com/feed"}
        topics, unmatched, unhandled = build_topics_override(
            {"rss": {"feeds": [unmatched_feed]}}, {}, None
        )
        assert len(unmatched) == 1
        assert unmatched[0]["url"] == "https://custom-blog.example.com/feed"
        assert unhandled == []

    # ─── H-8 Regression: refuse --apply on unhandled blocks/feeds ──────

    def test_refuses_apply_on_unmatched_feeds(self, tmp_path, capsys):
        """--apply must refuse and return 1 when there are unmatched feeds."""
        db = tmp_path / "test.sqlite"
        _make_synth_db(db, sources={"rss": {"feeds": [
            {"name": "Custom Blog", "url": "https://custom-blog.example.com/feed"}
        ]}})

        from migrate_news_sources import main
        exit_code = main(["--db", str(db), "--apply"])
        assert exit_code == 1

        # DB must not be modified
        settings = _read_settings(str(db))
        assert "sources" in settings
        assert "topics" not in settings

        captured = capsys.readouterr()
        assert "REFUSING" in captured.out

    def test_refuses_apply_on_unhandled_non_rss_blocks(self, tmp_path, capsys):
        """--apply must refuse when non-RSS blocks (reddit, github) exist."""
        db = tmp_path / "test.sqlite"
        _make_synth_db(db, sources={
            "rss": {"feeds": [_OPENAI_FEED]},
            "reddit": {"subreddits": ["custom_sub"]},
        })

        from migrate_news_sources import main
        exit_code = main(["--db", str(db), "--apply"])
        assert exit_code == 1

        # DB must not be modified
        settings = _read_settings(str(db))
        assert "sources" in settings
        assert "topics" not in settings

        captured = capsys.readouterr()
        assert "REFUSING" in captured.out
        assert "reddit" in captured.out

    def test_dry_run_reports_unhandled_blocks_without_writing(self, tmp_path, capsys):
        """Dry-run must report unhandled blocks but not write anything."""
        db = tmp_path / "test.sqlite"
        _make_synth_db(db, sources={
            "rss": {"feeds": [_OPENAI_FEED]},
            "github": {"queries": ["custom-query"]},
        })

        from migrate_news_sources import main
        exit_code = main(["--db", str(db)])
        assert exit_code == 0  # dry-run succeeds even with blockers

        settings = _read_settings(str(db))
        assert "sources" in settings
        assert "topics" not in settings

        captured = capsys.readouterr()
        assert "github" in captured.out
        assert "cannot be migrated" in captured.out

    def test_apply_succeeds_when_all_feeds_matched_no_extra_blocks(self, tmp_path):
        """Clean migration: only RSS feeds, all match pack URLs exactly."""
        db = tmp_path / "test.sqlite"
        _make_synth_db(db, sources={"rss": {"feeds": [
            _OPENAI_FEED,
            {"name": "Google DeepMind", "url": "https://deepmind.google/discover/blog/rss.xml"},
            {"name": "IGN", "url": "https://feeds.ign.com/ign/all"},
        ]}})

        from migrate_news_sources import main
        exit_code = main(["--db", str(db), "--apply"])
        assert exit_code == 0

        settings = _read_settings(str(db))
        assert "sources" not in settings
        assert "topics" in settings

    # ─── H-8 Regression: SettingsStore reuse + validation ─────────────

    def test_uses_settings_store_not_raw_sqlite(self):
        """The script must import SettingsStore, not use raw sqlite3."""
        import migrate_news_sources as m
        assert hasattr(m, "SettingsStore")
        assert hasattr(m, "SettingsStoreConfig")
        # The raw sqlite3 module should NOT be imported
        assert not hasattr(m, "sqlite3"), "sqlite3 should not be imported — use SettingsStore"

    def test_dry_run_prints_validation_result(self, tmp_path, capsys):
        """Dry-run must print the validation result of the override."""
        db = tmp_path / "test.sqlite"
        _make_synth_db(db, sources={"rss": {"feeds": [_OPENAI_FEED]}})

        from migrate_news_sources import main
        main(["--db", str(db)])
        captured = capsys.readouterr()
        assert "=== validation ===" in captured.out
        assert "valid" in captured.out.lower()

    def test_validation_catches_unknown_topic_in_override(self, tmp_path, capsys):
        """If existing topics override has an unknown key, validation reports it."""
        db = tmp_path / "test.sqlite"
        _make_synth_db(db, sources={"rss": {"feeds": [_OPENAI_FEED]}})

        # Pre-set a topics override with an unknown topic name
        conn = sqlite3.connect(str(db))
        existing = {"nonexistent_topic": {"enabled": True}}
        conn.execute(
            "INSERT INTO settings(namespace, key, value_json, updated_at) VALUES(?,?,?,?)",
            ("news", "topics", json.dumps(existing), "2026-01-01T00:00:00+00:00"),
        )
        conn.commit()
        conn.close()

        from migrate_news_sources import main
        exit_code = main(["--db", str(db), "--apply"])
        # Validation error should block --apply
        assert exit_code == 1

        captured = capsys.readouterr()
        assert "validation error" in captured.out.lower() or "nonexistent_topic" in captured.out

        # DB must not be modified
        settings = _read_settings(str(db))
        assert "sources" in settings

    def test_settings_store_updated_at_precision(self, tmp_path):
        """SettingsStore uses timespec='seconds' — consistent timestamp precision."""
        from core.settings_store import SettingsStore, SettingsStoreConfig
        db = tmp_path / "test.sqlite"
        _make_synth_db(db)

        store = SettingsStore(SettingsStoreConfig(db_path=db))
        store.set("news", "test_key", {"a": 1})
        row = store._conn.execute(
            "SELECT updated_at FROM settings WHERE namespace='news' AND key='test_key'"
        ).fetchone()
        store.close()

        # SettingsStore._utc_now_iso uses timespec='seconds' — no microseconds
        ts = row["updated_at"]
        # Should end with '+00:00' (timezone offset, no fractional seconds)
        assert "+00:00" in ts
        assert "." not in ts or ts.count(".") == 0, f"Expected no fractional seconds, got {ts}"

    def test_apply_writes_via_settings_store(self, tmp_path):
        """After --apply, the data is written correctly via SettingsStore."""
        db = tmp_path / "test.sqlite"
        _make_synth_db(db, sources={"rss": {"feeds": [_OPENAI_FEED]}})

        from migrate_news_sources import main
        main(["--db", str(db), "--apply"])

        # Verify via SettingsStore (not raw sqlite3)
        from core.settings_store import SettingsStore, SettingsStoreConfig
        store = SettingsStore(SettingsStoreConfig(db_path=db))
        topics = store.get("news", "topics", default=None)
        sources = store.get("news", "sources", default=None)
        store.close()

        assert topics is not None
        assert "ai" in topics
        assert sources is None  # deleted
