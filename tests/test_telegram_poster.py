"""Tests for newsbot/telegram_poster.py — chunking, retries, tag balance."""

from unittest.mock import AsyncMock, patch, MagicMock

import httpx
import pytest

from newsbot.telegram_poster import _split_for_telegram, post_digest, _balance_tags


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


def test_split_balances_open_tags():
    """When a chunk has an unclosed <b> tag, it should be closed and re-opened."""
    # Text where <b> opens but content exceeds limit before </b>
    text = "<b>" + "A" * 3000 + "</b>"
    chunks = _split_for_telegram(text, limit=1000)
    assert len(chunks) >= 2
    # Each chunk should have balanced <b> tags
    for chunk in chunks:
        opens = chunk.lower().count("<b>")
        closes = chunk.lower().count("</b>")
        assert opens == closes, f"Unbalanced <b> tag: opens={opens}, closes={closes}"


def test_balance_tags_closes_and_reopens():
    """_balance_tags should close open tags and provide a re-open prefix."""
    chunk = "<b>hello world"
    remaining = " more text"
    balanced, prefix = _balance_tags(chunk, remaining)
    assert balanced == "<b>hello world</b>"
    assert prefix == "<b>"


def test_balance_tags_handles_nested():
    """Nested tags should be closed in reverse order."""
    chunk = "<b><i>hello"
    remaining = " world"
    balanced, prefix = _balance_tags(chunk, remaining)
    assert "</i></b>" in balanced
    assert "<b><i>" in prefix


def test_balance_tags_no_change_when_balanced():
    """Already-balanced tags should return unchanged."""
    chunk = "<b>hello</b>"
    remaining = " world"
    balanced, prefix = _balance_tags(chunk, remaining)
    assert balanced == "<b>hello</b>"
    assert prefix == ""


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
async def test_post_digest_retries_on_500():
    """5xx should be retried (bounded transient retries)."""
    ok_resp = MagicMock()
    ok_resp.status_code = 200
    ok_resp.json.return_value = {"ok": True, "result": {"message_id": 1}}
    ok_resp.raise_for_status = MagicMock()

    server_err = MagicMock()
    server_err.status_code = 500
    server_err.text = "Internal Server Error"

    fake_client = AsyncMock()
    fake_client.post = AsyncMock(side_effect=[server_err, ok_resp])
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)

    with patch("newsbot.telegram_poster.httpx.AsyncClient", return_value=fake_client), \
         patch("newsbot.telegram_poster.asyncio.sleep", new=AsyncMock()):
        results = await post_digest("text", bot_token="t", chat_id="@c")

    assert fake_client.post.call_count == 2
    assert results[0]["ok"] is True


@pytest.mark.asyncio
async def test_post_digest_retries_on_timeout():
    """Transport timeout should be retried (bounded)."""
    ok_resp = MagicMock()
    ok_resp.status_code = 200
    ok_resp.json.return_value = {"ok": True, "result": {"message_id": 1}}
    ok_resp.raise_for_status = MagicMock()

    fake_client = AsyncMock()
    fake_client.post = AsyncMock(side_effect=[httpx.TimeoutException("timeout"), ok_resp])
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)

    with patch("newsbot.telegram_poster.httpx.AsyncClient", return_value=fake_client), \
         patch("newsbot.telegram_poster.asyncio.sleep", new=AsyncMock()):
        results = await post_digest("text", bot_token="t", chat_id="@c")

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
async def test_post_digest_500_exhausts_retries_and_raises():
    """5xx after all retries should raise PartialDeliveryError."""
    from newsbot.telegram_poster import PartialDeliveryError
    server_err = MagicMock()
    server_err.status_code = 500
    server_err.text = "Internal Server Error"

    fake_client = AsyncMock()
    fake_client.post = AsyncMock(return_value=server_err)
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)

    with patch("newsbot.telegram_poster.httpx.AsyncClient", return_value=fake_client), \
         patch("newsbot.telegram_poster.asyncio.sleep", new=AsyncMock()):
        with pytest.raises((PartialDeliveryError, RuntimeError)):
            await post_digest("text", bot_token="t", chat_id="@c")

    # Should have tried MAX_TRANSIENT_RETRIES + 1 times
    from newsbot.telegram_poster import MAX_TRANSIENT_RETRIES
    assert fake_client.post.call_count == MAX_TRANSIENT_RETRIES + 1


@pytest.mark.asyncio
async def test_post_digest_caps_retry_after():
    """retry_after from server should be capped at MAX_RETRY_AFTER."""
    from newsbot.telegram_poster import MAX_RETRY_AFTER

    ok_resp = MagicMock()
    ok_resp.status_code = 200
    ok_resp.json.return_value = {"ok": True, "result": {"message_id": 1}}
    ok_resp.raise_for_status = MagicMock()

    rate_limited = MagicMock()
    rate_limited.status_code = 429
    rate_limited.json.return_value = {"parameters": {"retry_after": 9999.0}}

    sleep_calls: list[float] = []

    async def mock_sleep(seconds):
        sleep_calls.append(seconds)

    fake_client = AsyncMock()
    fake_client.post = AsyncMock(side_effect=[rate_limited, ok_resp])
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)

    with patch("newsbot.telegram_poster.httpx.AsyncClient", return_value=fake_client), \
         patch("newsbot.telegram_poster.asyncio.sleep", new=mock_sleep):
        await post_digest("text", bot_token="t", chat_id="@c")

    # The sleep should have been capped
    assert len(sleep_calls) >= 1
    assert sleep_calls[0] <= MAX_RETRY_AFTER


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


def test_split_long_anchor_balanced():
    """Long <a> tags spanning multiple chunks must be balanced in every chunk."""
    # An anchor longer than one chunk.
    href = "http://example.com/" + "x" * 200
    text = f'<a href="{href}">{"A" * 3500}</a>'
    chunks = _split_for_telegram(text, limit=1000)
    assert len(chunks) >= 3
    for i, chunk in enumerate(chunks):
        opens = chunk.count("<a ")
        closes = chunk.count("</a>")
        assert opens == closes, f"Chunk {i}: unbalanced <a> -- opens={opens}, closes={closes}"


def test_split_nested_tags_balanced():
    """Nested tags (<b><i>...</i></b>) must be balanced in every chunk."""
    text = "<b><i>" + "A" * 3000 + "</i></b>"
    chunks = _split_for_telegram(text, limit=1000)
    assert len(chunks) >= 3
    for i, chunk in enumerate(chunks):
        b_opens = chunk.lower().count("<b>") + chunk.lower().count("<b ")
        b_closes = chunk.lower().count("</b>")
        i_opens = chunk.lower().count("<i>") + chunk.lower().count("<i ")
        i_closes = chunk.lower().count("</i>")
        assert b_opens == b_closes, f"Chunk {i}: unbalanced <b> -- opens={b_opens}, closes={b_closes}"
        assert i_opens == i_closes, f"Chunk {i}: unbalanced <i> -- opens={i_opens}, closes={i_closes}"


def test_strip_html_removes_tags_and_unescapes():
    """_strip_html should remove all tags and unescape entities."""
    from newsbot.telegram_poster import _strip_html
    assert _strip_html("<b>Title</b>") == "Title"
    assert _strip_html('<a href="http://x.com">Link</a>') == "Link"
    assert _strip_html("A &amp; B") == "A & B"
    assert _strip_html("&lt;script&gt;") == "<script>"
    assert _strip_html("plain text") == "plain text"
    assert _strip_html("") == ""


@pytest.mark.asyncio
async def test_post_digest_does_not_plain_text_retry_on_chat_not_found():
    """HTTP 400 'chat not found' must NOT be retried as plain text."""
    bad_resp = MagicMock()
    bad_resp.status_code = 400
    bad_resp.text = "Bad Request: chat not found"
    bad_resp.raise_for_status = MagicMock(side_effect=httpx.HTTPStatusError(
        "400", request=MagicMock(), response=bad_resp))

    fake_client = AsyncMock()
    fake_client.post = AsyncMock(return_value=bad_resp)
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)

    with patch("newsbot.telegram_poster.httpx.AsyncClient", return_value=fake_client):
        with pytest.raises(httpx.HTTPStatusError):
            await post_digest("<b>text</b>", bot_token="t", chat_id="@c")

    # Only one POST -- no plain-text retry for non-parse-error 400.
    assert fake_client.post.call_count == 1


def test_split_does_not_break_entity_at_boundary():
    """Entity-boundary test: _split_for_telegram must not split inside
    &amp; &lt; &gt; &#NNN; entities. A long text with &amp; at the split
    point should move the cut to before the &."""
    # Build text > 3000 chars with an &amp; entity near the split point.
    # effective_limit = 3000 - 100 (balance margin) = 2900.
    prefix = "A" * 2990
    entity = "&amp;"
    suffix = "B" * 100
    text = prefix + entity + suffix

    chunks = _split_for_telegram(text, limit=3000)
    assert len(chunks) >= 2

    # No chunk should contain a partial entity (like "&amp" without ";").
    for chunk in chunks:
        # A complete &amp; entity is fine; partial is &amp without semicolon.
        assert "&amp" not in chunk or "&amp;" in chunk, "Partial &amp entity without semicolon"
        assert "&lt" not in chunk or "&lt;" in chunk, "Partial &lt entity"
        assert "&gt" not in chunk or "&gt;" in chunk, "Partial &gt entity"


def test_split_entity_numeric_at_boundary():
    """Numeric entities like &#123; must not be split either."""
    prefix = "A" * 2990
    entity = "&#1234;"
    suffix = "B" * 100
    text = prefix + entity + suffix

    chunks = _split_for_telegram(text, limit=3000)
    assert len(chunks) >= 2

    for chunk in chunks:
        assert "&#12" not in chunk or "&#1234;" in chunk, "Partial numeric entity"


def test_split_chunk_size_with_balancing_overhead():
    """Final chunks must not exceed the limit after tag balancing."""
    # Build a long text with open tags that need balancing.
    text = "<b>" + "X" * 2900 + "</b>" + "\n\n" + "Y" * 200
    chunks = _split_for_telegram(text, limit=3000)

    for chunk in chunks:
        assert len(chunk) <= 3000, f"Chunk exceeds limit: {len(chunk)} > 3000"


def test_split_oversized_opening_tag_no_infinite_loop():
    """An opening tag longer than the chunk limit must not cause an infinite loop.

    _find_safe_cut() must skip past the oversized tag instead of returning 0.
    """
    # An <a href="..."> tag longer than the effective limit.
    long_url = "http://" + "x" * 3100
    text = f'<a href="{long_url}">{"A" * 100}</a>'
    chunks = _split_for_telegram(text, limit=500)
    # Must produce output, not loop forever.
    assert len(chunks) >= 1
    # No chunk should exceed the limit + small margin.
    for chunk in chunks:
        assert len(chunk) <= 520, f"Chunk exceeds limit: {len(chunk)}"


def test_split_first_tag_larger_than_limit():
    """Text starting with a tag larger than limit must still split."""
    # First 4000 chars are all inside an <a href> tag.
    text = '<a href="' + "x" * 4000 + '">link</a>' + "B" * 200
    chunks = _split_for_telegram(text, limit=3000)
    assert len(chunks) >= 2  # Must produce multiple chunks, not loop.


@pytest.mark.asyncio
async def test_partial_delivery_raises_typed_error_with_chunk_count():
    """When chunk 2 fails after chunk 1 succeeds, PartialDeliveryError must be raised
    with delivered_chunks=1."""
    from newsbot.telegram_poster import PartialDeliveryError

    # Build text that produces 2 chunks.
    text = "A" * 2000 + "\n\n" + "B" * 2000

    ok_resp = MagicMock()
    ok_resp.status_code = 200
    ok_resp.json.return_value = {"ok": True, "result": {"message_id": 1}}
    ok_resp.raise_for_status = MagicMock()

    fail_resp = MagicMock()
    fail_resp.status_code = 500
    fail_resp.text = "Internal Server Error"

    fake_client = AsyncMock()
    fake_client.post = AsyncMock(side_effect=[ok_resp, fail_resp, fail_resp, fail_resp])
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)

    with patch("newsbot.telegram_poster.httpx.AsyncClient", return_value=fake_client), \
         patch("newsbot.telegram_poster.asyncio.sleep", new=AsyncMock()):
        with pytest.raises(PartialDeliveryError) as exc_info:
            await post_digest(text, bot_token="t", chat_id="@c")

    assert exc_info.value.delivered_chunks == 1


@pytest.mark.asyncio
async def test_partial_delivery_transport_error_with_chunk_count():
    """When transport error on chunk 2 after chunk 1 succeeds, PartialDeliveryError raised."""
    from newsbot.telegram_poster import PartialDeliveryError

    text = "A" * 2000 + "\n\n" + "B" * 2000

    ok_resp = MagicMock()
    ok_resp.status_code = 200
    ok_resp.json.return_value = {"ok": True, "result": {"message_id": 1}}
    ok_resp.raise_for_status = MagicMock()

    fake_client = AsyncMock()
    # Chunk 1 succeeds, chunk 2 fails with transport error on all retry attempts.
    fake_client.post = AsyncMock(side_effect=[ok_resp, httpx.ConnectError("connection refused"),
                                               httpx.ConnectError("connection refused"),
                                               httpx.ConnectError("connection refused")])
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)

    with patch("newsbot.telegram_poster.httpx.AsyncClient", return_value=fake_client), \
         patch("newsbot.telegram_poster.asyncio.sleep", new=AsyncMock()):
        with pytest.raises(PartialDeliveryError) as exc_info:
            await post_digest(text, bot_token="t", chat_id="@c")

    assert exc_info.value.delivered_chunks == 1