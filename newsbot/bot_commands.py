"""Telegram bot command handler via long polling.

Runs concurrently with the scheduler loop. Listens for commands from
ADMIN_USER_ID via Bot API getUpdates (long polling). All other users
are silently ignored.

Commands:
  /setstyle <text>  — update the style prompt for Pass B
  /style            — show the current style prompt
  /digest           — run the generation cycle immediately
  /post             — post the next pending post to the channel now
  /status           — show pending posts count + next gen/post time
  /help             — list commands
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Awaitable, Callable

import httpx

from core.settings_store import SettingsStore
from newsbot.config import DEFAULT_STYLE_PROMPT

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
    ) -> None:
        self.bot_token = bot_token
        self.admin_user_id = str(admin_user_id).strip()
        self.settings = settings
        self.on_digest = on_digest
        self.on_post = on_post
        self.on_status = on_status
        self._offset = 0  # getUpdates offset for ack
        self._client = httpx.AsyncClient(timeout=POLL_TIMEOUT + 10)

    async def _send(self, chat_id: int, text: str) -> None:
        """Send a message to a chat."""
        url = f"{BOT_API_BASE}/bot{self.bot_token}/sendMessage"
        # Split long messages at 4000 chars (Bot API limit is 4096).
        for i in range(0, len(text), 4000):
            chunk = text[i : i + 4000]
            try:
                await self._client.post(url, json={
                    "chat_id": chat_id,
                    "text": chunk,
                    "parse_mode": "",
                })
            except Exception as exc:
                log.warning("bot command reply failed: %s", exc)

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
        elif command == "/help":
            await self._send(chat_id, self._help_text())
        else:
            await self._send(chat_id, f"Unknown command. Try /help")

    def _help_text(self) -> str:
        return (
            "News-bot commands:\n"
            "/setstyle <text> — set the style prompt for post writing\n"
            "/style — show the current style prompt\n"
            "/digest — run the generation cycle now (collect → filter → style → queue)\n"
            "/post — post the next pending post to the channel immediately\n"
            "/status — show pending posts and schedule info\n"
            "/help — show this message"
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
                    await self._send(chat_id, "✅ Generation complete. Posts queued for hourly delivery.")
                except Exception as exc:
                    await self._send(chat_id, f"Generation failed: {exc}")
            asyncio.create_task(_run_and_notify())
        else:
            await self._send(chat_id, "No generation handler registered.")

    async def _cmd_post(self, chat_id: int) -> None:
        if self.on_post:
            async def _post_and_notify() -> None:
                try:
                    await self.on_post()
                except Exception as exc:
                    await self._send(chat_id, f"Post failed: {exc}")
            asyncio.create_task(_post_and_notify())
        else:
            await self._send(chat_id, "No post handler registered.")

    async def _cmd_status(self, chat_id: int) -> None:
        if self.on_status:
            try:
                status_text = await self.on_status()
                await self._send(chat_id, status_text)
            except Exception as exc:
                await self._send(chat_id, f"Status error: {exc}")
        else:
            await self._send(chat_id, "Status handler not available.")

    async def poll_loop(self) -> None:
        """Long-poll getUpdates forever."""
        url = f"{BOT_API_BASE}/bot{self.bot_token}/getUpdates"
        log.info("bot command handler started (admin=%s)", self.admin_user_id)

        while True:
            try:
                params = {"timeout": POLL_TIMEOUT, "offset": self._offset}
                r = await self._client.post(url, json=params)

                if r.status_code >= 400:
                    log.warning("getUpdates failed: %s %s", r.status_code, r.text[:200])
                    await asyncio.sleep(5)
                    continue

                data = r.json()
                if not data.get("ok"):
                    log.warning("getUpdates not ok: %s", data)
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
                log.error("bot command poll error: %s", exc, exc_info=True)
                await asyncio.sleep(5)

    async def close(self) -> None:
        await self._client.aclose()