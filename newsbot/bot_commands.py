"""Telegram bot command handler via long polling.

Runs concurrently with the scheduler loop. Listens for commands from
ADMIN_USER_ID via Bot API getUpdates (long polling). All other users
are silently ignored.

Commands:
  Preview (DM only — nothing posted, no DB writes):
    /preview          — style the hottest store story and show it here
    /recap            — preview the daily recap here (input sheet + recap)
    /recap prompt     — show the current recap prompt
  Inspect:
    /status           — store counts, threshold, slots, schedule info
    /scores           — hype scores for all store rows
    /store            — browse all store rows (hottest first)
    /store <id>       — full dump of one store row (scores, merges, styled content)
    /style            — show the current style prompt
  Run (posts to the channel):
    /digest           — run the generation cycle immediately
    /post             — post the hottest store story to the channel now
    /summary          — run the daily recap job now
  Configure:
    /setstyle <text>  — update the style prompt for Pass B
    /setrecap <text>  — update the recap prompt
    /help             — list commands
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Awaitable, Callable

import httpx

from core.log_sanitizer import redact_exception, redact_text
from core.settings_store import SettingsStore
from newsbot.config import DEFAULT_RECAP_PROMPT, DEFAULT_STYLE_PROMPT

log = logging.getLogger(__name__)

BOT_API_BASE = "https://api.telegram.org"
POLL_TIMEOUT = 60  # long-poll seconds


class BotCommandHandler:
    """Long-polls Telegram getUpdates and dispatches commands."""

    def __init__(
        self,
        bot_token: str,
        admin_user_id: str,
        settings: SettingsStore,
        on_digest: Callable[[], Awaitable[None]] | None = None,
        on_post: Callable[[], Awaitable[None]] | None = None,
        on_status: Callable[[], Awaitable[str]] | None = None,
        on_scores: Callable[[], Awaitable[str]] | None = None,
        on_summary: Callable[[], Awaitable[None]] | None = None,
        on_store: Callable[[str], Awaitable[str]] | None = None,
        on_preview: Callable[[], Awaitable[str]] | None = None,
        on_recap_preview: Callable[[], Awaitable[tuple[str, str]]] | None = None,
    ) -> None:
        self.bot_token = bot_token
        self.admin_user_id = str(admin_user_id).strip()
        self.settings = settings
        self.on_digest = on_digest
        self.on_post = on_post
        self.on_status = on_status
        self.on_scores = on_scores
        self.on_summary = on_summary
        self.on_store = on_store
        self.on_preview = on_preview
        self.on_recap_preview = on_recap_preview
        self._offset = 0  # getUpdates offset for ack
        self._client = httpx.AsyncClient(timeout=POLL_TIMEOUT + 10)

    async def _send(self, chat_id: int, text: str, parse_mode: str = "") -> bool:
        """Send a message to a chat. Returns True on success, False on failure."""
        url = f"{BOT_API_BASE}/bot{self.bot_token}/sendMessage"
        # Split long messages at 4000 chars (Bot API limit is 4096).
        for i in range(0, len(text), 4000):
            chunk = text[i : i + 4000]
            try:
                r = await self._client.post(url, json={
                    "chat_id": chat_id,
                    "text": chunk,
                    "parse_mode": parse_mode,
                })
            except Exception as exc:
                log.warning("bot command reply failed: %s", redact_exception(exc))
                return False
            # Validate HTTP status.
            if r.status_code >= 400:
                log.warning("bot command reply failed: status=%d", r.status_code)
                return False
            # Validate Telegram ok field.
            try:
                data = r.json()
                if not data.get("ok"):
                    log.warning("bot command reply: Telegram returned ok=false")
                    return False
            except Exception:
                log.warning("bot command reply: invalid JSON response")
                return False
        return True

    def _is_admin(self, user_id: int) -> bool:
        return str(user_id) == self.admin_user_id

    async def _handle(self, update: dict[str, Any]) -> None:
        """Dispatch a single update."""
        message = update.get("message")
        if not message:
            return

        user = message.get("from") or {}
        user_id = user.get("id")
        chat_id = message.get("chat", {}).get("id")
        text = (message.get("text") or "").strip()

        if not user_id or not self._is_admin(user_id):
            return  # silently ignore non-admin users

        if not text.startswith("/"):
            return

        parts = text.split(maxsplit=1)
        command = parts[0].lower().split("@")[0]  # strip bot suffix
        arg = parts[1].strip() if len(parts) > 1 else ""

        log.info("bot command: %s from user %s", command, user_id)

        if command == "/setstyle":
            await self._cmd_setstyle(chat_id, arg)
        elif command == "/style":
            await self._cmd_show_style(chat_id)
        elif command == "/digest":
            await self._cmd_digest(chat_id)
        elif command == "/post":
            await self._cmd_post(chat_id)
        elif command == "/status":
            await self._cmd_status(chat_id)
        elif command == "/scores":
            await self._cmd_scores(chat_id)
        elif command == "/store":
            await self._cmd_store(chat_id, arg)
        elif command == "/summary":
            await self._cmd_summary(chat_id)
        elif command == "/preview":
            await self._cmd_preview(chat_id)
        elif command == "/recap":
            await self._cmd_recap(chat_id, arg)
        elif command == "/setrecap":
            await self._cmd_setrecap(chat_id, arg)
        elif command == "/help":
            await self._send(chat_id, self._help_text())
        else:
            await self._send(chat_id, f"Unknown command. Try /help")

    def _help_text(self) -> str:
        return (
            "👁 Preview (to this DM — nothing is posted)\n"
            "/preview — style the hottest store story, show here\n"
            "/recap — preview the daily recap (input sheet + recap), show here\n"
            "/recap prompt — show the current recap prompt\n"
            "\n"
            "🔍 Inspect\n"
            "/status — store counts, threshold, slots, schedule\n"
            "/scores — hype scores for all store rows\n"
            "/store — browse all store rows (hottest first)\n"
            "/store <id> — full dump of one store row\n"
            "/style — show the current style prompt\n"
            "\n"
            "▶️ Run (posts to the channel)\n"
            "/digest — collect → filter → store raw now\n"
            "/post — pick hottest, style, post now\n"
            "/summary — run the daily recap now\n"
            "\n"
            "⚙️ Configure\n"
            "/setstyle <text> — set the style prompt\n"
            "/setrecap <text> — set the recap prompt"
        )

    async def _cmd_setstyle(self, chat_id: int, arg: str) -> None:
        if not arg:
            await self._send(chat_id, "Usage: /setstyle <style instructions>")
            return
        self.settings.set("news", "style_prompt", arg)
        log.info("style prompt updated via /setstyle")
        await self._send(chat_id, f"Style prompt updated.\n\nNew prompt:\n{arg}")

    async def _cmd_show_style(self, chat_id: int) -> None:
        prompt = self.settings.get("news", "style_prompt", DEFAULT_STYLE_PROMPT)
        await self._send(chat_id, f"Current style prompt:\n\n{prompt}")

    async def _cmd_digest(self, chat_id: int) -> None:
        if self.on_digest:
            await self._send(chat_id, "Triggering generation cycle now...")
            async def _run_and_notify() -> None:
                try:
                    await self.on_digest()
                    await self._send(chat_id, "✅ Generation complete. Raw stories stored — styling happens at pick.")
                except RuntimeError as exc:
                    await self._send(chat_id, str(exc))
                except Exception:
                    await self._send(chat_id, "Generation failed. Check logs for details.")
            asyncio.create_task(_run_and_notify())
        else:
            await self._send(chat_id, "No generation handler registered.")

    async def _cmd_summary(self, chat_id: int) -> None:
        handler = self.on_summary
        if handler:
            await self._send(chat_id, "Running daily recap job now...")
            async def _run_and_notify() -> None:
                try:
                    await handler()
                    await self._send(chat_id, "✅ Daily recap posted.")
                except RuntimeError as exc:
                    await self._send(chat_id, str(exc))
                except Exception:
                    await self._send(chat_id, "Daily recap failed. Check logs for details.")
            asyncio.create_task(_run_and_notify())
        else:
            await self._send(chat_id, "No summary handler registered.")

    async def _cmd_post(self, chat_id: int) -> None:
        if self.on_post:
            async def _post_and_notify() -> None:
                try:
                    await self.on_post()
                    await self._send(chat_id, "✅ Post delivered to channel.")
                except RuntimeError as exc:
                    await self._send(chat_id, str(exc))
                except Exception:
                    await self._send(chat_id, "Post failed. Check logs for details.")
            asyncio.create_task(_post_and_notify())
        else:
            await self._send(chat_id, "No post handler registered.")

    async def _cmd_preview(self, chat_id: int) -> None:
        """Preview the hottest pick, styled, in this DM. No posting, no DB writes."""
        handler = self.on_preview
        if not handler:
            await self._send(chat_id, "Preview handler not available.")
            return
        await self._send(chat_id, "Styling the hottest store story for preview…")
        try:
            message = await handler()
        except RuntimeError as exc:
            await self._send(chat_id, str(exc))
            return
        except Exception:
            await self._send(chat_id, "Preview failed. Check logs for details.")
            return
        if not message:
            await self._send(chat_id, "Preview returned nothing — styler may have failed.")
            return
        await self._send(chat_id, message, parse_mode="HTML")

    async def _cmd_recap(self, chat_id: int, arg: str = "") -> None:
        """Recap commands.

        /recap          — preview the daily recap in this DM. Two messages:
                          first the input sheet (what the LLM receives),
                          then the generated recap. No posting, no DB writes.
        /recap prompt   — show the current recap prompt.
        """
        if arg.lower() == "prompt":
            prompt = self.settings.get("news", "recap_prompt", DEFAULT_RECAP_PROMPT)
            await self._send(chat_id, f"Current recap prompt:\n\n{prompt}")
            return

        handler = self.on_recap_preview
        if not handler:
            await self._send(chat_id, "Recap preview handler not available.")
            return
        await self._send(chat_id, "Writing the daily recap preview…")
        try:
            sheet, message = await handler()
        except RuntimeError as exc:
            await self._send(chat_id, str(exc))
            return
        except Exception:
            await self._send(chat_id, "Recap preview failed. Check logs for details.")
            return
        if not message:
            await self._send(chat_id, "Recap preview returned nothing — summarizer may have failed.")
            return
        await self._send(chat_id, sheet)
        await self._send(chat_id, message, parse_mode="HTML")

    async def _cmd_setrecap(self, chat_id: int, arg: str) -> None:
        if not arg:
            await self._send(chat_id, "Usage: /setrecap <recap instructions>")
            return
        self.settings.set("news", "recap_prompt", arg)
        log.info("recap prompt updated via /setrecap")
        await self._send(chat_id, f"Recap prompt updated.\n\nNew prompt:\n{arg}")

    async def _cmd_status(self, chat_id: int) -> None:
        if self.on_status:
            try:
                status_text = await self.on_status()
                await self._send(chat_id, status_text)
            except Exception:
                await self._send(chat_id, "Status error. Check logs for details.")
        else:
            await self._send(chat_id, "Status handler not available.")

    async def _cmd_scores(self, chat_id: int) -> None:
        if self.on_scores:
            try:
                scores_text = await self.on_scores()
                await self._send(chat_id, scores_text)
            except Exception:
                await self._send(chat_id, "Scores error. Check logs for details.")
        else:
            await self._send(chat_id, "Scores handler not available.")

    async def _cmd_store(self, chat_id: int, arg: str) -> None:
        if self.on_store:
            try:
                store_text = await self.on_store(arg)
                await self._send(chat_id, store_text)
            except Exception:
                await self._send(chat_id, "Store error. Check logs for details.")
        else:
            await self._send(chat_id, "Store handler not available.")

    async def poll_loop(self) -> None:
        """Long-poll getUpdates forever."""
        url = f"{BOT_API_BASE}/bot{self.bot_token}/getUpdates"
        log.info("bot command handler started (admin=%s)", self.admin_user_id)

        while True:
            try:
                params = {"timeout": POLL_TIMEOUT, "offset": self._offset}
                r = await self._client.post(url, json=params)

                if r.status_code >= 400:
                    log.warning("getUpdates failed: status=%d", r.status_code)
                    await asyncio.sleep(5)
                    continue

                data = r.json()
                if not data.get("ok"):
                    # Log only that the response was not ok — never log the
                    # response object (may contain request echoes or error details).
                    log.warning("getUpdates returned ok=false")
                    await asyncio.sleep(5)
                    continue

                updates = data.get("result") or []
                for update in updates:
                    self._offset = update.get("update_id", 0) + 1
                    await self._handle(update)

            except httpx.TimeoutException:
                # Normal for long polling — just continue.
                continue
            except Exception as exc:
                log.error("bot command poll error: %s", redact_exception(exc), exc_info=False)
                await asyncio.sleep(5)

    async def close(self) -> None:
        await self._client.aclose()