"""Tests for log sanitization — redact secrets from error messages (flow_001024)."""
import logging
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

from core.log_sanitizer import (
    redact_exception,
    redact_text,
    redact_url,
    safe_log_dict,
)


class TestRedactUrl:
    def test_telegram_bot_token_redacted(self):
        url = "https://api.telegram.org/bot123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxx/sendMessage"
        redacted = redact_url(url)
        assert "123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxx" not in redacted
        assert "***" in redacted
        assert "sendMessage" in redacted

    def test_no_token_in_url(self):
        url = "https://example.com/api/v1/chat"
        assert redact_url(url) == url

    def test_empty_url(self):
        assert redact_url("") == ""


class TestRedactText:
    def test_bot_token_redacted(self):
        text = "Failed to connect to https://api.telegram.org/bot123456:AAExxxxxxxxxxxxxxxxxxxx/getMe"
        redacted = redact_text(text)
        assert "AAExxxxxxxxxxxxxxxxxxxx" not in redacted
        assert "***" in redacted

    def test_bearer_token_redacted(self):
        text = "Authorization: Bearer sk-abcdef1234567890abcdef1234567890"
        redacted = redact_text(text)
        assert "sk-abcdef1234567890abcdef1234567890" not in redacted
        assert "***" in redacted

    def test_long_text_truncated(self):
        text = "x" * 500
        redacted = redact_text(text, max_length=100)
        assert len(redacted) <= 120  # 100 + truncation marker
        assert "truncated" in redacted

    def test_empty_text(self):
        assert redact_text("") == ""

    def test_prompt_content_not_specifically_redacted_but_truncated(self):
        # We can't detect all prompt content, but we truncate it.
        text = "Error: " + "A" * 1000
        redacted = redact_text(text, max_length=50)
        assert len(redacted) <= 70


class TestRedactException:
    def test_exception_with_bot_token(self):
        exc = Exception("Request to https://api.telegram.org/bot999:AAExxxxxxxxxxxxxxxxxxxx/sendMessage failed")
        redacted = redact_exception(exc)
        assert "AAExxxxxxxxxxxxxxxxxxxx" not in redacted
        assert "***" in redacted

    def test_long_exception_truncated(self):
        exc = Exception("x" * 1000)
        redacted = redact_exception(exc)
        assert len(redacted) <= 520  # 500 + truncation marker


class TestSafeLogDict:
    def test_sensitive_keys_redacted(self):
        data = {"bot_token": "123:ABC", "api_key": "sk-xxx", "url": "https://example.com", "name": "test"}
        safe = safe_log_dict(data)
        assert safe["bot_token"] == "***"
        assert safe["api_key"] == "***"
        assert safe["url"] == "https://example.com"
        assert safe["name"] == "test"

    def test_nested_bot_token_in_string(self):
        data = {"error": "Failed: https://api.telegram.org/bot123:AAExxxxxxxxxxxxxxxxxxxx/getMe"}
        safe = safe_log_dict(data)
        assert "AAExxxxxxxxxxxxxxxxxxxx" not in safe["error"]
        assert "***" in safe["error"]


class TestTelegramLoggerOutput:
    """Verify that captured log output does not contain bot tokens."""

    def test_log_error_does_not_contain_token(self, caplog):
        """When telegram_poster logs an error, the bot token must not appear."""
        # Simulate what the poster does:
        token = "123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxx"
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        # This is what redact_text does before logging:
        safe_body = redact_text(f"Error connecting to {url}", max_length=200)
        log = logging.getLogger("test")
        with caplog.at_level(logging.ERROR):
            log.error("Telegram send failed: status=400 body=%s", safe_body)

        # Check no captured log record contains the token.
        for record in caplog.records:
            assert token not in record.getMessage(), f"Bot token leaked in log: {record.getMessage()}"


class TestCoordinatorRedaction:
    """Verify that JobCoordinator error logs do not leak bot tokens (flow_001024 round 1)."""

    @pytest.fixture
    def store(self, tmp_path: Path):
        from newsbot.db import NewsStore
        return NewsStore(tmp_path / "test.sqlite")

    @pytest.fixture
    def settings(self):
        class MockSettings:
            def __init__(self):
                self._data: dict[str, dict[str, object]] = {}
            def get(self, section, key, default=None):
                return self._data.get(section, {}).get(key, default)
            def set(self, section, key, value):
                self._data.setdefault(section, {})[key] = value
        return MockSettings()

    @pytest.mark.asyncio
    async def test_post_one_redacts_bot_token(self, store, settings, caplog):
        """When post_digest raises with bot token in URL, coordinator must redact it."""
        from newsbot.jobs import JobCoordinator

        coordinator = JobCoordinator(store, settings)
        store.add_pending_post({"title": "T", "body": "B", "url": "http://x.com"})

        token = "123456789:AAExxxxxxxxxxxxxxxxxxxx"

        async def fake_post(*args, **kwargs):
            raise Exception(f"Request to https://api.telegram.org/bot{token}/sendMessage failed")

        with patch.dict("os.environ", {"BOT_TOKEN": token, "NEWS_CHANNEL_ID": "@test"}):
            with patch("newsbot.jobs.post_digest", side_effect=fake_post):
                with caplog.at_level(logging.ERROR):
                    result = await coordinator.run_posting()

        assert result == 1
        # Bot token must NOT appear in any log record
        for record in caplog.records:
            assert token not in record.getMessage(), f"Bot token leaked in log: {record.getMessage()}"
            assert "AAExxxxxxxxxxxxxxxxxxxx" not in record.getMessage()


class TestLLMErrorMessages:
    """Verify that LLM error messages don't contain response bodies."""

    def test_400_error_no_body_in_message(self):
        """The LLM 400 error message should NOT include response body."""
        from lm_client import LMClient

        client = LMClient("http://localhost:11434", "test-model", 30.0)
        # Simulate a 400 response with prompt content in the body.
        body = '{"error": "Invalid request", "echo": "This is the prompt text that should not appear"}'
        is_transient, error_class, message = client._classify_http_error(400, body)

        # The message must not contain the response body.
        assert "prompt text" not in message
        assert "echo" not in message
        assert "HTTP 400" in message

    def test_400_transient_no_body_in_message(self):
        """The transient 400 error message should NOT include response body."""
        from lm_client import LMClient

        client = LMClient("http://localhost:11434", "test-model", 30.0)
        body = '{"error": "Server is overloaded", "request_body": "sensitive prompt content"}'
        is_transient, error_class, message = client._classify_http_error(400, body)

        assert is_transient is True
        assert "sensitive prompt content" not in message
        assert "overloaded" not in message  # The marker itself shouldn't appear
        assert "HTTP 400" in message