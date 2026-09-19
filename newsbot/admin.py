"""Admin command actions — bot callbacks live here, not as nested defs."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta, timezone
from typing import Any

from core.log_sanitizer import redact_exception
from core.settings_store import SettingsStore
from newsbot.admin_views import (
    _format_dry_run_report,
    _format_scores,
    _format_store_browse,
    _format_store_detail,
    _pick_snapshot,
)
from newsbot.bot_commands import outcome_message
from newsbot.clock import local_now
from newsbot.config import load_config
from newsbot.db import NewsStore
from newsbot.env import Env, resolve
from newsbot.generation import (
    GENERATION_TIMEOUT_SECONDS,
    _run_generation,
    _run_generation_pipeline,
    _run_retention,
)
from newsbot.images import extract_article_media
from newsbot.jobs import Busy, JobCoordinator, JobKind, exclusive
import newsbot.llm as llm
from newsbot.outcome import Outcome
from newsbot.poster import (
    _format_recap_html_fallback,
    _row_to_styler_input,
    deliver_one,
    format_post_message,
)
from newsbot.recap import _format_recap_input_sheet, _recap_input_items, _run_summary
from newsbot.richmd import render_post, render_post_blocks, render_recap, signature_for
from newsbot.summarizer import llm_daily_summary, llm_style_posts

log = logging.getLogger(__name__)


def _skip_label(outcome: Outcome | None) -> str:
    if outcome is Outcome.NOTHING_TO_DO:
        return "empty"
    if outcome is Outcome.BELOW_THRESHOLD:
        return "below_threshold"
    return "none"


class AdminActions:
    """Bot command implementations. Dispatch calls self.<name>()."""

    def __init__(
        self,
        store: NewsStore,
        settings: SettingsStore,
        coordinator: JobCoordinator,
        gen_hours: list[int],
        env: Env | None = None,
    ) -> None:
        self._store = store
        self.settings = settings
        self.coordinator = coordinator
        self.gen_hours = gen_hours
        self.env = resolve(env)

    async def digest(self) -> None:
        result = await exclusive(
            self.coordinator,
            JobKind.GENERATION,
            lambda: _run_generation(self._store, self.settings, self.env),
            timeout=GENERATION_TIMEOUT_SECONDS,
        )
        _run_retention(self._store, self.env)
        msg = outcome_message("digest", result)
        if msg:
            raise RuntimeError(msg)

    async def digest_dry(self) -> str:
        """Dry-run generation through the lock; returns the report string."""

        async def _dry_run() -> str:
            cfg = load_config(self.settings, self.env)
            result = await _run_generation_pipeline(self._store, cfg, self.env)
            if result is None:
                return (
                    "Dry-run: pipeline produced nothing "
                    "(empty collection, all seen, or LLM filter empty)."
                )
            report = _format_dry_run_report(result)
            if result.items:
                md_parts = ["", "```markdown"]
                for item in result.items:
                    title = str(item.get("title") or "(untitled)")
                    url = str(item.get("url") or "")
                    body = str(item.get("snippet") or item.get("body") or "")[:200]
                    md_parts.append(render_post(title, body, url))
                md_parts.append("```")
                report += "\n" + "\n".join(md_parts)
            return report

        try:
            return await self.coordinator.run_exclusive(
                JobKind.GENERATION, _dry_run, timeout=GENERATION_TIMEOUT_SECONDS
            )
        except Busy:
            raise RuntimeError(
                outcome_message("digest", Outcome.BUSY)
                or "generation already in progress — skipped"
            )
        except asyncio.TimeoutError:
            raise RuntimeError(
                outcome_message("digest", Outcome.FAILED)
                or "generation failed — check logs for details"
            )

    async def post(self) -> None:
        result = await exclusive(
            self.coordinator,
            JobKind.POSTING,
            lambda: deliver_one(self._store, self.settings, self.env),
        )
        msg = outcome_message("post", result)
        if msg:
            raise RuntimeError(msg)

    async def status(self) -> str:
        cfg = load_config(self.settings, self.env)
        rows = self._store.list_store_rows("telegram")
        result, floor, ratio, _, _ = _pick_snapshot(self._store, cfg)
        last_gen_slot = self.settings.get("scheduler", "last_gen_slot", default="") or ""
        last_post_slot = self.settings.get("scheduler", "last_post_slot", default="") or ""
        last_summary_day = self.settings.get("scheduler", "last_summary_day", default="") or ""
        skip = _skip_label(self.coordinator.last_outcome.get(JobKind.POSTING))
        tz_name = self.env.news_tz
        gen_status = "running" if self.coordinator.running(JobKind.GENERATION) else "idle"
        post_status = "running" if self.coordinator.running(JobKind.POSTING) else "idle"
        summary_status = "running" if self.coordinator.running(JobKind.SUMMARY) else "idle"
        hours = ",".join(str(h) for h in self.gen_hours)
        return (
            f"Store: {len(rows)} rows\n"
            f"Threshold: {result.threshold:.1f} (floor {floor:.1f}, {ratio:.2f}× median {result.median:.1f})\n"
            f"Last skip: {skip}\n"
            f"Last generation slot: {last_gen_slot or 'never'}\n"
            f"Last post slot: {last_post_slot or 'never'}\n"
            f"Last summary day: {last_summary_day or 'never'}\n"
            f"Generation: {gen_status} (slots: {hours})\n"
            f"Posting: {post_status} (even hours)\n"
            f"Summary: {summary_status} (daily at 13:00)\n"
            f"Timezone: {tz_name}"
        )

    async def scores(self) -> str:
        return _format_scores(self._store, load_config(self.settings, self.env))

    async def store(self, arg: str) -> str:
        if arg.strip():
            try:
                row_id = int(arg.strip())
            except ValueError:
                return f"Invalid id: {arg.strip()!r} — /store expects a row id number."
            return _format_store_detail(self._store, row_id)
        return _format_store_browse(self._store, load_config(self.settings, self.env))

    async def summary(self) -> None:
        result = await exclusive(
            self.coordinator,
            JobKind.SUMMARY,
            lambda: _run_summary(self._store, self.settings, local_now(self.env.news_tz), self.env),
        )
        msg = outcome_message("summary", result)
        if msg:
            raise RuntimeError(msg)

    async def preview(self) -> tuple[str, str, list[dict[str, Any]] | None]:
        cfg = load_config(self.settings, self.env)
        result, floor, ratio, merge_bonus, merge_cap = _pick_snapshot(self._store, cfg)
        if result.reason == "empty":
            raise RuntimeError("Store is empty — run /digest first")
        if result.reason == "below_threshold" or result.row is None:
            raise RuntimeError(
                f"Nothing hot enough: hottest {result.hottest:.1f} < "
                f"threshold {result.threshold:.1f}"
            )
        row = result.row
        styled = await llm_style_posts(
            [_row_to_styler_input(row)],
            llm.build_lm_client("style"),
            style_prompt=cfg["style_prompt"],
        )
        if not styled:
            raise RuntimeError("styler returned nothing — check logs")
        title = str(styled[0].get("title") or row.get("title") or "").strip()
        body = str(styled[0].get("body") or "").strip()
        if not body:
            raise RuntimeError("styler returned an empty body — check logs")
        url = row.get("url") or ""
        signature = signature_for(self.env.news_channel_id)
        markdown = render_post(title, body, url, signature)
        try:
            media = await asyncio.to_thread(extract_article_media, url)
        except Exception as exc:
            log.warning("preview media extraction raised: %s", redact_exception(exc))
            media = []
        blocks = None
        if media:
            blocks = render_post_blocks(
                title, body, url, signature=signature, media=media,
            )
            log.info("preview carries %d media item(s)", len(media))
        return markdown, format_post_message(title, body, url), blocks

    async def recap_preview(self) -> tuple[str, str, str]:
        now = local_now(self.env.news_tz)
        since_utc = (now.astimezone(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
        rows = self._store.list_posted_since("telegram", since_utc)
        if not rows:
            raise RuntimeError("nothing posted in the last 24h — nothing to recap")
        cfg = load_config(self.settings, self.env)
        items = _recap_input_items(rows)
        sheet = _format_recap_input_sheet(items)
        result = await llm_daily_summary(
            items, llm.build_lm_client("style"), recap_prompt=cfg["recap_prompt"],
        )
        if not result:
            raise RuntimeError("recap LLM returned nothing — check logs")
        chat_id = self.env.news_channel_id
        return (
            sheet,
            render_recap(
                result["title"], result["items"], chat_id=chat_id,
                signature=signature_for(chat_id),
            ),
            _format_recap_html_fallback(result["title"], result["items"], chat_id=chat_id),
        )
