"""Tests for newsbot/richmd.py — pure rich markdown renderers.

Layout revision 2026-08-21: headings used deliberately. Posts render as
the article layout (H1 title + divider + body + collapsible Source +
optional signature); recaps render as the index layout (H1 title +
divider + H4-linked-title bullets + divider + optional signature).
"""
from typing import Any

import pytest

from newsbot.richmd import (
    escape_rich_md,
    render_post,
    render_post_blocks,
    render_recap,
    signature_for,
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


# --- signature_for --------------------------------------------------------

class TestSignatureFor:
    def test_username_channel_returns_handle(self):
        assert signature_for("@cyb3rcr34m") == "@cyb3rcr34m"

    def test_numeric_chat_id_empty(self):
        assert signature_for("-1001234567890") == ""

    def test_empty(self):
        assert signature_for("") == ""

    def test_strips_whitespace(self):
        assert signature_for("  @chan  ") == "@chan"


# --- render_post (article layout) -----------------------------------------

class TestRenderPost:
    def test_basic_shape(self):
        """Exact article layout: H1, divider, body, collapsible source. No top margin."""
        md = render_post("Big Launch", "Company X released a new product.", "https://example.com/post")
        expected = (
            "# Big Launch\n"
            "\n"
            "---\n"
            "\n"
            "Company X released a new product.\n"
            "\n"
            "<details><summary>Source</summary>\n"
            "\n"
            "[example.com](https://example.com/post)\n"
            "</details>"
        )
        assert md == expected

    def test_title_escaped(self):
        md = render_post("AI *is* great", "Body.", "https://x.io")
        assert md.startswith("# AI \\*is\\* great\n")

    def test_title_with_hash_escaped(self):
        """'#' in a heading title must be escaped so it doesn't nest."""
        md = render_post("C# 13 ships", "Body.", "")
        assert "# C\\# 13 ships" in md

    def test_source_link_has_no_source_prefix(self):
        """Link text is the bare domain — collapsible summary already says Source."""
        md = render_post("T", "Body.", "https://blog.example.com/post")
        assert "[blog.example.com](https://blog.example.com/post)" in md
        assert "Source: " not in md

    def test_no_top_margin(self):
        """Message starts directly with the H1 — margin attempts rejected (2026-08-21)."""
        md = render_post("T", "Body.", "")
        assert md.startswith("# ")

    def test_no_url_omits_details_block(self):
        md = render_post("Title", "Body.", "")
        assert "<details>" not in md
        assert "Source:" not in md

    def test_signature_appended(self):
        md = render_post("T", "Body.", "https://x.io", signature="@cyb3rcr34m")
        assert md.endswith("</details>\n\n@cyb3rcr34m")

    def test_empty_signature_no_trailing_blank(self):
        md = render_post("T", "Body.", "https://x.io", signature="")
        assert not md.endswith("\n")

    def test_signature_without_url(self):
        md = render_post("T", "Body.", "", signature="@chan")
        assert md.endswith("Body.\n\n@chan")

    def test_no_title(self):
        """Empty title: body first, no heading, no divider."""
        md = render_post("", "Body.", "https://x.io")
        assert md.startswith("Body.")
        assert "---" not in md.split("Body.")[0]

    def test_body_truncation_respects_budget(self):
        """Body exceeding the budget is truncated to fit RICH_MESSAGE_MAX_CHARS."""
        from newsbot.richmd import RICH_MESSAGE_MAX_CHARS
        long_body = "This is a sentence. " * (RICH_MESSAGE_MAX_CHARS // 10)
        md = render_post("T", long_body, "https://example.com/very/long/url", signature="@chan")
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

    def test_url_with_parens_in_source_link(self):
        md = render_post("T", "Body.", "https://en.wikipedia.org/wiki/Foo_(bar)")
        assert "%28" in md
        assert "%29" in md


# --- render_recap (index layout) -------------------------------------------

class TestRenderRecap:
    def test_exact_shape_with_links_and_signature(self):
        """Snapshot-style exact match: H1, divider, H4 linked bullets, divider, signature."""
        items = [
            {"title": "Story A", "url": "https://a.example.com", "message_id": 10},
            {"title": "Story B", "url": "https://b.example.com", "message_id": 20},
        ]
        md = render_recap("Day Recap", items, chat_id="@chan", signature="@chan")
        expected = (
            "# Day Recap\n"
            "\n"
            "---\n"
            "\n"
            "- #### [Story A](https://t.me/chan/10)\n"
            "- #### [Story B](https://t.me/chan/20)\n"
            "\n"
            "---\n"
            "\n"
            "@chan"
        )
        assert md == expected

    def test_no_signature_omits_trailing_divider(self):
        items = [{"title": "S", "url": "", "message_id": 5}]
        md = render_recap("R", items, chat_id="@chan")
        assert md.endswith("- #### [S](https://t.me/chan/5)")

    def test_no_top_margin(self):
        """Recap starts directly with the H1 — margin attempts rejected (2026-08-21)."""
        md = render_recap("R", [], chat_id="")
        assert md.startswith("# ")

    def test_legacy_item_unlinked_h4_bullet(self):
        """Item without message_id renders as unlinked H4 bullet, no crash."""
        items = [
            {"title": "Old Story", "url": "https://old.example.com", "message_id": None},
        ]
        md = render_recap("Recap", items, chat_id="@chan")
        assert "- #### Old Story" in md
        assert "t.me" not in md

    def test_no_per_item_source_segments(self):
        """Approved index shape drops the ' — [domain](url)' segments."""
        items = [{"title": "S", "url": "https://a.example.com", "message_id": 5}]
        md = render_recap("R", items, chat_id="@chan")
        assert "a.example.com" not in md
        assert " — " not in md

    def test_recap_title_escaped(self):
        items = [{"title": "AI *rocks*", "url": "", "message_id": None}]
        md = render_recap("R", items, chat_id="")
        assert "# R" in md
        assert "\\*rocks\\*" in md

    def test_30_item_guard(self, caplog):
        """Items > 30 are cut at 30 with a warning."""
        items = [
            {"title": f"Story {i}", "url": "", "message_id": None}
            for i in range(35)
        ]
        md = render_recap("Recap", items, chat_id="")
        # Count H4 bullet lines.
        lines = [l for l in md.split("\n") if l.startswith("- #### ")]
        assert len(lines) == 30
        assert any("cutting" in r.getMessage() for r in caplog.records)

    def test_empty_items(self):
        md = render_recap("Empty Day", [], chat_id="@chan")
        assert md.strip() == "# Empty Day\n\n---"

    def test_numeric_chat_id_link(self):
        items = [{"title": "S", "url": "", "message_id": 5}]
        md = render_recap("R", items, chat_id="-1001234567890")
        assert "https://t.me/c/1234567890/5" in md


# --- render_post_blocks ------------------------------------------------------
#
# Blocks layout (Bot API 10.2) — media leads the post. Photos are wrapped
# in ONE slideshow block (verified live 2026-08-21: separate photo blocks
# do NOT group; slideshow/collage do; Anton picked slideshow). Videos are
# separate blocks after the slideshow. Then H1 title, divider, body
# paragraph, collapsible Source, footer signature.

class TestRenderPostBlocks:
    PHOTOS = [
        {"type": "photo", "media": "https://cdn.example.com/a.png"},
        {"type": "photo", "media": "https://cdn.example.com/b.png"},
    ]
    VIDEO = {"type": "video", "media": "https://cdn.example.com/clip.mp4"}

    def test_no_media_text_only(self):
        """Without media the blocks equal the text-only article layout."""
        blocks = render_post_blocks("Title", "Body.", "https://ex.com/a",
                                    signature="@chan")
        types = [b["type"] for b in blocks]
        assert types == ["heading", "divider", "paragraph", "details", "footer"]
        assert blocks[0]["text"] == "Title"
        assert blocks[0]["size"] == 1
        assert blocks[2]["text"] == "Body."
        assert blocks[4]["text"] == "@chan"

    def test_multiple_photos_grouped_in_slideshow_first(self):
        blocks = render_post_blocks("T", "B", "", media=self.PHOTOS)
        assert blocks[0]["type"] == "slideshow"
        inner = blocks[0]["blocks"]
        assert len(inner) == 2
        assert all(b["type"] == "photo" for b in inner)
        assert inner[0]["photo"]["media"] == "https://cdn.example.com/a.png"

    def test_single_photo_no_wrapper(self):
        blocks = render_post_blocks("T", "B", "", media=[self.PHOTOS[0]])
        assert blocks[0]["type"] == "photo"

    def test_video_separate_block_after_slideshow(self):
        blocks = render_post_blocks("T", "B", "",
                                    media=[*self.PHOTOS, self.VIDEO])
        # Video block first in array build order... then slideshow inserted
        # at position 0 — so slideshow leads, video follows.
        assert blocks[0]["type"] == "slideshow"
        assert blocks[1]["type"] == "video"
        assert blocks[1]["video"]["media"] == "https://cdn.example.com/clip.mp4"
        assert blocks[1]["video"]["supports_streaming"] is True

    def test_media_before_title(self):
        """The whole point: media blocks come before the heading."""
        blocks = render_post_blocks("T", "B", "https://ex.com/a",
                                    media=self.PHOTOS)
        types = [b["type"] for b in blocks]
        assert types.index("slideshow") < types.index("heading")

    def test_source_details_with_url_text(self):
        blocks = render_post_blocks("T", "B", "https://www.ex.com/story")
        details = next(b for b in blocks if b["type"] == "details")
        assert details["summary"] == "Source"
        url_text = details["blocks"][0]["text"]
        assert url_text["type"] == "url"
        assert url_text["text"] == "ex.com"  # bare domain label
        assert url_text["url"] == "https://www.ex.com/story"

    def test_no_url_no_details(self):
        blocks = render_post_blocks("T", "B", "")
        assert all(b["type"] != "details" for b in blocks)

    def test_no_signature_no_footer(self):
        blocks = render_post_blocks("T", "B", "", signature="")
        assert all(b["type"] != "footer" for b in blocks)

    def test_body_truncated_at_sentence_boundary(self):
        long_body = ("Sentence one. " * 4000)  # ~56k chars, over budget
        blocks = render_post_blocks("T", long_body, "")
        para = next(b for b in blocks if b["type"] == "paragraph")
        assert len(para["text"]) <= 32736
        assert para["text"].endswith(".")

    def test_block_text_not_markdown_escaped(self):
        """Block text entities are plain RichText — no escaping needed."""
        blocks = render_post_blocks("A *bold* #title", "Body with _stars_", "")
        assert blocks[0]["text"] == "A *bold* #title"
        para = next(b for b in blocks if b["type"] == "paragraph")
        assert para["text"] == "Body with _stars_"

    def test_unknown_media_type_skipped(self):
        blocks = render_post_blocks("T", "B", "",
                                    media=[{"type": "audio", "media": "x.mp3"},
                                           self.PHOTOS[0]])
        types = [b["type"] for b in blocks]
        assert "audio" not in types
        assert "photo" in types
