"""Reddit collector via JSON API (OAuth).

Fetches configured subreddits in batches through Reddit's multi-subreddit
JSON endpoint (oauth.reddit.com/r/a+b+c/hot.json?limit=N&raw_json=1),
which returns entries for every listed subreddit in ONE request. Reddit
throttles this host's IP on near-simultaneous per-subreddit request bursts
— first request 200, then 429 even at 30s spacing — so batching collapses
N requests into N/batch_size and sequential group fetches with a small
inter-group delay keep the host out of the throttle window.

Auth (verified working 2026-08-22):
  - Env var REDDIT_REFRESH_TOKEN (permanent, non-rotating).
  - Access token: POST https://www.reddit.com/api/v1/access_token
    form grant_type=refresh_token, header Authorization: Basic (public
    client id, no secret), User-Agent: devvit-cli.
  - Access token is a JWT, TTL 86400s, scope *. Cached module-level;
    refreshed on expiry or 401.
  - API calls use a descriptive User-Agent (cybercream-hypebot/0.1).

Mapping data.children[].data -> Candidate:
  - upvotes = score, comments = num_comments (real numbers now!)
  - source_name from data.subreddit (as r/<name>)
  - url = permalink; external link + preview image stored in raw_json
  - over_18 (NSFW) entries dropped; self-posts kept

Config (under news.sources.reddit):
  subreddits: list[str]  — e.g. ['LocalLLaMA', 'MachineLearning']
  limit: int             — per-subreddit cap (default 10)

Env:
  REDDIT_REFRESH_TOKEN   — permanent refresh token (required; no token = [])
  NEWS_REDDIT_BATCH_SIZE — subreddits per batched request (default 4)
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import time
from typing import Any

import httpx

from newsbot.collectors.base import Candidate, new_candidate, strip_html, truncate, to_iso_utc
from newsbot.collectors._shared import get_shared_semaphore

log = logging.getLogger(__name__)

# --- Auth constants -------------------------------------------------------

# Devvit CLI's public client id — there is no secret (Responsible Builder
# Policy killed self-service script apps; Anton registered a Devvit app).
_REDDIT_CLIENT_ID = "TWTsqXa53CexlrYGBWaesQ"

# User-Agent for the token endpoint (must match what Devvit CLI uses).
_REDDIT_TOKEN_USER_AGENT = "devvit-cli"

# Descriptive User-Agent for API calls.
_REDDIT_API_USER_AGENT = "cybercream-hypebot/0.1"

# Token endpoint.
_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"

# API base for all data calls (requires Bearer token).
_API_BASE = "https://oauth.reddit.com"

# Access token TTL safety margin (refresh 60s before actual expiry).
_TOKEN_SAFETY_MARGIN_SECONDS = 60

# Module-level access token cache: {"token": str, "expires_at": float}.
# Tests can reset via _access_token_cache.clear().
_access_token_cache: dict[str, str | float] = {}


# --- HTTP / pacing constants ----------------------------------------------

# HTTP timeout for Reddit fetches.
_REDDIT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

# Subreddits per batched multi-subreddit request.
_DEFAULT_BATCH_SIZE = 4

# Pacing between sequential group fetches (keeps the host out of Reddit's
# burst throttle). Constant by design — the only knob is the batch size.
_GROUP_DELAY_SECONDS = 2.0

# Reddit JSON API honors ?limit=N up to 100 entries per response.
_REDDIT_JSON_LIMIT_MAX = 100

# A 429 retry never sleeps longer than this, however large Retry-After is.
_MAX_RETRY_AFTER_SECONDS = 30.0


# --- Token management -----------------------------------------------------

def _basic_auth_header() -> str:
    """Build the Basic auth header for the token endpoint.

    The client id is public (Devvit CLI's); there is no secret — the
    password portion is empty.
    """
    raw = f"{_REDDIT_CLIENT_ID}:".encode()
    return "Basic " + base64.b64encode(raw).decode()


async def _refresh_access_token(refresh_token: str) -> tuple[str, float]:
    """Refresh the Reddit access token.

    Returns (access_token, expires_at_epoch). On failure returns ("", 0.0).
    Never raises — the caller handles the empty-token case.
    """
    try:
        async with httpx.AsyncClient(
            timeout=_REDDIT_TIMEOUT,
            follow_redirects=True,
        ) as client:
            resp = await client.post(
                _TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                },
                headers={
                    "Authorization": _basic_auth_header(),
                    "User-Agent": _REDDIT_TOKEN_USER_AGENT,
                },
            )
    except httpx.TimeoutException:
        log.warning("Reddit token refresh timed out")
        return "", 0.0
    except Exception as exc:
        log.warning("Reddit token refresh failed: %s", exc)
        return "", 0.0

    if resp.status_code != 200:
        log.warning("Reddit token refresh returned status %s", resp.status_code)
        return "", 0.0

    try:
        body = resp.json()
    except Exception as exc:
        log.warning("Reddit token response parse failed: %s", exc)
        return "", 0.0

    token = str(body.get("access_token") or "")
    if not token:
        log.warning("Reddit token response missing access_token")
        return "", 0.0

    expires_in = float(body.get("expires_in") or 86400)
    expires_at = time.time() + expires_in - _TOKEN_SAFETY_MARGIN_SECONDS
    return token, expires_at


async def _get_access_token(refresh_token: str) -> str:
    """Return a valid access token, refreshing if the cache is stale.

    Returns "" if refresh fails. Never raises.
    """
    cached = str(_access_token_cache.get("token") or "")
    expires_at = float(_access_token_cache.get("expires_at") or 0.0)
    if cached and time.time() < expires_at:
        return cached

    token, expires_at = await _refresh_access_token(refresh_token)
    if token:
        _access_token_cache["token"] = token
        _access_token_cache["expires_at"] = expires_at
    return token


def _invalidate_token_cache() -> None:
    """Clear the access token cache (called on 401)."""
    _access_token_cache.clear()


# --- Pacing / config helpers ----------------------------------------------

def _batch_size() -> int:
    """Subreddits per batched request (NEWS_REDDIT_BATCH_SIZE, default 4)."""
    try:
        return max(1, int(os.environ.get("NEWS_REDDIT_BATCH_SIZE", str(_DEFAULT_BATCH_SIZE))))
    except ValueError:
        return _DEFAULT_BATCH_SIZE


def _retry_after_seconds(header: Any) -> float:
    """Parse a Retry-After header into seconds, clamped to the retry cap.

    Numeric headers are the common case; an HTTP-date or garbage value
    yields 0 (retry without extra sleep).
    """
    if header is None:
        return 0.0
    try:
        return max(0.0, min(float(str(header)), _MAX_RETRY_AFTER_SECONDS))
    except (TypeError, ValueError):
        return 0.0


async def _sleep(seconds: float) -> None:
    """Indirection over asyncio.sleep so tests can skip/observe pacing."""
    await asyncio.sleep(seconds)


# --- Group fetch ----------------------------------------------------------

async def _fetch_group(
    subs: list[str],
    limit: int,
    configured_by_lower: dict[str, str],
    refresh_token: str,
) -> list[Candidate]:
    """Fetch one batch of subreddits via the multi-subreddit JSON endpoint.

    Entries are attributed to their subreddit via the data.subreddit field.
    Entries for subreddits outside the configured list are dropped. The
    per-subreddit cap is applied after attribution. Never raises: any
    failure logs a warning and returns [].
    """
    group_label = "+".join(subs)
    total_limit = min(len(subs) * limit, _REDDIT_JSON_LIMIT_MAX)
    url = (
        f"{_API_BASE}/r/{group_label}/hot.json"
        f"?limit={total_limit}&raw_json=1"
    )

    body_json: dict[str, Any] | None = None
    status = 0
    retry_after: str | None = None
    sem = get_shared_semaphore()

    for attempt in range(2):  # initial try + one retry after 429/401
        token = await _get_access_token(refresh_token)
        if not token:
            log.warning("Reddit: no access token, skipping group %s", group_label)
            return []

        async with sem:
            try:
                async with httpx.AsyncClient(
                    timeout=_REDDIT_TIMEOUT,
                    follow_redirects=True,
                    headers={"User-Agent": _REDDIT_API_USER_AGENT},
                ) as client:
                    response = await client.get(
                        url,
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    status = response.status_code
                    retry_after = (
                        response.headers.get("Retry-After")
                        if status == 429 else None
                    )
                    if status == 200:
                        try:
                            body_json = response.json()
                        except Exception as exc:
                            log.warning(
                                "Reddit JSON parse failed for %s: %s",
                                group_label, exc,
                            )
                            return []
            except httpx.TimeoutException:
                log.warning(
                    "Reddit batch fetch timed out for %s url=%s",
                    group_label, url,
                )
                return []
            except Exception as exc:
                log.warning(
                    "Reddit batch fetch failed for %s url=%s: %s",
                    group_label, url, exc,
                )
                return []

        if status == 429 and attempt == 0:
            delay = _retry_after_seconds(retry_after)
            log.warning(
                "Reddit batch %s throttled (429); retrying once in %.1fs",
                group_label, delay,
            )
            await _sleep(delay)
            continue
        if status == 401 and attempt == 0:
            log.warning(
                "Reddit batch %s got 401; refreshing token and retrying",
                group_label,
            )
            _invalidate_token_cache()
            continue
        break

    if status >= 400 or body_json is None:
        log.warning(
            "Reddit batch fetch failed for %s url=%s status=%s",
            group_label, url, status or "unknown",
        )
        return []

    # Parse data.children[].data -> Candidate.
    children: list[dict[str, Any]] = (
        body_json.get("data", {}).get("children", []) or []
    )
    items: list[Candidate] = []
    per_sub_counts: dict[str, int] = {}

    for child in children:
        data: dict[str, Any] = child.get("data") or {}
        if not data:
            continue

        # NSFW filter: drop over_18 entries.
        if data.get("over_18"):
            continue

        sub_raw = str(data.get("subreddit") or "").strip()
        if not sub_raw:
            continue
        configured_sub = configured_by_lower.get(sub_raw.lower())
        if configured_sub is None:
            continue  # not in the configured list
        if per_sub_counts.get(configured_sub, 0) >= limit:
            continue  # per-subreddit cap, applied after attribution

        title = str(data.get("title") or "").strip()
        if not title:
            continue

        permalink = str(data.get("permalink") or "").strip()
        post_url = f"https://www.reddit.com{permalink}" if permalink else ""
        if not post_url:
            continue  # unattributable entry

        score = data.get("score")
        num_comments = data.get("num_comments")
        selftext = str(data.get("selftext") or "")
        snippet = truncate(strip_html(selftext)) if selftext else ""
        external_url = data.get("url")  # the link the post points to

        # Store external link + preview for the media extractor.
        raw_json: dict[str, Any] = {
            "external_url": external_url,
            "preview": data.get("preview"),
            "is_self": data.get("is_self", False),
            "subreddit": data.get("subreddit"),
            "permalink": permalink,
            "thumbnail": data.get("thumbnail"),
        }

        items.append(
            new_candidate(
                title=title,
                url=post_url,
                source="reddit",
                source_name=f"r/{configured_sub}",
                snippet=snippet,
                published_at=to_iso_utc(data.get("created_utc")),
                upvotes=int(score) if score is not None else None,
                comments=int(num_comments) if num_comments is not None else None,
                raw_text=selftext.strip() or None,
                raw_json=raw_json,
            )
        )
        per_sub_counts[configured_sub] = per_sub_counts.get(configured_sub, 0) + 1

    if not items:
        log.warning(
            "Reddit batch fetch returned zero usable items for %s url=%s status=%s",
            group_label, url, status or "unknown",
        )
    return items


# --- Public entry point ---------------------------------------------------

async def collect(config: dict[str, Any]) -> list[Candidate]:
    """Fetch Reddit candidates via batched multi-subreddit JSON API.

    *config* is the news.sources.reddit block. Subreddits are fetched in
    groups (one request per group) SEQUENTIALLY with a small delay between
    groups — Reddit throttles this host's IP on request bursts.

    Requires REDDIT_REFRESH_TOKEN env var. If unset, returns [] (no-op).
    """
    refresh_token = os.environ.get("REDDIT_REFRESH_TOKEN", "").strip()
    if not refresh_token:
        log.warning("Reddit collector skipped: REDDIT_REFRESH_TOKEN not set")
        return []

    subreddits: list[str] = []
    configured_by_lower: dict[str, str] = {}
    for raw in config.get("subreddits") or []:
        sub = str(raw).strip().strip("/")
        if sub and sub.lower() not in configured_by_lower:
            configured_by_lower[sub.lower()] = sub
            subreddits.append(sub)
    if not subreddits:
        return []

    limit = max(1, min(int(config.get("limit") or 10), 25))
    batch_size = _batch_size()
    groups = [subreddits[i:i + batch_size] for i in range(0, len(subreddits), batch_size)]

    results: list[Candidate] = []
    for index, group in enumerate(groups):
        if index:
            await _sleep(_GROUP_DELAY_SECONDS)
        try:
            results.extend(
                await _fetch_group(group, limit, configured_by_lower, refresh_token)
            )
        except Exception as exc:  # defensive: the collector never raises
            log.warning("Reddit batch fetch error for %s: %s", "+".join(group), exc)
    return results
