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

class PartialDeliveryError(Exception):
    """Raised when some chunks were delivered but a later chunk failed.

    Carries the count of successfully delivered chunks so the caller
    can mark the post as partially delivered and avoid re-sending
    the already-delivered chunks on retry.
    """
    def __init__(self, message: str, delivered_chunks: int = 0) -> None:
        super().__init__(message)
        self.delivered_chunks = delivered_chunks


# HTML tags that need closing — used for chunk balance checking.
_OPEN_TAG_RE = re.compile(r"<(b|i|u|s|a|code|pre)(\s[^>]*)?>", re.IGNORECASE)
_CLOSE_TAG_RE = re.compile(r"</(b|i|u|s|a|code|pre)>", re.IGNORECASE)


def _split_for_telegram(text: str, *, limit: int = MAX_CHUNK_CHARS) -> list[str]:
    """Split a long digest into chunks <= limit, on blank-line boundaries.

    Tag-aware: won't split inside HTML tags (<b>...</b>) or HTML entities
    (&amp;). Each chunk is independently valid HTML — all opened tags are
    closed at the chunk boundary and re-opened in the next chunk.

    Tag-balancing overhead (closing + reopening tags) is accounted for
    so the final chunk never exceeds the limit.
    """
    if len(text) <= limit:
        return [text]

    # Reserve margin for tag-balancing overhead (closing + reopening tags).
    # Each tag pair adds at most ~40 chars (e.g. </a><a href="...">).
    _BALANCE_MARGIN = 100
    effective_limit = limit - _BALANCE_MARGIN

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        # Use the effective limit to leave room for tag balancing.
        cut = _find_safe_cut(remaining, effective_limit)
        chunk_text = remaining[:cut].rstrip()
        remaining = remaining[cut:].lstrip("\n")

        # Balance HTML tags in the chunk.
        chunk_text, remaining_suffix = _balance_tags(chunk_text, remaining)
        chunks.append(chunk_text)
        remaining = remaining_suffix + remaining
    if remaining:
        chunks.append(remaining)
    return chunks


def _find_safe_cut(text: str, limit: int) -> int:
    """Find a safe position to cut text at or before limit.

    Avoids splitting inside HTML tags and HTML entities.
    Never returns 0 — if the first opening tag is larger than the limit,
    skips past it to prevent infinite loops in _split_for_telegram().
    """
    # Prefer to split on the last blank line before the limit.
    cut = text.rfind("\n\n", 0, limit)
    if cut <= 0:
        # Fall back to the last newline, else hard cut.
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit

    # Don't split inside an HTML tag (e.g. <a href="...">).
    tag_start = text.rfind("<", 0, cut)
    if tag_start >= 0:
        tag_end = text.find(">", tag_start, cut + 200)
        if tag_end == -1 or tag_end >= cut:
            # We're inside a tag — split before it.
            cut = tag_start

    # Don't split inside an HTML entity (e.g. &amp;, &#123;).
    amp_pos = text.rfind("&", max(0, cut - 20), cut)
    if amp_pos >= 0:
        semi_pos = text.find(";", amp_pos, cut + 20)
        if semi_pos == -1 or semi_pos >= cut:
            cut = amp_pos

    # CRITICAL: if cut is 0, we'd loop forever. This happens when the
    # first thing in the text is an HTML tag longer than the limit.
    # Skip past the oversized tag to make progress.
    if cut <= 0:
        # Find the end of the first tag (if any) and cut after it.
        first_tag_end = text.find(">", 0, limit + 500)
        if first_tag_end >= 0:
            cut = first_tag_end + 1
        else:
            # No tag end found — hard cut at limit (better than infinite loop).
            cut = limit

    return max(1, cut)


def _balance_tags(chunk: str, remaining: str) -> tuple[str, str]:
    """Ensure chunk has balanced HTML tags by closing open tags and
    re-opening them in the remaining text.

    Returns (balanced_chunk, prefix_to_prepend_to_remaining).

    For <a> tags, the original opening tag (with attributes) is preserved
    so the continuation chunk has a matching <a ...> before </a>.

    Tags are processed in token order (interleaved opens and closes),
    not in separate passes — this ensures correct nesting and prevents
    entity fragments from being emitted.
    """
    # Find all open and close tags in token order.
    _ANY_TAG_RE = re.compile(
        r"(?P<open><(b|i|u|s|a|code|pre)(?:\s[^>]*)?>)|(?P<close></(b|i|u|s|a|code|pre)>)",
        re.IGNORECASE,
    )

    stack: list[tuple[str, str]] = []  # (tag_name, full_open_tag)
    for m in _ANY_TAG_RE.finditer(chunk):
        if m.group("open"):
            tag = m.group(2).lower()
            stack.append((tag, m.group(0)))
        elif m.group("close"):
            tag = m.group(4).lower()
            # Pop the matching open tag from the stack.
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

    # Close unclosed tags at the end of the chunk (in reverse order).
    closing = "".join(f"</{tag}>" for tag, _ in reversed(stack))
    balanced_chunk = chunk + closing

    # Re-open the same tags at the start of remaining.
    # For <a> tags, use the original opening tag (with href) so the
    # continuation has a matching <a ...> before </a>.
    reopening_parts: list[str] = []
    for tag, full_tag in stack:
        if tag == "a":
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
                if results:
                    raise PartialDeliveryError(
                        f"chunk {idx}: transport error after {len(results)} chunks delivered",
                        delivered_chunks=len(results),
                    ) from exc
                raise

            if result is not None:
                results.append(result)
            else:
                # Transient retries exhausted — stop sending further chunks.
                # Earlier chunks are already delivered; raise with count
                # so the caller knows how many were sent.
                raise PartialDeliveryError(
                    f"chunk {idx}: Telegram delivery failed after transient retries",
                    delivered_chunks=len(results),
                )

    log.info("Posted digest (%d chunk(s)) to %s", len(results), chat_id)
    return results


# --- Rich Messages (Bot API 10.1+) ---------------------------------------

# Probed empirically on 2026-08-18 by sending oversized markdown to
# Anton's DM (chat_id 60870461) via sendRichMessage and binary-searching
# the accepted length between 4096 and 65536. The API accepted up to
# 32736 characters of markdown; 32800+ returned 400.
# Binary search path: 65536 rejected, 19456 accepted, 32896 rejected,
# 31936 accepted, 32736 accepted, 32800 rejected.
# We use 32736 (last accepted) as the safe limit. Leave a 256-char
# margin for markdown structure overhead (headings, links, etc).
RICH_MESSAGE_MAX_CHARS = 32736


class RichSendRejected(Exception):
    """Raised when sendRichMessage rejects the markdown after re-escaping.

    Callers should catch this to fall back to the HTML sendMessage path
    so the channel never ends up with nothing posted.
    """


def _escape_rich_markdown(text: str) -> str:
    """Backslash-escape an entire document for rich markdown.

    Transport-level last resort: applied to the WHOLE document when
    the API rejects the original. This is the nuclear option — it
    destroys intended markdown structure. Delegates to
    richmd.escape_rich_md for the character set (same chars), applied
    indiscriminately. Renderer-level escaping (applied to content
    segments before structure) uses richmd.escape_rich_md directly.
    """
    from newsbot.richmd import escape_rich_md
    return escape_rich_md(text)


async def post_rich_message(
    markdown: str,
    *,
    bot_token: str,
    chat_id: str,
) -> list[dict[str, Any]]:
    """Post a rich-markdown message via Bot API sendRichMessage.

    Returns a list with one result dict (rich messages fit in one call
    — no chunking needed; RICH_MESSAGE_MAX_CHARS is the probed limit).

    Retries on 429 (capped retry_after) and transient 5xx (bounded to
    MAX_TRANSIENT_RETRIES), reusing _send_with_retry.

    Graceful degradation: on HTTP 400 from the rich call, retries ONCE
    with re-escaped markdown. If that also fails, raises RichSendRejected
    so the caller can fall back to the HTML sendMessage path.
    """
    if not bot_token:
        raise ValueError("BOT_TOKEN is not set")
    if not chat_id:
        raise ValueError("chat_id is not set")

    if len(markdown) > RICH_MESSAGE_MAX_CHARS:
        log.warning(
            "rich message %d chars exceeds probed limit %d — truncating",
            len(markdown), RICH_MESSAGE_MAX_CHARS,
        )
        markdown = markdown[:RICH_MESSAGE_MAX_CHARS]

    # The URL contains the bot token — never log it directly.
    url = f"{BOT_API_BASE}/bot{bot_token}/sendRichMessage"
    payload = {"chat_id": chat_id, "rich_message": {"markdown": markdown}}

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            result = await _send_with_retry(client, url, payload, 0)
        except httpx.HTTPStatusError:
            # 400 or other non-transient HTTP error — retry with escaped markdown.
            log.warning("rich message rejected (HTTP error) — retrying with escaped markdown")
        except httpx.HTTPError as exc:
            log.error("rich message transport error: %s", redact_exception(exc))
            raise RichSendRejected("transport error after retries") from exc
        else:
            if result is not None:
                log.info("Posted rich message to %s", chat_id)
                return [result]
            # _send_with_retry returned None — exhausted 5xx retries.
            log.warning("rich message failed after transient retries — retrying with escaped markdown")

        # Retry with escaped markdown (covers both 400 and None paths).
        escaped = _escape_rich_markdown(markdown)
        escaped_payload = {"chat_id": chat_id, "rich_message": {"markdown": escaped}}

        try:
            result = await _send_with_retry(client, url, escaped_payload, 0)
        except httpx.HTTPStatusError:
            log.error("rich message escaped retry also rejected (HTTP error)")
            raise RichSendRejected("rich message rejected even after re-escaping — fall back to HTML")
        except httpx.HTTPError as exc:
            log.error("rich message escaped retry transport error: %s", redact_exception(exc))
            raise RichSendRejected("escaped retry transport error") from exc

        if result is not None:
            log.warning("rich message sent after escaping fallback")
            return [result]

        raise RichSendRejected("rich message rejected even after re-escaping — fall back to HTML")