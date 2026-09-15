"""Daily recap generation and delivery."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from core.log_sanitizer import redact_exception
from core.settings_store import SettingsStore
from newsbot.config import load_config
from newsbot.clock import summary_day
from newsbot.db import NewsStore
from newsbot.env import Env, resolve
import newsbot.llm as llm
from newsbot.outcome import Outcome
from newsbot.poster import _format_recap_html_fallback
from newsbot.richmd import render_recap, signature_for
from newsbot.summarizer import llm_daily_summary
from newsbot.telegram_poster import RichSendRejected, post_digest, post_rich_message

log = logging.getLogger(__name__)

def _recap_input_items(rows: list[dict]) -> list[dict[str, Any]]:
    """Build the item list llm_daily_summary receives, from posted store rows.

    Prefers the channel copy on the delivery (styled_title / styled_body /
    message_id). A legacy delivery without styled columns falls back to
    the engine title + summary.
    """
    items: list[dict[str, Any]] = []
    for row in rows:
        styled_body = str(row.get("styled_body") or "").strip()
        summary = str(row.get("summary") or "").strip()
        title = str(row.get("styled_title") or row.get("title") or "")
        items.append({
            "title": title,
            "body": styled_body or summary,
            "category": row.get("category") or "",
            "url": row.get("url") or "",
            "source": row.get("source") or "",
            "posted_at": row.get("posted_at") or "",
            "message_id": row.get("message_id"),
        })
    return items


def _format_recap_input_sheet(items: list[dict[str, Any]]) -> str:
    """Render the /recap input sheet: exactly what the LLM receives.

    Item count, 24h window, and per item: title, category, source,
    posted time. Plain text — this is a transparency aid, not a post.
    """
    lines = [
        f"Recap input — {len(items)} posts from the last 24h:",
        "",
    ]
    for idx, item in enumerate(items, start=1):
        lines.append(f"{idx}. {item.get('title') or '(untitled)'}")
        meta_bits = [
            item.get("category") or "",
            item.get("source") or "",
            item.get("posted_at") or "",
        ]
        lines.append("   " + " | ".join(b for b in meta_bits if b))
    return "\n".join(lines)


async def _run_summary(
    store: NewsStore, settings: SettingsStore, now: datetime, env: Env | None = None,
) -> Outcome:
    """Build and deliver the daily recap of the last 24h of posted news.

    Returns:
        OK — summary generated, delivered, and recorded.
        FAILED — LLM or delivery failure — day NOT consumed, retry next tick.
        NOTHING_TO_DO — nothing posted in the window. Day IS consumed —
            there is nothing to recap and retrying would be pointless.
    """
    day = summary_day(now)
    since_utc = (now.astimezone(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
    rows = store.list_posted_since("telegram", since_utc)
    if not rows:
        log.info("daily summary: no posts in the last 24h — skipping day %s", day)
        return Outcome.NOTHING_TO_DO

    cfg = load_config(settings, env)
    items = _recap_input_items(rows)

    try:
        result = await llm_daily_summary(
            items, llm.build_lm_client("style"), recap_prompt=cfg["recap_prompt"],
        )
    except Exception as exc:
        log.error("daily summary LLM call failed: %s", redact_exception(exc))
        return Outcome.FAILED
    if not result:
        log.error("daily summary LLM returned nothing — will retry")
        return Outcome.FAILED

    env = resolve(env)
    bot_token = env.bot_token
    chat_id = env.news_channel_id

    # Build rich markdown recap + HTML fallback for sendRichMessage failure.
    markdown = render_recap(
        result["title"], result["items"], chat_id=chat_id,
        signature=signature_for(chat_id),
    )
    html_fallback = _format_recap_html_fallback(
        result["title"], result["items"], chat_id=chat_id,
    )

    if not bot_token or not chat_id:
        log.info("dry-run: daily summary to stdout (no BOT_TOKEN/NEWS_CHANNEL_ID)")
        print(markdown)
    else:
        try:
            await post_rich_message(markdown, bot_token=bot_token, chat_id=chat_id)
        except RichSendRejected:
            log.warning("rich recap rejected — falling back to HTML sendMessage")
            try:
                await post_digest(html_fallback, bot_token=bot_token, chat_id=chat_id)
            except Exception as exc:
                log.error("daily summary HTML fallback also failed — will retry: %s", redact_exception(exc))
                return Outcome.FAILED
        except Exception as exc:
            log.error("daily summary delivery failed — will retry: %s", redact_exception(exc))
            return Outcome.FAILED

    try:
        store.add_summary(day, markdown, env.lm_model, len(items))
    except Exception as db_exc:
        # day UNIQUE constraint fires on a re-delivery — not an error for us.
        log.warning("daily summary already recorded for %s: %s", day, redact_exception(db_exc))
    return Outcome.OK
