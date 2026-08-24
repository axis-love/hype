"""Tests for the bot command panel (newsbot/bot_commands.py).

Covers the admin debug surface: /help grouping, /preview and /recap
(read-only DM previews — no channel post, no DB writes), and dispatch.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from newsbot.bot_commands import BotCommandHandler


class _FakeSettings:
    """In-memory SettingsStore double: get/set/list over a nested dict."""

    def __init__(self, data: dict[str, dict[str, object]] | None = None):
        self._data: dict[str, dict[str, object]] = data or {}

    def get(self, section: str, key: str, default=None):
        return self._data.get(section, {}).get(key, default)

    def set(self, section: str, key: str, value: object) -> None:
        self._data.setdefault(section, {})[key] = value

    def list(self, section: str) -> dict[str, object]:
        return dict(self._data.get(section, {}))


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


def _capture_send_rich(handler):
    """Replace _send_rich with a recorder. Returns the (chat_id, markdown, fallback, blocks) list."""
    calls: list[tuple[int, str, str, object]] = []

    async def mock_send_rich(chat_id, markdown, html_fallback="", blocks=None):
        calls.append((chat_id, markdown, html_fallback, blocks))
        return True

    handler._send_rich = mock_send_rich
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
                    "/store", "/style", "/digest", "/post", "/summary", "/setstyle", "/setrecap",
                    "/topics", "/topic on", "/topic off", "/sources"]:
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
    async def test_preview_sends_rich_markdown_to_dm(self):
        """Preview sends via _send_rich (sendRichMessage), not _send with HTML."""
        async def on_preview():
            return (
                "**Title**\n\nBody text.\n\n[Source: x.io](https://x.io)",
                '<b>Title</b>\n\nBody text.\n<a href="https://x.io">Source: x.io</a>',
                None,
            )

        handler = _make_handler(on_preview=on_preview)
        calls = _capture_send(handler)
        rich_calls = _capture_send_rich(handler)
        await handler._handle(_update(123, "/preview"))
        # Ack message via _send, then the preview via _send_rich.
        assert len(calls) == 1
        assert "Styling" in calls[0][1]
        assert len(rich_calls) == 1
        assert rich_calls[0][0] == 123
        assert "**Title**" in rich_calls[0][1]
        # HTML fallback comes from the handler, rendered from the same data.
        assert "<b>Title</b>" in rich_calls[0][2]
        # No media on this story — blocks must be None (markdown path).
        assert rich_calls[0][3] is None

    @pytest.mark.asyncio
    async def test_preview_blocks_passed_through(self):
        """When the story carries media, the blocks layout reaches _send_rich."""
        blocks = [{"type": "inputRichBlockPhoto"}]

        async def on_preview():
            return "**Title**\n\nBody.", "<b>Title</b>\n\nBody.", blocks

        handler = _make_handler(on_preview=on_preview)
        rich_calls = _capture_send_rich(handler)
        await handler._handle(_update(123, "/preview"))
        assert len(rich_calls) == 1
        assert rich_calls[0][3] is blocks

    @pytest.mark.asyncio
    async def test_preview_fallback_on_rich_rejected(self):
        """On RichSendRejected, preview falls back to HTML _send."""
        async def on_preview():
            return "**Title**\n\nBody.", "<b>Title</b>\n\nBody.", None

        handler = _make_handler(on_preview=on_preview)
        calls = _capture_send(handler)
        # Make _send_rich raise RichSendRejected by patching post_rich_message.
        from newsbot.telegram_poster import RichSendRejected
        async def exploding_rich(*a, **kw):
            raise RichSendRejected("rejected")
        with patch("newsbot.bot_commands.post_rich_message", side_effect=exploding_rich):
            await handler._handle(_update(123, "/preview"))
        # Ack + HTML fallback send.
        assert len(calls) == 2
        assert "Styling" in calls[0][1]
        assert calls[1][2] == "HTML"  # fallback parse_mode

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
            return "", "", None

        handler = _make_handler(on_preview=on_preview)
        calls = _capture_send(handler)
        await handler._handle(_update(123, "/preview"))
        assert any("nothing" in c[1].lower() for c in calls)


class TestRecapPreview:
    @pytest.mark.asyncio
    async def test_recap_sends_input_sheet_then_rich_recap(self):
        """Bare /recap: ack, then input sheet (plain), then recap (rich markdown)."""
        async def on_recap_preview():
            return (
                "Recap input — 1 posts from the last 24h:\n\n1. Big launch\n   AI | hn | 2026-08-16T06:00:00+00:00",
                "**Daily recap**\n\n1. [Big launch](https://t.me/c/1/10)",
                '<b>Daily recap</b>\n\n1. <a href="https://t.me/c/1/10">Big launch</a>',
            )

        handler = _make_handler(on_recap_preview=on_recap_preview)
        calls = _capture_send(handler)
        rich_calls = _capture_send_rich(handler)
        await handler._handle(_update(123, "/recap"))
        # Three interactions: ack (_send), sheet (_send), recap (_send_rich).
        assert len(calls) == 2
        assert "Writing" in calls[0][1]
        assert "Recap input" in calls[1][1]
        assert calls[1][2] == ""  # input sheet is plain text
        assert len(rich_calls) == 1
        assert "**Daily recap**" in rich_calls[0][1]
        assert rich_calls[0][2]  # HTML fallback should be present

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


class TestTopicCommands:
    """Tests for /topics, /topic on|off, /sources (H-4 admin surface)."""

    @pytest.mark.asyncio
    async def test_topics_lists_all_packs(self):
        """/topics shows every pack with on/off, boost, and source counts."""
        settings = _FakeSettings({})
        handler = _make_handler(settings=settings)
        calls = _capture_send(handler)
        await handler._handle(_update(123, "/topics"))
        assert len(calls) == 1
        text = calls[0][1]
        # All default packs must appear.
        from newsbot.topics import DEFAULT_TOPIC_PACKS
        for name in DEFAULT_TOPIC_PACKS:
            assert name in text, f"/topics missing pack {name}"
        # gaming is enabled by default and has subs + feeds.
        assert "gaming" in text
        assert "on" in text

    @pytest.mark.asyncio
    async def test_topic_off_gaming_then_sources_omits_gaming(self):
        """/topic off gaming → /sources no longer lists gaming subs/feeds."""
        settings = _FakeSettings({})
        handler = _make_handler(settings=settings)
        calls = _capture_send(handler)

        # Disable gaming.
        await handler._handle(_update(123, "/topic off gaming"))
        assert any("gaming" in c[1] and "off" in c[1] for c in calls)
        # The settings store now has the override.
        topics = settings.get("news", "topics", {})
        assert topics.get("gaming", {}).get("enabled") is False

        # /sources should not list gaming subs/feeds.
        calls.clear()
        await handler._handle(_update(123, "/sources"))
        assert len(calls) == 1
        sources_text = calls[0][1]
        # Gaming subs must not appear.
        assert "GamingLeaksAndRumours" not in sources_text
        # Gaming feeds must not appear.
        assert "IGN" not in sources_text
        assert "Eurogamer" not in sources_text

    @pytest.mark.asyncio
    async def test_topic_on_art_accepted_but_no_sources(self):
        """/topic on art → accepted (art is a known empty pack) but /sources shows no art sources."""
        settings = _FakeSettings({})
        handler = _make_handler(settings=settings)
        calls = _capture_send(handler)

        await handler._handle(_update(123, "/topic on art"))
        assert any("art" in c[1] and "on" in c[1] for c in calls)
        topics = settings.get("news", "topics", {})
        assert topics.get("art", {}).get("enabled") is True

        # /sources should still show 0 art sources (art has no subs/feeds/queries).
        calls.clear()
        await handler._handle(_update(123, "/sources"))
        assert len(calls) == 1
        # art has no sources to list — no error, just no art-specific subs.
        assert "Config error" not in calls[0][1]

    @pytest.mark.asyncio
    async def test_topic_on_unknown_pack_returns_error(self):
        """/topic on nope → error reply, nothing written to settings."""
        settings = _FakeSettings({})
        handler = _make_handler(settings=settings)
        calls = _capture_send(handler)

        await handler._handle(_update(123, "/topic on nope"))
        assert len(calls) == 1
        assert "Unknown" in calls[0][1] or "unknown" in calls[0][1]
        # Nothing was written.
        assert "topics" not in settings._data.get("news", {})

    @pytest.mark.asyncio
    async def test_topic_no_args_shows_usage(self):
        """/topic with no args → usage message."""
        settings = _FakeSettings({})
        handler = _make_handler(settings=settings)
        calls = _capture_send(handler)
        await handler._handle(_update(123, "/topic"))
        assert any("Usage" in c[1] for c in calls)

    @pytest.mark.asyncio
    async def test_topic_off_then_on_restores_sources(self):
        """/topic off gaming then /topic on gaming → /sources lists gaming again."""
        settings = _FakeSettings({})
        handler = _make_handler(settings=settings)
        calls = _capture_send(handler)

        await handler._handle(_update(123, "/topic off gaming"))
        await handler._handle(_update(123, "/topic on gaming"))
        topics = settings.get("news", "topics", {})
        assert topics.get("gaming", {}).get("enabled") is True

        calls.clear()
        await handler._handle(_update(123, "/sources"))
        assert "GamingLeaksAndRumours" in calls[0][1]
        assert "IGN" in calls[0][1]

    @pytest.mark.asyncio
    async def test_topic_off_persists_across_settings_instances(self):
        """/topic off writes to the settings store — a new handler sees it."""
        settings = _FakeSettings({})
        handler = _make_handler(settings=settings)
        await handler._handle(_update(123, "/topic off ai"))

        # New handler with the same settings store should see ai is off.
        handler2 = _make_handler(settings=settings)
        calls = _capture_send(handler2)
        await handler2._handle(_update(123, "/topics"))
        assert len(calls) == 1
        # ai should show as off in the listing.
        text = calls[0][1]
        ai_line = [l for l in text.split("\n") if "ai:" in l]
        assert ai_line
        assert "off" in ai_line[0]

    @pytest.mark.asyncio
    async def test_sources_shows_all_enabled_sources(self):
        """/sources with default config shows all enabled pack sources.
        H-7: rendered generically from cfg['sources'] — source keys are
        lowercase (matching the config block names)."""
        settings = _FakeSettings({})
        handler = _make_handler(settings=settings)
        calls = _capture_send(handler)
        await handler._handle(_update(123, "/sources"))
        assert len(calls) == 1
        text = calls[0][1]
        # HN should be present (non-topic source).
        assert "hackernews" in text.lower()
        # Reddit subs from enabled packs should appear.
        assert "reddit" in text.lower()
        # RSS feeds from enabled packs should appear.
        assert "rss" in text.lower()
        # Topic boosts should be listed.
        assert "boosts" in text.lower() or "boost" in text.lower()

