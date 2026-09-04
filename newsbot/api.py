"""H4 HTTP consumer API — aiohttp, in-process, bearer-auth per consumer.

Started inside ``_scheduled_loop`` (main.py) on the same event loop when
``HYPE_API_PORT`` is set. No threads, no second DB connection — the API
shares the existing ``NewsStore`` and reads config via ``load_config``
on each request so it always sees the current settings.

Auth model
----------
``HYPE_API_KEYS`` env maps ``"consumer:key,consumer:key"`` to a
``{token: consumer_name}`` dict. Each request's ``Authorization: Bearer
<token>`` header is resolved to a consumer name, then
``config.consumer_profile(config, name)`` looks up the profile — raising
``ValueError`` on unknown consumers, which maps to a 403 (the key is
valid but no profile exists for that consumer).

Endpoints
---------
- ``GET /api/v1/items?limit=N`` — ranked eligible items for the bearer's
  consumer profile. Uses the same topic filter + per-consumer cooldown
  as ``select_for_consumer``, then ranks by current temperature (× merge
  multiplier) descending. ``limit`` is capped by the profile's
  ``max_candidates``.
- ``POST /api/v1/deliveries`` — body ``{item_id, external_ref}``.
  Idempotent via ``mark_delivered`` (INSERT OR IGNORE on the deliveries
  UNIQUE(post_id, channel) constraint). The first POST persists
  ``external_ref``; a repeat POST is a no-op — the original ref is kept.
  Returns ``{ok, already_delivered}``.
  ``mark_delivered`` raises ``ValueError`` for unknown post_ids → 404.
- ``GET /healthz`` — ``{ok: true, schema_version: N}``, no auth.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone

from aiohttp import web

from core.settings_store import SettingsStore, default_store
from newsbot.config import consumer_profile, load_config
from newsbot.db import NewsStore
from newsbot.scoring import merge_multiplier
from newsbot.selection import select_for_consumer

log = logging.getLogger(__name__)

# Default limit when the caller doesn't pass ?limit=N.
_DEFAULT_LIMIT = 5

# Module-level clock — tests patch this to freeze time. Production
# uses the real wall clock. (Same pattern as collectors' _sleep.)
_now = lambda: datetime.now(timezone.utc)

# AppKey instances for type-safe application storage (avoids
# NotAppKeyWarning — recommended by aiohttp for non-string keys).
_STORE_KEY = web.AppKey("store", NewsStore)
_SETTINGS_KEY = web.AppKey("settings", SettingsStore)
_KEYS_KEY = web.AppKey("api_keys", dict[str, str])


def _parse_api_keys(raw: str | None) -> dict[str, str]:
    """Parse ``HYPE_API_KEYS`` env into a ``{token: consumer_name}`` dict.

    Format: ``"girllm:abc123,blog:def456"``. Whitespace around entries
    is trimmed. Entries without a colon are skipped (defensive — a
    malformed entry shouldn't crash the whole API).
    """
    if not raw:
        return {}
    result: dict[str, str] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry or ":" not in entry:
            continue
        consumer, _, token = entry.partition(":")
        consumer = consumer.strip()
        token = token.strip()
        if consumer and token:
            result[token] = consumer
    return result


def _resolve_consumer(request: web.Request) -> str:
    """Extract the bearer token, resolve to a consumer name.

    Returns the consumer name on success. Raises ``web.HTTPUnauthorized``
    (401) when the token is missing or doesn't match any known key.
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise web.HTTPUnauthorized(
            text=json.dumps({"error": "missing or invalid Authorization header"}),
            content_type="application/json",
        )
    token = auth[len("Bearer "):].strip()
    keys: dict[str, str] = request.app[_KEYS_KEY]
    consumer = keys.get(token)
    if consumer is None:
        raise web.HTTPUnauthorized(
            text=json.dumps({"error": "unknown API key"}),
            content_type="application/json",
        )
    return consumer


async def _handle_items(request: web.Request) -> web.Response:
    """GET /api/v1/items?limit=N — ranked eligible items for the caller."""
    consumer = _resolve_consumer(request)
    store: NewsStore = request.app[_STORE_KEY]
    settings: SettingsStore = request.app[_SETTINGS_KEY]

    cfg = load_config(settings)

    # consumer_profile raises ValueError("unknown consumer: X") on unknown
    # consumers — that maps to 403 (the key is valid, but no profile exists).
    try:
        profile = consumer_profile(cfg, consumer)
    except ValueError as exc:
        raise web.HTTPForbidden(
            text=json.dumps({"error": str(exc)}),
            content_type="application/json",
        )

    channel = str(profile.get("channel") or consumer)
    rows = store.list_store_rows(channel)
    now = _now()

    # Per-consumer cooldown: fetch this consumer's recent deliveries.
    since = (now - timedelta(hours=24)).isoformat(timespec="seconds")
    deliveries = store.list_posted_since(channel, since)

    # Use select_for_consumer to get the PickResult — its temps dict has
    # the full temperature map for all rows after topic filtering. We
    # then build the ranked list from the same eligible set.
    result = select_for_consumer(rows, deliveries, profile, cfg, now=now)

    # Build the ranked list from all rows that passed topic filter +
    # cooldown + threshold. select_for_consumer already applied all three
    # before calling pick_hottest, but pick_hottest only returns the winner.
    # We need to re-apply the same filtering to get the full eligible list.
    #
    # Rather than duplicating the filter logic, we use the temps dict from
    # the PickResult (which covers all rows after topic filtering —
    # pick_hottest receives the filtered list) and reconstruct the eligible
    # set: rows whose temp >= threshold and not in excluded_ids.
    max_candidates = int(profile.get("max_candidates", _DEFAULT_LIMIT))

    # Parse limit from query string, cap to max_candidates.
    try:
        limit = int(request.query.get("limit", _DEFAULT_LIMIT))
    except (ValueError, TypeError):
        limit = _DEFAULT_LIMIT
    if limit <= 0:
        limit = _DEFAULT_LIMIT
    limit = min(limit, max_candidates)

    # Reconstruct the topic-filtered + cooldown-excluded row set.
    # select_for_consumer did this internally; we need the same set to
    # rank them. The temps dict keys are row IDs after topic filtering.
    # excluded_ids contains rows excluded by cooldown.
    temps = result.temps
    excluded = set(result.excluded_ids)

    # Filter: temp >= threshold AND not excluded.
    eligible = [
        (row_id, temp)
        for row_id, temp in temps.items()
        if row_id not in excluded and temp >= result.threshold
    ]

    # Sort by temperature × merge_multiplier descending.
    # We need merge_count per row — fetch from the rows list.
    row_map = {row["id"]: row for row in rows}
    merge_bonus = float(profile.get("merge_bonus", 0.2))
    merge_cap = float(profile.get("merge_cap", 2.0))

    def _rank_key(item: tuple[int, float]) -> float:
        row_id, temp = item
        row = row_map.get(row_id)
        mc = int(row.get("merge_count") or 1) if row else 1
        return temp * merge_multiplier(mc, bonus=merge_bonus, cap=merge_cap)

    eligible.sort(key=_rank_key, reverse=True)
    eligible = eligible[:limit]

    items = []
    for row_id, temp in eligible:
        row = row_map.get(row_id, {})
        matched_topics = row.get("matched_topics")
        if isinstance(matched_topics, str):
            try:
                matched_topics = json.loads(matched_topics)
            except (ValueError, TypeError):
                matched_topics = []
        items.append({
            "id": row_id,
            "title": str(row.get("title") or ""),
            "snippet": str(row.get("snippet") or ""),
            "url": str(row.get("url") or ""),
            "source_name": str(row.get("source_name") or ""),
            "origin_topic": str(row.get("origin_topic") or ""),
            "matched_topics": matched_topics or [],
            "temperature": round(temp, 2),
            "upvotes": int(row.get("upvotes") or 0),
            "comments": int(row.get("comments") or 0),
            "published_at": str(row.get("published_at") or ""),
            "merge_count": int(row.get("merge_count") or 1),
            "collected_at": str(row.get("scored_at") or ""),
        })

    return web.json_response({"items": items})


async def _handle_deliveries(request: web.Request) -> web.Response:
    """POST /api/v1/deliveries — idempotent delivery recording."""
    consumer = _resolve_consumer(request)
    store: NewsStore = request.app[_STORE_KEY]
    settings: SettingsStore = request.app[_SETTINGS_KEY]

    cfg = load_config(settings)
    try:
        profile = consumer_profile(cfg, consumer)
    except ValueError as exc:
        raise web.HTTPForbidden(
            text=json.dumps({"error": str(exc)}),
            content_type="application/json",
        )

    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(
            text=json.dumps({"error": "invalid JSON body"}),
            content_type="application/json",
        )
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(
            text=json.dumps({"error": "body must be a JSON object"}),
            content_type="application/json",
        )

    item_id = body.get("item_id")
    external_ref = body.get("external_ref")
    if item_id is None or not isinstance(item_id, int):
        raise web.HTTPBadRequest(
            text=json.dumps({"error": "item_id is required and must be an integer"}),
            content_type="application/json",
        )
    if not external_ref or not isinstance(external_ref, str):
        raise web.HTTPBadRequest(
            text=json.dumps({"error": "external_ref is required and must be a string"}),
            content_type="application/json",
        )

    channel = str(profile.get("channel") or consumer)

    # Check if already delivered — the deliveries table UNIQUE(post_id,
    # channel) constraint makes this idempotent. mark_delivered uses
    # INSERT OR IGNORE, so a duplicate insert is a no-op.
    already = store.is_delivered(item_id, channel)

    try:
        store.mark_delivered(item_id, channel, external_ref=external_ref)
    except ValueError:
        # mark_delivered raises ValueError for unknown post_ids (H2b).
        raise web.HTTPNotFound(
            text=json.dumps({"error": f"item_id {item_id} does not exist"}),
            content_type="application/json",
        )

    return web.json_response({
        "ok": True,
        "already_delivered": already,
    })


async def _handle_healthz(request: web.Request) -> web.Response:
    """GET /healthz — liveness probe, no auth."""
    store: NewsStore = request.app[_STORE_KEY]
    return web.json_response({"ok": True, "schema_version": store.schema_version()})


def create_api_app(
    store: NewsStore,
    api_keys: str,
    *,
    settings: SettingsStore | None = None,
) -> web.Application:
    """Build the aiohttp Application for the H4 consumer API.

    ``store`` is the shared NewsStore from the main loop — no second DB
    connection. ``api_keys`` is the raw ``HYPE_API_KEYS`` env string.
    ``settings`` is the shared SettingsStore; if None, a new one is
    created from NEWS_DB (for standalone/test use).
    """
    keys = _parse_api_keys(api_keys)
    if settings is None:
        settings = default_store(os.getenv("NEWS_DB", "data/newsbot.sqlite"))

    app = web.Application()
    app[_STORE_KEY] = store
    app[_SETTINGS_KEY] = settings
    app[_KEYS_KEY] = keys

    app.router.add_get("/healthz", _handle_healthz)
    app.router.add_get("/api/v1/items", _handle_items)
    app.router.add_post("/api/v1/deliveries", _handle_deliveries)
    return app


async def start_api(
    store: NewsStore,
    port: int,
    *,
    settings: SettingsStore | None = None,
) -> web.AppRunner | None:
    """Start the API on the given port if HYPE_API_KEYS is set.

    Returns an AppRunner (for cleanup) or None if the API is disabled
    (no keys configured). ``settings`` is the shared SettingsStore from
    the main loop; if None, a new one is created from NEWS_DB.
    """
    api_keys = os.getenv("HYPE_API_KEYS", "").strip()
    if not api_keys:
        log.warning("HYPE_API_PORT set but HYPE_API_KEYS is empty — API disabled")
        return None

    app = create_api_app(store, api_keys, settings=settings)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("H4 consumer API listening on port %d", port)
    return runner
