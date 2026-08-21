"""Rich Markdown renderers for Telegram Bot API sendRichMessage.

Pure functions — no network, no DB. These produce GFM-flavored markdown
strings that post_rich_message (telegram_poster.py) sends via the
sendRichMessage endpoint (Bot API 10.1+).

Four public renderers:
  - escape_rich_md(text)     — backslash-escape content segments
  - signature_for(chat_id)   — "@handle" signature for @-channels, else ""
  - render_post(title, body, url, signature)  — article layout
  - render_recap(title, items, chat_id, signature) — index layout

Layout decision (2026-08-21): headings ARE used, deliberately. An earlier
revision avoided headings because Telegram clients render them in
client-specific document fonts. Anton probed the actual layouts live via
sendRichMessage (H1–H6 ladders, dividers, collapsible blocks, H4 link
lists) and approved the two shapes below — the readability win outweighs
the font inconsistency. Do not re-reverse this without new live testing.

Post (article layout):
    # {title}

    ---

    {body}

    <details><summary>Source</summary>

    [Source: {domain}]({url})
    </details>

    {signature}

Recap (index layout):
    # {recap title}

    ---

    - #### [{item title}]({channel post url})
    - ...

    ---

    {signature}
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
    placed inside markdown structure (headings, bold, link text, list
    items). URLs are NOT escaped here — they go into link targets as-is
    (see _safe_url).
    """
    result = text.replace("\\", "\\\\")
    for char in _RICH_SPECIAL_CHARS:
        result = result.replace(char, f"\\{char}")
    return result


def signature_for(chat_id: str) -> str:
    """Channel signature appended to posts: the @handle itself.

    Only @username channels get a signature (it doubles as a tappable
    channel mention). Numeric chat ids have no displayable handle.
    """
    chat = chat_id.strip()
    return chat if chat.startswith("@") else ""


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


def render_post(title: str, body: str, url: str, signature: str = "") -> str:
    """Render a single post as rich markdown (article layout).

    Layout:
        # {title}

        ---

        {body paragraphs}

        <details><summary>Source</summary>

        [Source: {domain}]({url})
        </details>

        {signature}

    The <details> block is omitted when url is empty; the signature block
    is omitted when signature is empty (no trailing blank line either).

    Body is truncated at a sentence boundary to fit the rich message
    char budget (RICH_MESSAGE_MAX_CHARS minus structure overhead), then
    escaped — the styler emits plain text, so any markdown special chars
    in it are literal content, not formatting.
    """
    escaped_title = escape_rich_md(title) if title else ""

    # Structure overhead: heading + divider when titled, the details
    # wrapper around the source link, and the signature block.
    _OVERHEAD = 260
    title_block_len = len(f"# {escaped_title}\n\n---\n\n") if escaped_title else 0
    link_block_len = 0
    if url:
        label = _source_label(url)
        link_block_len = len(
            f"\n\n<details><summary>Source</summary>\n\n"
            f"[Source: {escape_rich_md(label)}]({_safe_url(url)})\n</details>"
        )
    sig_block_len = len(f"\n\n{signature}") if signature else 0

    body_budget = max(
        100,
        RICH_MESSAGE_MAX_CHARS - title_block_len - link_block_len - sig_block_len - _OVERHEAD,
    )

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
        parts.append(f"# {escaped_title}")
        parts.append("")
        parts.append("---")
        parts.append("")
    parts.append(escape_rich_md(body))
    if url:
        label = escape_rich_md(_source_label(url))
        safe_url = _safe_url(url)
        parts.append("")
        parts.append("<details><summary>Source</summary>")
        parts.append("")
        parts.append(f"[Source: {label}]({safe_url})")
        parts.append("</details>")
    if signature:
        parts.append("")
        parts.append(signature)
    return "\n".join(parts)


def render_recap(
    title: str,
    items: list[dict[str, Any]],
    chat_id: str = "",
    signature: str = "",
) -> str:
    """Render the daily recap as rich markdown (index layout).

    Layout:
        # {title}

        ---

        - #### [{item title}]({channel post url})
        - ...

        ---

        {signature}

    Each item is an H4 bullet whose whole title is a link to the channel
    post. Items without message_id (legacy) render as unlinked H4 bullets.
    The trailing divider + signature block is omitted when signature is
    empty. No per-item source segments (dropped 2026-08-21 — the approved
    index shape is H4 linked titles only).

    Hard guard: if items > RECAP_MAX_ITEMS, cut and log a warning.
    """
    if len(items) > RECAP_MAX_ITEMS:
        log.warning("recap has %d items, cutting to %d", len(items), RECAP_MAX_ITEMS)
        items = items[:RECAP_MAX_ITEMS]

    escaped_title = escape_rich_md(title)
    lines = [f"# {escaped_title}", "", "---", ""]

    for item in items:
        item_title = str(item.get("title") or "(untitled)").strip()
        message_id = item.get("message_id")

        escaped_item = escape_rich_md(item_title)

        # H4 bullet: linked if we have a channel link, else plain.
        link = _build_channel_link(chat_id, message_id)
        if link:
            lines.append(f"- #### [{escaped_item}]({_safe_url(link)})")
        else:
            lines.append(f"- #### {escaped_item}")

    if signature:
        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append(signature)

    return "\n".join(lines)
