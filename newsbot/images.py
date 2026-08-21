"""Media extraction from news article pages (images + videos).

Fetches the article HTML and collects candidate media in priority order:
1. og:image / twitter:image / og:video meta tags — the article's chosen media.
2. Inline <img> and <video>/<source> tags with lazy-load attribute
   resolution (srcset, data-src, data-lazy-src, etc.), filtered for junk
   (logos, icons, avatars, ads).

Design constraints:
- Never blocks posting: every failure returns an empty list, never raises.
- No hard external deps: stdlib html.parser + httpx. PIL is OPTIONAL —
  used for image dimension checks when available, skipped otherwise.
- Bounded work: TIMEOUT per fetch, per-type download caps, MAX_MEDIA total.
- Telegram limits enforced: photos by URL <= 5 MB, videos by URL <= 20 MB.
  (Telegram re-fetches each URL server-side; oversized media 400s the
  whole sendRichMessage call, so we pre-validate.)
- Deduplication: same media under different query params counts once.
- YouTube/watch-page links are NOT media — Telegram rejects them (400).
  Only direct image/video file URLs are attached.
"""
from __future__ import annotations

import logging
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx

log = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
TIMEOUT = 15.0
MAX_MEDIA = 10                    # Telegram media group cap per post

# Telegram's documented URL-fetch limits (Sending Files): photos 5 MB,
# other media (video) 20 MB. Enforce so a single oversized URL doesn't
# 400 the entire sendRichMessage call.
PHOTO_MAX_BYTES = 5 * 1024 * 1024
VIDEO_MAX_BYTES = 20 * 1024 * 1024

MIN_IMG_BYTES = 5000              # below this is icon/placeholder territory
MIN_IMG_DIM = 200                 # px — below this is icon/logo territory

# Junk path fragments — logos, icons, avatars, ads, tracking.
# Conservative: must not kill legit content paths like /uploads/ or
# /images/ when they carry article content (xdaimages.com, substackcdn, etc).
_JUNK_FRAGMENTS = (
    "logo", "icon", "avatar", "favicon", "badge", "banner",
    "sprite", "placeholder", "blank.", "1x1", "pixel",
    "/ads/", "/ad/", "advert", "sponsor", "promo",
    "emoji", "reaction", "gravatar", "profile-pic",
)

_JUNK_NAMES = re.compile(
    r"(logo|icon|avatar|favicon|badge|banner|sprite|placeholder|blank|pixel)",
    re.I,
)

_IMG_EXT = re.compile(r"\.(jpe?g|png|webp|gif|bmp|avif)(\?.*)?$", re.I)
_VIDEO_EXT = re.compile(r"\.(mp4|webm|mov|m4v|mkv)(\?.*)?$", re.I)

# Optional PIL for image dimension checks.
try:
    from PIL import Image as _PILImage
except Exception:  # pragma: no cover — depends on environment
    _PILImage = None


class MediaParser(HTMLParser):
    """Collect og/twitter meta media and inline img/video srcs."""

    _IMG_META = ("og:image", "og:image:url", "og:image:secure_url",
                 "twitter:image", "twitter:image:src")
    _VIDEO_META = ("og:video", "og:video:url", "og:video:secure_url",
                   "twitter:player:stream")

    def __init__(self) -> None:
        super().__init__()
        self.meta_images: list[str] = []
        self.meta_videos: list[str] = []
        self.img_srcs: list[str] = []
        self.video_srcs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_dict = dict(attrs)
        if tag == "meta":
            prop = (attr_dict.get("property") or attr_dict.get("name") or "").lower()
            content = (attr_dict.get("content") or "").strip()
            if content:
                if prop in self._IMG_META:
                    self.meta_images.append(content)
                elif prop in self._VIDEO_META:
                    self.meta_videos.append(content)
        elif tag == "img":
            src = self._resolve_src(attr_dict)
            if src:
                self.img_srcs.append(src.strip())
        elif tag in ("video", "source"):
            src = (attr_dict.get("src") or "").strip()
            if src:
                self.video_srcs.append(src)

    @staticmethod
    def _resolve_src(attrs: dict[str, str | None]) -> str | None:
        """Pick the best src from an img tag, handling lazy-load attrs.

        Lazy-load attributes (data-src etc.) take priority over src:
        lazy-loaded sites put the real URL there and a tiny placeholder
        (often a data: URI) in src. A data: src is only used when no
        lazy attribute exists — and then dropped later by _normalize.
        """
        for key in ("data-src", "data-lazy-src", "data-original",
                    "data-srcset", "srcset", "data-lazy-srcset", "src"):
            val = attrs.get(key)
            if not val or val.startswith("data:"):
                continue
            if "srcset" in key:
                candidates = [c.strip().split()[0] for c in val.split(",") if c.strip()]
                if candidates:
                    return candidates[-1]  # last is usually the largest
            return val
        return None


def _normalize(url: str, base_url: str) -> str | None:
    """Resolve relative URLs; drop data:/blob:/non-http schemes."""
    url = url.strip()
    if not url or url.startswith("data:") or url.startswith("blob:"):
        return None
    if url.startswith("//"):
        url = "https:" + url
    resolved = urljoin(base_url, url)
    parsed = urlparse(resolved)
    if parsed.scheme not in ("http", "https"):
        return None
    return resolved


def _is_junk(url: str) -> bool:
    """Heuristic junk filter — logos, icons, avatars, ads."""
    lower = url.lower()
    for frag in _JUNK_FRAGMENTS:
        if frag in lower:
            return True
    path = urlparse(lower).path
    filename = path.rsplit("/", 1)[-1] if path else ""
    if _JUNK_NAMES.search(filename):
        return True
    return False


def _image_dims(data: bytes) -> tuple[int, int] | None:
    """Return (width, height) via PIL, or None if PIL missing/unreadable."""
    if _PILImage is None:
        return None
    try:
        import io
        with _PILImage.open(io.BytesIO(data)) as img:
            return img.size
    except Exception:
        return None


def _validate_photo(url: str, client: httpx.Client) -> dict | None:
    """Download + validate one image. Returns media dict or None."""
    try:
        resp = client.get(url)
        if resp.status_code != 200:
            return None
        if not resp.headers.get("content-type", "").startswith("image/"):
            return None
        data = resp.content
        if len(data) > PHOTO_MAX_BYTES:
            log.debug("photo over Telegram 5MB cap (%d B): %s", len(data), url)
            return None
        if len(data) < MIN_IMG_BYTES:
            log.debug("photo too small (%d B): %s", len(data), url)
            return None
        dims = _image_dims(data)
        if dims is not None and (dims[0] < MIN_IMG_DIM or dims[1] < MIN_IMG_DIM):
            log.debug("photo too small (%dx%d): %s", dims[0], dims[1], url)
            return None
        return {"type": "photo", "media": url}
    except Exception as exc:
        log.debug("photo fetch failed %s: %s", url, exc)
        return None


def _validate_video(url: str, client: httpx.Client) -> dict | None:
    """Validate one video via headers (no full download). Returns dict or None."""
    try:
        # HEAD first for content-type/length; some servers refuse HEAD.
        resp = client.head(url)
        ctype = resp.headers.get("content-type", "")
        clen = resp.headers.get("content-length")
        if resp.status_code >= 400 or not ctype.startswith("video/"):
            # Fall back to a small ranged GET to sniff the content type.
            probe = client.get(url, headers={"Range": "bytes=0-0"})
            ctype = probe.headers.get("content-type", "")
            clen = probe.headers.get("content-length") or clen
            if probe.status_code >= 400 or not ctype.startswith("video/"):
                return None
        if clen and clen.isdigit() and int(clen) > VIDEO_MAX_BYTES:
            log.debug("video over Telegram 20MB cap (%s B): %s", clen, url)
            return None
        return {"type": "video", "media": url, "supports_streaming": True}
    except Exception as exc:
        log.debug("video validation failed %s: %s", url, exc)
        return None


def extract_article_media(url: str, *, max_media: int = MAX_MEDIA) -> list[dict]:
    """Extract validated media (photos + videos) from an article page.

    Returns a list of Telegram InputMedia-style dicts, hero/meta media first.
    On any failure returns [] — never raises.
    """
    if not url:
        return []
    try:
        with httpx.Client(timeout=TIMEOUT, follow_redirects=True,
                          headers={"User-Agent": UA,
                                   "Accept-Encoding": "gzip, deflate"}) as client:
            resp = client.get(url)
            if resp.status_code != 200:
                log.warning("article fetch %d for %s", resp.status_code, url)
                return []
            html = resp.text
    except Exception as exc:
        log.warning("article fetch failed for %s: %s", url, exc)
        return []

    parser = MediaParser()
    try:
        parser.feed(html)
    except Exception as exc:
        log.warning("HTML parse failed for %s: %s", url, exc)
        return []

    # Candidate ordering: meta (hero) first, then inline. Photos before
    # videos within each tier so the visual lead is imagery.
    # Dedupe key is scheme+host+path only: WordPress/CDN sites reference
    # the same file with different query params (sizes, crops, quality),
    # which would otherwise post the identical image several times. The
    # first occurrence wins — meta images come first and are usually the
    # full-size hero.
    photo_candidates: list[str] = []
    video_candidates: list[str] = []
    seen: set[str] = set()

    def _add(raw: str, bucket: list[str]) -> None:
        norm = _normalize(raw, url)
        if not norm or _is_junk(norm):
            return
        parsed = urlparse(norm)
        dedup_key = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        if dedup_key in seen:
            return
        seen.add(dedup_key)
        bucket.append(norm)

    for u in parser.meta_images:
        _add(u, photo_candidates)
    for u in parser.img_srcs:
        _add(u, photo_candidates)
    for u in parser.meta_videos:
        _add(u, video_candidates)
    for u in parser.video_srcs:
        _add(u, video_candidates)

    total_candidates = len(photo_candidates) + len(video_candidates)
    if total_candidates == 0:
        log.info("no media candidates for %s", url)
        return []
    log.info("extracting %d media candidates (%d photo, %d video) from %s",
             total_candidates, len(photo_candidates), len(video_candidates), url)

    results: list[dict] = []
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True,
                      headers={"User-Agent": UA}) as client:
        for cand in photo_candidates:
            if len(results) >= max_media:
                break
            validated = _validate_photo(cand, client)
            if validated:
                results.append(validated)
        for cand in video_candidates:
            if len(results) >= max_media:
                break
            validated = _validate_video(cand, client)
            if validated:
                results.append(validated)

    log.info("extracted %d valid media from %s", len(results), url)
    return results
