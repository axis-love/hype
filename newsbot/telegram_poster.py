"""Telegram Bot API poster.

Posts the digest to a channel via the Bot API `sendMessage` endpoint.
No Telethon, no session file, no user-account auth — just an httpx POST
with the bot token. Handles 429 (retry_after) and splits messages over
the 4096-char Bot API limit.

This is the entire delivery surface for the news bot.
"""

from __future__ import annotations

import asyncio
import html as html_module
import logging
import re
from typing import Any

import httpx

from core.log_sanitizer import redact_exception, redact_text, redact_url

log = logging.getLogger(__name__)

BOT_API_BASE = "https://api.telegram.org"
# Bot API hard limit is 4096; leave a margin for HTML overhead.
MAX_CHUNK_CHARS = 3000
# Cap retry_after to prevent malicious/erroneous values from suspending
# posting indefinitely.
MAX_RETRY_AFTER = 60.0
# Max retries for transient server errors (5xx, timeout).
MAX_TRANSIENT_RETRIES = 2

# HTML tags that need closing — used for chunk balance checking.
_OPEN_TAG_RE = re.compile(r"<(b|i|u|s|a|code|pre)(\s[^>]*)?>", re.IGNORECASE)
_CLOSE_TAG_RE = re.compile(r"</(b|i|u|s|a|code|pre)>", re.IGNORECASE)


def _split_for_telegram(text: str, *, limit: int = MAX_CHUNK_CHARS) -> list[str]:
    """Split a long digest into chunks <= limit, on blank-line boundaries.

    Tag-aware: won't split inside HTML tags (<b>...</b>) or HTML entities
    (&amp;). Each chunk is independently valid HTML — all opened tags are
    closed at the chunk boundary and re-opened in the next chunk.
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
        chunk_text = remaining[:cut].rstrip()
        remaining = remaining[cut:].lstrip("\n")

        # Balance HTML tags in the chunk.
        chunk_text, remaining_suffix = _balance_tags(chunk_text, remaining)
        chunks.append(chunk_text)
        remaining = remaining_suffix + remaining
    if remaining:
        chunks.append(remaining)
    return chunks


def _balance_tags(chunk: str, remaining: str) -> tuple[str, str]:
    """Ensure chunk has balanced HTML tags by closing open tags and
    re-opening them in the remaining text.

    Returns (balanced_chunk, prefix_to_prepend_to_remaining).

    For <a> tags, the original opening tag (with attributes) is preserved
    so the continuation chunk has a matching <a ...> before </a>.
    """
    # Count open vs close tags in the chunk.
    open_tags: list[str] = []
    open_tag_strs: list[str] = []  # Full tag strings for re-opening (e.g. '<a href="...">')
    for m in _OPEN_TAG_RE.finditer(chunk):
        tag = m.group(1).lower()
        open_tags.append(tag)
        open_tag_strs.append(m.group(0))  # Full tag match including attributes
    close_tags: list[str] = []
    for m in _CLOSE_TAG_RE.finditer(chunk):
        tag = m.group(1).lower()
        close_tags.append(tag)

    # Stack-based matching to find unclosed tags.
    # Track both the tag name and the full opening tag string.
    stack: list[tuple[str, str]] = []  # (tag_name, full_open_tag)
    si = 0
    for tag in open_tags:
        stack.append((tag, open_tag_strs[si]))
        si += 1
    for tag in close_tags:
        if stack and stack[-1][0] == tag:
            stack.pop()
        elif tag in [s[0] for s in stack]:
            # Close out of order — remove from stack
            for i in range(len(stack) - 1, -1, -1):
                if stack[i][0] == tag:
                    stack.pop(i)
                    break

    if not stack:
        return chunk, ""

    # Close unclosed tags at the end of the chunk.
    closing = "".join(f"</{tag}>" for tag, _ in reversed(stack))
    balanced_chunk = chunk + closing

    # Re-open the same tags at the start of remaining.
    # For <a> tags, use the original opening tag (with href) so the
    # continuation has a matching <a ...> before </a>.
    reopening_parts: list[str] = []
    for tag, full_tag in stack:
        if tag == "a":
            # Re-open <a> with original attributes
            reopening_parts.append(full_tag)
        else:
            reopening_parts.append(f"<{tag}>")
    prefix = "".join(reopening_parts)

    return balanced_chunk, prefix


def _is_transient(status_code: int) -> bool:
    """Check if an HTTP status code is a transient error worth retrying."""
    return status_code >= 500


# Regex to strip HTML tags for plain-text fallback.
_STRIP_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    """Strip HTML tags and unescape entities for plain-text fallback.

    Converts <b>Title</b> → Title, &amp; → &, &lt; → <, etc.
    """
    if not text:
        return ""
    # Remove all HTML tags.
    stripped = _STRIP_TAG_RE.sub("", text)
    # Unescape HTML entities.
    return html_module.unescape(stripped)


async def _send_with_retry(
    client: httpx.AsyncClient,
    url: str,
    payload: dict[str, Any],
    chunk_idx: int,
) -> dict[str, Any] | None:
    """Send a single chunk, retrying on transient errors.

    Returns the JSON response dict on success, or None if all retries failed.
    Raises httpx.HTTPError on transport failures that exhaust retries.
    """
    last_exc: Exception | None = None
    for attempt in range(MAX_TRANSIENT_RETRIES + 1):
        try:
            r = await client.post(url, json=payload)
        except httpx.HTTPError as exc:
            last_exc = exc
            if attempt < MAX_TRANSIENT_RETRIES:
                log.warning(
                    "chunk %d: transport error (attempt %d/%d): %s",
                    chunk_idx, attempt + 1, MAX_TRANSIENT_RETRIES + 1,
                    redact_exception(exc),
                )
                await asyncio.sleep(2 ** attempt)  # exponential backoff
                continue
            raise

        if r.status_code == 429:
            try:
                retry_after = float(r.json().get("parameters", {}).get("retry_after", 1))
            except Exception:
                retry_after = 1.0
            retry_after = min(retry_after, MAX_RETRY_AFTER)
            log.warning("chunk %d: Telegram 429: sleeping %.1fs before retry", chunk_idx, retry_after)
            await asyncio.sleep(retry_after)
            continue  # retry on 429

        if _is_transient(r.status_code):
            if attempt < MAX_TRANSIENT_RETRIES:
                log.warning(
                    "chunk %d: server error %d (attempt %d/%d), retrying",
                    chunk_idx, r.status_code, attempt + 1, MAX_TRANSIENT_RETRIES + 1,
                )
                await asyncio.sleep(2 ** attempt)
                continue
            # Exhausted retries on server error — log and return None
            log.error("chunk %d: server error %d after %d attempts", chunk_idx, r.status_code, MAX_TRANSIENT_RETRIES + 1)
            return None

        if r.status_code >= 400:
            # Non-transient error (400, 401, 403, 404, etc.)
            # Only retry as plain text for HTML parse errors (400 with parse
            # error message). Do NOT retry chat-not-found, auth, or other 400s.
            if r.status_code == 400 and payload.get("parse_mode") == "HTML":
                # Check if this is an actual HTML/entity parse error.
                body = ""
                try:
                    body = r.text or ""
                except Exception:
                    pass
                is_parse_error = any(marker in body.lower() for marker in (
                    "can't parse entities",
                    "can't parse ent",
                    "bad request: can't parse",
                    "entity",
                    "tag",
                    "unclosed",
                ))
                if is_parse_error:
                    log.warning("chunk %d: Telegram 400 parse error; retrying as plain text", chunk_idx)
                    # Strip HTML tags and unescape entities for plain-text fallback.
                    plain_text = _strip_html(payload.get("text", ""))
                    try:
                        r = await client.post(url, json={**payload, "text": plain_text, "parse_mode": ""})
                    except httpx.HTTPError as exc:
                        log.error("chunk %d: plain-text retry failed: %s", chunk_idx, redact_exception(exc))
                        raise
                    if r.status_code < 400:
                        return r.json()
            # Log only safe metadata — never log response bodies (may contain
            # echoed request content, chat payloads, or error details).
            log.error("chunk %d: Telegram send failed: status=%d", chunk_idx, r.status_code)
            r.raise_for_status()

        # Success
        return r.json()

    # Shouldn't reach here, but just in case
    if last_exc:
        raise last_exc
    return None


async def post_digest(
    text: str,
    *,
    bot_token: str,
    chat_id: str,
    parse_mode: str = "HTML",
) -> list[dict[str, Any]]:
    """Post *text* to the Telegram channel. Returns per-chunk send results.

    Retries on 429 (sleeps capped retry_after) and transient server errors
    (5xx, timeout — bounded to MAX_TRANSIENT_RETRIES). Does NOT retry on
    auth failures (401/403), not found (404), or other non-transient errors.

    Each chunk is tracked independently — if an early chunk succeeds and a
    later one fails, the function raises immediately. The caller is
    responsible for NOT re-calling with the same text (the post is already
    partially delivered).

    Raises on any final non-2xx response or exhausted transport retries.
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
        for idx, chunk in enumerate(chunks):
            payload = {"chat_id": chat_id, "text": chunk, "parse_mode": parse_mode}
            try:
                result = await _send_with_retry(client, url, payload, idx)
            except httpx.HTTPError as exc:
                log.error("chunk %d: transport error after retries: %s", idx, redact_exception(exc))
                raise

            if result is not None:
                results.append(result)
            else:
                # Transient retries exhausted — stop sending further chunks.
                # Earlier chunks are already delivered; caller must not retry.
                raise RuntimeError(
                    f"chunk {idx}: Telegram delivery failed after transient retries "
                    f"(earlier chunks {len(results)} already sent)"
                )

    log.info("Posted digest (%d chunk(s)) to %s", len(results), chat_id)
    return results