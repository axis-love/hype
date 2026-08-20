"""Tests for newsbot/richmd.py — pure rich markdown renderers."""
from typing import Any

import pytest

from newsbot.richmd import (
    escape_rich_md,
    render_post,
    render_recap,
    _build_channel_link,
    _source_label,
    _safe_url,
)


# --- escape_rich_md ------------------------------------------------------

class TestEscapeRichMd:
    def test_escapes_all_special_chars(self):
        """Every special char gets a backslash prefix."""
        text = r"\*_~`[]|>#"
        escaped = escape_rich_md(text)
        # Every special char should be preceded by a backslash.
        # Backslash itself is escaped first: \ -> \\
        assert "\\\\" in escaped
        assert "\\*" in escaped
        assert "\\_" in escaped
        assert "\\~" in escaped
        assert "\\`" in escaped
        assert "\\[" in escaped
        assert "\\]" in escaped
        assert "\\|" in escaped
        assert "\\>" in escaped
        assert "\\#" in escaped

    def test_plain_text_unchanged(self):
        assert escape_rich_md("hello world") == "hello world"
        assert escape_rich_md("12345") == "12345"

    def test_backslash_escaped_first(self):
        """Backslash must be escaped first so it doesn't double-escape."""
        # \* should become \\*, not \\\*.
        escaped = escape_rich_md(r"\*")
        assert escaped == r"\\\*"

    def test_title_with_markdown_chars(self):
        """Title containing markdown chars renders safely after escaping."""
        title = "How *AI* is changing [everything] in _tech_"
        escaped = escape_rich_md(title)
        assert "*" not in escaped.replace("\\*", "")  # all * are escaped
        assert "[" not in escaped.replace("\\[", "")
        assert "_" not in escaped.replace("\\_", "")

    def test_url_like_text_escaped(self):
        """URLs in content segments get escaped (they're in link text, not targets)."""
        text = "https://example.com/path?x=1&y=2"
        escaped = escape_rich_md(text)
        # No special chars to escape in a typical URL, but should be safe.
        assert escaped == text


# --- _safe_url -----------------------------------------------------------

class TestSafeUrl:
    def test_parens_encoded(self):
        url = "https://en.wikipedia.org/wiki/Foo_(bar)"
        safe = _safe_url(url)
        assert "(" not in safe
        assert ")" not in safe
        assert "%28" in safe
        assert "%29" in safe

    def test_spaces_encoded(self):
        url = "https://example.com/path with spaces"
        safe = _safe_url(url)
        assert " " not in safe
        assert "%20" in safe

    def test_plain_url_unchanged(self):
        url = "https://example.com/path?x=1&y=2"
        assert _safe_url(url) == url


# --- _source_label -------------------------------------------------------

class TestSourceLabel:
    def test_extracts_domain(self):
        assert _source_label("https://blog.example.com/post") == "blog.example.com"

    def test_strips_www(self):
        assert _source_label("https://www.example.com/page") == "example.com"

    def test_no_hostname_returns_url(self):
        assert _source_label("not a url") == "not a url"


# --- _build_channel_link -------------------------------------------------

class TestBuildChannelLink:
    def test_numeric_channel(self):
        assert _build_channel_link("-1001234567890", 42) == "https://t.me/c/1234567890/42"

    def test_username_channel(self):
        assert _build_channel_link("@mychan", 7) == "https://t.me/mychan/7"

    def test_none_message_id(self):
        assert _build_channel_link("-1001234567890", None) is None

    def test_empty_chat_id(self):
        assert _build_channel_link("", 42) is None


# --- render_post ----------------------------------------------------------

class TestRenderPost:
    def test_basic_shape(self):
        md = render_post("Big Launch", "Company X released a new product.", "https://example.com/post")
        assert md.startswith("**Big Launch**")
        assert "Company X released a new product." in md
        assert "[Source: example.com](https://example.com/post)" in md

    def test_title_escaped(self):
        md = render_post("AI *is* great", "Body.", "https://x.io")
        assert "**AI \\*is\\* great**" in md

    def test_no_url(self):
        md = render_post("Title", "Body.", "")
        assert "Source:" not in md

    def test_no_title(self):
        md = render_post("", "Body.", "https://x.io")
        assert not md.startswith("**")

    def test_body_truncation_respects_budget(self):
        """Body exceeding the budget is truncated to fit RICH_MESSAGE_MAX_CHARS."""
        from newsbot.richmd import RICH_MESSAGE_MAX_CHARS
        long_body = "This is a sentence. " * (RICH_MESSAGE_MAX_CHARS // 10)
        md = render_post("T", long_body, "https://example.com/very/long/url")
        assert len(md) <= RICH_MESSAGE_MAX_CHARS
        # The body must have been truncated — the full input is much larger.
        assert len(md) < len(long_body)

    def test_short_body_unchanged(self):
        body = "Short body."
        md = render_post("T", body, "https://x.io")
        assert body in md

    def test_body_escaped(self):
        """Styler output is plain text — markdown specials in it are literal."""
        md = render_post("T", "a *b* _c_ [d] #e", "")
        assert "a \\*b\\* \\_c\\_ \\[d\\] \\#e" in md


# --- render_recap --------------------------------------------------------

class TestRenderRecap:
    def test_exact_shape_with_links(self):
        """Snapshot-style exact match for a simple recap."""
        items = [
            {"title": "Story A", "url": "https://a.example.com", "message_id": 10},
            {"title": "Story B", "url": "https://b.example.com", "message_id": 20},
        ]
        md = render_recap("Day Recap", items, chat_id="@chan")
        expected = (
            "**Day Recap**\n"
            "\n"
            "1. [Story A](https://t.me/chan/10) — [a.example.com](https://a.example.com)\n"
            "2. [Story B](https://t.me/chan/20) — [b.example.com](https://b.example.com)"
        )
        assert md == expected

    def test_legacy_item_plain_title(self):
        """Item without message_id renders plain title, still shows source link."""
        items = [
            {"title": "Old Story", "url": "https://old.example.com", "message_id": None},
        ]
        md = render_recap("Recap", items, chat_id="@chan")
        assert "1. Old Story — [old.example.com](https://old.example.com)" in md
        assert "t.me" not in md

    def test_title_escaped_in_recap(self):
        items = [{"title": "AI *rocks*", "url": "", "message_id": None}]
        md = render_recap("R", items, chat_id="")
        assert "\\*rocks\\*" in md

    def test_30_item_guard(self, caplog):
        """Items > 30 are cut at 30 with a warning."""
        items = [
            {"title": f"Story {i}", "url": "", "message_id": None}
            for i in range(35)
        ]
        md = render_recap("Recap", items, chat_id="")
        # Count numbered lines (1. through 30.)
        lines = [l for l in md.split("\n") if l and l[0].isdigit()]
        assert len(lines) == 30
        assert any("cutting" in r.getMessage() for r in caplog.records)

    def test_empty_items(self):
        md = render_recap("Empty Day", [], chat_id="@chan")
        assert md.strip() == "**Empty Day**"

    def test_numeric_chat_id(self):
        items = [{"title": "S", "url": "", "message_id": 5}]
        md = render_recap("R", items, chat_id="-1001234567890")
        assert "https://t.me/c/1234567890/5" in md

    def test_no_source_url_when_empty(self):
        items = [{"title": "No URL", "url": "", "message_id": 5}]
        md = render_recap("R", items, chat_id="@chan")
        assert "—" not in md
        assert "https://t.me/chan/5" in md

    def test_url_with_parens_safe(self):
        items = [{"title": "Wiki", "url": "https://en.wikipedia.org/wiki/Foo_(bar)", "message_id": 1}]
        md = render_recap("R", items, chat_id="@chan")
        assert "%28" in md
        assert "(" not in md.split("](")[1].split(")")[0]  # parens encoded in URL
