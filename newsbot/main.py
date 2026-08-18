"""News bot entrypoint — split generation + posting pipeline.

Runs as a long-lived process inside Docker:

    python -m newsbot.main              # scheduled mode (default)
    python -m newsbot.main --once       # one-shot mode (dry runs, testing)

In scheduled mode, two wall-clock slot loops run concurrently
(local time from NEWS_TZ, default Asia/Bangkok):

  - Generation slots (NEWS_GEN_HOURS, default "5,17"):
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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from core.logging_config import configure_logging
from core.log_sanitizer import redact_exception
from core.settings_store import SettingsStore, default_store
from lm_client import LMClient

from newsbot.bot_commands import BotCommandHandler
from newsbot.clock import gen_slots, latest_due_gen_slot, local_now, post_slot, summary_day
from newsbot.collectors import (
    hackernews as hn_collector,
    reddit as reddit_collector,
    github as github_collector,
    rss as rss_collector,
    producthunt as ph_collector,
    huggingface_papers as hf_collector,
)
from newsbot.collectors.base import Candidate
from newsbot.config import load_config
from newsbot.db import NewsStore, _as_dict
from newsbot.dedupe import dedupe_and_merge, match_candidate_to_store, _set_pre_merge_weights
from newsbot.jobs import (
    JobCoordinator,
    _env_float,
    _row_to_styler_input,
    format_post_message,
    format_recap_message,
)
from newsbot.selection import pick_hottest
from newsbot.telegram_poster import post_digest
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
    # Sync pre-merge weights with active config so dedupe uses configured weights.
    _set_pre_merge_weights(cfg.get("source_weights") or {})

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

    # 5b. Assign candidate IDs here (after diverse selection, before LLM filter)
    #     so they can be logged and preserved through the filter pass.
    _assign_candidate_ids(top)

    # 5c. Log each candidate sent to the LLM filter as one JSON line at INFO.
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
        })
        log.info(log_line)

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

    # 7. Select a diverse top-N for the store.
    final = select_diverse_top_items(kept, cfg["max_final_news"])
    final = [_as_dict(item) for item in final]  # normalize Candidates to dicts
    log.info("selected %d diverse items for the store", len(final))

    # 8. Match survivors against the store.
    store_rows = store.list_store_rows()
    to_add: list[dict[str, Any]] = []
    merges: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for item in final:
        hit = match_candidate_to_store(item, store_rows)
        if hit:
            merges.append((hit, item))
        else:
            to_add.append(item)

    # 9. Merges: fold each duplicate into its existing store row
    #    (per-field engagement max + engagement recompute inside).
    for row, item in merges:
        store.merge_into_store_row(row["id"], item, str(item.get("url") or ""))
        updated = store._conn.execute(
            "SELECT merge_count FROM pending_posts WHERE id=?", (row["id"],)
        ).fetchone()
        log.info(
            "merged %r into store row %d (merge_count=%d)",
            str(item.get("url") or item.get("title") or "?"),
            row["id"],
            int(updated["merge_count"]) if updated else -1,
        )

    # 10. Append new raw stories (body='') and mark ALL survivors seen —
    #     added and merged alike; they live in the store now. The v1 rule
    #     "styler omitted → don't mark seen" is obsolete: there is no styler.
    try:
        inserted = store.add_stories_to_store(to_add, seen_items=final)
    except sqlite3.Error as exc:
        log.error("additive store insert failed: %s — merges already applied", exc)
        return 1
    log.info("appended %d raw stories (%d merged into existing rows)", inserted, len(merges))

    # 11. Eviction: trim the store back to NEWS_STORE_CAP, coldest first.
    #     Known trade-off (documented in README): an evicted story stays in
    #     `seen` for NEWS_RETENTION_SEEN_DAYS and cannot re-enter on the
    #     same URL.
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

    message = format_recap_message(
        result["title"], result["items"],
        chat_id=os.getenv("NEWS_CHANNEL_ID", "").strip(),
    )

    bot_token = os.getenv("BOT_TOKEN", "").strip()
    chat_id = os.getenv("NEWS_CHANNEL_ID", "").strip()
    if not bot_token or not chat_id:
        log.info("dry-run: daily summary to stdout (no BOT_TOKEN/NEWS_CHANNEL_ID)")
        print(message)
    else:
        try:
            await post_digest(message, bot_token=bot_token, chat_id=chat_id)
        except Exception as exc:
            log.error("daily summary delivery failed — will retry: %s", redact_exception(exc))
            return 1

    try:
        store.add_summary(day, message, os.getenv("LM_MODEL", ""), len(items))
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
        lines.append(f"source={source} | published={(row.get('published_at') or '')[:10] or 'unknown'}")
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
    ``"5,17"`` local time) with catch-up after downtime. Posting fires on
    even hours, never backfilling. Bot command handler polls Telegram
    getUpdates concurrently.

    All jobs go through a JobCoordinator that serializes generation and
    posting, preventing overlap between scheduled and manual commands.
    """
    db_path = os.getenv("NEWS_DB", "data/newsbot.sqlite")
    store = NewsStore(Path(db_path))
    coordinator = JobCoordinator(store, settings)

    gen_hours = gen_slots(os.getenv("NEWS_GEN_HOURS", "5,17"))

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

        async def on_summary() -> None:
            result = await coordinator.run_summary(lambda: _run_summary(store, settings, local_now()))
            if result == 2:
                raise RuntimeError("summary already in progress — skipped")
            if result == 3:
                raise RuntimeError("nothing posted in the last 24h — nothing to recap")
            if result == 1:
                raise RuntimeError("daily recap failed — check logs for details")

        async def on_preview() -> str:
            """Style the hottest store story for a DM preview.

            Read-only: no DB writes, no delivery, no slot consumed. The
            scheduled poster will style the same row again at post time.
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
            return format_post_message(title, body, row.get("url") or "")

        async def on_recap_preview() -> tuple[str, str]:
            """Write the daily recap for a DM preview.

            Read-only: no DB writes, no delivery, day key untouched.
            Returns (input_sheet, recap_message): the sheet shows exactly
            what the LLM will receive; the recap is the generated output.
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
            return sheet, format_recap_message(
                result["title"], result["items"],
                chat_id=os.getenv("NEWS_CHANNEL_ID", "").strip(),
            )

        bot_handler = BotCommandHandler(
            bot_token=bot_token,
            admin_user_id=admin_user_id,
            settings=settings,
            on_digest=on_digest,
            on_post=on_post,
            on_status=on_status,
            on_scores=on_scores,
            on_summary=on_summary,
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