#!/usr/bin/env python3
"""Migrate legacy ``news.sources`` settings into ``news.topics`` overrides.

Before H-2/topic-packs, the operator could set ``news.sources`` in the
settings DB with a hand-crafted source block (typically ``{"rss": {"feeds":
[...]}}``). After topic packs, that override still wins over pack-derived
sources (see ``load_config``), so the packs never take effect.

This script reads ``news.sources`` if present, converts each RSS feed into
the appropriate ``news.topics`` override (adding the feed to the matching
topic pack's ``feeds`` list), and deletes the ``news.sources`` key so topic
packs become the source of truth.

Usage::

    python scripts/migrate_news_sources.py --db data/newsbot.sqlite          # dry-run
    python scripts/migrate_news_sources.py --db data/newsbot.sqlite --apply

If ``news.sources`` is absent (the common case after H-2), the script
reports a no-op and exits 0.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_sources(db_path: str) -> dict | None:
    """Read ``news.sources`` from the settings table. Returns None if absent."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT value_json FROM settings WHERE namespace='news' AND key='sources'"
    ).fetchone()
    conn.close()
    if row is None:
        return None
    return json.loads(row["value_json"])


def _load_topics(db_path: str) -> dict | None:
    """Read ``news.topics`` from the settings table. Returns None if absent."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT value_json FROM settings WHERE namespace='news' AND key='topics'"
    ).fetchone()
    conn.close()
    if row is None:
        return None
    return json.loads(row["value_json"])



def _match_topic_for_feed(feed: dict, topic_packs: dict) -> str | None:
    """Match a feed to a topic pack by URL or name.

    Priority: exact URL match against pack feeds > name match > keyword
    match with word boundaries (avoids 'intel' inside 'artificial').
    Returns the topic name or None if no match.
    """
    url = (feed.get("url") or "").lower()
    name = (feed.get("name") or "").lower()
    if not url and not name:
        return None

    # 1. Exact URL match against existing pack feeds
    for topic_name, pack in topic_packs.items():
        for pack_feed in pack.get("feeds") or []:
            pack_url = (pack_feed.get("url") or "").lower()
            if pack_url and (pack_url in url or url in pack_url):
                return topic_name
            pack_name = (pack_feed.get("name") or "").lower()
            if pack_name and pack_name in name:
                return topic_name

    # 2. Keyword match with word boundaries (prevents 'intel' in 'artificial')
    for topic_name, pack in topic_packs.items():
        keywords = pack.get("keywords") or []
        for kw in keywords:
            kw_lower = kw.lower()
            # Use word boundary so 'ar' doesn't match 'artificial'
            if re.search(r"\b" + re.escape(kw_lower) + r"\b", url) or \
               re.search(r"\b" + re.escape(kw_lower) + r"\b", name):
                return topic_name
    return None


def build_topics_override(
    sources: dict,
    topic_packs: dict,
    existing_topics: dict | None,
) -> dict:
    """Convert ``news.sources`` RSS feeds into ``news.topics`` overrides.

    For each feed in ``sources["rss"]["feeds"]``, match it to a topic pack.
    If matched, add the feed to that topic's ``feeds`` override. If no match,
    log a warning — the feed is dropped (it should be added manually to the
    appropriate topic pack or a custom pack).

    The returned dict is a partial ``news.topics`` override that can be
    merged with any existing overrides.
    """
    topics = dict(existing_topics) if existing_topics else {}
    rss_cfg = sources.get("rss") or {}
    feeds = rss_cfg.get("feeds") or []

    if not feeds:
        return topics

    print(f"  Found {len(feeds)} RSS feed(s) in news.sources")
    matched = 0
    unmatched: list[dict] = []
    for feed in feeds:
        topic = _match_topic_for_feed(feed, topic_packs)
        if topic is None:
            unmatched.append(feed)
            continue
        pack_override = topics.setdefault(topic, {})
        pack_feeds = pack_override.setdefault("feeds", [])
        # Avoid duplicates: don't add if URL already present
        existing_urls = {f.get("url") for f in pack_feeds}
        if feed.get("url") not in existing_urls:
            pack_feeds.append(dict(feed))
        matched += 1
        print(f"    {feed.get('name', '?')} -> topic/{topic}")

    if unmatched:
        for f in unmatched:
            print(f"    WARNING: no topic match for feed: {f.get('name', '?')} ({f.get('url', '?')})")
        print(f"  {len(unmatched)} feed(s) unmatched — add them to a topic pack manually")

    print(f"  Matched {matched}/{len(feeds)} feed(s) to topic packs")
    return topics


def apply_migration(db_path: str, topics_override: dict) -> None:
    """Write ``news.topics`` and delete ``news.sources``."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    topics_json = json.dumps(topics_override, ensure_ascii=False)
    conn.execute(
        """
        INSERT INTO settings(namespace, key, value_json, updated_at)
        VALUES('news', 'topics', ?, ?)
        ON CONFLICT(namespace, key) DO UPDATE SET
          value_json = excluded.value_json,
          updated_at = excluded.updated_at
        """,
        (topics_json, _utc_now_iso()),
    )
    conn.execute(
        "DELETE FROM settings WHERE namespace='news' AND key='sources'"
    )
    conn.commit()
    conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Migrate news.sources into news.topics overrides."
    )
    parser.add_argument(
        "--db", required=True, help="Path to newsbot.sqlite"
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Apply the migration (default: dry-run only)"
    )
    args = parser.parse_args(argv)

    db_path = args.db
    if not Path(db_path).exists():
        print(f"ERROR: database not found: {db_path}")
        return 1

    # Import topic packs from the repo
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from newsbot.topics import DEFAULT_TOPIC_PACKS

    print(f"Database: {db_path}")
    print(f"Mode: {'APPLY' if args.apply else 'DRY-RUN'}")
    print()

    # Step 1: read news.sources
    sources = _load_sources(db_path)
    if sources is None:
        print("news.sources not found in settings DB — nothing to migrate.")
        return 0

    print("=== news.sources found ===")
    print(json.dumps(sources, indent=2, ensure_ascii=False))
    print()

    # Step 2: read existing news.topics (if any)
    existing_topics = _load_topics(db_path)
    if existing_topics:
        print("=== existing news.topics overrides ===")
        print(json.dumps(existing_topics, indent=2, ensure_ascii=False))
        print()

    # Step 3: build topics override
    print("=== migration plan ===")
    topics_override = build_topics_override(sources, DEFAULT_TOPIC_PACKS, existing_topics)

    print()
    print("=== resulting news.topics ===")
    print(json.dumps(topics_override, indent=2, ensure_ascii=False))
    print()
    print("=== actions ===")
    print(f"  1. {'WRITE' if args.apply else 'DRY-RUN'} news.topics (merge with existing)")
    print(f"  2. {'DELETE' if args.apply else 'DRY-RUN'} news.sources key")
    if not args.apply:
        print()
        print("  Dry-run complete. Re-run with --apply to execute.")
    else:
        apply_migration(db_path, topics_override)
        print()
        print("  Migration applied successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
