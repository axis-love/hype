"""Source text for news candidates: fetch the linked article, keep its text.

Collectors only carry a title and at most a 200-char snippet; the LLM
passes need the actual content of the story. attach_extracted_text()
fetches each candidate's article page, extracts the readable paragraphs,
and sets candidate['extracted_text'] to the best text available:

1. The fetched article text, when it has real substance (MIN_ARTICLE_CHARS).
2. Otherwise the longer of that and the collector's raw_text (Reddit
   selftext, HN story_text, RSS summary, GitHub description, paper
   abstract).

Items with no fetchable article (images, galleries, videos, tweets,
Reddit self-posts) keep whatever raw_text exists. The writer is told to
keep those posts short.

Design constraints (same as images.py):
- Never blocks generation: every failure leaves the candidate's raw_text
  as its text, never raises.
- No external deps: stdlib html.parser + httpx.
- Bounded work: TIMEOUT per fetch, MAX_HTML_BYTES per page,
  MAX_CONCURRENT fetches, MAX_TEXT_CHARS kept per item.
"""
from __future__ import annotations

import asyncio
import logging
import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit

import httpx

from newsbot.collectors.base import strip_html
from newsbot.httpclient import owned_client
from newsbot.images import UA

log = logging.getLogger(__name__)

TIMEOUT = 12.0
MAX_CONCURRENT = 8
MAX_HTML_BYTES = 3 * 1024 * 1024
MAX_TEXT_CHARS = 12000           # kept per item (store + prompts slice further)
MIN_ARTICLE_CHARS = 400          # below this the fetch found no real article
MIN_BLOCK_CHARS = 40             # shorter paragraphs are menus, bylines, buttons

# Hosts that never serve a readable article: media files, video, social
# posts that render client-side, discussion pages (their text is comments).
_NO_ARTICLE_HOSTS = (
    "news.ycombinator.com",
    "i.redd.it", "v.redd.it", "preview.redd.it", "i.imgur.com", "imgur.com",
    "youtube.com", "youtu.be", "twitter.com", "x.com", "t.co",
    "instagram.com", "tiktok.com", "streamable.com", "gfycat.com",
)
_MEDIA_EXT = re.compile(r"\.(jpe?g|png|webp|gif|bmp|avif|mp4|webm|mov|m4v|mkv|pdf)$", re.I)
_REDDIT_HOSTS = ("reddit.com", "redd.it")
_WS_RE = re.compile(r"\s+")


def _host(url: str) -> str:
    try:
        return (urlsplit(url).netloc or "").lower().removeprefix("www.")
    except ValueError:
        return ""


def _host_in(host: str, suffixes: tuple[str, ...]) -> bool:
    return any(host == s or host.endswith("." + s) for s in suffixes)


def article_url(candidate: dict[str, Any]) -> str | None:
    """The page to read for *candidate*, or None when there is no article.

    Reddit link posts point at the linked page (raw_json.external_url);
    Reddit self-posts and crossposts have no external article; their
    selftext arrives as raw_text.
    """
    url = str(candidate.get("url") or "").strip()
    if str(candidate.get("source") or "").lower() == "reddit":
        raw = candidate.get("raw_json") if isinstance(candidate.get("raw_json"), dict) else {}
        if raw.get("is_self"):
            return None
        url = str(raw.get("external_url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return None
    host = _host(url)
    if not host or _host_in(host, _NO_ARTICLE_HOSTS) or _host_in(host, _REDDIT_HOSTS):
        return None
    if _MEDIA_EXT.search(urlsplit(url).path):
        return None
    return url


class _TextParser(HTMLParser):
    """Collect paragraph-level text blocks, skipping page chrome.

    Blocks inside <article>/<main> are flagged so extract_text() can
    prefer them over sidebars and footers.
    """

    _SKIP = {"script", "style", "noscript", "nav", "header", "footer",
             "aside", "form", "svg", "button", "select", "template", "iframe"}
    _BLOCKS = {"p", "h1", "h2", "h3", "h4", "li", "blockquote", "pre", "figcaption"}
    _MAIN = {"article", "main"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[tuple[str, bool]] = []
        self.description = ""
        self._skip = 0
        self._main = 0
        self._buf: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "meta":
            a = dict(attrs)
            name = (a.get("property") or a.get("name") or "").lower()
            if name in ("og:description", "description") and not self.description:
                self.description = (a.get("content") or "").strip()
            return
        if tag in self._SKIP:
            self._skip += 1
        elif tag in self._MAIN:
            self._main += 1
        elif tag in self._BLOCKS and not self._skip:
            self._flush()
            self._buf = []

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in self._MAIN:
            self._main = max(0, self._main - 1)
        elif tag in self._BLOCKS:
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._buf is not None and not self._skip:
            self._buf.append(data)

    def _flush(self) -> None:
        if self._buf is None:
            return
        text = _WS_RE.sub(" ", "".join(self._buf)).strip()
        self._buf = None
        if len(text) >= MIN_BLOCK_CHARS:
            self.blocks.append((text, self._main > 0))

    def close(self) -> None:
        super().close()
        self._flush()


def extract_text(html: str) -> str:
    """Readable text of an article page: its paragraphs, joined by blank lines.

    Prefers blocks inside <article>/<main> when they carry enough text;
    falls back to all page blocks, then to the meta description.
    """
    parser = _TextParser()
    try:
        parser.feed(html or "")
        parser.close()
    except Exception:  # malformed markup: keep what was parsed so far
        pass
    main = [t for t, in_main in parser.blocks if in_main]
    blocks = main if sum(map(len, main)) >= MIN_ARTICLE_CHARS else [t for t, _ in parser.blocks]
    text = "\n\n".join(dict.fromkeys(blocks)) or parser.description
    return text[:MAX_TEXT_CHARS]


async def fetch_article_text(url: str, client: httpx.AsyncClient) -> str:
    """Fetch *url* and return its article text, or '' on any failure."""
    try:
        resp = await client.get(url, timeout=TIMEOUT, headers={"User-Agent": UA})
        resp.raise_for_status()
        if "html" not in resp.headers.get("content-type", "").lower():
            return ""
        if len(resp.content) > MAX_HTML_BYTES:
            return ""
        return extract_text(resp.text)
    except Exception as exc:
        log.info("article fetch failed for %s: %s", url, type(exc).__name__)
        return ""


def best_text(fetched: str, raw_text: str | None) -> str:
    """Pick the item's source text: a real article wins, else the longer one."""
    raw = strip_html(raw_text or "")[:MAX_TEXT_CHARS]
    if len(fetched) >= MIN_ARTICLE_CHARS:
        return fetched
    return fetched if len(fetched) >= len(raw) else raw


async def attach_extracted_text(
    candidates: list[dict[str, Any]], client: httpx.AsyncClient | None = None,
) -> None:
    """Set candidate['extracted_text'] on every candidate (mutates in place)."""
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)

    async def one(c: dict[str, Any], http: httpx.AsyncClient) -> None:
        url = article_url(c)
        fetched = ""
        if url:
            async with semaphore:
                fetched = await fetch_article_text(url, http)
        c["extracted_text"] = best_text(fetched, c.get("raw_text")) or None

    async with owned_client(client, follow_redirects=True) as http:
        await asyncio.gather(*(one(c, http) for c in candidates))

    with_text = sum(1 for c in candidates if len(c.get("extracted_text") or "") >= MIN_ARTICLE_CHARS)
    log.info("article text: %d/%d candidates have source text >= %d chars",
             with_text, len(candidates), MIN_ARTICLE_CHARS)
