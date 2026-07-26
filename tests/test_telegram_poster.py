"""Tests for newsbot/telegram_poster.py — chunking and 429 retry."""

from unittest.mock import AsyncMock, patch, MagicMock

import httpx
import pytest

from newsbot.telegram_poster import _split_for_telegram, post_digest


def test_split_keeps_short_text_as_one_chunk():
    assert _split_for_telegram("short text") == ["short text"]


def test_split_on_blank_line_boundary():
    body = "para1\n\npara2\n\npara3"
    long = body * 500  # force > 3000 chars
    chunks = _split_for_telegram(long, limit=3000)
    assert len(chunks) >= 2
    assert all(len(c) <= 3000 for c in chunks)
    # No chunk should start with a leftover newline from the split.
    assert all(not c.startswith("\n") for c in chunks)


@pytest.mark.asyncio
async def test_post_digest_retries_on_429():
    ok_resp = MagicMock()
    ok_resp.status_code = 200
    ok_resp.json.return_value = {"ok": True, "result": {"message_id": 1}}
    ok_resp.raise_for_status = MagicMock()

    rate_limited = MagicMock()
    rate_limited.status_code = 429
    rate_limited.json.return_value = {"parameters": {"retry_after": 0}}

    fake_client = AsyncMock()
    fake_client.post = AsyncMock(side_effect=[rate_limited, ok_resp])
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)

    with patch("newsbot.telegram_poster.httpx.AsyncClient", return_value=fake_client), \
         patch("newsbot.telegram_poster.asyncio.sleep", new=AsyncMock()):
        results = await post_digest("digest", bot_token="t", chat_id="@c")

    assert fake_client.post.call_count == 2
    assert results[0]["ok"] is True


@pytest.mark.asyncio
async def test_post_digest_retries_chunk_as_plain_text_on_markdown_error():
    # First send (Markdown) -> 400 (parse error). Second send (plain) -> 200.
    bad_resp = MagicMock()
    bad_resp.status_code = 400
    bad_resp.text = "Bad Request: can't parse entities"
    bad_resp.raise_for_status = MagicMock()

    ok_resp = MagicMock()
    ok_resp.status_code = 200
    ok_resp.json.return_value = {"ok": True, "result": {"message_id": 1}}
    ok_resp.raise_for_status = MagicMock()

    fake_client = AsyncMock()
    fake_client.post = AsyncMock(side_effect=[bad_resp, ok_resp])
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)

    with patch("newsbot.telegram_poster.httpx.AsyncClient", return_value=fake_client):
        results = await post_digest("*bad markdown [", bot_token="t", chat_id="@c")

    assert fake_client.post.call_count == 2
    # Second call should have empty parse_mode (plain text).
    second_call = fake_client.post.call_args_list[1]
    assert second_call.kwargs["json"]["parse_mode"] == ""


@pytest.mark.asyncio
async def test_post_digest_raises_on_missing_token():
    with pytest.raises(ValueError):
        await post_digest("x", bot_token="", chat_id="@c")
    with pytest.raises(ValueError):
        await post_digest("x", bot_token="t", chat_id="")


@pytest.mark.asyncio
async def test_post_digest_does_not_retry_on_auth_failure():
    """401/403 should NOT be retried as plain text."""
    auth_fail = MagicMock()
    auth_fail.status_code = 401
    auth_fail.text = "Unauthorized"
    auth_fail.raise_for_status = MagicMock(side_effect=httpx.HTTPStatusError(
        "401", request=MagicMock(), response=auth_fail))

    fake_client = AsyncMock()
    fake_client.post = AsyncMock(return_value=auth_fail)
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)

    with patch("newsbot.telegram_poster.httpx.AsyncClient", return_value=fake_client):
        with pytest.raises(httpx.HTTPStatusError):
            await post_digest("text", bot_token="t", chat_id="@c")

    # Only one POST — no plain-text retry.
    assert fake_client.post.call_count == 1


@pytest.mark.asyncio
async def test_post_digest_does_not_retry_on_server_error():
    """500 should NOT be retried as plain text."""
    server_err = MagicMock()
    server_err.status_code = 500
    server_err.text = "Internal Server Error"
    server_err.raise_for_status = MagicMock(side_effect=httpx.HTTPStatusError(
        "500", request=MagicMock(), response=server_err))

    fake_client = AsyncMock()
    fake_client.post = AsyncMock(return_value=server_err)
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)

    with patch("newsbot.telegram_poster.httpx.AsyncClient", return_value=fake_client):
        with pytest.raises(httpx.HTTPStatusError):
            await post_digest("text", bot_token="t", chat_id="@c")

    assert fake_client.post.call_count == 1


def test_split_does_not_break_html_tag():
    """Splitting should not break inside an HTML tag."""
    # Build text with a long link tag that spans past the split point.
    before = "A" * 2950
    tag = f'<a href="{"http://x.com/" + "b" * 100}">link</a>'
    text = before + tag + "C" * 200
    chunks = _split_for_telegram(text, limit=3000)
    # Each chunk should have balanced tags (no split inside <a ...>)
    for chunk in chunks:
        # Count opening and closing tags
        opens = chunk.count("<a ")
        closes = chunk.count("</a>")
        # Either both 0 or both 1
        assert opens == closes, f"Unbalanced <a> tag in chunk: opens={opens}, closes={closes}"