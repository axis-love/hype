"""Tests for newsbot/telegram_poster.py — chunking and 429 retry."""

from unittest.mock import AsyncMock, patch, MagicMock

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