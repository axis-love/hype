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

log = logging.getLogger(__name__)

BOT_API_BASE = "https://api.telegram.org"
# Bot API hard limit is 4096; leave a margin for Markdown overhead.
MAX_CHUNK_CHARS = 3000


def _split_for_telegram(text: str, *, limit: int = MAX_CHUNK_CHARS) -> list[str]:
    """Split a long digest into chunks <= limit, on blank-line boundaries.

    Falls back to hard character splits if a single block exceeds the limit.
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
                cut = limit
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

    url = f"{BOT_API_BASE}/bot{bot_token}/sendMessage"
    chunks = _split_for_telegram(text)
    results: list[dict[str, Any]] = []

    async with httpx.AsyncClient(timeout=30.0) as client:
        for chunk in chunks:
            payload = {"chat_id": chat_id, "text": chunk, "parse_mode": parse_mode}
            r = await client.post(url, json=payload)

            if r.status_code == 429:
                try:
                    retry_after = float(r.json().get("parameters", {}).get("retry_after", 1))
                except Exception:
                    retry_after = 1.0
                log.warning("Telegram 429: sleeping %.1fs before retry", retry_after)
                await asyncio.sleep(retry_after)
                r = await client.post(url, json=payload)

            if r.status_code >= 400:
                # HTML parse errors are common; retry the chunk as plain text.
                if parse_mode == "HTML":
                    log.warning("Telegram send failed (status=%s); retrying chunk as plain text", r.status_code)
                    r = await client.post(url, json={**payload, "parse_mode": ""})
                if r.status_code >= 400:
                    log.error("Telegram send failed permanently: %s %s", r.status_code, r.text[:300])
                    r.raise_for_status()

            results.append(r.json())

    log.info("Posted digest (%d chunk(s)) to %s", len(results), chat_id)
    return results