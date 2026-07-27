"""Log sanitization utilities — redact secrets from error messages.

Provides utilities to strip bot tokens from URLs and sanitize exception
messages before they enter log output. Logs should preserve actionable
status and request context without including bot tokens, full request
URLs, prompts, article bodies, or raw upstream response bodies.
"""
from __future__ import annotations

import os
import re
from typing import Any

# Pattern for Telegram bot token URLs: https://api.telegram.org/bot<TOKEN>/...
_TELEGRAM_TOKEN_RE = re.compile(r"(api\.telegram\.org/bot)([A-Za-z0-9:_-]+)", re.IGNORECASE)

# Generic bot token pattern (numbers:alphanumerics, typical Telegram format)
_BOT_TOKEN_RE = re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b")

# Bearer token pattern
_BEARER_RE = re.compile(r"(Bearer\s+)([A-Za-z0-9_.\-]+)", re.IGNORECASE)

# API key patterns
_API_KEY_RE = re.compile(r"(api[_-]?key[\"'\s:=]+)([A-Za-z0-9_\-]{20,})", re.IGNORECASE)

# Env vars whose exact values should be redacted regardless of format.
_SECRET_ENV_VARS = ("BOT_TOKEN", "LM_API_KEY", "GITHUB_TOKEN", "NEWS_CHANNEL_ID")


def _get_configured_secrets() -> list[str]:
    """Collect non-empty secret values from the environment.

    These are redacted by exact string replacement so arbitrary
    high-entropy keys without recognised prefixes are still caught.
    """
    secrets: list[str] = []
    for var in _SECRET_ENV_VARS:
        val = os.environ.get(var, "").strip()
        if val and len(val) >= 8:
            secrets.append(val)
    return secrets


def _redact_configured(text: str) -> str:
    """Replace exact occurrences of configured secret values with ***."""
    for secret in _get_configured_secrets():
        if secret in text:
            text = text.replace(secret, "***")
    return text


def redact_url(url: str) -> str:
    """Remove secrets from a URL, replacing them with ***.

    Handles:
    - Telegram bot token URLs: api.telegram.org/bot<TOKEN>/method → bot***/method
    - Bearer tokens in URL params
    """
    if not url:
        return url

    # Redact Telegram bot token in URL path.
    result = _TELEGRAM_TOKEN_RE.sub(r"\1***", url)
    # Also catch bare bot tokens that might appear in error messages.
    result = _BOT_TOKEN_RE.sub("***", result)
    return result


def redact_text(text: str, *, max_length: int = 200) -> str:
    """Sanitize arbitrary text from error responses.

    - Removes bot tokens and bearer tokens
    - Redacts configured env secrets (BOT_TOKEN, LM_API_KEY, etc.) by value
    - Truncates to max_length to prevent log flooding with response bodies
    """
    if not text:
        return ""

    result = text
    # Redact configured secret values (exact match, regardless of format).
    result = _redact_configured(result)
    # Redact Telegram bot tokens.
    result = _TELEGRAM_TOKEN_RE.sub(r"\1***", result)
    result = _BOT_TOKEN_RE.sub("***", result)
    # Redact bearer tokens.
    result = _BEARER_RE.sub(r"\1***", result)
    # Redact API key patterns.
    result = _API_KEY_RE.sub(r"\1***", result)

    if len(result) > max_length:
        result = result[:max_length] + "...[truncated]"
    return result


def redact_exception(exc: BaseException) -> str:
    """Convert an exception to a sanitized string for logging.

    Removes bot tokens, URLs with embedded secrets, and truncates
    long messages that might contain prompts or response bodies.
    """
    msg = str(exc)
    return redact_text(msg, max_length=500)


def safe_log_dict(data: dict[str, Any], *, sensitive_keys: set[str] | None = None) -> dict[str, Any]:
    """Return a copy of a dict with sensitive values redacted.

    Default sensitive keys: token, api_key, secret, password, authorization.
    Recursively redacts nested dicts and lists.
    """
    if sensitive_keys is None:
        sensitive_keys = {"token", "api_key", "secret", "password", "authorization",
                          "bot_token", "key", "bearer"}

    safe: dict[str, Any] = {}
    for k, v in data.items():
        if k.lower() in sensitive_keys:
            safe[k] = "***"
        elif isinstance(v, dict):
            safe[k] = safe_log_dict(v, sensitive_keys=sensitive_keys)
        elif isinstance(v, list):
            safe[k] = [
                safe_log_dict(item, sensitive_keys=sensitive_keys)
                if isinstance(item, dict)
                else redact_text(item, max_length=200)
                if isinstance(item, str)
                else item
                for item in v
            ]
        elif isinstance(v, str):
            safe[k] = redact_text(v, max_length=200)
        else:
            safe[k] = v
    return safe