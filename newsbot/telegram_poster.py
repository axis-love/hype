"""Telegram Bot API poster.

Posts the digest to a channel via the Bot API `sendMessage` endpoint.
No Telethon, no session file, no user-account auth — just an httpx POST
with the bot token. Handles 429 (retry_after) and splits messages over
the 4096-char Bot API limit.

This is the entire delivery surface for the news bot.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from core.log_sanitizer import redact_exception, redact_text, redact_url

log = logging.getLogger(__name__)

BOT_API_BASE = "https://api.telegram.org"
# Bot API hard limit is 4096; leave a margin for Markdown overhead.
MAX_CHUNK_CHARS = 3000


def _split_for_telegram(text: str, *, limit: int = MAX_CHUNK_CHARS) -> list[str]:
    """Split a long digest into chunks <= limit, on blank-line boundaries.

    Tag-aware: won't split inside HTML tags (<b>...</b>) or HTML entities
    (&amp;). Falls back to hard character splits only when a single block
    exceeds the limit.
    """
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        # Prefer to split on the last blank line before the limit.
        cut = remaining.rfind("\n\n", 0, limit)
        if cut <= 0:
            # Fall back to the last newline, else hard cut.
            cut = remaining.rfind("\n", 0, limit)
            if cut <= 0:
                # Hard cut, but check if we're inside an HTML tag or entity.
                cut = limit
                # Don't split inside an HTML tag (e.g. <a href="...">).
                tag_start = remaining.rfind("<", 0, cut)
                tag_end = remaining.find(">", tag_start, cut + 50) if tag_start >= 0 else -1
                if tag_start >= 0 and tag_end == -1:
                    # We're inside a tag — split before it.
                    cut = tag_start
                # Don't split inside an HTML entity (e.g. &amp;).
                elif "&" in remaining[cut - 10:cut]:
                    amp_pos = remaining.rfind("&", cut - 10, cut)
                    semi_pos = remaining.find(";", amp_pos, cut + 10) if amp_pos >= 0 else -1
                    if amp_pos >= 0 and semi_pos == -1:
                        cut = amp_pos
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


async def post_digest(
    text: str,
    *,
    bot_token: str,
    chat_id: str,
    parse_mode: str = "HTML",
) -> list[dict[str, Any]]:
    """Post *text* to the Telegram channel. Returns per-chunk send results.

    Retries once on HTTP 429 (sleeps `retry_after` seconds from the response).
    Raises on any non-2xx final response.
    """
    if not bot_token:
        raise ValueError("BOT_TOKEN is not set")
    if not chat_id:
        raise ValueError("NEWS_CHANNEL_ID is not set")

    # The URL contains the bot token — never log it directly.
    url = f"{BOT_API_BASE}/bot{bot_token}/sendMessage"
    chunks = _split_for_telegram(text)
    results: list[dict[str, Any]] = []

    async with httpx.AsyncClient(timeout=30.0) as client:
        for chunk in chunks:
            payload = {"chat_id": chat_id, "text": chunk, "parse_mode": parse_mode}
            try:
                r = await client.post(url, json=payload)
            except httpx.HTTPError as exc:
                log.error("Telegram request failed: %s", redact_exception(exc))
                raise

            if r.status_code == 429:
                try:
                    retry_after = float(r.json().get("parameters", {}).get("retry_after", 1))
                except Exception:
                    retry_after = 1.0
                log.warning("Telegram 429: sleeping %.1fs before retry", retry_after)
                await asyncio.sleep(retry_after)
                try:
                    r = await client.post(url, json=payload)
                except httpx.HTTPError as exc:
                    log.error("Telegram 429 retry failed: %s", redact_exception(exc))
                    raise

            if r.status_code >= 400:
                # Only retry as plain text for HTML parse errors (400).
                # Do NOT retry on auth failures (401/403), not found (404),
                # server errors (5xx), or a second 429.
                if parse_mode == "HTML" and r.status_code == 400:
                    log.warning("Telegram 400 parse error; retrying chunk as plain text")
                    try:
                        r = await client.post(url, json={**payload, "parse_mode": ""})
                    except httpx.HTTPError as exc:
                        log.error("Telegram plain-text retry failed: %s", redact_exception(exc))
                        raise
                if r.status_code >= 400:
                    # Log status code and a redacted snippet of the response.
                    safe_body = redact_text(r.text[:300], max_length=200)
                    log.error("Telegram send failed: status=%s body=%s", r.status_code, safe_body)
                    r.raise_for_status()

            results.append(r.json())

    log.info("Posted digest (%d chunk(s)) to %s", len(results), chat_id)
    return results