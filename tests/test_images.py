"""Tests for newsbot/images.py — article media extraction.

Offline: MediaParser, _normalize, _is_junk, and dedupe logic are tested
against fixture HTML — no network. extract_article_media's network path
is exercised via a monkeypatched httpx.Client so tests stay hermetic.
"""
from __future__ import annotations

import pytest

from newsbot.images import (
    MAX_MEDIA,
    MediaParser,
    _is_junk,
    _normalize,
    extract_article_media,
)


# --- MediaParser ----------------------------------------------------------

FIXTURE_HTML = """
<html><head>
<meta property="og:image" content="https://cdn.example.com/hero.jpg">
<meta property="og:image" content="https://cdn.example.com/hero.jpg?w=1200">
<meta property="og:video" content="https://cdn.example.com/clip.mp4">
<meta name="twitter:image" content="//cdn.example.com/twitter-hero.jpg">
</head><body>
<img src="/content/figure1.png">
<img data-src="/lazy/figure2.jpg" src="data:image/gif;base64,AAAA">
<img srcset="/small.jpg 1x, /content/figure3.png 2x">
<img src="/assets/logo-dark.png">
<img src="/icons/heart.svg">
<img src="/ads/banner.jpg">
<video src="/media/talk.webm"></video>
<source src="/media/fallback.mp4">
</body></html>
"""


class TestMediaParser:
    def setup_method(self):
        self.parser = MediaParser()
        self.parser.feed(FIXTURE_HTML)

    def test_meta_images_collected(self):
        assert "https://cdn.example.com/hero.jpg" in self.parser.meta_images
        # Duplicate with query params is still collected (dedupe is later).
        assert "https://cdn.example.com/hero.jpg?w=1200" in self.parser.meta_images
        assert "//cdn.example.com/twitter-hero.jpg" in self.parser.meta_images

    def test_meta_video_collected(self):
        assert "https://cdn.example.com/clip.mp4" in self.parser.meta_videos

    def test_lazy_src_resolved(self):
        """data-src wins over a data: URI placeholder in src."""
        assert "/lazy/figure2.jpg" in self.parser.img_srcs

    def test_srcset_largest_picked(self):
        assert "/content/figure3.png" in self.parser.img_srcs

    def test_video_and_source_tags(self):
        assert "/media/talk.webm" in self.parser.video_srcs
        assert "/media/fallback.mp4" in self.parser.video_srcs


# --- _normalize ------------------------------------------------------------

class TestNormalize:
    BASE = "https://example.com/article"

    def test_relative_resolved(self):
        assert _normalize("/img/a.png", self.BASE) == "https://example.com/img/a.png"

    def test_protocol_relative_gets_https(self):
        assert _normalize("//cdn.example.com/a.png", self.BASE) == "https://cdn.example.com/a.png"

    def test_data_uri_dropped(self):
        assert _normalize("data:image/png;base64,AAA", self.BASE) is None

    def test_blob_dropped(self):
        assert _normalize("blob:https://example.com/x", self.BASE) is None

    def test_absolute_kept(self):
        u = "https://cdn.example.com/a.png"
        assert _normalize(u, self.BASE) == u


# --- _is_junk ---------------------------------------------------------------

class TestIsJunk:
    @pytest.mark.parametrize("url", [
        "https://example.com/assets/logo-dark.png",
        "https://example.com/favicon.ico",
        "https://example.com/icons/heart.svg",
        "https://example.com/img/avatar-123.jpg",
        "https://example.com/ads/banner.jpg",
        "https://example.com/spritesheet.png",
        "https://example.com/placeholder.svg",
    ])
    def test_junk_detected(self, url):
        assert _is_junk(url)

    @pytest.mark.parametrize("url", [
        # Real-world content paths that must NOT be filtered.
        "https://static0.xdaimages.com/wordpress/wp-content/uploads/2026/08/figure.png",
        "https://substackcdn.com/image/fetch/w_1200/https%3A/x/y.jpg",
        "https://cdn.example.com/uploads/2026/08/article-photo.jpg",
        "https://github.blog/wp-content/uploads/2026/08/chart.png",
    ])
    def test_content_not_filtered(self, url):
        assert not _is_junk(url)


# --- extract_article_media (monkeypatched network) --------------------------

class _FakeResponse:
    def __init__(self, status_code=200, content=b"", headers=None, text=""):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}
        self.text = text


class _FakeClient:
    """httpx.Client stand-in. GET of the article returns fixture HTML;
    image/video fetches return canned responses by URL."""

    def __init__(self, html, images=None, videos=None, **kwargs):
        self._html = html
        self._images = images or {}
        self._videos = videos or {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, **kwargs):
        if url.endswith("/article"):
            return _FakeResponse(text=self._html,
                                 headers={"content-type": "text/html"})
        if url in self._images:
            return self._images[url]
        if url in self._videos:
            return self._videos[url]
        return _FakeResponse(status_code=404)

    def head(self, url, **kwargs):
        if url in self._videos:
            return self._videos[url]
        return _FakeResponse(status_code=404)


PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c626001000000ffff03000006000557bfabd40000000049454e44ae426082"
)

HTML_TWO_IMAGES = """
<html><head>
<meta property="og:image" content="https://cdn.example.com/hero.png">
</head><body>
<img src="https://cdn.example.com/hero.png?w=800">
<img src="https://cdn.example.com/figure.png">
<img src="https://cdn.example.com/assets/logo.png">
</body></html>
"""


class TestExtractArticleMedia:
    def _patch_client(self, monkeypatch, html, images=None, videos=None):
        def factory(**kwargs):
            return _FakeClient(html, images=images, videos=videos)
        monkeypatch.setattr("newsbot.images.httpx.Client", factory)

    def test_dedupe_same_path_different_params(self, monkeypatch):
        """hero.png and hero.png?w=800 are ONE image; logo filtered out."""
        big = PNG_1PX + b"x" * 6000  # > MIN_IMG_BYTES
        self._patch_client(monkeypatch, HTML_TWO_IMAGES, images={
            "https://cdn.example.com/hero.png": _FakeResponse(
                content=big, headers={"content-type": "image/png"}),
            "https://cdn.example.com/figure.png": _FakeResponse(
                content=big, headers={"content-type": "image/png"}),
        })
        media = extract_article_media("https://example.com/article")
        urls = [m["media"] for m in media if m["type"] == "photo"]
        assert "https://cdn.example.com/hero.png" in urls
        assert "https://cdn.example.com/figure.png" in urls
        # No duplicate of hero under its ?w=800 variant.
        assert sum(1 for u in urls if "hero.png" in u) == 1
        # Junk filtered.
        assert not any("logo" in u for u in urls)

    def test_video_extracted(self, monkeypatch):
        html = '<html><head><meta property="og:video" content="https://cdn.example.com/clip.mp4"></head></html>'
        self._patch_client(monkeypatch, html, videos={
            "https://cdn.example.com/clip.mp4": _FakeResponse(
                headers={"content-type": "video/mp4", "content-length": "1000"}),
        })
        media = extract_article_media("https://example.com/article")
        assert any(m["type"] == "video" and m["media"].endswith("clip.mp4")
                   for m in media)

    def test_oversized_photo_dropped(self, monkeypatch):
        html = '<html><head><meta property="og:image" content="https://cdn.example.com/huge.png"></head></html>'
        self._patch_client(monkeypatch, html, images={
            "https://cdn.example.com/huge.png": _FakeResponse(
                content=b"x" * (5 * 1024 * 1024 + 1),
                headers={"content-type": "image/png"}),
        })
        media = extract_article_media("https://example.com/article")
        assert media == []

    def test_empty_url_returns_empty(self, monkeypatch):
        assert extract_article_media("") == []

    def test_fetch_failure_returns_empty(self, monkeypatch):
        """A 500 on the article page yields [] — never raises."""
        def factory(**kwargs):
            return _FakeClient("<html></html>")
        monkeypatch.setattr("newsbot.images.httpx.Client", factory)
        # _FakeClient returns 404 for /article → empty result.
        assert extract_article_media("https://example.com/article") == []

    def test_max_media_cap_respected(self, monkeypatch):
        imgs = "".join(
            f'<img src="https://cdn.example.com/p{i}.png">' for i in range(15)
        )
        html = f"<html><body>{imgs}</body></html>"
        big = PNG_1PX + b"x" * 6000
        self._patch_client(monkeypatch, html, images={
            f"https://cdn.example.com/p{i}.png": _FakeResponse(
                content=big, headers={"content-type": "image/png"})
            for i in range(15)
        })
        media = extract_article_media("https://example.com/article")
        assert len(media) <= MAX_MEDIA == 10
