"""In-process job coordinator for generation and posting.

Ensures at most one generation job and one posting/drain operation can
mutate the queue at a time. Both scheduled loops and manual bot commands
go through this coordinator, preventing overlap that could duplicate,
reorder, or lose posts.
"""
from __future__ import annotations

import asyncio
import html as html_module
import logging
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from core.log_sanitizer import redact_exception, redact_text
from core.settings_store import SettingsStore
from newsbot.db import NewsStore
from newsbot.telegram_poster import post_digest, PartialDeliveryError

log = logging.getLogger(__name__)


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

    @property
    def generation_running(self) -> bool:
        return self._gen_running

    @property
    def posting_running(self) -> bool:
        return self._post_running

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

        Used by --once and dry-run modes. Posts all pending posts
        sequentially. Returns 0 on success, 1 on failure, 2 if
        another posting is already in progress (skipped).
        """
        if self._post_running:
            log.info("posting already in progress — cannot drain")
            return 2
        self._post_running = True
        try:
            async with self._job_lock:
                while True:
                    result = await self._deliver_one()
                    if result == 3:
                        # No more pending posts — done.
                        return 0
                    if result != 0:
                        return result
            # Continue until no more pending posts.
        finally:
            self._post_running = False

    async def _deliver_one(self) -> int:
        """Fetch, format, deliver, and mark one pending post.

        Shared by run_posting (single) and drain_posts (loop).
        Returns:
            0 — success: a post was delivered and marked posted.
            1 — failure: delivery or marking failed.
            3 — no-op: no pending posts to deliver.
        """
        bot_token = os.getenv("BOT_TOKEN", "").strip()
        chat_id = os.getenv("NEWS_CHANNEL_ID", "").strip()

        # TEMPORARY (flow_001092 T2): get_next_pending_post() was removed with
        # the v2 store redesign — the poster will pick the hottest styled row
        # in a later task. Until then, keep oldest-first delivery semantics.
        unposted = self._store.list_unposted_posts()
        post = unposted[0] if unposted else None
        if not post:
            log.debug("no pending posts to deliver")
            return 3

        title = post["title"]
        body = post["body"]
        url = post.get("url") or ""
        message = format_post_message(title, body, url)

        if not bot_token or not chat_id:
            log.info("dry-run: posting to stdout (no BOT_TOKEN/NEWS_CHANNEL_ID)")
            print(message)
            try:
                self._store.mark_posted(post["id"])
            except Exception as db_exc:
                log.error(
                    "CRITICAL: post id=%d dry-run delivered but mark_posted failed: %s",
                    post["id"], redact_exception(db_exc),
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
                post["id"], exc.delivered_chunks, redact_exception(exc),
            )
            try:
                self._store.mark_posted(post["id"])
            except Exception as db_exc:
                log.error("CRITICAL: post id=%d delivered but mark_posted failed: %s "
                          "— row may be re-delivered on retry", post["id"], redact_exception(db_exc))
            return 1  # Still report failure — operator should investigate
        except Exception as exc:
            log.error("failed to post pending post id=%d: %s", post["id"], redact_exception(exc))
            return 1

        # Delivery succeeded — now mark as posted.
        # If mark_posted fails, the row stays pending and will be re-delivered
        # on the next posting cycle, causing duplicate channel posts.
        # We must handle this atomically: if DB fails after Telegram success,
        # log a CRITICAL error so the operator can manually mark it.
        try:
            self._store.mark_posted(post["id"])
        except Exception as db_exc:
            log.error(
                "CRITICAL: post id=%d delivered to Telegram but mark_posted failed: %s "
                "— row will be re-delivered on next cycle unless manually resolved",
                post["id"], redact_exception(db_exc),
            )
            return 1  # Report failure so the scheduler doesn't advance timestamp

        return 0