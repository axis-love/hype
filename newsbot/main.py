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
    failed_collectors: list[str] = []
    for (name, _), batch in zip(tasks, batches):
        if isinstance(batch, Exception):
            log.warning("collector %s failed: %s", name, batch)
            failed_collectors.append(name)
            continue
        log.info("collector %s returned %d items", name, len(batch))
        items.extend(batch)
    if failed_collectors and items:
        log.warning(
            "partial collection: %d/%d collectors failed (%s), proceeding with %d items",
            len(failed_collectors), len(tasks), ", ".join(failed_collectors), len(items),
        )
    return items


def filter_seen(items: list[dict[str, Any]], store: NewsStore) -> list[dict[str, Any]]:
    """Drop items whose URL or title was already posted (per the seen table).

    Uses batch SQL (is_seen_batch) instead of per-item queries.
    """
    if not items:
        return []
    seen_indices = store.is_seen_batch(items)
    kept = [item for i, item in enumerate(items) if i not in seen_indices]
    dropped = len(items) - len(kept)
    if dropped:
        log.info("filter_seen dropped %d already-seen items", dropped)
    return kept


def _select_diverse_candidates(
    scored: list[dict[str, Any]],
    max_candidates: int,
    cfg: dict[str, Any],
) -> list[dict[str, Any]]:
    """Select top candidates with guaranteed source diversity.

    Uses round-robin allocation: sources are ordered by their top score,
    and each source contributes one item per round until it exhausts its
    quota or all slots are filled. This ensures every source with eligible
    candidates gets at least one slot before any source gets a second.

    When a guarantee cannot be met (not enough eligible items), remaining
    slots are filled by global score ranking.
    """
    if not scored:
        return []

    source_quota = int(cfg.get("source_quota") or 8)

    # Group by source, sorted by score within each group.
    by_source: dict[str, list[dict[str, Any]]] = {}
    for c in scored:
        src = str(c.get("source") or "unknown")
        by_source.setdefault(src, []).append(c)
    for src in by_source:
        by_source[src].sort(key=lambda c: float(c.get("score") or 0.0), reverse=True)

    # Order sources by their top item's score (descending).
    source_order = sorted(
        by_source,
        key=lambda s: float(by_source[s][0].get("score") or 0.0),
        reverse=True,
    )

    top: list[dict[str, Any]] = []
    used: set[int] = set()

    # Phase 1: round-robin allocation — one item per source per round.
    # This ensures every source gets at least one slot before any gets two.
    rounds = min(source_quota, max_candidates)
    for round_idx in range(rounds):
        for src in source_order:
            if len(top) >= max_candidates:
                break
            items = by_source[src]
            if round_idx < len(items):
                item = items[round_idx]
                if id(item) not in used:
                    top.append(item)
                    used.add(id(item))
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
    return top


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
    #    Uses round-robin allocation so all sources get representation even
    #    when number_of_sources × quota > max_candidates.
    min_score = float(cfg.get("min_score") or 0.0)
    scored = [c for c in candidates if float(c.get("score") or 0.0) >= min_score]
    max_candidates = int(cfg["max_candidates"])

    top = _select_diverse_candidates(scored, max_candidates, cfg)

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

    # 9. Only mark seen the items that were actually styled into posts.
    #    Items omitted by the styler must NOT be marked seen — they should
    #    remain eligible for future generation cycles.
    styled_ids = {p.get("candidate_id") for p in posts if p.get("candidate_id")}
    seen_items = [item for item in final if item.get("candidate_id") in styled_ids]
    omitted = len(final) - len(seen_items)
    if omitted:
        log.warning("LLM styler omitted %d items — not marking them seen", omitted)

    # 10. Atomically replace unposted queue with new posts and mark items as seen.
    #     If insertion fails, the old queue remains intact (rollback).
    try:
        inserted, seen = store.replace_unposted_batch(posts, seen_items)
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
            result = await coordinator.run_generation(
                lambda: _run_generation(store, settings)
            )
            if result == 2:
                raise RuntimeError("generation already in progress — skipped")
            if result != 0:
                raise RuntimeError(f"generation failed (code={result})")

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
        """Generation timer: runs the full pipeline on schedule.

        last_gen_utc advances only after successful generation.
        Failed jobs retain the previous timestamp so the scheduler
        retries on the next tick (bounded by the 60s sleep).
        """
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
            gen_failed = False
            try:
                result = await coordinator.run_generation(
                    lambda: _run_generation(store, settings)
                )
                if result == 2:
                    log.info("generation skipped — already in progress")
                    # Skipped does NOT advance timestamp — next tick retries
                elif result == 0:
                    log.info("generation cycle complete")
                else:
                    log.error("generation failed (code=%d)", result)
                    gen_failed = True
            except Exception as exc:
                log.error("generation cycle failed: %s", exc, exc_info=True)
                gen_failed = True

            # Only advance last_gen_utc on success (result 0).
            if not gen_failed:
                now = datetime.now(timezone.utc)
                settings.set("scheduler", "last_gen_utc", now.isoformat())
                log.info("generation cycle complete, next in %.1fh", gen_interval_hours)
            else:
                log.warning("generation failed — will retry on next tick (last_gen_utc unchanged)")

            await asyncio.sleep(60)

    async def posting_loop() -> None:
        """Posting timer: posts one pending post on schedule.

        last_post_utc advances only after a successful post (or when
        there are no pending posts). Failed posts retain the previous
        timestamp so the scheduler retries on the next tick.
        """
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

            post_failed = False
            try:
                result = await coordinator.run_posting()
                if result == 2:
                    log.info("posting skipped — already in progress")
                    # Skipped does NOT advance timestamp
                    post_failed = True  # treat as non-success so timestamp unchanged
                # result 0 = success or no-op (no pending posts)
                # result 1 = failure
                if result == 1:
                    post_failed = True
            except Exception as exc:
                log.error("posting cycle failed: %s", exc, exc_info=True)
                post_failed = True

            # Only advance last_post_utc on success (or no pending posts).
            if not post_failed:
                now = datetime.now(timezone.utc)
                settings.set("scheduler", "last_post_utc", now.isoformat())
            else:
                log.warning("posting failed — will retry on next tick (last_post_utc unchanged)")

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
            result = await coordinator.run_generation(
                lambda: _run_generation(store, settings)
            )
            if result != 0:
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