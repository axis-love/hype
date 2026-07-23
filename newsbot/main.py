"""News bot entrypoint — the linear pipeline.

Run by cron:

    0 9,18 * * * cd /opt/newsbot && python -m newsbot.main

One invocation runs the full collect → filter-seen → dedupe → score →
LLM filter → LLM digest → post pipeline. No worker loop, no job queue.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
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


async def run() -> int:
    """One full pipeline run. Returns a process exit code (0 = ok)."""
    load_dotenv()
    configure_logging(process_name="newsbot")

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


def main() -> None:
    """Sync entrypoint for `python -m newsbot.main`."""
    code = asyncio.run(run())
    sys.exit(code)


if __name__ == "__main__":
    main()