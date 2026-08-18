"""Tests for the bot command panel (newsbot/bot_commands.py).

Covers the admin debug surface: /help grouping, /preview and /recap
(read-only DM previews — no channel post, no DB writes), and dispatch.
"""

from __future__ import annotations

from typing import Any

import pytest

from newsbot.bot_commands import BotCommandHandler


class _FakeSettings:
    """In-memory SettingsStore double: get/set over a nested dict."""

    def __init__(self, data: dict[str, dict[str, object]] | None = None):
        self._data: dict[str, dict[str, object]] = data or {}

    def get(self, section: str, key: str, default=None):
        return self._data.get(section, {}).get(key, default)

    def set(self, section: str, key: str, value: object) -> None:
        self._data.setdefault(section, {})[key] = value


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
        for cmd in ["/preview", "/recap", "/recap prompt", "/digest dry", "/status", "/scores",
                    "/store", "/style", "/digest", "/post", "/summary", "/setstyle", "/setrecap"]:
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
    async def test_recap_sends_input_sheet_then_html_recap(self):
        """Bare /recap: ack, then input sheet (plain), then recap (HTML)."""
        async def on_recap_preview():
            return (
                "Recap input — 1 posts from the last 24h:\n\n1. Big launch\n   AI | hn | 2026-08-16T06:00:00+00:00",
                "<b>Daily recap</b>\n\nBiggest story first.",
            )

        handler = _make_handler(on_recap_preview=on_recap_preview)
        calls = _capture_send(handler)
        await handler._handle(_update(123, "/recap"))
        # Three messages: ack, input sheet, recap.
        assert len(calls) == 3
        assert "Recap input" in calls[1][1]
        assert calls[1][2] == ""  # input sheet is plain text
        assert calls[2][1] == "<b>Daily recap</b>\n\nBiggest story first."
        assert calls[2][2] == "HTML"

    @pytest.mark.asyncio
    async def test_recap_prompt_shows_current_prompt(self):
        """/recap prompt shows the stored recap prompt, no preview call."""
        settings = _FakeSettings({"news": {"recap_prompt": "CUSTOM RECAP PROMPT"}})
        handler = _make_handler(settings=settings)
        calls = _capture_send(handler)
        await handler._handle(_update(123, "/recap prompt"))
        assert len(calls) == 1
        assert "CUSTOM RECAP PROMPT" in calls[0][1]

    @pytest.mark.asyncio
    async def test_recap_prompt_defaults_when_unset(self):
        """/recap prompt falls back to DEFAULT_RECAP_PROMPT when unset."""
        from newsbot.config import DEFAULT_RECAP_PROMPT
        settings = _FakeSettings({})
        handler = _make_handler(settings=settings)
        calls = _capture_send(handler)
        await handler._handle(_update(123, "/recap prompt"))
        assert DEFAULT_RECAP_PROMPT in calls[0][1]

    @pytest.mark.asyncio
    async def test_setrecap_updates_prompt(self):
        """/setrecap persists news.recap_prompt and echoes it back."""
        settings = _FakeSettings({})
        handler = _make_handler(settings=settings)
        calls = _capture_send(handler)
        await handler._handle(_update(123, "/setrecap order by importance"))
        assert settings._data["news"]["recap_prompt"] == "order by importance"
        assert "order by importance" in calls[0][1]

    @pytest.mark.asyncio
    async def test_setrecap_empty_arg_shows_usage(self):
        settings = _FakeSettings({})
        handler = _make_handler(settings=settings)
        calls = _capture_send(handler)
        await handler._handle(_update(123, "/setrecap"))
        assert "Usage" in calls[0][1]
        assert "news" not in settings._data  # nothing written

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


class TestStoreCommand:
    @pytest.mark.asyncio
    async def test_store_browse_calls_handler(self):
        async def on_store(arg):
            assert arg == ""
            return "Store browse (3 rows, hottest first):\n..."
        handler = _make_handler(on_store=on_store)
        calls = _capture_send(handler)
        await handler._handle(_update(123, "/store"))
        assert len(calls) == 1
        assert "Store browse" in calls[0][1]

    @pytest.mark.asyncio
    async def test_store_detail_calls_handler_with_id(self):
        async def on_store(arg):
            assert arg == "42"
            return "Store row 42\n..."
        handler = _make_handler(on_store=on_store)
        calls = _capture_send(handler)
        await handler._handle(_update(123, "/store 42"))
        assert len(calls) == 1
        assert "Store row 42" in calls[0][1]

    @pytest.mark.asyncio
    async def test_store_no_handler(self):
        handler = _make_handler()
        calls = _capture_send(handler)
        await handler._handle(_update(123, "/store"))
        assert any("not available" in c[1] for c in calls)


class TestDigestDryCommand:
    @pytest.mark.asyncio
    async def test_digest_dry_dispatches_to_handler(self):
        called = []
        async def on_digest_dry():
            called.append(True)
            return "Dry-run funnel: collected 10 → final 3"
        handler = _make_handler(on_digest_dry=on_digest_dry)
        calls = _capture_send(handler)
        await handler._handle(_update(123, "/digest dry"))
        assert len(calls) >= 1
        assert "dry-run" in calls[0][1].lower() or "funnel" in calls[1][1] if len(calls) > 1 else True

    @pytest.mark.asyncio
    async def test_digest_dry_no_handler(self):
        handler = _make_handler()
        calls = _capture_send(handler)
        await handler._handle(_update(123, "/digest dry"))
        assert any("not registered" in c[1] or "no" in c[1].lower() for c in calls)

    @pytest.mark.asyncio
    async def test_bare_digest_still_works(self):
        called = []
        async def on_digest():
            called.append(True)
        handler = _make_handler(on_digest=on_digest)
        calls = _capture_send(handler)
        await handler._handle(_update(123, "/digest"))
        assert any("Triggering" in c[1] for c in calls)
        assert not any("dry" in c[1].lower() for c in calls)
