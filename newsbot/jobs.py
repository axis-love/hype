"""In-process job coordinator for generation and posting.

Ensures at most one generation job and one posting/drain operation can
mutate the queue at a time. Both scheduled loops and manual bot commands
go through this coordinator, preventing overlap that could duplicate,
reorder, or lose posts.
"""
from __future__ import annotations

import asyncio
import html as html_module
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from core.log_sanitizer import redact_exception, redact_text
from core.settings_store import SettingsStore
from lm_client import LMClient
from newsbot.config import load_config
from newsbot.db import NewsStore
from newsbot.selection import pick_hottest
from newsbot.summarizer import llm_style_posts
from newsbot.telegram_poster import post_digest, PartialDeliveryError

log = logging.getLogger(__name__)


def _build_lm_client() -> LMClient:
    """Build the LMClient for the LLM styler from env (LM_BASE / LM_MODEL / LM_API_KEY)."""
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


def _env_float(name: str, default: str) -> float:
    """Read a float env var, falling back (with a warning) on bad values."""
    try:
        return float(os.getenv(name, default))
    except ValueError:
        log.warning("invalid float for %s — falling back to %s", name, default)
        return float(default)


def _row_to_styler_input(row: dict[str, Any]) -> dict[str, Any]:
    """Shape a store row like a digest-time candidate for llm_style_posts.

    The styler reads title/url/snippet/published_at plus the engagement
    signal fields — all persisted on store rows by add_stories_to_store.
    """
    return {
        "candidate_id": f"s{row['id']:03d}",
        "title": row.get("title") or "",
        "url": row.get("url") or "",
        "snippet": row.get("snippet") or "",
        "published_at": row.get("published_at") or "",
        "upvotes": row.get("upvotes") or 0,
        "comments": row.get("comments") or 0,
        "stars": row.get("stars") or 0,
        "crosspost_count": row.get("crosspost_count") or 1,
    }


def _source_label(url: str) -> str:
    """Extract a clean 'domain.tld' label from a URL for the source link."""
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        if host.startswith("www."):
            host = host[4:]
        return host or url
    except Exception:
        return url


def format_post_message(title: str, body: str, url: str) -> str:
    """Build the Telegram HTML message for a single post.

    Format: <b>Title</b> → blank line → body → clickable source link.
    The source link shows a clean domain label instead of the raw URL.

    The body is capped so the final HTML message stays under ~3000 chars,
    keeping each post to a single Telegram message (limit 4096). This makes
    the partial-delivery code path unreachable during normal operation.
    """
    # Budget: 3000 chars total for the HTML message.
    # Subtract space for <b>title</b>, source link, and HTML overhead.
    _MAX_MESSAGE_CHARS = 3000
    _LINK_OVERHEAD = 200  # <a href="...">Source: domain.tld</a> worst case

    title_escaped = html_module.escape(title) if title else ""
    title_block_len = len(f"<b>{title_escaped}</b>\n\n") if title_escaped else 0
    link_budget = _LINK_OVERHEAD if url else 0
    body_budget = max(100, _MAX_MESSAGE_CHARS - title_block_len - link_budget)

    # Truncate body at a sentence boundary if it exceeds the budget.
    if len(body) > body_budget:
        # Try to cut at the last sentence end within the budget.
        cut = body.rfind(". ", 0, body_budget)
        if cut > body_budget // 2:
            body = body[:cut + 1]
        else:
            body = body[:body_budget].rsplit(" ", 1)[0] + "…"
        log.debug("truncated post body to %d chars (budget %d) to fit single Telegram message",
                  len(body), body_budget)

    parts: list[str] = []
    if title:
        parts.append(f"<b>{html_module.escape(title)}</b>")
        parts.append("")
    parts.append(html_module.escape(body))
    if url:
        label = html_module.escape(_source_label(url))
        safe_url = html_module.escape(url, quote=True)
        parts.append(f'<a href="{safe_url}">Source: {label}</a>')
    return "\n".join(parts)


class JobCoordinator:
    """Serializes generation and posting jobs via a single asyncio lock.

    A single lock ensures generation and posting can NEVER overlap.
    The poster can read a row and await Telegram while generation
    replaces the queue — a single lock prevents that race.

    Admission flags are set *before* awaiting the lock so that additional
    same-type requests immediately return 2 (skipped) instead of queuing
    behind the lock and running after the first completes.

    - At most one job (generation OR posting) runs at a time.
    - Re-entrant calls of the same type are skipped (returns 2).
    - Generation result (0=success, 1=failure) is propagated to the caller.
    """

    def __init__(self, store: NewsStore, settings: SettingsStore) -> None:
        self._store = store
        self._settings = settings
        self._job_lock = asyncio.Lock()
        self._gen_running = False
        self._post_running = False
        self._summary_running = False

    @property
    def generation_running(self) -> bool:
        return self._gen_running

    @property
    def posting_running(self) -> bool:
        return self._post_running

    @property
    def summary_running(self) -> bool:
        return self._summary_running

    async def run_summary(self, summary_fn: Any) -> int:
        """Acquire the job lock and run the daily summary job.

        Returns the summary_fn's result (0=success, 1=failure, 3=skipped
        because fewer than one post landed in the window), or 2 if another
        job is already holding the lock (busy).
        """
        if self._summary_running:
            log.info("summary already in progress — skipping")
            return 2
        self._summary_running = True
        try:
            async with self._job_lock:
                result = await summary_fn()
                return int(result) if result is not None else 0
        finally:
            self._summary_running = False

    async def run_generation(self, gen_fn: Any, *, timeout: float = 0) -> int:
        """Acquire the job lock and run the generation cycle.

        Returns the gen_fn's result (0=success, 1=failure), or 2 if
        another generation is already in progress (skipped).

        If *timeout* > 0, the generation is bounded to that many seconds.
        A timeout returns 1 (failure) — the prior queue remains intact.
        """
        # Admission check: set flag BEFORE awaiting the lock so concurrent
        # requests see the flag and immediately return 2 instead of queuing.
        if self._gen_running:
            log.info("generation already in progress — skipping")
            return 2
        self._gen_running = True
        try:
            async with self._job_lock:
                if timeout > 0:
                    try:
                        result = await asyncio.wait_for(gen_fn(), timeout=timeout)
                    except asyncio.TimeoutError:
                        log.error("generation timed out after %ds — keeping existing queue", timeout)
                        return 1
                else:
                    result = await gen_fn()
                return int(result) if result is not None else 0
        finally:
            self._gen_running = False

    async def run_posting(self) -> int:
        """Acquire the job lock and post one pending post.

        Returns 0 on success, 1 on failure, 2 if another posting is
        in progress (skipped).
        """
        if self._post_running:
            log.info("posting already in progress — skipping")
            return 2
        self._post_running = True
        try:
            async with self._job_lock:
                return await self._deliver_one()
        finally:
            self._post_running = False

    async def drain_posts(self) -> int:
        """Acquire the job lock and drain all pending posts.

        Used by --once and dry-run modes. Picks and delivers posts
        sequentially until the store is empty (3) or nothing is hot
        enough (4). Returns 0 on success, 1 on failure, 2 if another
        posting is already in progress (skipped).
        """
        if self._post_running:
            log.info("posting already in progress — cannot drain")
            return 2
        self._post_running = True
        try:
            async with self._job_lock:
                while True:
                    result = await self._deliver_one()
                    if result in (3, 4):
                        # Empty store or nothing hot enough — done. Both are
                        # healthy terminal states, so --once/dry-run exit 0.
                        return 0
                    if result != 0:
                        return result
            # Continue until no more pending posts.
        finally:
            self._post_running = False

    async def _deliver_one(self) -> int:
        """Pick the hottest eligible store row, style it, deliver, mark posted.

        Style-at-pick: the store holds RAW scored rows; styling happens here,
        on the single winner, so ≤1 LLM styling call per post (≤12/day)
        instead of styling the whole digest up front.

        Shared by run_posting (single) and drain_posts (loop).
        Returns:
            0 — success: a post was styled, delivered, and marked posted.
            1 — failure: styler or delivery failed (slot NOT consumed).
            3 — no-op: store is empty.
            4 — threshold skip: nothing hot enough (slot consumed).
        """
        cfg = load_config(self._settings)
        rows = self._store.list_store_rows()
        now = datetime.now(timezone.utc)
        result = pick_hottest(
            rows, cfg, now=now,
            floor=_env_float("NEWS_TEMP_FLOOR", "35"),
            ratio=_env_float("NEWS_THRESHOLD_RATIO", "0.5"),
            merge_bonus=_env_float("NEWS_MERGE_BONUS", "0.2"),
            merge_cap=_env_float("NEWS_MERGE_CAP", "2.0"),
        )
        if result.reason == "empty":
            log.debug("store empty — nothing to post")
            return 3
        if result.reason == "below_threshold":
            log.info(json.dumps({
                "event": "post_skip",
                "threshold": round(result.threshold, 2),
                "median": round(result.median, 2),
                "hottest": round(result.hottest, 2),
            }))
            return 4

        row = result.row
        if row is None:
            return 4  # unreachable in practice; keeps the type narrowed
        row_id = int(row["id"])
        raw_temp = result.temps[row_id]

        # Style the single winner at pick time.
        try:
            styled = await llm_style_posts(
                [_row_to_styler_input(row)],
                _build_lm_client(),
                style_prompt=cfg["style_prompt"],
            )
        except Exception as exc:
            log.error("styler raised for row id=%d — will retry within the hour: %s",
                      row_id, redact_exception(exc))
            return 1
        if not styled:
            log.error("styler failed for row id=%d — will retry within the hour", row_id)
            return 1

        styled_title = str(styled[0].get("title") or row.get("title") or "").strip()
        styled_body = str(styled[0].get("body") or "").strip()
        if not styled_body:
            log.error("styler returned empty body for row id=%d — will retry", row_id)
            return 1

        try:
            self._store.set_styled_content(row_id, styled_title, styled_body)
        except Exception as db_exc:
            log.error("CRITICAL: styled row id=%d but set_styled_content failed: %s",
                      row_id, redact_exception(db_exc))
            return 1

        log.info(json.dumps({
            "event": "post_pick",
            "threshold": round(result.threshold, 2),
            "median": round(result.median, 2),
            "hottest": round(result.hottest, 2),
            "chosen_id": row_id,
            "raw_temp": round(raw_temp, 2),
            "merge_count": row.get("merge_count") or 1,
        }))

        message = format_post_message(styled_title, styled_body, row.get("url") or "")
        return await self._send_and_mark(row_id, message)

    async def _send_and_mark(self, row_id: int, message: str) -> int:
        """Deliver a formatted message and mark the row posted.

        Dry-run (no BOT_TOKEN/NEWS_CHANNEL_ID) prints to stdout. Delivery
        error handling is unchanged from the v1 poster.
        """
        bot_token = os.getenv("BOT_TOKEN", "").strip()
        chat_id = os.getenv("NEWS_CHANNEL_ID", "").strip()

        if not bot_token or not chat_id:
            log.info("dry-run: posting to stdout (no BOT_TOKEN/NEWS_CHANNEL_ID)")
            print(message)
            try:
                self._store.mark_posted(row_id)
            except Exception as db_exc:
                log.error(
                    "CRITICAL: post id=%d dry-run delivered but mark_posted failed: %s",
                    row_id, redact_exception(db_exc),
                )
                return 1
            return 0

        try:
            await post_digest(message, bot_token=bot_token, chat_id=chat_id)
        except PartialDeliveryError as exc:
            # Some chunks were delivered, later chunks failed.
            # Mark as posted to prevent duplicate delivery of early chunks on retry.
            log.warning(
                "post id=%d partially delivered (%d chunks sent) — marking as posted "
                "to prevent duplicate sends: %s",
                row_id, exc.delivered_chunks, redact_exception(exc),
            )
            try:
                self._store.mark_posted(row_id)
            except Exception as db_exc:
                log.error("CRITICAL: post id=%d delivered but mark_posted failed: %s "
                          "— row may be re-delivered on retry", row_id, redact_exception(db_exc))
            return 1  # Still report failure — operator should investigate
        except Exception as exc:
            log.error("failed to post store row id=%d: %s", row_id, redact_exception(exc))
            return 1

        # Delivery succeeded — now mark as posted.
        # If mark_posted fails, the row stays pending and will be re-delivered
        # on the next posting cycle, causing duplicate channel posts.
        # We must handle this atomically: if DB fails after Telegram success,
        # log a CRITICAL error so the operator can manually mark it.
        try:
            self._store.mark_posted(row_id)
        except Exception as db_exc:
            log.error(
                "CRITICAL: post id=%d delivered to Telegram but mark_posted failed: %s "
                "— row will be re-delivered on next cycle unless manually resolved",
                row_id, redact_exception(db_exc),
            )
            return 1  # Report failure so the scheduler doesn't advance timestamp

        return 0