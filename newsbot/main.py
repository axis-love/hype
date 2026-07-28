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
admin commands (/setstyle, /style, /digest, /post, /status, /help).
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
from core.log_sanitizer import redact_exception
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


def _validate_llm_env() -> None:
    """Validate that required LLM environment variables are set at startup.

    In scheduled mode, collectors run before the LLM client is built.
    This function catches missing env vars early instead of failing mid-generation.
    LM_API_KEY is required alongside LM_BASE and LM_MODEL — the LLM
    cannot authenticate without it.
    """
    errors: list[str] = []
    if not os.getenv("LM_BASE", "").strip():
        errors.append("LM_BASE is not set")
    if not os.getenv("LM_MODEL", "").strip():
        errors.append("LM_MODEL is not set")
    if not os.getenv("LM_API_KEY", "").strip():
        errors.append("LM_API_KEY is not set")
    if errors:
        raise RuntimeError("LLM configuration error: " + "; ".join(errors))


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


# Maximum concurrent collector coroutines.
MAX_CONCURRENT_COLLECTORS = 10
# Overall generation deadline (seconds). 2 LLM passes × 3 retries × 300s timeout
# = ~30 min worst case. 1200s (20 min) bounds this without cutting off healthy runs.
# Generation timeout: 600s (10 min). Operationally acceptable — allows
# collectors + LLM filter + LLM style to complete without a 25-min stall.
# Collectors have explicit per-request timeouts (15-30s), LLM has its own
# timeout (LM_TIMEOUT), and the shared semaphore bounds concurrency to 10.
GENERATION_TIMEOUT_SECONDS = 600

async def collect_all(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Run every enabled collector concurrently and merge results.

    Uses a semaphore to bound concurrency (MAX_CONCURRENT_COLLECTORS) so
    that a large number of RSS feeds or Reddit subreddits doesn't create
    hundreds of simultaneous connections.
    """
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

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_COLLECTORS)

    async def _bounded(coro):
        async with semaphore:
            return await coro

    coros = [_bounded(c) for _, c in tasks]
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

    sq = cfg.get("source_quota")
    source_quota = int(sq) if sq is not None else 8

    # Deterministic sort key: score desc, title asc, source asc, URL asc.
    # Used everywhere to ensure order-independent selection.
    def _sort_key(c: dict[str, Any]) -> tuple:
        return (
            -float(c.get("score") or 0.0),
            str(c.get("title") or ""),
            str(c.get("source") or ""),
            str(c.get("url") or ""),
        )

    # Group by source, sorted by score within each group.
    by_source: dict[str, list[dict[str, Any]]] = {}
    for c in scored:
        src = str(c.get("source") or "unknown")
        by_source.setdefault(src, []).append(c)
    for src in by_source:
        by_source[src].sort(key=_sort_key)

    # Order sources by their top item's score (descending), then alphabetically.
    # Uses the same key (including title) as the pool sort for consistency.
    source_order = sorted(
        by_source,
        key=lambda s: (
            -float(by_source[s][0].get("score") or 0.0),
            str(by_source[s][0].get("title") or ""),
            s,
        ),
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
    # Use the same deterministic key: score desc, title asc, source asc, URL asc.
    if len(top) < max_candidates:
        remaining = [c for c in scored if id(c) not in used]
        remaining.sort(key=_sort_key)
        for item in remaining:
            top.append(item)
            if len(top) >= max_candidates:
                break

    # Re-sort the final selection by score for the LLM filter.
    # Deterministic tie-break: score desc, title asc, source asc, URL asc.
    top.sort(key=_sort_key)
    return top


async def _run_generation(store: NewsStore, settings: SettingsStore) -> int:
    """Generation cycle: collect → filter → score → LLM filter → LLM style → store posts.

    The queue replacement is transactional: existing unposted items are not
    deleted until the new batch is fully built and ready. If any step fails
    (collection, LLM, insertion), the prior queue remains intact.

    Returns:
        0 — success: new posts were queued and seen-items marked.
        1 — failure: an error occurred (DB, LLM exception, etc.).
        3 — no-progress: no new posts produced (empty collection, all seen,
            LLM filter empty, styler empty). Distinct from success so the
            scheduler can decide whether to advance the timestamp.
    """
    cfg = load_config(settings)

    # NOTE: Do NOT clear unposted items here. The old queue stays intact
    # until the new batch is ready (transactional replacement at the end).

    # 1. Collect from every enabled source concurrently.
    log.info("collecting from %d source types", len(cfg["sources"]))
    candidates = await collect_all(cfg)
    if not candidates:
        log.warning("no candidates collected; keeping existing queue")
        return 3
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
        return 3

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
        return 3

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
        return 3

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


def _run_retention(store: NewsStore) -> None:
    """Run retention cleanup using configurable ages from env vars.

    Retention ages (days):
      NEWS_RETENTION_POSTED_DAYS (default 30) — posted_posts cleanup
      NEWS_RETENTION_SEEN_DAYS   (default 14)  — seen entries cleanup
      NEWS_RETENTION_DIGEST_DAYS  (default 90)  — digests cleanup

    Called on every generation cycle outcome (success, no-progress, failure)
    so cleanup is not skipped when generation produces no new posts.
    """
    posted_days = int(os.getenv("NEWS_RETENTION_POSTED_DAYS", "30"))
    seen_days = int(os.getenv("NEWS_RETENTION_SEEN_DAYS", "14"))
    digest_days = int(os.getenv("NEWS_RETENTION_DIGEST_DAYS", "90"))
    try:
        store.prune_posted_posts(max_age_days=posted_days)
        store.prune_seen(max_age_days=seen_days)
        store.prune_digests(max_age_days=digest_days)
    except Exception as exc:
        log.warning("retention cleanup failed: %s", exc)


async def _scheduler_gen_iteration(
    coordinator: JobCoordinator,
    store: NewsStore,
    settings: SettingsStore,
    gen_interval_s: float,
    *,
    timeout: float = 0,
) -> int:
    """One iteration of the generation scheduler loop.

    Returns:
        0 — generation succeeded, timestamp advanced.
        1 — generation failed, timestamp unchanged.
        2 — generation skipped (already running), timestamp unchanged.
        3 — generation no-progress, timestamp unchanged.

    Runs retention cleanup regardless of outcome.
    """
    last_gen_str = settings.get("scheduler", "last_gen_utc", default="") or ""
    now = datetime.now(timezone.utc)

    if last_gen_str:
        try:
            last_gen = datetime.fromisoformat(last_gen_str)
            if last_gen.tzinfo is None:
                last_gen = last_gen.replace(tzinfo=timezone.utc)
            elapsed = (now - last_gen).total_seconds()
            if elapsed < gen_interval_s:
                log.debug("next generation in %.0fs", gen_interval_s - elapsed)
                return 0  # not time yet — treat as idle success
        except (ValueError, TypeError):
            log.warning("invalid last_gen_utc: %s — generating now", last_gen_str)

    log.info("generation cycle starting at %s", now.isoformat())
    gen_success = False
    result = 1
    try:
        result = await coordinator.run_generation(
            lambda: _run_generation(store, settings),
            timeout=timeout,
        )
        if result == 0:
            gen_success = True
        elif result == 2:
            log.info("generation skipped — already in progress")
        elif result == 3:
            log.info("generation no-progress — will retry on next tick")
        else:
            log.error("generation failed (code=%d)", result)
    except Exception as exc:
        log.error("generation cycle failed: %s", redact_exception(exc))

    if gen_success:
        now = datetime.now(timezone.utc)
        settings.set("scheduler", "last_gen_utc", now.isoformat())
        log.info("generation cycle complete")
    else:
        log.warning("generation did not succeed — will retry on next tick (last_gen_utc unchanged)")

    _run_retention(store)
    return 0 if gen_success else result


async def _scheduler_post_iteration(
    coordinator: JobCoordinator,
    settings: SettingsStore,
    post_interval_s: float,
) -> int:
    """One iteration of the posting scheduler loop.

    Returns:
        0 — posting succeeded, timestamp advanced.
        1 — posting failed, timestamp unchanged.
        2 — posting skipped (already running), timestamp unchanged.
        3 — no pending posts, timestamp unchanged.
    """
    last_post_str = settings.get("scheduler", "last_post_utc", default="") or ""
    now = datetime.now(timezone.utc)

    if last_post_str:
        try:
            last_post = datetime.fromisoformat(last_post_str)
            if last_post.tzinfo is None:
                last_post = last_post.replace(tzinfo=timezone.utc)
            elapsed = (now - last_post).total_seconds()
            if elapsed < post_interval_s:
                log.debug("next post in %.0fs", post_interval_s - elapsed)
                return 0  # not time yet — idle success
        except (ValueError, TypeError):
            pass  # run now

    post_success = False
    result = 1
    try:
        result = await coordinator.run_posting()
        if result == 0:
            post_success = True
        elif result == 2:
            log.info("posting skipped — already in progress")
        elif result == 3:
            log.debug("no pending posts to deliver")
        else:
            log.error("posting failed (code=%d)", result)
    except Exception as exc:
        log.error("posting cycle failed: %s", redact_exception(exc))

    if post_success:
        now = datetime.now(timezone.utc)
        settings.set("scheduler", "last_post_utc", now.isoformat())
    else:
        log.warning("posting did not succeed — will retry on next tick (last_post_utc unchanged)")

    return result


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
                lambda: _run_generation(store, settings),
                timeout=GENERATION_TIMEOUT_SECONDS,
            )
            # Run retention after manual /digest too (same as scheduled).
            _run_retention(store)
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

        Delegates to _scheduler_gen_iteration() for the bookkeeping logic.
        """
        log.info("generation timer started: interval=%.1fh", gen_interval_hours)
        while True:
            await _scheduler_gen_iteration(
                coordinator, store, settings, gen_interval_s,
                timeout=GENERATION_TIMEOUT_SECONDS,
            )
            await asyncio.sleep(60)

    async def posting_loop() -> None:
        """Posting timer: posts one pending post on schedule.

        Delegates to _scheduler_post_iteration() for the bookkeeping logic.
        """
        log.info("posting timer started: interval=%.0fmin", post_interval_minutes)
        while True:
            await _scheduler_post_iteration(coordinator, settings, post_interval_s)
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
        store.close()


def main() -> None:
    """Entry point for `python -m newsbot.main`."""
    parser = argparse.ArgumentParser(description="News bot pipeline")
    parser.add_argument("--once", action="store_true", help="Run generation + drain all posts and exit")
    args = parser.parse_args()

    load_dotenv()
    configure_logging(process_name="newsbot")

    # Validate LLM env at startup (catches missing config before collectors run).
    _validate_llm_env()

    db_path = os.getenv("NEWS_DB", "data/newsbot.sqlite")
    settings: SettingsStore = default_store(db_path)

    if args.once:
        store = NewsStore(Path(db_path))

        async def _once() -> int:
            coordinator = JobCoordinator(store, settings)
            result = await coordinator.run_generation(
                lambda: _run_generation(store, settings),
                timeout=GENERATION_TIMEOUT_SECONDS,
            )
            # Run retention even on no-progress or failure.
            _run_retention(store)
            if result != 0 and result != 3:
                return 1
            # Drain all generated posts immediately for testing.
            return await coordinator.drain_posts()

        try:
            code = asyncio.run(_once())
        finally:
            store.close()
        sys.exit(code)

    # Scheduled mode: needs BOT_TOKEN for posting. Without it, run once for testing.
    if not os.getenv("BOT_TOKEN", "").strip() and not os.getenv("NEWS_INTERVAL_HOURS", "").strip():
        log.info("no BOT_TOKEN and no NEWS_INTERVAL_HOURS — running once (dry-run mode)")
        store = NewsStore(Path(db_path))

        async def _dry() -> int:
            coordinator = JobCoordinator(store, settings)
            await coordinator.run_generation(
                lambda: _run_generation(store, settings),
                timeout=GENERATION_TIMEOUT_SECONDS,
            )
            _run_retention(store)
            return await coordinator.drain_posts()

        try:
            code = asyncio.run(_dry())
        finally:
            store.close()
        sys.exit(code)

    try:
        asyncio.run(_scheduled_loop(settings))
    except KeyboardInterrupt:
        log.info("shutting down")
        sys.exit(0)


if __name__ == "__main__":
    main()