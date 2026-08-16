"""Tests for the bot command panel (newsbot/bot_commands.py).

Covers the admin debug surface: /help grouping, /preview and /recap
(read-only DM previews — no channel post, no DB writes), and dispatch.
"""

from __future__ import annotations

from typing import Any

import pytest

from newsbot.bot_commands import BotCommandHandler


def _make_handler(**overrides: Any) -> BotCommandHandler:
    kwargs: dict[str, Any] = dict(bot_token="test", admin_user_id="123", settings=None)
    kwargs.update(overrides)
    return BotCommandHandler(**kwargs)


def _capture_send(handler):
    """Replace _send with a recorder. Returns the (chat_id, text, parse_mode) list."""
    calls: list[tuple[int, str, str]] = []

    async def mock_send(chat_id, text, parse_mode=""):
        calls.append((chat_id, text, parse_mode))
        return True

    handler._send = mock_send
    return calls


def _update(user_id, text, chat_id=123):
    """Build a minimal Telegram update dict for _handle()."""
    return {
        "update_id": 1,
        "message": {
            "from": {"id": user_id},
            "chat": {"id": chat_id},
            "text": text,
        },
    }


class TestHelpPanel:
    def test_help_groups_all_commands(self):
        handler = _make_handler()
        text = handler._help_text()
        # Every command in the panel must be documented.
        for cmd in ["/preview", "/recap", "/status", "/scores", "/style",
                    "/digest", "/post", "/summary", "/setstyle"]:
            assert cmd in text, f"/help missing {cmd}"
        # Grouped sections exist.
        assert "Preview" in text
        assert "Inspect" in text
        assert "Run" in text
        assert "Configure" in text

    def test_help_marks_preview_as_non_posting(self):
        """The preview group must be clearly labelled as not posting."""
        handler = _make_handler()
        text = handler._help_text()
        assert "nothing is posted" in text

    @pytest.mark.asyncio
    async def test_help_command_dispatch(self):
        handler = _make_handler()
        calls = _capture_send(handler)
        await handler._handle(_update(123, "/help"))
        assert len(calls) == 1
        assert "/preview" in calls[0][1]

    @pytest.mark.asyncio
    async def test_unknown_command_reply(self):
        handler = _make_handler()
        calls = _capture_send(handler)
        await handler._handle(_update(123, "/bogus"))
        assert len(calls) == 1
        assert "Unknown command" in calls[0][1]

    @pytest.mark.asyncio
    async def test_non_admin_ignored(self):
        handler = _make_handler()
        calls = _capture_send(handler)
        await handler._handle(_update(999, "/help"))
        assert calls == []


class TestPreview:
    @pytest.mark.asyncio
    async def test_preview_sends_styled_html_to_dm(self):
        async def on_preview():
            return "<b>Title</b>\n\nBody text."

        handler = _make_handler(on_preview=on_preview)
        calls = _capture_send(handler)
        await handler._handle(_update(123, "/preview"))
        # Two messages: the "styling…" ack, then the preview itself.
        assert len(calls) == 2
        assert calls[1][1] == "<b>Title</b>\n\nBody text."
        # Preview is rendered as HTML like a real channel post.
        assert calls[1][2] == "HTML"

    @pytest.mark.asyncio
    async def test_preview_runtime_error_surfaced(self):
        async def on_preview():
            raise RuntimeError("Nothing hot enough: hottest 30.0 < threshold 35.0")

        handler = _make_handler(on_preview=on_preview)
        calls = _capture_send(handler)
        await handler._handle(_update(123, "/preview"))
        assert any("Nothing hot enough" in c[1] for c in calls)

    @pytest.mark.asyncio
    async def test_preview_no_handler(self):
        handler = _make_handler()
        calls = _capture_send(handler)
        await handler._handle(_update(123, "/preview"))
        assert any("not available" in c[1] for c in calls)

    @pytest.mark.asyncio
    async def test_preview_empty_result(self):
        async def on_preview():
            return ""

        handler = _make_handler(on_preview=on_preview)
        calls = _capture_send(handler)
        await handler._handle(_update(123, "/preview"))
        assert any("returned nothing" in c[1] for c in calls)


class TestRecapPreview:
    @pytest.mark.asyncio
    async def test_recap_sends_html_to_dm(self):
        async def on_recap_preview():
            return "<b>Daily recap</b>\n\nBiggest story first."

        handler = _make_handler(on_recap_preview=on_recap_preview)
        calls = _capture_send(handler)
        await handler._handle(_update(123, "/recap"))
        assert len(calls) == 2
        assert calls[1][1] == "<b>Daily recap</b>\n\nBiggest story first."
        assert calls[1][2] == "HTML"

    @pytest.mark.asyncio
    async def test_recap_runtime_error_surfaced(self):
        async def on_recap_preview():
            raise RuntimeError("nothing posted in the last 24h — nothing to recap")

        handler = _make_handler(on_recap_preview=on_recap_preview)
        calls = _capture_send(handler)
        await handler._handle(_update(123, "/recap"))
        assert any("nothing posted in the last 24h" in c[1] for c in calls)

    @pytest.mark.asyncio
    async def test_recap_no_handler(self):
        handler = _make_handler()
        calls = _capture_send(handler)
        await handler._handle(_update(123, "/recap"))
        assert any("not available" in c[1] for c in calls)
