"""Generation pipeline: collect, filter, score, store."""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

import httpx

from core.settings_store import SettingsStore
from newsbot.collectors import (
    github, hackernews, huggingface_papers, reddit, rss, trends,
)
from newsbot.config import load_config
from newsbot.env import Env, resolve
from newsbot.db import NewsStore
from newsbot.dedupe import _set_pre_merge_weights, dedupe_and_merge, match_candidate_to_store
import newsbot.llm as llm
from newsbot.outcome import Outcome
from newsbot.scoring import current_temperature, score_all
from newsbot.selection import select_diverse_candidates
from newsbot.summarizer import llm_filter, select_diverse_top_items, _assign_candidate_ids

log = logging.getLogger(__name__)

COLLECTORS: dict[str, Any] = {
    "hackernews": hackernews,
    "reddit": reddit,
    "github": github,
    "rss": rss,
    "huggingface_papers": huggingface_papers,
    "trends": trends,
}

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

    async with httpx.AsyncClient(follow_redirects=True) as shared:
        for name, module in COLLECTORS.items():
            if name not in sources:
                continue
            if name == "reddit":
                tasks.append((name, module.collect(sources[name])))
            else:
                tasks.append((name, module.collect(sources[name], shared)))

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
    store: NewsStore, cfg: dict[str, Any], env: Env | None = None,
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

    filter_lm = llm.build_lm_client("filter")
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
    final = list(final)

    # 8. Classify against store (add vs merge).
    #    Use list_merge_target_rows (undelivered + recently delivered to
    #    telegram) so a story arriving from a different source can merge
    #    into a recently-delivered row instead of being inserted as a
    #    duplicate (flow_001123).
    merge_window_days = resolve(env).merge_window_days
    store_rows = store.list_merge_target_rows("telegram", merge_window_days)
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


_REDDIT_HOST_SUFFIXES = ("reddit.com", "redd.it")


def _is_reddit_self_host(url: str) -> bool:
    """True if *url* points at reddit itself (crosspost/self-referential)."""
    try:
        host = (urlsplit(url).netloc or "").lower().removeprefix("www.")
    except ValueError:
        return False
    return any(host == s or host.endswith("." + s) for s in _REDDIT_HOST_SUFFIXES)


def _swap_reddit_link_post_url(item: dict[str, Any]) -> dict[str, Any]:
    """Swap a reddit link-post's permalink for the article URL at the write
    boundary.

    When the FINAL candidate about to be written to the store is a reddit
    LINK post — source == 'reddit', raw_json.is_self is falsy,
    raw_json.external_url is a valid http(s) URL — swap: row url =
    external_url; the permalink goes into contributing_urls (which
    add_stories_to_store seeds into merged_urls and _run_generation seeds
    into seen). Reddit SELF posts keep the permalink. The swap is skipped
    when external_url points at reddit itself (crossposts).

    This runs AFTER classification/dedupe/scoring so in-cycle identity
    matching and scoring keep operating on the original permalink. Media
    extraction (extract_article_media in jobs.py) then fetches the article
    page — better images.

    Returns the (mutated in place) item for chaining.
    """
    if str(item.get("source") or "").lower() != "reddit":
        return item

    raw_json = item.get("raw_json")
    if not isinstance(raw_json, dict):
        return item

    if raw_json.get("is_self"):
        return item  # self-post — permalink is the right URL

    external = str(raw_json.get("external_url") or "").strip()
    if not external.startswith(("http://", "https://")):
        return item

    if _is_reddit_self_host(external):
        return item  # crosspost — don't swap one permalink for another

    permalink = str(item.get("url") or "").strip()
    if not permalink or permalink == external:
        return item  # nothing to swap

    item["url"] = external
    contributing = item.get("contributing_urls")
    if not isinstance(contributing, list):
        contributing = []
        item["contributing_urls"] = contributing
    if permalink not in contributing:
        contributing.append(permalink)

    log.info(
        "reddit link-post URL swap: %s -> %s (permalink archived in contributing_urls)",
        permalink, external,
    )
    return item


async def _run_generation(
    store: NewsStore, settings: SettingsStore, env: Env | None = None,
) -> Outcome:
    """Generation cycle: collect → filter → score → LLM filter → store.

    v2 additive pipeline: digest fills the store with RAW scored stories
    (body=''), merging duplicates into existing rows — no styling pass.
    Styling happens at pick time (jobs). If the append fails, the store
    keeps any merges already applied; nothing is ever bulk-deleted.

    Returns:
        OK — success: store updated (rows appended and/or merged), survivors
            marked seen. Empty appends with non-empty merges still count
            as success.
        FAILED — an error occurred (DB, LLM exception, etc.).
        NOTHING_TO_DO — nothing to do (empty collection, all seen, LLM
            filter empty). Distinct from success so the scheduler can
            decide whether to advance the timestamp.
    """
    env = resolve(env)
    cfg = load_config(settings, env)

    # Run the pure pipeline (collect → filter → dedupe → score → LLM filter → classify).
    # No DB writes — the pipeline only reads the store for seen-filtering and classification.
    pipeline = await _run_generation_pipeline(store, cfg, env)
    if pipeline is None:
        log.warning("generation pipeline produced nothing; keeping existing queue")
        return Outcome.NOTHING_TO_DO

    log.info(
        "generation funnel: collected %d → unseen %d → deduped %d → above_min %d → filter %d → kept %d → final %d",
        pipeline.collected, pipeline.unseen, pipeline.deduped,
        pipeline.above_min_score, pipeline.sent_to_filter,
        pipeline.llm_kept, pipeline.final_count,
    )

    # 8b. Reddit link-post URL swap (flow_001125): for each item being
    #     ADDED to the store, if it's a reddit LINK post (not self, has a
    #     valid http(s) external_url not pointing at reddit itself), swap
    #     the permalink for the article URL. The permalink is archived in
    #     contributing_urls so add_stories_to_store seeds it into
    #     merged_urls and the seen-marking loop below picks it up —
    #     re-collecting the permalink next cycle will merge/drop, not
    #     create a new row. This runs AFTER classification/dedupe/scoring
    #     so in-cycle identity matching keeps operating on the original
    #     permalink. Merge items are NOT swapped (they fold into existing
    #     rows whose URL is already set).
    for item in pipeline.items:
        if item.get("action") == "add":
            _swap_reddit_link_post_url(item)

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
        merge_count = store.merge_into_store_row(row_id, item, urls_to_merge)
        log.info(
            "merged %r into store row %d (merge_count=%d)",
            str(item.get("url") or item.get("title") or "?"),
            row_id,
            merge_count,
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
        return Outcome.FAILED
    log.info("appended %d raw stories (%d merged into existing rows)", inserted, len(merges))

    # 11. Eviction: trim the store back to NEWS_STORE_CAP, coldest first.
    now_utc = datetime.now(timezone.utc)
    post_rows = store.list_store_rows("telegram")
    temps = {r["id"]: current_temperature(r, cfg, now=now_utc) for r in post_rows}
    cap = env.store_cap
    evicted = store.evict_coldest(temps, cap=cap)
    if evicted:
        remaining_ids = {r["id"] for r in store.list_store_rows("telegram")}
        gone = sorted(
            ((tid, t) for tid, t in temps.items() if tid not in remaining_ids),
            key=lambda kv: kv[1],
        )
        for tid, t in gone:
            title = next((r["title"] for r in post_rows if r["id"] == tid), "?")
            log.info("evicted coldest row %d (%s, temp=%.2f)", tid, title, t)

    return Outcome.OK


def _run_retention(store: NewsStore, env: Env | None = None) -> None:
    """Run retention cleanup using ages from Env."""
    env = resolve(env)
    posted_days = env.retention_posted_days
    seen_days = env.retention_seen_days
    try:
        store.prune_delivered(max_age_days=posted_days)
        store.prune_seen(max_age_days=seen_days)
    except Exception as exc:
        log.warning("retention cleanup failed: %s", exc)
