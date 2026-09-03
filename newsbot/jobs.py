"""In-process job coordinator for generation and posting.

Ensures at most one generation job and one posting/drain operation can
mutate the queue at a time. Both scheduled loops and manual bot commands
go through this coordinator, preventing overlap that could duplicate,
reorder, or lose posts.
"""
from __future__ import annotations

import asyncio
import html as html_module
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from core.log_sanitizer import redact_exception, redact_text
from core.settings_store import SettingsStore
from lm_client import LMClient
from newsbot.config import consumer_profile, load_config
from newsbot.db import NewsStore
from newsbot.images import extract_article_media
from newsbot.richmd import (
    RECAP_MAX_ITEMS,
    _build_channel_link,
    _source_label,
    render_post,
    render_post_blocks,
    signature_for,
)
from newsbot.selection import pick_hottest, select_for_consumer
from newsbot.summarizer import llm_style_posts
from newsbot.telegram_poster import (
    PartialDeliveryError,
    RichSendRejected,
    post_digest,
    post_rich_message,
)

log = logging.getLogger(__name__)


def _build_lm_client() -> LMClient:
    """Build the LMClient for the LLM styler from env (LM_BASE / LM_MODEL / LM_API_KEY)."""
    base = os.getenv("LM_BASE", "").rstrip("/")
    model = os.getenv("LM_MODEL", "")
    if not base or not model:
        raise RuntimeError("LM_BASE and LM_MODEL must be set in the environment")
    timeout = float(os.getenv("LM_TIMEOUT", "300"))
    headers = {}
    api_key = os.getenv("LM_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return LMClient(base, model, timeout, headers=headers, endpoint_path="/chat/completions")


def _row_to_styler_input(row: dict[str, Any]) -> dict[str, Any]:
    """Shape a store row like a digest-time candidate for llm_style_posts.

    The styler reads title/url/snippet/published_at plus the engagement
    signal fields — all persisted on store rows by add_stories_to_store.
    """
    return {
        "candidate_id": f"s{row['id']:03d}",
        "title": row.get("title") or "",
        "url": row.get("url") or "",
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
        self._summary_running = False
        self._last_skip_reason = ""  # last post-skip reason ("" | "empty" | "below_threshold")

    @property
    def last_skip_reason(self) -> str:
        return self._last_skip_reason

    @property
    def generation_running(self) -> bool:
        return self._gen_running

    @property
    def posting_running(self) -> bool:
        return self._post_running

    @property
    def summary_running(self) -> bool:
        return self._summary_running

    async def run_summary(self, summary_fn: Any) -> int:
        """Acquire the job lock and run the daily summary job.

        Returns the summary_fn's result (0=success, 1=failure, 3=skipped
        because fewer than one post landed in the window), or 2 if another
        job is already holding the lock (busy).
        """
        if self._summary_running:
            log.info("summary already in progress — skipping")
            return 2
        self._summary_running = True
        try:
            async with self._job_lock:
                result = await summary_fn()
                return int(result) if result is not None else 0
        finally:
            self._summary_running = False

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

        Used by --once and dry-run modes. Picks and delivers posts
        sequentially until the store is empty (3) or nothing is hot
        enough (4). Returns 0 on success, 1 on failure, 2 if another
        posting is already in progress (skipped).
        """
        if self._post_running:
            log.info("posting already in progress — cannot drain")
            return 2
        self._post_running = True
        try:
            async with self._job_lock:
                while True:
                    result = await self._deliver_one()
                    if result in (3, 4):
                        # Empty store or nothing hot enough — done. Both are
                        # healthy terminal states, so --once/dry-run exit 0.
                        return 0
                    if result != 0:
                        return result
            # Continue until no more pending posts.
        finally:
            self._post_running = False

    async def _deliver_one(self) -> int:
        """Pick the hottest eligible store row, style it, deliver, mark posted.

        Style-at-pick: the store holds RAW scored rows; styling happens here,
        on the single winner, so ≤1 LLM styling call per post (≤12/day)
        instead of styling the whole digest up front.

        Per-consumer selection (flow_001140): uses select_for_consumer with
        the 'telegram' consumer profile from config. The profile carries
        floor, ratio, cooldown_max, max_candidates, and topic filter.
        select_for_consumer computes per-consumer median (§4) and per-consumer
        cooldown (counts only this consumer's deliveries). Behaviour is
        unchanged — the telegram profile mirrors today's env defaults.

        Shared by run_posting (single) and drain_posts (loop).
        Returns:
            0 — success: a post was styled, delivered, and marked posted.
            1 — failure: styler or delivery failed (slot NOT consumed).
            3 — no-op: store is empty.
            4 — threshold skip: nothing hot enough (slot consumed).
        """
        cfg = load_config(self._settings)
        rows = self._store.list_store_rows("telegram")
        now = datetime.now(timezone.utc)

        # consumer_profile raises ValueError("unknown consumer: X") if the
        # telegram profile is missing — one lookup shared with the H4 API
        # key -> consumer mapping.
        profile = consumer_profile(cfg, "telegram")

        # Fetch this consumer's recent deliveries for per-consumer cooldown.
        since = (now - timedelta(hours=24)).isoformat(timespec="seconds")
        deliveries_for_channel = self._store.list_posted_since("telegram", since)

        result = select_for_consumer(
            rows, deliveries_for_channel, profile, cfg, now=now,
        )

        if result.reason == "empty":
            log.debug("store empty — nothing to post")
            self._last_skip_reason = "empty"
            return 3
        if result.reason == "below_threshold":
            log.info(json.dumps({
                "event": "post_skip",
                "threshold": round(result.threshold, 2),
                "median": round(result.median, 2),
                "hottest": round(result.hottest, 2),
                "cooldown_excluded": len(result.excluded_ids),
            }))
            self._last_skip_reason = "below_threshold"
            return 4

        row = result.row
        if row is None:
            return 4  # unreachable in practice; keeps the type narrowed
        row_id = int(row["id"])
        raw_temp = result.temps[row_id]
        self._last_skip_reason = ""  # a pick happened; no skip to report

        # Style the single winner at pick time.
        try:
            styled = await llm_style_posts(
                [_row_to_styler_input(row)],
                _build_lm_client(),
                style_prompt=cfg["style_prompt"],
            )
        except Exception as exc:
            log.error("styler raised for row id=%d — will retry within the hour: %s",
                      row_id, redact_exception(exc))
            return 1
        if not styled:
            log.error("styler failed for row id=%d — will retry within the hour", row_id)
            return 1

        styled_title = str(styled[0].get("title") or row.get("title") or "").strip()
        styled_body = str(styled[0].get("body") or "").strip()
        if not styled_body:
            log.error("styler returned empty body for row id=%d — will retry", row_id)
            return 1

        try:
            self._store.set_styled_content(row_id, styled_title, styled_body)
        except Exception as db_exc:
            log.error("CRITICAL: styled row id=%d but set_styled_content failed: %s",
                      row_id, redact_exception(db_exc))
            return 1

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
            signature=signature_for(os.getenv("NEWS_CHANNEL_ID", "")),
        )
        html_fallback = format_post_message(styled_title, styled_body, row.get("url") or "")

        # Extract article media (images/video) at posting time and switch to
        # the blocks layout when any was found — Bot API 10.2 blocks preserve
        # array order, so media leads the post at the top (the markdown +
        # tg://media path hoists media to the bottom; verified 2026-08-21).
        # Extraction never raises and never blocks posting: on any failure it
        # returns [] and the text-only markdown path is used unchanged.
        try:
            media = await asyncio.to_thread(
                extract_article_media, row.get("url") or ""
            )
        except Exception as exc:  # defensive — extractor is exception-safe
            log.warning("media extraction raised for id=%d: %s",
                        row_id, redact_exception(exc))
            media = []

        blocks = None
        if media:
            blocks = render_post_blocks(
                styled_title, styled_body, row.get("url") or "",
                signature=signature_for(os.getenv("NEWS_CHANNEL_ID", "")),
                media=media,
            )
            log.info("post id=%d carries %d media item(s)", row_id, len(media))

        return await self._send_and_mark(row_id, markdown, html_fallback, blocks=blocks)

    async def _send_and_mark(
        self,
        row_id: int,
        markdown: str,
        html_fallback: str,
        *,
        blocks: list[dict[str, Any]] | None = None,
    ) -> int:
        """Deliver a post (rich markdown/blocks, HTML fallback) and mark it posted.

        Delivery goes through sendRichMessage — the exact renderer and
        transport /preview uses — and falls back to the HTML sendMessage
        path on RichSendRejected. When *blocks* is given (post has embedded
        media), it is preferred over *markdown*; if the blocks send is
        rejected, delivery retries once with the plain markdown, then falls
        back to the HTML sendMessage path. Dry-run (no BOT_TOKEN/NEWS_CHANNEL_ID)
        prints the markdown to stdout. On success the Telegram message_id
        is captured from the response and persisted via mark_posted for
        channel-post linking (OQ-2).
        """
        bot_token = os.getenv("BOT_TOKEN", "").strip()
        chat_id = os.getenv("NEWS_CHANNEL_ID", "").strip()

        if not bot_token or not chat_id:
            log.info("dry-run: posting to stdout (no BOT_TOKEN/NEWS_CHANNEL_ID)")
            print(markdown)
            try:
                self._store.mark_posted(row_id)
            except Exception as db_exc:
                log.error(
                    "CRITICAL: post id=%d dry-run delivered but mark_posted failed: %s",
                    row_id, redact_exception(db_exc),
                )
                return 1
            return 0

        try:
            try:
                if blocks:
                    try:
                        results = await post_rich_message(
                            bot_token=bot_token, chat_id=chat_id, blocks=blocks,
                        )
                    except RichSendRejected:
                        # Blocks send rejected (e.g. a media URL Telegram
                        # couldn't fetch) — retry once with the plain markdown
                        # so the post still gets its text out before the
                        # final HTML fallback.
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
            # Some chunks were delivered, later chunks failed.
            # Mark as posted to prevent duplicate delivery of early chunks on retry.
            log.warning(
                "post id=%d partially delivered (%d chunks sent) — marking as posted "
                "to prevent duplicate sends: %s",
                row_id, exc.delivered_chunks, redact_exception(exc),
            )
            try:
                self._store.mark_posted(row_id)
            except Exception as db_exc:
                log.error("CRITICAL: post id=%d delivered but mark_posted failed: %s "
                          "— row may be re-delivered on retry", row_id, redact_exception(db_exc))
            return 1  # Still report failure — operator should investigate
        except Exception as exc:
            log.error("failed to post store row id=%d: %s", row_id, redact_exception(exc))
            return 1

        # Delivery succeeded — extract message_id from the first chunk's
        # Telegram response for channel-post linking (OQ-2).
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

        # If mark_posted fails, the row stays pending and will be re-delivered
        # on the next posting cycle, causing duplicate channel posts.
        # We must handle this atomically: if DB fails after Telegram success,
        # log a CRITICAL error so the operator can manually mark it.
        try:
            self._store.mark_posted(row_id, message_id=message_id)
        except Exception as db_exc:
            log.error(
                "CRITICAL: post id=%d delivered to Telegram but mark_posted failed: %s "
                "— row will be re-delivered on next cycle unless manually resolved",
                row_id, redact_exception(db_exc),
            )
            return 1  # Report failure so the scheduler doesn't advance timestamp

        return 0