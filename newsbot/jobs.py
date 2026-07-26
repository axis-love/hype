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

from core.settings_store import SettingsStore
from newsbot.db import NewsStore
from newsbot.telegram_poster import post_digest

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
    """
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
    """Serializes generation and posting jobs via asyncio locks.

    - At most one generation job runs at a time (gen_lock).
    - At most one posting/drain operation runs at a time (post_lock).
    - Generation and posting can run concurrently with each other,
      but not with another instance of the same type.
    """

    def __init__(self, store: NewsStore, settings: SettingsStore) -> None:
        self._store = store
        self._settings = settings
        self._gen_lock = asyncio.Lock()
        self._post_lock = asyncio.Lock()
        self._gen_running = False
        self._post_running = False

    @property
    def generation_running(self) -> bool:
        return self._gen_running

    @property
    def posting_running(self) -> bool:
        return self._post_running

    async def run_generation(self, gen_fn: Any) -> bool:
        """Acquire the generation lock and run the generation cycle.

        Returns True if the job ran, False if another generation is
        already in progress (caller should report coalesced/rejected).
        """
        if self._gen_running:
            log.info("generation already in progress — skipping")
            return False
        async with self._gen_lock:
            self._gen_running = True
            try:
                await gen_fn()
                return True
            finally:
                self._gen_running = False

    async def run_posting(self) -> int:
        """Acquire the posting lock and post one pending post.

        Returns 0 on success, 1 on failure, 2 if another posting is
        in progress (skipped).
        """
        if self._post_running:
            log.info("posting already in progress — skipping")
            return 2
        async with self._post_lock:
            self._post_running = True
            try:
                return await self._post_one()
            finally:
                self._post_running = False

    async def drain_posts(self) -> int:
        """Acquire the posting lock and drain all pending posts.

        Used by --once and dry-run modes. Posts all pending posts
        sequentially. Returns 0 on success, 1 on failure.
        """
        if self._post_running:
            log.info("posting already in progress — cannot drain")
            return 1
        async with self._post_lock:
            self._post_running = True
            try:
                return await self._drain_all()
            finally:
                self._post_running = False

    async def _post_one(self) -> int:
        """Post the oldest pending post to Telegram (or stdout in dry-run)."""
        bot_token = os.getenv("BOT_TOKEN", "").strip()
        chat_id = os.getenv("NEWS_CHANNEL_ID", "").strip()

        post = self._store.get_next_pending_post()
        if not post:
            log.debug("no pending posts to deliver")
            return 0

        title = post["title"]
        body = post["body"]
        url = post.get("url") or ""
        message = format_post_message(title, body, url)

        if not bot_token or not chat_id:
            log.info("dry-run: posting to stdout (no BOT_TOKEN/NEWS_CHANNEL_ID)")
            print(message)
            self._store.mark_posted(post["id"])
            return 0

        try:
            await post_digest(message, bot_token=bot_token, chat_id=chat_id)
            self._store.mark_posted(post["id"])
            log.info("posted pending post id=%d to Telegram", post["id"])
        except Exception as exc:
            log.error("failed to post pending post id=%d: %s", post["id"], exc)
            return 1

        return 0

    async def _drain_all(self) -> int:
        """Post all pending posts sequentially (for --once / dry-run)."""
        bot_token = os.getenv("BOT_TOKEN", "").strip()
        chat_id = os.getenv("NEWS_CHANNEL_ID", "").strip()

        while True:
            post = self._store.get_next_pending_post()
            if not post:
                break
            title = post["title"]
            body = post["body"]
            url = post.get("url") or ""
            message = format_post_message(title, body, url)
            if not bot_token or not chat_id:
                print(message)
                self._store.mark_posted(post["id"])
            else:
                try:
                    await post_digest(message, bot_token=bot_token, chat_id=chat_id)
                    self._store.mark_posted(post["id"])
                except Exception as exc:
                    log.error("failed to post pending post id=%d: %s", post["id"], exc)
                    return 1
        return 0