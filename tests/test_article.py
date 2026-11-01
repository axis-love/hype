"""Article source text: fetch, extract, persist, and feed both LLM passes.

Posts used to describe an item's votes instead of the story because no
step ever read the linked article (flow_001710 data: 86% of HN items had
no text at all). These tests pin the content path end to end.
"""
from __future__ import annotations

from typing import Any

import httpx
import pytest

import newsbot.article as article
from newsbot.article import (
    MIN_ARTICLE_CHARS, article_url, attach_extracted_text, best_text, extract_text,
)
from newsbot.article import fetch_article_text as REAL_FETCH  # conftest stubs the module attr
from newsbot.db import NewsStore
from newsbot.poster import _row_to_styler_input
from newsbot.summarizer import _format_candidate, llm_style_posts
from tests.helpers import scored_story

PARA = "The researchers measured a 43 percent conversion efficiency in a tandem cell built from perovskite and silicon layers."

ARTICLE_HTML = f"""
<html><head><meta property="og:description" content="Meta description fallback."></head>
<body>
  <nav><p>Home News Reviews Subscribe to our newsletter today please</p></nav>
  <script>var tracking = "this is not article text at all, it is a script";</script>
  <article>
    <h1>Solar cells pass the 33 percent ceiling in a new lab result</h1>
    <p>{PARA}</p>
    <p>{PARA.replace("43", "41")}</p>
    <p>{PARA}</p>
    <p>{PARA.replace("researchers", "authors")}</p>
    <p>Short.</p>
  </article>
  <footer><p>Copyright 2026 Example Media, all rights reserved worldwide.</p></footer>
</body></html>
"""


class TestExtractText:
    def test_keeps_article_paragraphs_and_drops_chrome(self) -> None:
        text = extract_text(ARTICLE_HTML)
        assert "43 percent conversion efficiency" in text
        assert "41 percent" in text
        assert "newsletter" not in text
        assert "tracking" not in text
        assert "Copyright" not in text
        assert "Short." not in text  # below MIN_BLOCK_CHARS

    def test_dedupes_repeated_paragraphs(self) -> None:
        assert extract_text(ARTICLE_HTML).count(PARA) == 1

    def test_falls_back_to_page_blocks_then_meta_description(self) -> None:
        no_article = f"<html><body><div><p>{PARA}</p></div></body></html>"
        assert PARA in extract_text(no_article)
        only_meta = '<html><head><meta name="description" content="Just the meta."></head><body></body></html>'
        assert extract_text(only_meta) == "Just the meta."

    def test_malformed_html_never_raises(self) -> None:
        assert isinstance(extract_text("<p>unclosed <b>tags <article"), str)


class TestArticleUrl:
    def test_reddit_link_post_reads_the_external_article(self) -> None:
        c = {"source": "reddit", "url": "https://www.reddit.com/r/x/comments/1/a/",
             "raw_json": {"is_self": False, "external_url": "https://www.ign.com/articles/zelda"}}
        assert article_url(c) == "https://www.ign.com/articles/zelda"

    def test_reddit_self_post_has_no_article(self) -> None:
        c = {"source": "reddit", "url": "https://www.reddit.com/r/x/comments/1/a/",
             "raw_json": {"is_self": True, "external_url": "https://www.reddit.com/r/x/comments/1/a/"}}
        assert article_url(c) is None

    @pytest.mark.parametrize("url", [
        "https://i.redd.it/0dbnx20a5myh1.jpeg",
        "https://www.reddit.com/gallery/1xjwtrv",
        "https://twitter.com/someone/status/1",
        "https://x.com/someone/status/1",
        "https://www.youtube.com/watch?v=abc",
        "https://news.ycombinator.com/item?id=1",
        "https://example.com/paper.pdf",
    ])
    def test_media_social_and_discussion_urls_have_no_article(self, url: str) -> None:
        assert article_url({"source": "hn", "url": url}) is None

    def test_plain_article_url(self) -> None:
        assert article_url({"source": "hn", "url": "https://nolanlawson.com/post/"}) == "https://nolanlawson.com/post/"


class TestBestText:
    def test_real_article_wins(self) -> None:
        fetched = "x" * MIN_ARTICLE_CHARS
        assert best_text(fetched, "y" * 5000) == fetched

    def test_thin_fetch_loses_to_longer_raw_text(self) -> None:
        assert best_text("short", "<p>Hey r/gamedev! Four days ago I launched my game.</p>") == (
            "Hey r/gamedev! Four days ago I launched my game."
        )

    def test_nothing_at_all(self) -> None:
        assert best_text("", None) == ""


@pytest.mark.asyncio
async def test_attach_fetches_articles_and_keeps_raw_text_otherwise(monkeypatch) -> None:
    monkeypatch.setattr(article, "fetch_article_text", REAL_FETCH)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "example.com":
            return httpx.Response(200, html=ARTICLE_HTML)
        return httpx.Response(404)

    cands: list[dict[str, Any]] = [
        {"source": "hn", "url": "https://example.com/solar", "raw_text": None},
        {"source": "reddit", "url": "https://www.reddit.com/r/gamedev/comments/1/x/",
         "raw_json": {"is_self": True}, "raw_text": "Self post text about launching a game."},
        {"source": "hn", "url": "https://gone.example.org/404", "raw_text": None},
    ]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await attach_extracted_text(cands, client)

    assert "43 percent conversion efficiency" in cands[0]["extracted_text"]
    assert cands[1]["extracted_text"] == "Self post text about launching a game."
    assert cands[2]["extracted_text"] is None


@pytest.mark.asyncio
async def test_non_html_response_yields_no_text() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"%PDF-1.7", headers={"content-type": "application/pdf"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await REAL_FETCH("https://example.com/doc", client) == ""


class TestStore:
    def test_insert_persists_extracted_text(self, tmp_path) -> None:
        store = NewsStore(tmp_path / "t.sqlite")
        story = {**scored_story("Solar ceiling", 100), "extracted_text": "Full article text."}
        store.add_stories_to_store([story], [])
        row = store.list_store_rows("telegram")[0]
        assert row["extracted_text"] == "Full article text."
        store.close()

    def test_merge_takes_richer_text_with_its_summary(self, tmp_path) -> None:
        store = NewsStore(tmp_path / "t.sqlite")
        thin = {**scored_story("Leak", 100), "short_summary": "Thin summary.", "extracted_text": "img"}
        store.add_stories_to_store([thin], [])
        row_id = store.list_store_rows("telegram")[0]["id"]

        rich = {**scored_story("Leak", 100), "short_summary": "Rich summary.",
                "extracted_text": "The full article about the leak, with details."}
        store.merge_into_store_row(row_id, rich, ["https://example.com/rich"])
        row = store.list_store_rows("telegram")[0]
        assert row["extracted_text"] == "The full article about the leak, with details."
        assert row["summary"] == "Rich summary."

        poorer = {**scored_story("Leak", 100), "short_summary": "Other.", "extracted_text": "short"}
        store.merge_into_store_row(row_id, poorer, ["https://example.com/poor"])
        row = store.list_store_rows("telegram")[0]
        assert row["extracted_text"] == "The full article about the leak, with details."
        assert row["summary"] == "Rich summary."
        store.close()


class _CapturingLM:
    model = "fake"

    def __init__(self) -> None:
        self.system = ""
        self.user = ""

    async def generate(self, messages, **params):
        self.system = messages[0]["content"]
        self.user = messages[-1]["content"]
        return ('{"posts":[{"id":"s007","title":"T","body":"B"}]}', "stop")


@pytest.mark.asyncio
async def test_writer_gets_source_text_and_no_engagement() -> None:
    shaped = _row_to_styler_input({
        "id": 7, "title": "Scientists crack the 33% ceiling", "url": "https://example.com/x",
        "summary": "Researchers report 43% efficiency.", "extracted_text": PARA,
        "published_at": "2026-10-30T17:26:50+00:00",
        "upvotes": 7121, "comments": 172, "stars": 0, "crosspost_count": 1,
    })
    lm = _CapturingLM()
    await llm_style_posts([shaped], lm, style_prompt="style")
    assert f"Source text: {PARA}" in lm.user
    for leak in ("7121", "7,121", "172", "upvote", "Signal"):
        assert leak not in lm.user
    assert "Never mention upvotes" in lm.system
    assert "Never exceed 1000 characters" in lm.system
    assert "entirely in the language the style instructions ask" in lm.system
    assert '{"posts":[{"id"' in lm.system  # JSON contract renders with single braces


@pytest.mark.asyncio
async def test_writer_marks_missing_source_text() -> None:
    shaped = _row_to_styler_input({"id": 7, "title": "Leaked screenshot", "url": "https://i.redd.it/a.jpg"})
    lm = _CapturingLM()
    await llm_style_posts([shaped], lm, style_prompt="style")
    assert "Source text: (none)" in lm.user


def test_filter_line_shows_text_instead_of_snippet() -> None:
    line = _format_candidate({"candidate_id": "c001", "source_name": "Hacker News", "title": "T",
                              "snippet": "SNIPPET", "extracted_text": PARA})
    assert f"Text: {PARA}" in line
    assert "SNIPPET" not in line
    assert "Snippet: SNIPPET" in _format_candidate({"title": "T", "snippet": "SNIPPET"})
