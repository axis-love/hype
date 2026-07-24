"""News bot entrypoint — the linear pipeline + scheduler loop.

Runs as a long-lived process inside Docker:

    python -m newsbot.main              # scheduled mode (default)
    python -m newsbot.main --once       # one-shot mode (dry runs, testing)

In scheduled mode, the pipeline runs at a configurable interval (default
every 8 hours) and the process stays alive between runs. The schedule
is tracked in SQLite so it survives restarts.

One invocation of _run_pipeline() does the full collect → filter-seen →
dedupe → score → LLM filter → LLM digest → post pipeline.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from core.logging_config import configure_logging
from core.settings_store import SettingsStore, default_store
from lm_client import LMClient

from newsbot.collectors import (
    hackernews as hn_collector,
    reddit as reddit_collector,
    github as github_collector,
    rss as rss_collector,
    producthunt as ph_collector,
    huggingface_papers as hf_collector,
)
from newsbot.config import load_config
from newsbot.db import NewsStore
from newsbot.dedupe import dedupe_and_merge
from newsbot.scoring import score_all
from newsbot.summarizer import llm_filter, llm_write_digest, select_diverse_top_items
from newsbot.telegram_poster import post_digest

log = logging.getLogger(__name__)

# Default interval between scheduled runs, in hours.
DEFAULT_INTERVAL_HOURS = 8


def _build_lm_client() -> LMClient:
    """Build the LMClient for the LLM digest writer from env (LM_BASE / LM_MODEL / LM_API_KEY)."""
    base = os.getenv("LM_BASE", "").rstrip("/")
    model = os.getenv("LM_MODEL", "")
    if not base or not model:
        raise RuntimeError("LM_BASE and LM_MODEL must be set in the environment")
    timeout = float(os.getenv("LM_TIMEOUT", "300"))
    headers = {}
    api_key = os.getenv("LM_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return LMClient(base, model, timeout, headers=headers, endpoint_path="/chat/completions")


def _build_filter_lm_client() -> LMClient:
    """Build the LMClient for the LLM filter pass. Uses LM_FILTER_MODEL if set, else LM_MODEL."""
    base = os.getenv("LM_BASE", "").rstrip("/")
    model = os.getenv("LM_FILTER_MODEL", "") or os.getenv("LM_MODEL", "")
    if not base or not model:
        raise RuntimeError("LM_BASE and LM_MODEL (or LM_FILTER_MODEL) must be set in the environment")
    timeout = float(os.getenv("LM_TIMEOUT", "300"))
    headers = {}
    api_key = os.getenv("LM_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return LMClient(base, model, timeout, headers=headers, endpoint_path="/chat/completions")


async def collect_all(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Run every enabled collector concurrently and merge results."""
    sources = cfg["sources"]
    tasks: list[tuple[str, Any]] = []

    if "hackernews" in sources:
        tasks.append(("hackernews", hn_collector.collect(sources["hackernews"])))
    if "reddit" in sources:
        tasks.append(("reddit", reddit_collector.collect(sources["reddit"])))
    if "github" in sources:
        tasks.append(("github", github_collector.collect(sources["github"])))
    if "huggingface_papers" in sources:
        tasks.append(("huggingface_papers", hf_collector.collect(sources["huggingface_papers"])))
    if "producthunt" in sources:
        tasks.append(("producthunt", ph_collector.collect(sources["producthunt"])))
    if "rss" in sources:
        tasks.append(("rss", rss_collector.collect(sources["rss"])))

    if not tasks:
        log.warning("no collectors enabled in config")
        return []

    coros = [c for _, c in tasks]
    batches = await asyncio.gather(*coros, return_exceptions=True)

    items: list[dict[str, Any]] = []
    for (name, _), batch in zip(tasks, batches):
        if isinstance(batch, Exception):
            log.warning("collector %s failed: %s", name, batch)
            continue
        log.info("collector %s returned %d items", name, len(batch))
        items.extend(batch)
    return items


def filter_seen(items: list[dict[str, Any]], store: NewsStore) -> list[dict[str, Any]]:
    """Drop items whose URL or title was already posted (per the seen table)."""
    kept: list[dict[str, Any]] = []
    for item in items:
        url = str(item.get("url") or "").strip()
        title = str(item.get("title") or "").strip()
        if store.is_seen(url, title):
            continue
        kept.append(item)
    dropped = len(items) - len(kept)
    if dropped:
        log.info("filter_seen dropped %d already-seen items", dropped)
    return kept


async def _run_pipeline() -> int:
    """One full pipeline run. Returns 0 on success, 1 on failure."""
    db_path = os.getenv("NEWS_DB", "data/newsbot.sqlite")
    settings: SettingsStore = default_store(db_path)
    cfg = load_config(settings)
    store = NewsStore(Path(db_path))

    # 1. Collect from every enabled source concurrently.
    log.info("collecting from %d source types", len(cfg["sources"]))
    candidates = await collect_all(cfg)
    if not candidates:
        log.warning("no candidates collected; nothing to do")
        return 0
    log.info("collected %d raw candidates", len(candidates))

    # 2. Drop already-seen items.
    candidates = filter_seen(candidates, store)

    # 3. Cross-source dedupe + merge engagement signals.
    candidates = dedupe_and_merge(candidates)

    # 4. Score by hype.
    candidates = score_all(candidates, cfg)
    candidates.sort(key=lambda c: float(c.get("score") or 0.0), reverse=True)

    # 5. Keep the top N for the LLM filter.
    top = candidates[: cfg["max_candidates"]]
    if not top:
        log.warning("no candidates after filtering; nothing to do")
        return 0
    log.info("top %d candidates (score >= %.1f) sent to LLM filter", len(top), float(top[-1].get("score") or 0.0))

    # 6. Pass A — LLM filter.
    filter_lm = _build_filter_lm_client()
    kept = await llm_filter(
        top,
        filter_lm,
        temperature=cfg["llm_temperature"],
        max_tokens=cfg["llm_max_tokens_filter"],
    )
    if not kept:
        log.warning("LLM filter kept zero items; nothing to post")
        return 0

    # 7. Select a diverse top-N for the digest.
    final = select_diverse_top_items(kept, cfg["max_final_news"])
    log.info("selected %d diverse items for the digest", len(final))

    # 8. Pass B — LLM digest writer.
    digest_lm = _build_lm_client()
    article = await llm_write_digest(
        final,
        digest_lm,
        temperature=cfg["llm_temperature"],
        max_tokens=cfg["llm_max_tokens_digest"],
    )
    if not article:
        log.warning("LLM digest writer produced empty output; nothing to post")
        return 0

    # 9. Persist the digest (history) before posting.
    store.insert_digest(article, digest_lm.model, len(final))

    # 10. Post to Telegram.
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    chat_id = os.getenv("NEWS_CHANNEL_ID", "").strip()
    if not bot_token or not chat_id:
        # Dry-run mode: print to stdout instead of posting. Useful for testing.
        log.warning("BOT_TOKEN or NEWS_CHANNEL_ID not set; dry-run (printing digest to stdout)")
        print(article)
        store.mark_seen(final)
        store.prune_old_items(cfg["item_prune_hours"])
        return 0

    await post_digest(article, bot_token=bot_token, chat_id=chat_id)

    # 11. Mark these items as seen so we don't repost them.
    store.mark_seen(final)
    store.prune_old_items(cfg["item_prune_hours"])
    log.info("run complete: posted digest with %d items", len(final))
    return 0


async def _scheduled_loop(settings: SettingsStore) -> None:
    """Long-running loop that executes the pipeline on a schedule.

    Interval is read from env NEWS_INTERVAL_HOURS (default 8) or the
    'news.schedule_interval_hours' setting. The last run time is
    persisted in SQLite so the schedule survives container restarts.
    """
    interval_hours = float(
        os.getenv("NEWS_INTERVAL_HOURS", "")
        or settings.get("news", "schedule_interval_hours", default=DEFAULT_INTERVAL_HOURS)
        or DEFAULT_INTERVAL_HOURS
    )
    interval_seconds = interval_hours * 3600

    log.info("scheduler started: interval=%.1fh", interval_hours)

    while True:
        # Check if it's time to run.
        last_run_str = settings.get("scheduler", "last_run_utc", default="") or ""
        now = datetime.now(timezone.utc)

        if last_run_str:
            try:
                last_run = datetime.fromisoformat(last_run_str)
                if last_run.tzinfo is None:
                    last_run = last_run.replace(tzinfo=timezone.utc)
                elapsed = (now - last_run).total_seconds()
                if elapsed < interval_seconds:
                    sleep_for = interval_seconds - elapsed
                    log.debug("next run in %.0fs (last run %s)", sleep_for, last_run_str)
                    await asyncio.sleep(min(sleep_for, 300))  # Check every 5 min max
                    continue
            except (ValueError, TypeError):
                log.warning("invalid last_run_utc in settings: %s — running now", last_run_str)

        log.info("scheduled run starting at %s", now.isoformat())
        try:
            await _run_pipeline()
        except Exception as exc:
            log.error("pipeline run failed: %s", exc, exc_info=True)

        now = datetime.now(timezone.utc)
        settings.set("scheduler", "last_run_utc", now.isoformat())
        log.info("scheduled run complete at %s, next in %.1fh", now.isoformat(), interval_hours)

        # Sleep in short intervals so the process is responsive to signals.
        await asyncio.sleep(60)


def main() -> None:
    """Entry point for `python -m newsbot.main`."""
    parser = argparse.ArgumentParser(description="News bot pipeline")
    parser.add_argument("--once", action="store_true", help="Run the pipeline once and exit")
    args = parser.parse_args()

    load_dotenv()
    configure_logging(process_name="newsbot")

    if args.once:
        code = asyncio.run(_run_pipeline())
        sys.exit(code)

    db_path = os.getenv("NEWS_DB", "data/newsbot.sqlite")
    settings: SettingsStore = default_store(db_path)

    # If NEWS_INTERVAL_HOURS=0 or not set and no BOT_TOKEN, run once for testing.
    if not os.getenv("BOT_TOKEN", "").strip() and not os.getenv("NEWS_INTERVAL_HOURS", "").strip():
        log.info("no BOT_TOKEN and no NEWS_INTERVAL_HOURS — running once (dry-run mode)")
        code = asyncio.run(_run_pipeline())
        sys.exit(code)

    try:
        asyncio.run(_scheduled_loop(settings))
    except KeyboardInterrupt:
        log.info("shutting down")
        sys.exit(0)


if __name__ == "__main__":
    main()