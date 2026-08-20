"""Rich Markdown renderers for Telegram Bot API sendRichMessage.

Pure functions — no network, no DB. These produce GFM-flavored markdown
strings that post_rich_message (telegram_poster.py) sends via the
sendRichMessage endpoint (Bot API 10.1+).

Three renderers:
  - escape_rich_md(text)     — backslash-escape content segments
  - render_post(title, body, url)  — single-post markdown (bold title + body + source link)
  - render_recap(title, items, chat_id) — daily recap (bold title + ordered list of linked titles)

Titles render as **bold**, never as # headings: Telegram clients render
markdown headings in client-specific document fonts (serif on iOS, UI sans
on desktop), so headings look different on every device. Bold text uses the
standard message font everywhere.
"""
from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger(__name__)

# Probed empirically on 2026-08-18 by sending oversized markdown to
# Anton's DM via sendRichMessage and binary-searching the accepted length
# between 4096 and 65536. The API accepted up to 32736 characters; 32800+
# returned 400. We use 32736 (last accepted) as the safe limit.
RICH_MESSAGE_MAX_CHARS = 32736

# Hard cap on recap list length — shared by render_recap and the HTML
# fallback renderer (jobs._format_recap_html_fallback).
RECAP_MAX_ITEMS = 30

# Characters with special meaning in GFM-flavored rich markdown.
# Backslash must be escaped first to avoid double-escaping.
_RICH_SPECIAL_CHARS = "*_~`[]|>#"


def escape_rich_md(text: str) -> str:
    """Backslash-escape a content segment for safe placement in rich markdown.

    Escapes backslash first, then: * _ ~ ` [ ] | > #
    Applied to titles, bodies, labels, and other content BEFORE they are
    placed inside markdown structure (bold, link text, list items). URLs are
    NOT escaped here — they go into link targets as-is (see _safe_url).
    """
    result = text.replace("\\", "\\\\")
    for char in _RICH_SPECIAL_CHARS:
        result = result.replace(char, f"\\{char}")
    return result


def _safe_url(url: str) -> str:
    """Make a URL safe inside a markdown link target: [text](url).

    Markdown links use (url) — parens inside the URL must be percent-encoded
    or the link breaks. Query params with parens are common (Wikipedia, etc).
    """
    # Percent-encode parentheses and spaces in the URL.
    return url.replace("(", "%28").replace(")", "%29").replace(" ", "%20")


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


def _build_channel_link(chat_id: str, message_id: int | None) -> str | None:
    """Build a t.me link to a channel post.

    - numeric id (-1001234567890) -> https://t.me/c/1234567890/<message_id>
    - @username channel           -> https://t.me/username/<message_id>
    - message_id is None          -> None (legacy rows have no link)
    """
    if message_id is None:
        return None
    chat = chat_id.strip()
    if chat.startswith("@"):
        return f"https://t.me/{chat[1:]}/{message_id}"
    if chat.startswith("-100"):
        raw_id = chat[4:]
        return f"https://t.me/c/{raw_id}/{message_id}"
    return None


def render_post(title: str, body: str, url: str) -> str:
    """Render a single post as rich markdown.

    Layout:
        **{title}**

        {body paragraphs}

        [Source: {domain}]({url})

    Body is truncated at a sentence boundary to fit the rich message
    char budget (RICH_MESSAGE_MAX_CHARS minus title + link overhead),
    then escaped — the styler emits plain text, so any markdown special
    chars in it are literal content, not formatting.
    """
    escaped_title = escape_rich_md(title) if title else ""

    # Overhead: "**{title}**\n\n" + "\n\n[Source: {label}]({url})"
    # plus margin for body escaping (backslashes added by escape_rich_md).
    _OVERHEAD = 200
    title_block_len = len(f"**{escaped_title}**\n\n") if escaped_title else 0
    link_block_len = 0
    if url:
        label = _source_label(url)
        link_block_len = len(f"\n\n[Source: {escape_rich_md(label)}]({_safe_url(url)})")

    body_budget = max(100, RICH_MESSAGE_MAX_CHARS - title_block_len - link_block_len - _OVERHEAD)

    # Truncate body at a sentence boundary if it exceeds the budget.
    if len(body) > body_budget:
        cut = body.rfind(". ", 0, body_budget)
        if cut > body_budget // 2:
            body = body[:cut + 1]
        else:
            body = body[:body_budget].rsplit(" ", 1)[0] + "..."
        log.debug("truncated post body to %d chars (budget %d) for rich message",
                  len(body), body_budget)

    parts: list[str] = []
    if escaped_title:
        parts.append(f"**{escaped_title}**")
        parts.append("")
    parts.append(escape_rich_md(body))
    if url:
        label = escape_rich_md(_source_label(url))
        safe_url = _safe_url(url)
        parts.append(f"\n[Source: {label}]({safe_url})")
    return "\n".join(parts)


def render_recap(title: str, items: list[dict[str, Any]], chat_id: str = "") -> str:
    """Render the daily recap as rich markdown.

    Layout:
        **{title}**

        1. [{item title}]({channel post url}) — [{domain}]({source url})
        2. ...

    Items without message_id (legacy) render the title as plain text
    but still show the source link. No per-item summaries.

    Hard guard: if items > RECAP_MAX_ITEMS, cut and log a warning.
    """
    if len(items) > RECAP_MAX_ITEMS:
        log.warning("recap has %d items, cutting to %d", len(items), RECAP_MAX_ITEMS)
        items = items[:RECAP_MAX_ITEMS]

    escaped_title = escape_rich_md(title)
    lines = [f"**{escaped_title}**", ""]

    for idx, item in enumerate(items, start=1):
        item_title = str(item.get("title") or "(untitled)").strip()
        url = str(item.get("url") or "").strip()
        message_id = item.get("message_id")

        escaped_title_seg = escape_rich_md(item_title)

        # Build the title segment: linked if we have a channel link, else plain.
        link = _build_channel_link(chat_id, message_id)
        if link:
            title_seg = f"[{escaped_title_seg}]({_safe_url(link)})"
        else:
            title_seg = escaped_title_seg

        # Source link segment.
        source_seg = ""
        if url:
            label = escape_rich_md(_source_label(url))
            safe_url = _safe_url(url)
            source_seg = f" — [{label}]({safe_url})"

        lines.append(f"{idx}. {title_seg}{source_seg}")

    return "\n".join(lines)
