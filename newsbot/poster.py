"""Telegram posting: pick, style, deliver, mark posted."""
from __future__ import annotations

import asyncio
import html as html_module
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from core.log_sanitizer import redact_exception
from core.settings_store import SettingsStore
import newsbot.llm as llm
from newsbot.config import consumer_profile, load_config
from newsbot.db import NewsStore
from newsbot.env import Env, resolve
from newsbot.outcome import Outcome
from newsbot.images import extract_article_media
from newsbot.richmd import (
    RECAP_MAX_ITEMS,
    _build_channel_link,
    _source_label,
    render_post,
    render_post_blocks,
    signature_for,
)
from newsbot.selection import select_for_consumer
from newsbot.summarizer import llm_style_posts
from newsbot.telegram_poster import (
    PartialDeliveryError,
    RichSendRejected,
    post_digest,
    post_rich_message,
)

log = logging.getLogger(__name__)


def _row_to_styler_input(row: dict[str, Any]) -> dict[str, Any]:
    """Shape a store row like a digest-time candidate for llm_style_posts.

    The styler reads title/url/short_summary/published_at plus the
    engagement signal fields. short_summary is the Pass A summary
    persisted on the store row; snippet is collector excerpt only.
    """
    return {
        "candidate_id": f"s{row['id']:03d}",
        "title": row.get("title") or "",
        "url": row.get("url") or "",
        "short_summary": row.get("summary") or "",
        "snippet": row.get("snippet") or "",
        "published_at": row.get("published_at") or "",
        "upvotes": row.get("upvotes") or 0,
        "comments": row.get("comments") or 0,
        "stars": row.get("stars") or 0,
        "crosspost_count": row.get("crosspost_count") or 1,
    }


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


def _format_recap_html_fallback(
    title: str, items: list[dict[str, Any]], *, chat_id: str = "",
) -> str:
    """HTML fallback for the daily recap when sendRichMessage is rejected.

    Renders the same title-only list as richmd.render_recap but in HTML:
    <b>title</b> + numbered <a> lines with channel-post and source links.
    Kept inline — not worth a separate module for a fallback path.
    """
    def render_item(idx: int, item: dict[str, Any]) -> str:
        item_title = str(item.get("title") or "(untitled)").strip()
        url = str(item.get("url") or "").strip()
        message_id = item.get("message_id")
        heading = f"{idx}. "

        parts: list[str] = []
        link = _build_channel_link(chat_id, message_id)
        title_escaped = html_module.escape(item_title)
        if link:
            safe_link = html_module.escape(link, quote=True)
            parts.append(f'{heading}<a href="{safe_link}">{title_escaped}</a>')
        else:
            parts.append(html_module.escape(heading + item_title))
        if url:
            label = html_module.escape(_source_label(url))
            safe_url = html_module.escape(url, quote=True)
            parts.append(f' — <a href="{safe_url}">Source: {label}</a>')
        return "".join(parts)

    lines = [f"<b>{html_module.escape(title)}</b>", ""]
    for idx, item in enumerate(items[:RECAP_MAX_ITEMS], start=1):
        lines.append(render_item(idx, item))
    return "\n".join(lines)

async def deliver_one(
    store: NewsStore, settings: SettingsStore, env: Env | None = None,
) -> Outcome:
    """Pick the hottest eligible store row, style it, deliver, mark posted."""
    env = resolve(env)
    cfg = load_config(settings, env)
    rows = store.list_store_rows("telegram")
    now = datetime.now(timezone.utc)
    profile = consumer_profile(cfg, "telegram")
    since = (now - timedelta(hours=24)).isoformat(timespec="seconds")
    deliveries_for_channel = store.list_posted_since("telegram", since)
    result = select_for_consumer(
        rows, deliveries_for_channel, profile, cfg, now=now,
    )
    if result.reason == "empty":
        log.debug("store empty — nothing to post")
        return Outcome.NOTHING_TO_DO
    if result.reason == "below_threshold":
        log.info(json.dumps({
            "event": "post_skip",
            "threshold": round(result.threshold, 2),
            "median": round(result.median, 2),
            "hottest": round(result.hottest, 2),
            "cooldown_excluded": len(result.excluded_ids),
        }))
        return Outcome.BELOW_THRESHOLD
    row = result.row
    if row is None:
        return Outcome.BELOW_THRESHOLD
    row_id = int(row["id"])
    raw_temp = result.temps[row_id]
    try:
        styled = await llm_style_posts(
            [_row_to_styler_input(row)],
            llm.build_lm_client("style"),
            style_prompt=cfg["style_prompt"],
        )
    except Exception as exc:
        log.error("styler raised for row id=%d — will retry within the hour: %s",
                  row_id, redact_exception(exc))
        return Outcome.FAILED
    if not styled:
        log.error("styler failed for row id=%d — will retry within the hour", row_id)
        return Outcome.FAILED
    styled_title = str(styled[0].get("title") or row.get("title") or "").strip()
    styled_body = str(styled[0].get("body") or "").strip()
    if not styled_body:
        log.error("styler returned empty body for row id=%d — will retry", row_id)
        return Outcome.FAILED
    log.info(json.dumps({
        "event": "post_pick",
        "threshold": round(result.threshold, 2),
        "median": round(result.median, 2),
        "hottest": round(result.hottest, 2),
        "chosen_id": row_id,
        "raw_temp": round(raw_temp, 2),
        "merge_count": row.get("merge_count") or 1,
        "cooldown_excluded": len(result.excluded_ids),
    }))
    markdown = render_post(
        styled_title, styled_body, row.get("url") or "",
        signature=signature_for(env.news_channel_id),
    )
    html_fallback = format_post_message(styled_title, styled_body, row.get("url") or "")
    try:
        media = await asyncio.to_thread(
            extract_article_media, row.get("url") or ""
        )
    except Exception as exc:
        log.warning("media extraction raised for id=%d: %s",
                    row_id, redact_exception(exc))
        media = []
    blocks = None
    if media:
        blocks = render_post_blocks(
            styled_title, styled_body, row.get("url") or "",
            signature=signature_for(env.news_channel_id),
            media=media,
        )
        log.info("post id=%d carries %d media item(s)", row_id, len(media))
    return await _send_and_mark(
        store, row_id, markdown, html_fallback, blocks=blocks,
        styled_title=styled_title, styled_body=styled_body, env=env,
    )


async def drain(
    store: NewsStore, settings: SettingsStore, env: Env | None = None,
) -> Outcome:
    """Drain pending posts until empty or below threshold. Healthy terminals -> OK."""
    while True:
        result = await deliver_one(store, settings, env)
        if result is Outcome.NOTHING_TO_DO or result is Outcome.BELOW_THRESHOLD:
            return Outcome.OK
        if result is not Outcome.OK:
            return result


async def _send_and_mark(
    store: NewsStore,
    row_id: int,
    markdown: str,
    html_fallback: str,
    *,
    blocks: list[dict[str, Any]] | None = None,
    styled_title: str | None = None,
    styled_body: str | None = None,
    env: Env | None = None,
) -> Outcome:
    env = resolve(env)
    bot_token = env.bot_token
    chat_id = env.news_channel_id
    if not bot_token or not chat_id:
        log.info("dry-run: posting to stdout (no BOT_TOKEN/NEWS_CHANNEL_ID)")
        print(markdown)
        try:
            store.mark_posted(
                row_id, styled_title=styled_title, styled_body=styled_body,
            )
        except Exception as db_exc:
            log.error(
                "CRITICAL: post id=%d dry-run delivered but mark_posted failed: %s",
                row_id, redact_exception(db_exc),
            )
            return Outcome.FAILED
        return Outcome.OK
    try:
        try:
            if blocks:
                try:
                    results = await post_rich_message(
                        bot_token=bot_token, chat_id=chat_id, blocks=blocks,
                    )
                except RichSendRejected:
                    log.warning("blocks post id=%d rejected — retrying as plain markdown", row_id)
                    results = await post_rich_message(
                        markdown, bot_token=bot_token, chat_id=chat_id,
                    )
            else:
                results = await post_rich_message(markdown, bot_token=bot_token, chat_id=chat_id)
        except RichSendRejected:
            log.warning("rich post id=%d rejected — falling back to HTML sendMessage", row_id)
            results = await post_digest(html_fallback, bot_token=bot_token, chat_id=chat_id)
    except PartialDeliveryError as exc:
        log.warning(
            "post id=%d partially delivered (%d chunks sent) — marking as posted "
            "to prevent duplicate sends: %s",
            row_id, exc.delivered_chunks, redact_exception(exc),
        )
        try:
            store.mark_posted(
                row_id, styled_title=styled_title, styled_body=styled_body,
            )
        except Exception as db_exc:
            log.error("CRITICAL: post id=%d delivered but mark_posted failed: %s "
                      "— row may be re-delivered on retry", row_id, redact_exception(db_exc))
        return Outcome.FAILED
    except Exception as exc:
        log.error("failed to post store row id=%d: %s", row_id, redact_exception(exc))
        return Outcome.FAILED
    message_id = None
    if results and isinstance(results[0], dict):
        try:
            message_id = results[0].get("result", {}).get("message_id")
        except (AttributeError, TypeError):
            pass
    if message_id is not None:
        log.info("post id=%d delivered as channel message_id=%s", row_id, message_id)
    else:
        log.warning("post id=%d delivered but message_id not found in response", row_id)
    try:
        store.mark_posted(
            row_id, message_id=message_id,
            styled_title=styled_title, styled_body=styled_body,
        )
    except Exception as db_exc:
        log.error(
            "CRITICAL: post id=%d delivered to Telegram but mark_posted failed: %s "
            "— row will be re-delivered on next cycle unless manually resolved",
            row_id, redact_exception(db_exc),
        )
        return Outcome.FAILED
    return Outcome.OK
