"""News bot entrypoint — split generation + posting pipeline.

Runs as a long-lived process inside Docker:

    python -m newsbot.main              # scheduled mode (default)
    python -m newsbot.main --once       # one-shot mode (dry runs, testing)

In scheduled mode, two timers run concurrently:

  - Generation (every NEWS_INTERVAL_HOURS, default 8h):
      collect → filter-seen → dedupe → score → LLM filter → LLM style
      → store 8 individual posts in pending_posts table.

  - Posting (every NEWS_POST_INTERVAL_MINUTES, default 60min):
      pull the oldest unposted post from pending_posts → post to
      Telegram → mark as posted.

A bot command handler (long polling) runs concurrently to accept
admin commands (/setstyle, /style, /run, /status, /help).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from core.logging_config import configure_logging
from core.settings_store import SettingsStore, default_store
from lm_client import LMClient

from newsbot.bot_commands import BotCommandHandler
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
from newsbot.jobs import JobCoordinator
from newsbot.scoring import score_all
from newsbot.summarizer import llm_filter, llm_style_posts, select_diverse_top_items

log = logging.getLogger(__name__)

# Default intervals.
DEFAULT_INTERVAL_HOURS = 8
DEFAULT_POST_INTERVAL_MINUTES = 60


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


async def _run_generation(store: NewsStore, settings: SettingsStore) -> int:
    """Generation cycle: collect → filter → score → LLM filter → LLM style → store posts.

    The queue replacement is transactional: existing unposted items are not
    deleted until the new batch is fully built and ready. If any step fails
    (collection, LLM, insertion), the prior queue remains intact.

    Returns 0 on success, 1 on failure.
    """
    cfg = load_config(settings)

    # NOTE: Do NOT clear unposted items here. The old queue stays intact
    # until the new batch is ready (transactional replacement at the end).

    # 1. Collect from every enabled source concurrently.
    log.info("collecting from %d source types", len(cfg["sources"]))
    candidates = await collect_all(cfg)
    if not candidates:
        log.warning("no candidates collected; keeping existing queue")
        return 0
    log.info("collected %d raw candidates", len(candidates))

    # 2. Drop already-seen items.
    candidates = filter_seen(candidates, store)

    # 3. Cross-source dedupe + merge engagement signals.
    candidates = dedupe_and_merge(candidates)

    # 4. Score by hype.
    candidates = score_all(candidates, cfg)
    candidates.sort(key=lambda c: float(c.get("score") or 0.0), reverse=True)

    # 5. Keep the top N for the LLM filter, dropping anything below min_score.
    #    Source diversity: guarantee minimum slots per source so one source
    #    (e.g. GitHub) can't crowd out all others.
    min_score = float(cfg.get("min_score") or 0.0)
    scored = [c for c in candidates if float(c.get("score") or 0.0) >= min_score]
    max_candidates = int(cfg["max_candidates"])

    # Group by source, sorted by score within each group.
    by_source: dict[str, list[dict[str, Any]]] = {}
    for c in scored:
        src = str(c.get("source") or "unknown")
        by_source.setdefault(src, []).append(c)
    for src in by_source:
        by_source[src].sort(key=lambda c: float(c.get("score") or 0.0), reverse=True)

    # Reserve guaranteed slots: each source gets up to `source_quota` items
    # before the remaining slots are filled by global score ranking.
    source_quota = int(cfg.get("source_quota") or 8)
    top: list[dict[str, Any]] = []
    used: set[int] = set()  # dedupe by id() to avoid double-counting

    # Phase 1: guaranteed slots per source (top-N from each, by score).
    for src in sorted(by_source, key=lambda s: by_source[s][0].get("score", 0), reverse=True):
        for item in by_source[src][:source_quota]:
            if id(item) not in used:
                top.append(item)
                used.add(id(item))
                if len(top) >= max_candidates:
                    break
        if len(top) >= max_candidates:
            break

    # Phase 2: fill remaining slots by global score ranking.
    if len(top) < max_candidates:
        remaining = [c for c in scored if id(c) not in used]
        remaining.sort(key=lambda c: float(c.get("score") or 0.0), reverse=True)
        for item in remaining:
            top.append(item)
            if len(top) >= max_candidates:
                break

    # Re-sort the final selection by score for the LLM filter.
    top.sort(key=lambda c: float(c.get("score") or 0.0), reverse=True)

    if not top:
        log.warning("no candidates above min_score=%.1f; nothing to do", min_score)
        return 0

    source_counts: dict[str, int] = {}
    for c in top:
        src = str(c.get("source") or "unknown")
        source_counts[src] = source_counts.get(src, 0) + 1
    log.info(
        "top %d candidates (score >= %.1f) sent to LLM filter — sources: %s",
        len(top), min_score, ", ".join(f"{k}={v}" for k, v in sorted(source_counts.items(), key=lambda x: -x[1])),
    )

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

    # 7. Select a diverse top-N for styling.
    final = select_diverse_top_items(kept, cfg["max_final_news"])
    log.info("selected %d diverse items for styling", len(final))

    # 8. Pass B — LLM styler (individual posts).
    style_lm = _build_lm_client()
    posts = await llm_style_posts(
        final,
        style_lm,
        style_prompt=cfg["style_prompt"],
        temperature=cfg["llm_temperature"],
        max_tokens=cfg["llm_max_tokens_digest"],
    )
    if not posts:
        log.warning("LLM styler produced zero posts; keeping existing queue")
        return 0

    # 9. Atomically replace unposted queue with new posts and mark items as seen.
    #    If insertion fails, the old queue remains intact (rollback).
    try:
        inserted, seen = store.replace_unposted_batch(posts, final)
    except sqlite3.Error as exc:
        log.error("transactional queue replacement failed: %s — keeping existing queue", exc)
        return 1

    log.info("queued %d posts for delivery (marked %d items as seen)", inserted, seen)
    store.prune_old_items(cfg["item_prune_hours"])

    return 0


async def _scheduled_loop(settings: SettingsStore) -> None:
    """Long-running loop with concurrent generation + posting timers.

    Generation runs every NEWS_INTERVAL_HOURS (default 8).
    Posting runs every NEWS_POST_INTERVAL_MINUTES (default 60).
    Bot command handler polls Telegram getUpdates concurrently.

    All jobs go through a JobCoordinator that serializes generation and
    posting, preventing overlap between scheduled and manual commands.
    """
    db_path = os.getenv("NEWS_DB", "data/newsbot.sqlite")
    store = NewsStore(Path(db_path))
    coordinator = JobCoordinator(store, settings)

    gen_interval_hours = float(
        os.getenv("NEWS_INTERVAL_HOURS", "")
        or settings.get("news", "schedule_interval_hours", default=DEFAULT_INTERVAL_HOURS)
        or DEFAULT_INTERVAL_HOURS
    )
    post_interval_minutes = float(
        os.getenv("NEWS_POST_INTERVAL_MINUTES", "")
        or settings.get("news", "post_interval_minutes", default=DEFAULT_POST_INTERVAL_MINUTES)
        or DEFAULT_POST_INTERVAL_MINUTES
    )
    gen_interval_s = gen_interval_hours * 3600
    post_interval_s = post_interval_minutes * 60

    # --- Bot command handler (optional) ---
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    admin_user_id = os.getenv("ADMIN_USER_ID", "").strip()
    bot_handler: BotCommandHandler | None = None

    if bot_token and admin_user_id:
        async def on_digest() -> None:
            ran = await coordinator.run_generation(
                lambda: _run_generation(store, settings)
            )
            if not ran:
                raise RuntimeError("generation already in progress — skipped")

        async def on_post() -> None:
            result = await coordinator.run_posting()
            if result == 2:
                raise RuntimeError("posting already in progress — skipped")
            if result != 0:
                raise RuntimeError(f"posting failed (code={result})")

        async def on_status() -> str:
            pending = store.count_pending()
            last_gen = settings.get("scheduler", "last_gen_utc", default="") or ""
            last_post = settings.get("scheduler", "last_post_utc", default="") or ""
            gen_status = "running" if coordinator.generation_running else "idle"
            post_status = "running" if coordinator.posting_running else "idle"
            return (
                f"Pending posts: {pending}\n"
                f"Last generation: {last_gen or 'never'}\n"
                f"Last post: {last_post or 'never'}\n"
                f"Generation: {gen_status} (interval: {gen_interval_hours:.1f}h)\n"
                f"Posting: {post_status} (interval: {post_interval_minutes:.0f}min)"
            )

        bot_handler = BotCommandHandler(
            bot_token=bot_token,
            admin_user_id=admin_user_id,
            settings=settings,
            on_digest=on_digest,
            on_post=on_post,
            on_status=on_status,
        )

    async def generation_loop() -> None:
        """Generation timer: runs the full pipeline on schedule."""
        log.info("generation timer started: interval=%.1fh", gen_interval_hours)
        while True:
            last_gen_str = settings.get("scheduler", "last_gen_utc", default="") or ""
            now = datetime.now(timezone.utc)

            if last_gen_str:
                try:
                    last_gen = datetime.fromisoformat(last_gen_str)
                    if last_gen.tzinfo is None:
                        last_gen = last_gen.replace(tzinfo=timezone.utc)
                    elapsed = (now - last_gen).total_seconds()
                    if elapsed < gen_interval_s:
                        sleep_for = gen_interval_s - elapsed
                        log.debug("next generation in %.0fs", sleep_for)
                        await asyncio.sleep(min(sleep_for, 300))
                        continue
                except (ValueError, TypeError):
                    log.warning("invalid last_gen_utc: %s — generating now", last_gen_str)

            log.info("generation cycle starting at %s", now.isoformat())
            try:
                await coordinator.run_generation(
                    lambda: _run_generation(store, settings)
                )
            except Exception as exc:
                log.error("generation cycle failed: %s", exc, exc_info=True)

            now = datetime.now(timezone.utc)
            settings.set("scheduler", "last_gen_utc", now.isoformat())
            log.info("generation cycle complete, next in %.1fh", gen_interval_hours)
            await asyncio.sleep(60)

    async def posting_loop() -> None:
        """Posting timer: posts one pending post on schedule."""
        log.info("posting timer started: interval=%.0fmin", post_interval_minutes)
        while True:
            last_post_str = settings.get("scheduler", "last_post_utc", default="") or ""
            now = datetime.now(timezone.utc)

            if last_post_str:
                try:
                    last_post = datetime.fromisoformat(last_post_str)
                    if last_post.tzinfo is None:
                        last_post = last_post.replace(tzinfo=timezone.utc)
                    elapsed = (now - last_post).total_seconds()
                    if elapsed < post_interval_s:
                        sleep_for = post_interval_s - elapsed
                        await asyncio.sleep(min(sleep_for, 60))
                        continue
                except (ValueError, TypeError):
                    pass  # run now

            try:
                await coordinator.run_posting()
            except Exception as exc:
                log.error("posting cycle failed: %s", exc, exc_info=True)

            now = datetime.now(timezone.utc)
            settings.set("scheduler", "last_post_utc", now.isoformat())
            await asyncio.sleep(30)

    tasks = [asyncio.create_task(generation_loop()), asyncio.create_task(posting_loop())]
    if bot_handler:
        tasks.append(asyncio.create_task(bot_handler.poll_loop()))

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        log.info("shutting down")
    finally:
        if bot_handler:
            await bot_handler.close()


def main() -> None:
    """Entry point for `python -m newsbot.main`."""
    parser = argparse.ArgumentParser(description="News bot pipeline")
    parser.add_argument("--once", action="store_true", help="Run generation + drain all posts and exit")
    args = parser.parse_args()

    load_dotenv()
    configure_logging(process_name="newsbot")

    db_path = os.getenv("NEWS_DB", "data/newsbot.sqlite")
    settings: SettingsStore = default_store(db_path)

    if args.once:
        store = NewsStore(Path(db_path))

        async def _once() -> int:
            coordinator = JobCoordinator(store, settings)
            ran = await coordinator.run_generation(
                lambda: _run_generation(store, settings)
            )
            if not ran:
                return 1
            # Drain all generated posts immediately for testing.
            return await coordinator.drain_posts()

        code = asyncio.run(_once())
        sys.exit(code)

    # Scheduled mode: needs BOT_TOKEN for posting. Without it, run once for testing.
    if not os.getenv("BOT_TOKEN", "").strip() and not os.getenv("NEWS_INTERVAL_HOURS", "").strip():
        log.info("no BOT_TOKEN and no NEWS_INTERVAL_HOURS — running once (dry-run mode)")
        store = NewsStore(Path(db_path))

        async def _dry() -> int:
            coordinator = JobCoordinator(store, settings)
            await coordinator.run_generation(
                lambda: _run_generation(store, settings)
            )
            return await coordinator.drain_posts()

        code = asyncio.run(_dry())
        sys.exit(code)

    try:
        asyncio.run(_scheduled_loop(settings))
    except KeyboardInterrupt:
        log.info("shutting down")
        sys.exit(0)


if __name__ == "__main__":
    main()