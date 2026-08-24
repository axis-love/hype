"""H-7 regression tests: config & admin correctness.

Covers the four acceptance items from the H-7 brief:
  1. DEFAULT_GEN_HOURS — single constant in clock.py, main.py references it.
  2. /topic — merge enabled into existing override (preserves feeds),
     validate before set.
  3. /sources — generic rendering from cfg["sources"], shadowed-block flag.
  4. Single source of truth for source keys — registry keys == config keys.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from newsbot.bot_commands import BotCommandHandler
from tests.test_bot_commands import _FakeSettings, _capture_send, _make_handler, _update


# ──────────────────────────────────────────────────────────────────────
# Item 1: DEFAULT_GEN_HOURS
# ──────────────────────────────────────────────────────────────────────

class TestDefaultGenHours:
    """Single constant in clock.py, referenced by main.py."""

    def test_clock_exports_default_gen_hours(self):
        from newsbot.clock import DEFAULT_GEN_HOURS
        assert DEFAULT_GEN_HOURS == "5,9,13,17,21"

    def test_main_py_uses_default_gen_hours_not_hardcoded(self):
        """main.py must import DEFAULT_GEN_HOURS from clock, not hardcode "5,17"."""
        import newsbot.clock as clock
        import newsbot.main as main_mod

        # The import line in main.py references DEFAULT_GEN_HOURS.
        src = open(main_mod.__file__).read()
        assert "DEFAULT_GEN_HOURS" in src, "main.py must import DEFAULT_GEN_HOURS"
        # The stale hardcoded "5,17" default must be gone from the getenv call.
        assert 'os.getenv("NEWS_GEN_HOURS", "5,17")' not in src, (
            "main.py still has the stale hardcoded '5,17' default — "
            "use DEFAULT_GEN_HOURS instead"
        )
        # And the module docstring must not say "5,17".
        assert 'default "5,17"' not in src, "main.py docstring still says default '5,17'"

    def test_gen_slots_default_matches_cadence(self):
        from newsbot.clock import DEFAULT_GEN_HOURS, gen_slots
        assert gen_slots(DEFAULT_GEN_HOURS) == [5, 9, 13, 17, 21]


# ──────────────────────────────────────────────────────────────────────
# Item 2: /topic — merge + validate-before-set
# ──────────────────────────────────────────────────────────────────────

class TestTopicMergePreservesFeeds:
    """/topic on|off must merge into the existing per-topic override dict,
    not replace it — keys like 'feeds' written by the migration script
    must survive."""

    @pytest.mark.asyncio
    async def test_topic_off_preserves_existing_feeds_key(self):
        """An existing override with a 'feeds' key must keep it after /topic off."""
        settings = _FakeSettings({"news": {"topics": {
            "gaming": {"feeds": [{"name": "IGN", "url": "https://x"}]},
        }}})
        handler = _make_handler(settings=settings)
        calls = _capture_send(handler)

        await handler._handle(_update(123, "/topic off gaming"))
        topics = settings.get("news", "topics", {})
        # enabled is now False, but feeds survived.
        assert topics["gaming"]["enabled"] is False
        assert "feeds" in topics["gaming"], "feeds key was clobbered by /topic off"
        assert topics["gaming"]["feeds"] == [{"name": "IGN", "url": "https://x"}]

    @pytest.mark.asyncio
    async def test_topic_on_preserves_existing_subreddits_key(self):
        """An existing override with 'subreddits' must keep it after /topic on."""
        settings = _FakeSettings({"news": {"topics": {
            "ai": {"subreddits": ["LocalLLaMA"]},
        }}})
        handler = _make_handler(settings=settings)

        await handler._handle(_update(123, "/topic on ai"))
        topics = settings.get("news", "topics", {})
        assert topics["ai"]["enabled"] is True
        assert topics["ai"]["subreddits"] == ["LocalLLaMA"]

    @pytest.mark.asyncio
    async def test_topic_off_on_cycle_preserves_feeds(self):
        """/topic off then /topic on must not lose feeds across the cycle."""
        settings = _FakeSettings({"news": {"topics": {
            "gaming": {"feeds": [{"name": "IGN", "url": "https://x"}]},
        }}})
        handler = _make_handler(settings=settings)

        await handler._handle(_update(123, "/topic off gaming"))
        await handler._handle(_update(123, "/topic on gaming"))
        topics = settings.get("news", "topics", {})
        assert topics["gaming"]["enabled"] is True
        assert topics["gaming"]["feeds"] == [{"name": "IGN", "url": "https://x"}]


class TestTopicValidateBeforeSet:
    """/topic must validate the merged overrides before persisting —
    a stale key takes down every subsequent load_config tick."""

    @pytest.mark.asyncio
    async def test_refuses_to_persist_stale_key(self):
        """If the existing override has an invalid pack-field key, /topic must
        refuse to persist and reply with the validation error."""
        settings = _FakeSettings({"news": {"topics": {
            "gaming": {"bogus_field": "should-not-exist"},
        }}})
        handler = _make_handler(settings=settings)
        calls = _capture_send(handler)

        await handler._handle(_update(123, "/topic off gaming"))

        # Must NOT have persisted — the original override is untouched.
        topics = settings.get("news", "topics", {})
        assert "enabled" not in topics.get("gaming", {}), (
            "/topic persisted an invalid override — validation did not run"
        )
        # Must have replied with a validation error.
        assert any("validation" in c[1].lower() or "refusing" in c[1].lower()
                   for c in calls), "Expected validation error reply"

    @pytest.mark.asyncio
    async def test_valid_override_persists(self):
        """A clean /topic on must still persist normally."""
        settings = _FakeSettings({})
        handler = _make_handler(settings=settings)

        await handler._handle(_update(123, "/topic on ai"))
        topics = settings.get("news", "topics", {})
        assert topics["ai"]["enabled"] is True

    @pytest.mark.asyncio
    async def test_unknown_pack_name_still_rejected_before_validation(self):
        """Unknown pack name is caught by the existing check, not validation."""
        settings = _FakeSettings({})
        handler = _make_handler(settings=settings)
        calls = _capture_send(handler)

        await handler._handle(_update(123, "/topic on nope"))
        assert any("Unknown" in c[1] or "unknown" in c[1] for c in calls)
        assert "topics" not in settings._data.get("news", {})


# ──────────────────────────────────────────────────────────────────────
# Item 3: /sources — generic rendering + shadow detection
# ──────────────────────────────────────────────────────────────────────

class TestSourcesGenericRendering:
    """/sources renders generically from cfg['sources']."""

    @pytest.mark.asyncio
    async def test_sources_lists_all_source_blocks(self):
        """Default config: /sources shows every active source block."""
        settings = _FakeSettings({})
        handler = _make_handler(settings=settings)
        calls = _capture_send(handler)

        await handler._handle(_update(123, "/sources"))
        text = calls[0][1]
        # All non-topic defaults must appear.
        assert "hackernews" in text
        assert "huggingface_papers" in text
        assert "trends" in text
        # Pack-derived sources too.
        assert "reddit" in text
        assert "rss" in text
        assert "github" in text

    @pytest.mark.asyncio
    async def test_sources_renders_unknown_block_as_json(self):
        """An unknown source block (if it got into config) renders as JSON,
        not silently dropped — the generic renderer handles any shape."""
        from newsbot.bot_commands import BotCommandHandler
        block = {"custom_key": "custom_value", "items": [1, 2, 3]}
        summary = BotCommandHandler._render_source_block("unknown_src", block)
        assert "custom_key" in summary
        assert "custom_value" in summary

    @pytest.mark.asyncio
    async def test_sources_renders_empty_block(self):
        from newsbot.bot_commands import BotCommandHandler
        assert BotCommandHandler._render_source_block("reddit", {}) == "(empty)"


class TestShadowDetection:
    """load_config reports shadowed blocks; /sources surfaces the signal."""

    def test_load_config_reports_no_shadowed_by_default(self):
        from newsbot.config import load_config
        settings = _FakeSettings({})
        cfg = load_config(settings)
        assert cfg["shadowed_sources"] == []

    def test_load_config_reports_shadowed_for_explicit_source(self):
        """An explicit news.sources override shadows the pack-derived block."""
        from newsbot.config import load_config
        settings = _FakeSettings({"news": {"sources": {
            "reddit": {"subreddits": ["customsub"], "limit": 5},
        }}})
        cfg = load_config(settings)
        assert "reddit" in cfg["shadowed_sources"]
        # The override won.
        assert cfg["sources"]["reddit"]["subreddits"] == ["customsub"]

    def test_shadowed_disables_pack_toggle(self):
        """When news.sources shadows reddit, disabling the ai topic does NOT
        change the reddit block — /topic is inert for shadowed blocks."""
        from newsbot.config import load_config
        settings = _FakeSettings({"news": {
            "sources": {"reddit": {"subreddits": ["customsub"], "limit": 5}},
            "topics": {"ai": {"enabled": False}},
        }})
        cfg = load_config(settings)
        # reddit block is still the explicit override, not pack-derived.
        assert cfg["sources"]["reddit"]["subreddits"] == ["customsub"]
        assert "reddit" in cfg["shadowed_sources"]

    @pytest.mark.asyncio
    async def test_sources_flags_shadowed_blocks(self):
        """/sources shows a warning when shadowed blocks exist."""
        settings = _FakeSettings({"news": {"sources": {
            "reddit": {"subreddits": ["customsub"], "limit": 5},
        }}})
        handler = _make_handler(settings=settings)
        calls = _capture_send(handler)

        await handler._handle(_update(123, "/sources"))
        text = calls[0][1]
        assert "override" in text.lower() or "shadow" in text.lower()
        assert "reddit" in text


# ──────────────────────────────────────────────────────────────────────
# Item 4: Single source of truth for source keys
# ──────────────────────────────────────────────────────────────────────

class TestSourceKeyRegistry:
    """COLLECTORS registry keys == VALID_SOURCE_KEYS == config valid keys."""

    def test_valid_source_keys_exported_from_base(self):
        from newsbot.collectors.base import VALID_SOURCE_KEYS
        assert isinstance(VALID_SOURCE_KEYS, frozenset)
        assert "hackernews" in VALID_SOURCE_KEYS
        assert "reddit" in VALID_SOURCE_KEYS
        assert "github" in VALID_SOURCE_KEYS
        assert "rss" in VALID_SOURCE_KEYS
        assert "huggingface_papers" in VALID_SOURCE_KEYS
        assert "trends" in VALID_SOURCE_KEYS

    def test_collectors_keys_match_valid_source_keys(self):
        """The COLLECTORS registry in main.py must have exactly the same keys
        as VALID_SOURCE_KEYS — no hand-sync drift."""
        from newsbot.main import COLLECTORS
        from newsbot.collectors.base import VALID_SOURCE_KEYS
        registry_keys = set(COLLECTORS.keys())
        assert registry_keys == set(VALID_SOURCE_KEYS), (
            f"COLLECTORS keys {sorted(registry_keys)} != "
            f"VALID_SOURCE_KEYS {sorted(VALID_SOURCE_KEYS)}"
        )

    def test_config_validation_uses_imported_keys(self):
        """config.py must not re-declare a local _VALID_SOURCE_KEYS set —
        it imports VALID_SOURCE_KEYS from collectors/base."""
        import newsbot.config as config_mod
        src = open(config_mod.__file__).read()
        assert "_VALID_SOURCE_KEYS = {" not in src, (
            "config.py still hand-declares _VALID_SOURCE_KEYS — "
            "import VALID_SOURCE_KEYS from collectors/base.py instead"
        )
        assert "VALID_SOURCE_KEYS" in src, (
            "config.py must import VALID_SOURCE_KEYS from collectors/base.py"
        )

    def test_known_sources_derived_from_valid_keys(self):
        """_KNOWN_SOURCES (Candidate source IDs) must be derived from
        VALID_SOURCE_KEYS, not hand-synced."""
        from newsbot.collectors.base import VALID_SOURCE_KEYS, _KNOWN_SOURCES
        # Every canonical config key is a known Candidate source.
        assert VALID_SOURCE_KEYS <= _KNOWN_SOURCES
        # Alias keys/targets are also known.
        assert "hn" in _KNOWN_SOURCES
        assert "hackernews" in _KNOWN_SOURCES

    def test_unknown_source_rejected_by_config_validation(self):
        """An unknown source key in config must still be rejected —
        the imported VALID_SOURCE_KEYS is used for the check."""
        from newsbot.config import _validate_config
        cfg = {
            "sources": {"twitter": {"limit": 10}},
            "source_weights": {"hn": 1.2}, "topic_boost": {},
            "lookback_hours": 48, "max_candidates": 20, "max_final_news": 8,
            "min_score": 35.0, "source_quota": 4, "item_prune_hours": 48,
            "llm_temperature": 0.4, "llm_max_tokens_filter": 8000,
            "llm_max_tokens_digest": 8000, "style_prompt": "",
        }
        with pytest.raises(ValueError, match="unknown source"):
            _validate_config(cfg)
