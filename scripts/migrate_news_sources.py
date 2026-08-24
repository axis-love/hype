#!/usr/bin/env python3
"""Migrate legacy ``news.sources`` settings into ``news.topics`` overrides.

Before H-2/topic-packs, the operator could set ``news.sources`` in the
settings DB with a hand-crafted source block (typically ``{"rss": {\"feeds\":
[...]}}``). After topic packs, that override still wins over pack-derived
sources (see ``load_config``), so the packs never take effect.

This script reads ``news.sources`` if present, converts each RSS feed into
the appropriate ``news.topics`` override (adding the feed to the matching
topic pack's ``feeds`` list), and deletes the ``news.sources`` key so topic
packs become the source of truth.

H-8 hardening (all fixes verified by execution during code review):

1. **Exact URL matching only.**  Empty/missing feed URLs no longer
   substring-match every pack (``\"\" in x`` → True).  Pack-name substrings
   no longer match without word boundaries (``\"ign\" in \"design weekly\"``
   → True via ``\"ign\"`` inside ``\"design\"``).  Only exact URL equality
   matches a feed to a pack; everything else is reported as unmatched.

2. **Full merged feeds list.**  The override now writes the complete merged
   list (pack default feeds + migrated extras, deduped by URL) instead of
   only the migrated feeds.  Previously ``merge_packs`` REPLACED the entire
   feeds list, so migrating the OpenAI feed dropped Google DeepMind from
   collection.

3. **No silent data loss on --apply.**  Non-RSS blocks (``reddit``,
   ``github``, ``hackernews``, ``trends``, ``huggingface_papers``) and
   unmatched RSS feeds are reported.  The script refuses to ``--apply`` while
   any block or feed is unhandled — the operator must resolve them manually
   (add feeds to a topic pack, re-create subreddits/queries as topic-pack
   overrides, etc.) before the migration can proceed.

4. **SettingsStore reuse + validation.**  Raw sqlite3 readers/writers are
   replaced with ``SettingsStore.get/set/delete`` (single source of truth,
   consistent ``updated_at`` precision).  ``topics.validate_topic_overrides``
   runs on the final override before writing; dry-run prints the validation
   result too.

Usage::

    python scripts/migrate_news_sources.py --db data/newsbot.sqlite          # dry-run
    python scripts/migrate_news_sources.py --db data/newsbot.sqlite --apply

If ``news.sources`` is absent (the common case after H-2), the script
reports a no-op and exits 0.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Ensure repo root is importable for newsbot.topics / core.settings_store
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.settings_store import SettingsStore, SettingsStoreConfig
from newsbot.topics import (
    DEFAULT_TOPIC_PACKS,
    merge_packs,
    validate_topic_overrides,
)


# RSS is the only source block that maps cleanly to topic-pack feeds.
# All other blocks (reddit.subreddits, github.queries, etc.) are
# hand-tuned collections that have no topic-pack equivalent — they
# would be silently dropped if we proceeded.
_RSS_KEY = "rss"


def _match_topic_for_feed(feed: dict, topic_packs: dict) -> str | None:
    """Match a feed to a topic pack by exact URL equality.

    Only exact (case-insensitive) URL equality counts.  Substring matching,
    name matching, and keyword matching were all removed (H-8): each produced
    false positives — empty URLs matched every pack, and pack-name substrings
    matched without word boundaries (``\"ign\"`` inside ``\"design\"``).

    Returns the topic name or None if no exact URL match.
    """
    url = (feed.get("url") or "").strip().lower()
    if not url:
        return None

    for topic_name, pack in topic_packs.items():
        for pack_feed in pack.get("feeds") or []:
            pack_url = (pack_feed.get("url") or "").strip().lower()
            if pack_url and pack_url == url:
                return topic_name
    return None


def _build_pack_feeds_override(
    pack_name: str,
    default_feeds: list[dict],
    migrated_feeds: list[dict],
) -> list[dict]:
    """Build the full merged feeds list for a topic-pack override.

    Returns pack default feeds + migrated extras, deduped by URL.
    The result replaces the pack's feeds at override time — since
    ``merge_packs`` REPLACES list values (not extends), we must include
    the defaults or they vanish.
    """
    merged: list[dict] = []
    seen_urls: set[str] = set()

    for feed in default_feeds:
        url = (feed.get("url") or "").strip().lower()
        if url and url not in seen_urls:
            merged.append(dict(feed))
            seen_urls.add(url)

    for feed in migrated_feeds:
        url = (feed.get("url") or "").strip().lower()
        if url and url not in seen_urls:
            merged.append(dict(feed))
            seen_urls.add(url)

    return merged


def build_topics_override(
    sources: dict,
    topic_packs: dict,
    existing_topics: dict | None,
) -> tuple[dict, list[dict], list[str]]:
    """Convert ``news.sources`` RSS feeds into ``news.topics`` overrides.

    For each feed in ``sources[\"rss\"][\"feeds\"]``, match it to a topic pack
    by exact URL equality.  If matched, add the feed to that topic's full
    merged feeds override (pack defaults + migrated extras, deduped by URL).
    If no match, the feed is reported as unmatched.

    Returns a 3-tuple:
      - topics override dict (partial ``news.topics`` merged with existing)
      - list of unmatched feeds (for reporting)
      - list of unhandled non-RSS block names (for reporting)
    """
    topics = dict(existing_topics) if existing_topics else {}

    # Identify unhandled non-RSS blocks
    unhandled_blocks = [k for k in sources if k != _RSS_KEY]

    rss_cfg = sources.get(_RSS_KEY) or {}
    feeds = rss_cfg.get("feeds") or []

    if not feeds and not unhandled_blocks:
        return topics, [], []

    print(f"  Found {len(feeds)} RSS feed(s) in news.sources")
    matched = 0
    unmatched: list[dict] = []

    # Group migrated feeds by topic name
    migrated_by_topic: dict[str, list[dict]] = {}

    for feed in feeds:
        topic = _match_topic_for_feed(feed, topic_packs)
        if topic is None:
            unmatched.append(feed)
            continue
        migrated_by_topic.setdefault(topic, []).append(dict(feed))
        matched += 1
        print(f"    {feed.get('name', '?')} -> topic/{topic}")

    # Build full merged feeds list for each matched topic
    for topic_name, migrated_feeds in migrated_by_topic.items():
        default_feeds = topic_packs.get(topic_name, {}).get("feeds") or []
        full_feeds = _build_pack_feeds_override(
            topic_name, default_feeds, migrated_feeds
        )
        # Merge into existing override: preserve other keys (enabled, etc.),
        # but replace feeds with the full merged list.
        pack_override = dict(topics.get(topic_name, {}))
        pack_override["feeds"] = full_feeds
        topics[topic_name] = pack_override

    if unmatched:
        for f in unmatched:
            print(
                f"    WARNING: no exact URL match for feed: "
                f"{f.get('name', '?')} ({f.get('url', '?')})"
            )
        print(
            f"  {len(unmatched)} feed(s) unmatched — "
            "add them to a topic pack manually"
        )

    if unhandled_blocks:
        for block_name in unhandled_blocks:
            block = sources.get(block_name)
            print(
                f"    WARNING: non-RSS block '{block_name}' has no "
                f"topic-pack equivalent — will be dropped: "
                f"{json.dumps(block, ensure_ascii=False)[:200]}"
            )
        print(
            f"  {len(unhandled_blocks)} non-RSS block(s) cannot be migrated"
        )

    print(f"  Matched {matched}/{len(feeds)} feed(s) to topic packs")
    return topics, unmatched, unhandled_blocks


def apply_migration(db_path: str, topics_override: dict) -> None:
    """Write ``news.topics`` and delete ``news.sources`` via SettingsStore."""
    store = SettingsStore(SettingsStoreConfig(db_path=Path(db_path)))
    try:
        store.set("news", "topics", topics_override)
        store.delete("news", "sources")
    finally:
        store.close()


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

    print(f"Database: {db_path}")
    print(f"Mode: {'APPLY' if args.apply else 'DRY-RUN'}")
    print()

    # Step 1: read news.sources via SettingsStore
    store = SettingsStore(SettingsStoreConfig(db_path=Path(db_path)))
    try:
        sources: Any = store.get("news", "sources", default=None)
    finally:
        store.close()

    if sources is None:
        print("news.sources not found in settings DB — nothing to migrate.")
        return 0
    if not isinstance(sources, dict):
        print(
            f"ERROR: news.sources is not a dict (got {type(sources).__name__})"
        )
        return 1

    print("=== news.sources found ===")
    print(json.dumps(sources, indent=2, ensure_ascii=False))
    print()

    # Step 2: read existing news.topics (if any)
    store = SettingsStore(SettingsStoreConfig(db_path=Path(db_path)))
    try:
        existing_topics: Any = store.get("news", "topics", default=None)
    finally:
        store.close()

    if existing_topics:
        print("=== existing news.topics overrides ===")
        print(json.dumps(existing_topics, indent=2, ensure_ascii=False))
        print()

    # Step 3: build topics override
    print("=== migration plan ===")
    topics_override, unmatched_feeds, unhandled_blocks = build_topics_override(
        sources, DEFAULT_TOPIC_PACKS, existing_topics
    )

    # Step 4: validate the final override
    print()
    print("=== validation ===")
    errors = validate_topic_overrides(topics_override)
    if errors:
        for err in errors:
            print(f"  ERROR: {err}")
        print(f"  {len(errors)} validation error(s) — override is invalid")
    else:
        print("  Override is valid (all keys and fields are known)")
    print()

    # Determine if we can proceed
    can_apply = not errors and not unmatched_feeds and not unhandled_blocks

    print("=== resulting news.topics ===")
    print(json.dumps(topics_override, indent=2, ensure_ascii=False))
    print()
    print("=== actions ===")
    if not can_apply:
        blockers: list[str] = []
        if unmatched_feeds:
            blockers.append(f"{len(unmatched_feeds)} unmatched feed(s)")
        if unhandled_blocks:
            blockers.append(
                f"{len(unhandled_blocks)} unhandled non-RSS block(s)"
            )
        if errors:
            blockers.append(f"{len(errors)} validation error(s)")
        print(
            f"  REFUSING to apply: {'; '.join(blockers)}"
        )
        print(
            "  Resolve the above manually, then re-run with --apply."
        )
        print()
        if args.apply:
            print(
                "  --apply was requested but blocked — "
                "no changes were made to the DB."
            )
            return 1
        print("  Dry-run complete. Fix the above before applying.")
        return 0

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
