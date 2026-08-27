"""News bot entrypoint — split generation + posting pipeline.

Runs as a long-lived process inside Docker:

    python -m newsbot.main              # scheduled mode (default)
    python -m newsbot.main --once       # one-shot mode (dry runs, testing)

In scheduled mode, two wall-clock slot loops run concurrently
(local time from NEWS_TZ, default Asia/Bangkok):

  - Generation slots (NEWS_GEN_HOURS, default "5,9,13,17,21"):
      collect → filter-seen → dedupe → score → LLM filter → store raw
      candidates. Catch-up: a missed slot still fires once after downtime.

  - Post slots (every even hour, never backfilled):
      pick the hottest candidate above the temperature threshold → LLM
      style → post to Telegram → mark posted.

A bot command handler (long polling) runs concurrently to accept
admin commands (/setstyle, /style, /digest, /post, /status, /help).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from core.logging_config import configure_logging
from core.log_sanitizer import redact_exception
from core.settings_store import SettingsStore, default_store
from lm_client import LMClient

from newsbot.bot_commands import BotCommandHandler
from newsbot.clock import gen_slots, latest_due_gen_slot, local_now, post_slot, summary_day, DEFAULT_GEN_HOURS
from newsbot.collectors import (
    hackernews,
    reddit,
    github,
    rss,
    huggingface_papers,
    trends,
)
from newsbot.collectors.base import Candidate

# Registry: source key -> collector module. Each module exposes
# ``async def collect(config: dict) -> list[Candidate]``.
# Adding a collector is one line here + one entry in config sources.
COLLECTORS: dict[str, Any] = {
    "hackernews": hackernews,
    "reddit": reddit,
    "github": github,
    "rss": rss,
    "huggingface_papers": huggingface_papers,
    "trends": trends,
}
from newsbot.config import load_config
from newsbot.db import NewsStore, _as_dict
from newsbot.dedupe import dedupe_and_merge, match_candidate_to_store, _set_pre_merge_weights
from newsbot.jobs import (
    JobCoordinator,
    _env_float,
    _format_recap_html_fallback,
    _row_to_styler_input,
    format_post_message,
)
from newsbot.images import extract_article_media
from newsbot.richmd import render_post, render_post_blocks, render_recap, signature_for
from newsbot.selection import pick_hottest, select_diverse_candidates
from newsbot.telegram_poster import post_digest, post_rich_message, RichSendRejected
from newsbot.summarizer import (
    _assign_candidate_ids,
    llm_daily_summary,
    llm_filter,
    llm_style_posts,
    select_diverse_top_items,
)
from newsbot.scoring import current_temperature, score_all

log = logging.getLogger(__name__)


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

    Also validates numeric env vars for early failure detection.
    """
    errors: list[str] = []
    if not os.getenv("LM_BASE", "").strip():
        errors.append("LM_BASE is not set")
    if not os.getenv("LM_MODEL", "").strip():
        errors.append("LM_MODEL is not set")
    if not os.getenv("LM_API_KEY", "").strip():
        errors.append("LM_API_KEY is not set")
    # Validate numeric env vars.
    lm_timeout = os.getenv("LM_TIMEOUT", "300")
    try:
        t = float(lm_timeout)
        if t <= 0:
            errors.append(f"LM_TIMEOUT must be positive, got {t}")
    except ValueError:
        errors.append(f"LM_TIMEOUT must be numeric, got {lm_timeout!r}")
    # Validate ADMIN_USER_ID if set.
    admin_id = os.getenv("ADMIN_USER_ID", "").strip()
    if admin_id:
        if not admin_id.lstrip("-").isdigit():
            errors.append(f"ADMIN_USER_ID must be numeric, got {admin_id!r}")
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

    for name, module in COLLECTORS.items():
        if name in sources:
            tasks.append((name, module.collect(sources[name])))

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





@dataclass
class GenerationPipelineResult:
    """Structured result of the pure generation pipeline (no DB writes).

    Funnel counts at each stage + final items annotated with their
    classification (add vs merge-against existing row). Used by both
    _run_generation (which applies writes) and /digest dry (which
    reports without writing).
    """
    collected: int
    unseen: int
    deduped: int
    above_min_score: int
    sent_to_filter: int
    llm_kept: int
    final_count: int
    items: list[dict[str, Any]]  # each carries 'action': 'add' or 'merge', 'merge_row_id': int | None
    failed_collectors: list[str]


async def _run_generation_pipeline(
    store: NewsStore, cfg: dict[str, Any],
) -> GenerationPipelineResult | None:
    """Pure pipeline: collect → filter_seen → dedupe → score → LLM filter →
    store-match classification. NO DB writes, NO seen-marking.

    Returns None if the pipeline produces nothing (empty collection,
    all-seen, or LLM filter empty). The caller decides what to do.
    """
    _set_pre_merge_weights(cfg.get("source_weights") or {})

    # 1. Collect.
    candidates = await collect_all(cfg)
    if not candidates:
        return None
    collected = len(candidates)

    # 2. Filter seen.
    candidates = filter_seen(candidates, store)
    unseen = len(candidates)

    # 3. Dedupe + merge.
    candidates = dedupe_and_merge(candidates)
    deduped = len(candidates)

    # 4. Score.
    candidates = score_all(candidates, cfg)
    candidates.sort(key=lambda c: float(c.get("score") or 0.0), reverse=True)

    # 5. Min_score + diverse selection.
    min_score = float(cfg.get("min_score") or 0.0)
    scored = [c for c in candidates if float(c.get("score") or 0.0) >= min_score]
    above_min_score = len(scored)
    top = select_diverse_candidates(scored, int(cfg["max_candidates"]), cfg)
    sent_to_filter = len(top)
    if not top:
        return None

    # 6. LLM filter.
    _assign_candidate_ids(top)

    # 6b. Log each candidate sent to the LLM filter (score_candidate event).
    for rank, c in enumerate(top, start=1):
        bd = c.get("score_breakdown") or {}
        log_line = json.dumps({
            "event": "score_candidate",
            "id": c.get("candidate_id"),
            "rank": rank,
            "score": float(c.get("score") or 0.0),
            "scored_at": bd.get("scored_at", ""),
            "source": str(c.get("source") or ""),
            "title": str(c.get("title") or "")[:80],
            "published_at": str(c.get("published_at") or "") if c.get("published_at") else "",
            "upvotes": c.get("upvotes") or 0,
            "comments": c.get("comments") or 0,
            "stars": c.get("stars") or 0,
            "reposts": c.get("reposts") or 0,
            "crosspost_count": c.get("crosspost_count") or 1,
            "engagement": float(bd.get("engagement") or 0.0),
            "recency": float(bd.get("recency") or 0.0),
            "source_weight": float(bd.get("source_weight")) if bd.get("source_weight") is not None else 1.0,
            "topic_bonus": int(bd.get("topic_bonus") or 0),
            "crosspost_bonus": float(bd.get("crosspost_bonus") or 0.0),
            "penalty": float(bd.get("penalty")) if bd.get("penalty") is not None else 1.0,
            "matched_topics": bd.get("matched_topics") or [],
            "origin_topic": bd.get("origin_topic") or "",
        })
        log.info(log_line)

    filter_lm = _build_filter_lm_client()
    kept = await llm_filter(
        top, filter_lm,
        temperature=cfg["llm_temperature"],
        max_tokens=cfg["llm_max_tokens_filter"],
    )
    if not kept:
        return None
    llm_kept = len(kept)

    # 7. Diverse top-N.
    final = select_diverse_top_items(kept, cfg["max_final_news"])
    final = [_as_dict(item) for item in final]

    # 8. Classify against store (add vs merge).
    #    Use list_merge_target_rows (unposted + recently posted) so a story
    #    arriving from a different source can merge into a posted row
    #    instead of being inserted as a duplicate (flow_001123).
    merge_window_days = int(os.getenv("NEWS_MERGE_WINDOW_DAYS", "7"))
    store_rows = store.list_merge_target_rows(merge_window_days)
    items: list[dict[str, Any]] = []
    for item in final:
        hit = match_candidate_to_store(item, store_rows)
        if hit:
            items.append({**item, "action": "merge", "merge_row_id": hit["id"]})
        else:
            items.append({**item, "action": "add", "merge_row_id": None})

    return GenerationPipelineResult(
        collected=collected,
        unseen=unseen,
        deduped=deduped,
        above_min_score=above_min_score,
        sent_to_filter=sent_to_filter,
        llm_kept=llm_kept,
        final_count=len(items),
        items=items,
        failed_collectors=[],  # collect_all logs internally; future: return tuple
    )


async def _run_generation(store: NewsStore, settings: SettingsStore) -> int:
    """Generation cycle: collect → filter → score → LLM filter → store.

    v2 additive pipeline: digest fills the store with RAW scored stories
    (body=''), merging duplicates into existing rows — no styling pass.
    Styling happens at pick time (jobs). If the append fails, the store
    keeps any merges already applied; nothing is ever bulk-deleted.

    Returns:
        0 — success: store updated (rows appended and/or merged), survivors
            marked seen. Empty appends with non-empty merges still count
            as success.
        1 — failure: an error occurred (DB, LLM exception, etc.).
        3 — no-progress: nothing to do (empty collection, all seen, LLM
            filter empty). Distinct from success so the scheduler can
            decide whether to advance the timestamp.
    """
    cfg = load_config(settings)

    # Run the pure pipeline (collect → filter → dedupe → score → LLM filter → classify).
    # No DB writes — the pipeline only reads the store for seen-filtering and classification.
    pipeline = await _run_generation_pipeline(store, cfg)
    if pipeline is None:
        log.warning("generation pipeline produced nothing; keeping existing queue")
        return 3

    log.info(
        "generation funnel: collected %d → unseen %d → deduped %d → above_min %d → filter %d → kept %d → final %d",
        pipeline.collected, pipeline.unseen, pipeline.deduped,
        pipeline.above_min_score, pipeline.sent_to_filter,
        pipeline.llm_kept, pipeline.final_count,
    )

    # Separate adds from merges for the write phase.
    to_add: list[dict[str, Any]] = []
    merges: list[tuple[int, dict[str, Any]]] = []  # (store_row_id, item)
    for item in pipeline.items:
        if item.get("action") == "merge" and item.get("merge_row_id") is not None:
            merges.append((int(item["merge_row_id"]), item))
        else:
            to_add.append(item)

    # 9. Merges: fold each duplicate into its existing store row.
    #    Pass ALL contributing URLs (from in-cycle merges) into the store
    #    row so they're persisted in merged_urls and can match future
    #    re-collected permalinks (flow_001123). A single call per
    #    candidate — merge_into_store_row accepts a list and increments
    #    merge_count exactly once regardless of URL count.
    for row_id, item in merges:
        candidate_url = str(item.get("url") or "")
        contributing = item.get("contributing_urls") or []
        urls_to_merge = [candidate_url] + [
            u for u in contributing if u and u != candidate_url
        ]
        store.merge_into_store_row(row_id, item, urls_to_merge)
        updated = store._conn.execute(
            "SELECT merge_count FROM pending_posts WHERE id=?", (row_id,)
        ).fetchone()
        log.info(
            "merged %r into store row %d (merge_count=%d)",
            str(item.get("url") or item.get("title") or "?"),
            row_id,
            int(updated["merge_count"]) if updated else -1,
        )

    # 10. Append new raw stories (body='') and mark ALL survivors seen —
    #     added and merged alike; they live in the store now.
    #     Include contributing URLs in seen-marking so a recycled permalink
    #     is dropped at filter_seen next cycle (flow_001123).
    final = [item for item in pipeline.items]
    # Build seen_items with contributing URLs appended.
    seen_items: list[dict[str, Any]] = []
    for item in final:
        seen_items.append(item)
        for curl in (item.get("contributing_urls") or []):
            cs = str(curl or "").strip()
            if cs:
                seen_items.append({"url": cs, "title": str(item.get("title") or "")})
    try:
        inserted = store.add_stories_to_store(to_add, seen_items=seen_items)
    except sqlite3.Error as exc:
        log.error("additive store insert failed: %s — merges already applied", exc)
        return 1
    log.info("appended %d raw stories (%d merged into existing rows)", inserted, len(merges))

    # 11. Eviction: trim the store back to NEWS_STORE_CAP, coldest first.
    now_utc = datetime.now(timezone.utc)
    post_rows = store.list_store_rows()
    temps = {r["id"]: current_temperature(r, cfg, now=now_utc) for r in post_rows}
    cap = int(os.getenv("NEWS_STORE_CAP", "36"))
    evicted = store.evict_coldest(temps, cap=cap)
    if evicted:
        remaining_ids = {r["id"] for r in store.list_store_rows()}
        gone = sorted(
            ((tid, t) for tid, t in temps.items() if tid not in remaining_ids),
            key=lambda kv: kv[1],
        )
        for tid, t in gone:
            title = next((r["title"] for r in post_rows if r["id"] == tid), "?")
            log.info("evicted coldest row %d (%s, temp=%.2f)", tid, title, t)

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
    try:
        store.prune_posted_posts(max_age_days=posted_days)
        store.prune_seen(max_age_days=seen_days)
    except Exception as exc:
        log.warning("retention cleanup failed: %s", exc)


def _recap_input_items(rows: list[dict]) -> list[dict[str, Any] | Candidate]:
    """Build the item list llm_daily_summary receives, from posted store rows.

    Rows carry the STYLED content actually posted: set_styled_content
    overwrites title/body before posting, so for styled rows these fields
    are exactly what the channel saw. Legacy rows posted before styling
    (body empty, styled_at NULL) fall back to the raw snippet.
    """
    items: list[dict[str, Any] | Candidate] = []
    for row in rows:
        body = (row.get("body") or "").strip()
        if not body:
            body = (row.get("snippet") or "").strip()
        items.append({
            "title": row.get("title") or "",
            "body": body,
            "category": row.get("category") or "",
            "url": row.get("url") or "",
            "source": row.get("source") or "",
            "posted_at": row.get("posted_at") or "",
            "message_id": row.get("message_id"),
        })
    return items


def _format_recap_input_sheet(items: list[dict[str, Any] | Candidate]) -> str:
    """Render the /recap input sheet: exactly what the LLM receives.

    Item count, 24h window, and per item: title, category, source,
    posted time. Plain text — this is a transparency aid, not a post.
    """
    lines = [
        f"Recap input — {len(items)} posts from the last 24h:",
        "",
    ]
    for idx, item in enumerate(items, start=1):
        lines.append(f"{idx}. {item.get('title') or '(untitled)'}")
        meta_bits = [
            item.get("category") or "",
            item.get("source") or "",
            item.get("posted_at") or "",
        ]
        lines.append("   " + " | ".join(b for b in meta_bits if b))
    return "\n".join(lines)


async def _run_summary(store: NewsStore, settings: SettingsStore, now: datetime) -> int:
    """Build and deliver the daily recap of the last 24h of posted news.

    Returns:
        0 — summary generated, delivered, and recorded.
        1 — failure (LLM or delivery) — day NOT consumed, retry next tick.
        3 — skipped: nothing posted in the window. Day IS consumed —
            there is nothing to recap and retrying would be pointless.
    """
    day = summary_day(now)
    since_utc = (now.astimezone(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
    rows = store.list_posted_since(since_utc)
    if not rows:
        log.info("daily summary: no posts in the last 24h — skipping day %s", day)
        return 3

    cfg = load_config(settings)
    items = _recap_input_items(rows)

    try:
        result = await llm_daily_summary(
            items, _build_lm_client(), recap_prompt=cfg["recap_prompt"],
        )
    except Exception as exc:
        log.error("daily summary LLM call failed: %s", redact_exception(exc))
        return 1
    if not result:
        log.error("daily summary LLM returned nothing — will retry")
        return 1

    bot_token = os.getenv("BOT_TOKEN", "").strip()
    chat_id = os.getenv("NEWS_CHANNEL_ID", "").strip()

    # Build rich markdown recap + HTML fallback for sendRichMessage failure.
    markdown = render_recap(
        result["title"], result["items"], chat_id=chat_id,
        signature=signature_for(chat_id),
    )
    html_fallback = _format_recap_html_fallback(
        result["title"], result["items"], chat_id=chat_id,
    )

    if not bot_token or not chat_id:
        log.info("dry-run: daily summary to stdout (no BOT_TOKEN/NEWS_CHANNEL_ID)")
        print(markdown)
    else:
        try:
            await post_rich_message(markdown, bot_token=bot_token, chat_id=chat_id)
        except RichSendRejected:
            log.warning("rich recap rejected — falling back to HTML sendMessage")
            try:
                await post_digest(html_fallback, bot_token=bot_token, chat_id=chat_id)
            except Exception as exc:
                log.error("daily summary HTML fallback also failed — will retry: %s", redact_exception(exc))
                return 1
        except Exception as exc:
            log.error("daily summary delivery failed — will retry: %s", redact_exception(exc))
            return 1

    try:
        store.add_summary(day, markdown, os.getenv("LM_MODEL", ""), len(items))
    except Exception as db_exc:
        # day UNIQUE constraint fires on a re-delivery — not an error for us.
        log.warning("daily summary already recorded for %s: %s", day, redact_exception(db_exc))
    return 0


async def _scheduler_summary_iteration(
    coordinator: JobCoordinator,
    store: NewsStore,
    settings: SettingsStore,
    *,
    now: datetime | None = None,
) -> int:
    """One iteration of the daily summary scheduler.

    Fires once per local day, the first tick at or after 13:00.
    Key ``scheduler.last_summary_day`` (= ``YYYY-MM-DD``): written on
    success AND on skip (nothing posted); left unset on failure so the
    next tick retries.
    """
    if now is None:
        now = local_now()
    if now.hour < 13:
        return 0  # not yet

    day = summary_day(now)
    last_summary_day = settings.get("scheduler", "last_summary_day", default="") or ""
    if last_summary_day == day:
        return 0  # already ran today

    result = await coordinator.run_summary(lambda: _run_summary(store, settings, now))
    if result in (0, 3):
        settings.set("scheduler", "last_summary_day", day)
    else:
        log.warning("daily summary did not succeed (code=%d) — will retry for day %s", result, day)
    return result


def _pick_snapshot(store: NewsStore, config: dict[str, Any]) -> tuple[Any, float, float, float, float]:
    """Run pick_hottest over the current store rows (pure — no delivery).

    Returns (PickResult, floor, ratio, merge_bonus, merge_cap) so callers
    (/scores, /status) share the exact same numbers the poster uses.
    """
    rows = store.list_store_rows()
    now = datetime.now(timezone.utc)
    floor = _env_float("NEWS_TEMP_FLOOR", "35")
    ratio = _env_float("NEWS_THRESHOLD_RATIO", "0.5")
    merge_bonus = _env_float("NEWS_MERGE_BONUS", "0.2")
    merge_cap = _env_float("NEWS_MERGE_CAP", "2.0")
    result = pick_hottest(rows, config, now=now, floor=floor, ratio=ratio, merge_bonus=merge_bonus, merge_cap=merge_cap)
    return result, floor, ratio, merge_bonus, merge_cap


def _format_scores(store: NewsStore, config: dict[str, Any]) -> str:
    """Format hype scores for all store rows (for /scores command).

    Built on the same pick_hottest call the poster uses: one pass yields
    current temperatures, the live threshold, and the median. Rows are
    sorted hottest-first by EFFECTIVE temperature (raw × merge multiplier);
    legacy rows (NULL score columns) have no reconstructable temperature
    and sink to the bottom marked 'score unavailable'.
    """
    from newsbot.scoring import merge_multiplier

    result, floor, ratio, merge_bonus, merge_cap = _pick_snapshot(store, config)
    rows = [row for row in store.list_store_rows()]
    if not rows:
        return "Store is empty."

    now = datetime.now(timezone.utc)
    lines = [
        f"Store temperatures ({len(rows)} rows)",
        f"As of: {now.isoformat(timespec='seconds')}",
        f"Threshold: {result.threshold:.1f} (floor {floor:.1f}, {ratio:.2f}× median {result.median:.1f})",
        "",
    ]

    ordered = sorted(
        rows,
        key=lambda row: result.temps.get(row["id"], 0.0)
        * merge_multiplier(row.get("merge_count"), bonus=merge_bonus, cap=merge_cap),
        reverse=True,
    )
    for i, row in enumerate(ordered, start=1):
        title = (row.get("title") or "")[:60]
        raw_temp = result.temps.get(row["id"], 0.0)
        if row.get("engagement_score") is None:
            lines.append(f"{i}. score unavailable — queued before scoring update")
            lines.append(title)
            lines.append("")
            continue
        mult = merge_multiplier(row.get("merge_count"), bonus=merge_bonus, cap=merge_cap)
        effective = raw_temp * mult
        flag = "styled" if row.get("styled_at") else "raw"
        merge = row.get("merge_count") or 1
        source = row.get("source") or "?"
        merge_note = f" merge={merge}×{mult:.2f}" if merge > 1 else ""
        lines.append(f"{i}. {effective:.1f} eff ({raw_temp:.1f} raw{merge_note}) [{flag}]")
        lines.append(title)
        origin = row.get("origin_topic")
        origin_str = f" | topic={origin}" if origin else ""
        lines.append(f"source={source}{origin_str} | published={(row.get('published_at') or '')[:10] or 'unknown'}")
        lines.append("")

    return "\n".join(lines).strip()


def _format_store_browse(store: NewsStore, config: dict[str, Any]) -> str:
    """Browse the store: all rows hottest-first, compact 3-line format.

    Per row: rank + effective temp, title (truncated), meta line
    (source, published, signals, merge count, raw/styled flag, snippet
    excerpt). Capped to fit a sane Telegram message.
    """
    from newsbot.scoring import merge_multiplier

    result, floor, ratio, merge_bonus, merge_cap = _pick_snapshot(store, config)
    rows = store.list_store_rows()
    if not rows:
        return "Store is empty."

    ordered = sorted(
        rows,
        key=lambda row: result.temps.get(row["id"], 0.0)
        * merge_multiplier(row.get("merge_count"), bonus=merge_bonus, cap=merge_cap),
        reverse=True,
    )

    lines = [f"Store browse ({len(rows)} rows, hottest first):", ""]

    for i, row in enumerate(ordered, start=1):
        title = (row.get("title") or "(untitled)")[:60]
        raw_temp = result.temps.get(row["id"], 0.0)
        mult = merge_multiplier(row.get("merge_count"), bonus=merge_bonus, cap=merge_cap)
        effective = raw_temp * mult
        flag = "styled" if row.get("styled_at") else "raw"
        source = row.get("source") or "?"
        published = (row.get("published_at") or "")[:10] or "?"
        merge = row.get("merge_count") or 1

        # Signal bits: upvotes/comments/stars/reposts
        signals = []
        for sig_key, sig_label in [("upvotes", "↑"), ("comments", "💬"), ("stars", "★"), ("reposts", "↻")]:
            val = row.get(sig_key)
            if val is not None:
                signals.append(f"{sig_label}{val}")
        signal_str = " ".join(signals) if signals else "—"

        snippet = (row.get("snippet") or "")[:100]
        if len(row.get("snippet") or "") > 100:
            snippet += "…"

        merge_note = f" merge×{merge}" if merge > 1 else ""
        lines.append(f"{i}. [{row['id']}] {effective:.1f}° [{flag}]{merge_note}")
        lines.append(f"   {title}")
        lines.append(f"   {source} | {published} | {signal_str} | {snippet}")
        lines.append("")

    return "\n".join(lines).strip()


def _format_store_detail(store: NewsStore, row_id: int) -> str:
    """Full dump of one store row: score components, merge trail, styled content.

    Returns a helpful error with valid id hints if the row is not found.
    """
    row = store.get_store_row(row_id)
    if row is None:
        valid_ids = store.list_store_ids()
        if valid_ids:
            id_str = ", ".join(str(i) for i in valid_ids[:20])
            suffix = f" … ({len(valid_ids)} total)" if len(valid_ids) > 20 else ""
            return f"Row id {row_id} not found in store.\n\nValid ids: {id_str}{suffix}"
        return f"Row id {row_id} not found — store is empty."

    lines = [f"Store row {row_id}", ""]

    title = row.get("title") or "(untitled)"
    flag = "styled" if row.get("styled_at") else "raw"
    lines.append(f"Title: {title}")
    lines.append(f"State: {flag}")
    lines.append("")

    # Score breakdown
    lines.append("Score components:")
    score_keys = [
        ("score_at_queue", "Score at queue"),
        ("engagement_score", "Engagement"),
        ("recency_at_queue", "Recency at queue"),
        ("source_weight", "Source weight"),
        ("topic_bonus", "Topic bonus"),
        ("crosspost_bonus", "Crosspost bonus"),
        ("penalty", "Penalty"),
        ("matched_topics", "Matched topics"),
        ("origin_topic", "Origin topic"),
    ]
    for key, label in score_keys:
        val = row.get(key)
        if val is not None:
            lines.append(f"  {label}: {val}")
        else:
            lines.append(f"  {label}: —")
    lines.append("")

    # Raw signals
    lines.append("Raw signals:")
    for key, label in [("upvotes", "Upvotes"), ("comments", "Comments"),
                       ("stars", "Stars"), ("reposts", "Reposts"),
                       ("crosspost_count", "Crossposts")]:
        val = row.get(key)
        if val is not None:
            lines.append(f"  {label}: {val}")
    lines.append("")

    # Meta
    lines.append("Metadata:")
    lines.append(f"  Source: {row.get('source') or '?'}")
    lines.append(f"  URL: {row.get('url') or '(none)'}")
    lines.append(f"  Category: {row.get('category') or '(none)'}")
    lines.append(f"  Published: {row.get('published_at') or '?'}")
    lines.append(f"  Merge count: {row.get('merge_count') or 1}")

    merged_urls_raw = row.get("merged_urls")
    if merged_urls_raw:
        try:
            import json
            merged = json.loads(merged_urls_raw) if isinstance(merged_urls_raw, str) else merged_urls_raw
            if isinstance(merged, list) and merged:
                lines.append(f"  Merged URLs ({len(merged)}):")
                for url in merged[:5]:
                    lines.append(f"    {url}")
                if len(merged) > 5:
                    lines.append(f"    … ({len(merged)} total)")
        except Exception:
            lines.append(f"  Merged URLs: (parse error)")

    styled_at = row.get("styled_at")
    if styled_at:
        lines.append(f"  Styled at: {styled_at}")
        body = row.get("body") or ""
        if body:
            lines.append(f"  Styled body: {body[:200]}{'…' if len(body) > 200 else ''}")
    else:
        snippet = row.get("snippet") or ""
        if snippet:
            lines.append(f"  Snippet: {snippet[:200]}{'…' if len(snippet) > 200 else ''}")

    lines.append(f"  Scored at: {row.get('scored_at') or '(unscored)'}")
    lines.append(f"  Lookback hours: {row.get('lookback_hours') or '?'}")
    lines.append(f"  Message ID: {row.get('message_id') or '(none)'}")

    return "\n".join(lines)


def _format_dry_run_report(result: GenerationPipelineResult) -> str:
    """Format the /digest dry-run funnel report + per-item classification."""
    lines = [
        f"Dry-run generation funnel:",
        f"  collected {result.collected}",
        f"  → unseen {result.unseen}",
        f"  → deduped {result.deduped}",
        f"  → above_min_score {result.above_min_score}",
        f"  → sent_to_filter {result.sent_to_filter}",
        f"  → llm_kept {result.llm_kept}",
        f"  → final {result.final_count}",
        "",
    ]
    if result.failed_collectors:
        lines.append(f"Failed collectors: {', '.join(result.failed_collectors)}")
        lines.append("")

    for idx, item in enumerate(result.items, start=1):
        title = (item.get("title") or "(untitled)")[:60]
        source = item.get("source") or "?"
        score = float(item.get("score") or 0.0)
        action = item.get("action") or "add"
        merge_id = item.get("merge_row_id")
        category = item.get("category") or ""
        importance = item.get("importance") or ""
        action_str = f"MERGE→row {merge_id}" if action == "merge" and merge_id else "ADD"
        cat_str = f" [{category} imp={importance}]" if category or importance else ""
        lines.append(f"{idx}. {title}")
        lines.append(f"   {source} | score={score:.1f} | {action_str}{cat_str}")
        lines.append("")

    return "\n".join(lines).strip()


async def _scheduler_gen_iteration(
    coordinator: JobCoordinator,
    store: NewsStore,
    settings: SettingsStore,
    gen_hours: list[int],
    *,
    now: datetime | None = None,
    timeout: float = 0,
) -> int:
    """One iteration of the wall-clock generation scheduler.

    Slot-based: ``NEWS_GEN_HOURS`` names the hours (local wall clock) when a
    digest must run. Slot key format ``YYYY-MM-DDTHH`` (local). A slot fires
    once — the key is written to ``scheduler.last_gen_slot`` on success.

    Catch-up: if the process was down (or crashed) past a scheduled hour,
    the most recent due slot (``latest_due_gen_slot``) still fires exactly
    once when the loop comes back. Failure / no-progress leaves the key
    unset so the next tick retries the same slot.

    Returns:
        0 — idle (slot already consumed) or success.
        1 — generation failed, slot NOT consumed.
        2 — generation skipped (already running), slot NOT consumed.
        3 — generation no-progress, slot NOT consumed.

    Runs retention cleanup regardless of outcome.
    """
    if now is None:
        now = local_now()
    due_slot = latest_due_gen_slot(now, gen_hours)
    last_gen_slot = settings.get("scheduler", "last_gen_slot", default="") or ""

    if last_gen_slot == due_slot:
        return 0  # this slot already ran — idle

    log.info("generation cycle starting (slot=%s, now=%s)", due_slot, now.isoformat())
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
    finally:
        # Retention runs on EVERY outcome (success, failure, no-progress, exception).
        _run_retention(store)

    if gen_success:
        settings.set("scheduler", "last_gen_slot", due_slot)
        log.info("generation cycle complete (slot=%s)", due_slot)
    else:
        log.warning("generation did not succeed — will retry slot %s on next tick", due_slot)

    return 0 if gen_success else result


async def _scheduler_post_iteration(
    coordinator: JobCoordinator,
    settings: SettingsStore,
    *,
    now: datetime | None = None,
) -> int:
    """One iteration of the wall-clock posting scheduler.

    Slot-based: post slots fall on even hours (local wall clock), key
    ``YYYY-MM-DDTHH``. Never backfills: a slot missed during downtime is
    gone once the hour ends. Consuming the slot:

        success (0), empty store (3), threshold skip (4) → key written.
        failure (1) or busy (2) → key NOT written → retry within the hour.

    Returns the coordinator result code unchanged.
    """
    if now is None:
        now = local_now()
    slot = post_slot(now)
    if slot is None:
        return 0  # odd hour — no post slot

    last_post_slot = settings.get("scheduler", "last_post_slot", default="") or ""
    if last_post_slot == slot:
        return 0  # this slot already ran — idle

    post_success = False
    result = 1
    try:
        result = await coordinator.run_posting()
        if result == 0:
            post_success = True
        elif result == 2:
            log.info("posting skipped — already in progress")
        elif result in (3, 4):
            # Empty store / nothing hot enough — a healthy slot skip. The
            # slot IS consumed (no retry storm; cadence preserved).
            post_success = True
            log.debug("posting slot %s consumed by skip (code=%d)", slot, result)
        else:
            log.error("posting failed (code=%d)", result)
    except Exception as exc:
        log.error("posting cycle failed: %s", redact_exception(exc))

    if post_success:
        settings.set("scheduler", "last_post_slot", slot)
    else:
        log.warning("posting did not succeed — will retry slot %s within the hour", slot)

    return result


async def _scheduled_loop(settings: SettingsStore) -> None:
    """Long-running loop with wall-clock slot-based scheduling.

    Generation fires at the hours named by ``NEWS_GEN_HOURS`` (default
    ``DEFAULT_GEN_HOURS`` = "5,9,13,17,21" local time) with catch-up after
    downtime. Posting fires on even hours, never backfilling. Bot command
    handler polls Telegram getUpdates concurrently.

    All jobs go through a JobCoordinator that serializes generation and
    posting, preventing overlap between scheduled and manual commands.
    """
    db_path = os.getenv("NEWS_DB", "data/newsbot.sqlite")
    store = NewsStore(Path(db_path))
    coordinator = JobCoordinator(store, settings)

    gen_hours = gen_slots(os.getenv("NEWS_GEN_HOURS", DEFAULT_GEN_HOURS))

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
            if result == 3:
                raise RuntimeError("no new posts generated (empty collection, all seen, or LLM returned nothing)")
            if result == 1:
                raise RuntimeError("generation failed — check logs for details")

        async def on_digest_dry() -> str:
            """Dry-run: run the pipeline through the generation lock, no DB writes."""
            async def _dry_run():
                cfg = load_config(settings)
                result = await _run_generation_pipeline(store, cfg)
                if result is None:
                    return "Dry-run: pipeline produced nothing (empty collection, all seen, or LLM filter empty)."
                report = _format_dry_run_report(result)
                # Append the markdown source for each final item (debugging
                # the LLM -> render boundary).
                if result.items:
                    md_parts = ["", "```markdown"]
                    for item in result.items:
                        title = str(item.get("title") or "(untitled)")
                        url = str(item.get("url") or "")
                        body = str(item.get("snippet") or item.get("body") or "")[:200]
                        md_parts.append(render_post(title, body, url))
                    md_parts.append("```")
                    report += "\n" + "\n".join(md_parts)
                return report
            # Returns int from run_generation, but _dry_run returns str.
            # We need to capture the string result before the lock wraps it.
            dry_result: list[str] = []
            async def _dry_wrapped():
                r = await _dry_run()
                dry_result.append(r)
                return 0
            rc = await coordinator.run_generation(_dry_wrapped, timeout=GENERATION_TIMEOUT_SECONDS)
            if rc == 2:
                raise RuntimeError("generation already in progress — skipped")
            if dry_result:
                return dry_result[0]
            return "Dry-run: no result returned."

        async def on_post() -> None:
            result = await coordinator.run_posting()
            if result == 2:
                raise RuntimeError("posting already in progress — skipped")
            if result == 3:
                raise RuntimeError("no pending posts to deliver")
            if result == 4:
                raise RuntimeError("nothing hot enough to post right now")
            if result == 1:
                raise RuntimeError("posting failed — check logs for details")

        async def on_status() -> str:
            cfg = load_config(settings)
            rows = store.list_store_rows()
            styled = sum(1 for row in rows if row.get("styled_at"))
            raw = len(rows) - styled
            result, floor, ratio, _, _ = _pick_snapshot(store, cfg)
            last_gen_slot = settings.get("scheduler", "last_gen_slot", default="") or ""
            last_post_slot = settings.get("scheduler", "last_post_slot", default="") or ""
            last_summary_day = settings.get("scheduler", "last_summary_day", default="") or ""
            skip = coordinator.last_skip_reason or "none"
            tz_name = os.getenv("NEWS_TZ", "Asia/Bangkok")
            gen_status = "running" if coordinator.generation_running else "idle"
            post_status = "running" if coordinator.posting_running else "idle"
            summary_status = "running" if coordinator.summary_running else "idle"
            return (
                f"Store: {len(rows)} rows ({raw} raw, {styled} styled)\n"
                f"Threshold: {result.threshold:.1f} (floor {floor:.1f}, {ratio:.2f}× median {result.median:.1f})\n"
                f"Last skip: {skip}\n"
                f"Last generation slot: {last_gen_slot or 'never'}\n"
                f"Last post slot: {last_post_slot or 'never'}\n"
                f"Last summary day: {last_summary_day or 'never'}\n"
                f"Generation: {gen_status} (slots: {gen_hours})\n"
                f"Posting: {post_status} (even hours)\n"
                f"Summary: {summary_status} (daily at 13:00)\n"
                f"Timezone: {tz_name}"
            )

        async def on_scores() -> str:
            return _format_scores(store, load_config(settings))

        async def on_store(arg: str) -> str:
            if arg.strip():
                try:
                    row_id = int(arg.strip())
                except ValueError:
                    return f"Invalid id: {arg.strip()!r} — /store expects a row id number."
                return _format_store_detail(store, row_id)
            return _format_store_browse(store, load_config(settings))

        async def on_summary() -> None:
            result = await coordinator.run_summary(lambda: _run_summary(store, settings, local_now()))
            if result == 2:
                raise RuntimeError("summary already in progress — skipped")
            if result == 3:
                raise RuntimeError("nothing posted in the last 24h — nothing to recap")
            if result == 1:
                raise RuntimeError("daily recap failed — check logs for details")

        async def on_preview() -> tuple[str, str, list[dict[str, Any]] | None]:
            """Style the hottest store story for a DM preview.

            Read-only: no DB writes, no delivery, no slot consumed. The
            scheduled poster will style the same row again at post time.
            Returns (rich_markdown, html_fallback, blocks) — markdown and
            fallback render from the same styled data; *blocks* is the
            media-bearing layout (photo slideshow + video blocks first)
            when the article yields extractable media, else None. Preview
            mirrors the channel posting path one-to-one (jobs.py).
            """
            cfg = load_config(settings)
            result, floor, ratio, merge_bonus, merge_cap = _pick_snapshot(store, cfg)
            if result.reason == "empty":
                raise RuntimeError("Store is empty — run /digest first")
            if result.reason == "below_threshold" or result.row is None:
                raise RuntimeError(
                    f"Nothing hot enough: hottest {result.hottest:.1f} < "
                    f"threshold {result.threshold:.1f}"
                )
            row = result.row
            styled = await llm_style_posts(
                [_row_to_styler_input(row)],
                _build_lm_client(),
                style_prompt=cfg["style_prompt"],
            )
            if not styled:
                raise RuntimeError("styler returned nothing — check logs")
            title = str(styled[0].get("title") or row.get("title") or "").strip()
            body = str(styled[0].get("body") or "").strip()
            if not body:
                raise RuntimeError("styler returned an empty body — check logs")
            url = row.get("url") or ""
            signature = signature_for(os.getenv("NEWS_CHANNEL_ID", ""))
            markdown = render_post(title, body, url, signature)

            # Same media extraction as the scheduled posting path: hero
            # tags + inline images/video, Telegram caps enforced, never
            # raises (returns [] on any failure).
            try:
                media = await asyncio.to_thread(extract_article_media, url)
            except Exception as exc:  # defensive — extractor is exception-safe
                log.warning("preview media extraction raised: %s", redact_exception(exc))
                media = []
            blocks = None
            if media:
                blocks = render_post_blocks(
                    title, body, url, signature=signature, media=media,
                )
                log.info("preview carries %d media item(s)", len(media))
            return markdown, format_post_message(title, body, url), blocks

        async def on_recap_preview() -> tuple[str, str, str]:
            """Write the daily recap for a DM preview.

            Read-only: no DB writes, no delivery, day key untouched.
            Returns (input_sheet, rich_markdown, html_fallback): the sheet
            shows exactly what the LLM will receive; markdown and fallback
            are both rendered from the same result data.
            """
            now = local_now()
            since_utc = (now.astimezone(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
            rows = store.list_posted_since(since_utc)
            if not rows:
                raise RuntimeError("nothing posted in the last 24h — nothing to recap")
            cfg = load_config(settings)
            items = _recap_input_items(rows)
            sheet = _format_recap_input_sheet(items)
            result = await llm_daily_summary(
                items, _build_lm_client(), recap_prompt=cfg["recap_prompt"],
            )
            if not result:
                raise RuntimeError("recap LLM returned nothing — check logs")
            chat_id = os.getenv("NEWS_CHANNEL_ID", "").strip()
            return (
                sheet,
                render_recap(
                    result["title"], result["items"], chat_id=chat_id,
                    signature=signature_for(chat_id),
                ),
                _format_recap_html_fallback(result["title"], result["items"], chat_id=chat_id),
            )

        bot_handler = BotCommandHandler(
            bot_token=bot_token,
            admin_user_id=admin_user_id,
            settings=settings,
            on_digest=on_digest,
            on_digest_dry=on_digest_dry,
            on_post=on_post,
            on_status=on_status,
            on_scores=on_scores,
            on_summary=on_summary,
            on_store=on_store,
            on_preview=on_preview,
            on_recap_preview=on_recap_preview,
        )

    async def generation_loop() -> None:
        """Wall-clock generation scheduler (see _scheduler_gen_iteration)."""
        log.info("generation scheduler started: slots=%s (%s)", gen_hours, local_now().tzinfo)
        while True:
            await _scheduler_gen_iteration(
                coordinator, store, settings, gen_hours,
                timeout=GENERATION_TIMEOUT_SECONDS,
            )
            await asyncio.sleep(30)

    async def posting_loop() -> None:
        """Wall-clock posting scheduler (see _scheduler_post_iteration)."""
        log.info("posting scheduler started: even hours (%s)", local_now().tzinfo)
        while True:
            await _scheduler_post_iteration(coordinator, settings)
            await asyncio.sleep(30)

    async def summary_loop() -> None:
        """Daily 13:00 recap scheduler (see _scheduler_summary_iteration)."""
        log.info("daily summary scheduler started: 13:00 (%s)", local_now().tzinfo)
        while True:
            await _scheduler_summary_iteration(coordinator, store, settings)
            await asyncio.sleep(60)

    tasks = [
        asyncio.create_task(generation_loop()),
        asyncio.create_task(posting_loop()),
        asyncio.create_task(summary_loop()),
    ]
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
            # Only drain if generation succeeded (result 0).
            # No-progress (3) or failure (1) should not proceed to drain —
            # the old queue is intact and draining it would report false success.
            if result == 0:
                return await coordinator.drain_posts()
            # Return the generation result code — do not drain.
            # No-progress (3) is not an error exit code, but also not a drain success.
            return 1 if result == 1 else 0

        try:
            code = asyncio.run(_once())
        finally:
            store.close()
        sys.exit(code)

    # Scheduled mode: needs BOT_TOKEN for posting. Without it, run once for testing.
    if not os.getenv("BOT_TOKEN", "").strip():
        log.info("no BOT_TOKEN — running once (dry-run mode)")
        store = NewsStore(Path(db_path))

        async def _dry() -> int:
            coordinator = JobCoordinator(store, settings)
            result = await coordinator.run_generation(
                lambda: _run_generation(store, settings),
                timeout=GENERATION_TIMEOUT_SECONDS,
            )
            _run_retention(store)
            # Only drain if generation succeeded (result 0).
            # No-progress (3) or failure (1) should not drain the old queue.
            if result == 0:
                return await coordinator.drain_posts()
            return 1 if result == 1 else 0

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