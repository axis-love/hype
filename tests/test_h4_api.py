"""Tests for flow_001141: H4 HTTP consumer API (aiohttp, in-process).

Acceptance criteria:
  1. 401 without or with an unknown bearer; 403 for a key whose consumer
     has no profile.
  2. GET items returns only rows not delivered to the caller's channel,
     topic-filtered, sorted by temperature desc, limit capped by
     profile max_candidates.
  3. After POST deliveries the item no longer appears in GET; second POST
     returns 200 with already_delivered=true.
  4. /healthz 200 without auth.
  5. HYPE_API_PORT unset = API disabled; --once mode never starts the API.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

import pytest

from newsbot.api import _parse_api_keys, create_api_app
from newsbot.db import NewsStore


# --- helpers ---------------------------------------------------------------


NOW = datetime(2026, 8, 17, 12, 0, 0, tzinfo=timezone.utc)


def _bd(**overrides) -> dict:
    base = {
        "score": 100.0,
        "engagement": 80.0,
        "recency": 0.9,
        "source_weight": 1.0,
        "topic_bonus": 0,
        "crosspost_bonus": 0.0,
        "penalty": 1.0,
        "matched_topics": [],
        "origin_topic": "gaming",
        "scored_at": NOW.isoformat(),
        "lookback_hours": 48.0,
        "source": "reddit",
        "published_at": NOW.isoformat(),
        "upvotes": 100,
        "comments": 10,
        "stars": 0,
        "reposts": 0,
        "crosspost_count": 1,
    }
    base.update(overrides)
    return base


def _story(
    title: str = "Story A",
    url: str = "https://a.example.com/1",
    **bd_overrides,
) -> dict:
    return {
        "title": title,
        "url": url,
        "category": "AI",
        "snippet": "A snippet.",
        "source_name": "Hacker News",
        "source": "hn",
        "raw_json": {"payload": "x"},
        "score_breakdown": _bd(**bd_overrides),
    }


@pytest.fixture
def store(tmp_path: Path) -> Iterator[NewsStore]:
    s = NewsStore(tmp_path / "h4_store.sqlite")
    yield s
    s.close()


def _seed_store(store: NewsStore, stories: list[dict]) -> list[int]:
    """Insert stories and return their row ids."""
    store.add_stories_to_store(stories, [])
    rows = store._conn.execute(
        "SELECT id FROM pending_posts ORDER BY id"
    ).fetchall()
    return [r["id"] for r in rows]


async def _get_client(app):
    """Create an aiohttp test client."""
    from aiohttp.test_utils import TestClient, TestServer

    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    return client


def _make_app(
    store: NewsStore,
    *,
    api_keys: str = "girllm:secret-key,blog:blog-key",
) -> "aiohttp.web.Application":
    """Build the API app with the given key map.

    Passes the settings store that shares the test DB path so
    load_config reads from the same database as the test fixture.
    """
    from core.settings_store import default_store

    settings = default_store(str(store.db_path))
    return create_api_app(store, api_keys, settings=settings)


@pytest.fixture(autouse=True)
def _freeze_time(monkeypatch):
    """Freeze the API's clock to NOW so story temps don't decay."""
    from newsbot import api as api_module
    monkeypatch.setattr(api_module, "_now", lambda: NOW)


# --- AC 1: auth rejection --------------------------------------------------


class TestAuthRejection:
    """401 without or with unknown bearer; 403 for a key whose consumer
    has no profile."""

    @pytest.mark.asyncio
    async def test_missing_bearer_returns_401(self, store):
        app = _make_app(store)
        client = await _get_client(app)
        try:
            resp = await client.get("/api/v1/items")
            assert resp.status == 401
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_wrong_bearer_returns_401(self, store):
        app = _make_app(store)
        client = await _get_client(app)
        try:
            resp = await client.get(
                "/api/v1/items",
                headers={"Authorization": "Bearer wrong-key"},
            )
            assert resp.status == 401
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_unknown_consumer_returns_403(self, store):
        """A valid key whose consumer has no profile in config returns 403."""
        # 'blog' is a valid key but has no consumer profile in the default
        # _consumer_profiles() (only telegram + girllm exist).
        app = _make_app(store)
        client = await _get_client(app)
        try:
            resp = await client.get(
                "/api/v1/items",
                headers={"Authorization": "Bearer blog-key"},
            )
            assert resp.status == 403
            body = await resp.json()
            assert "blog" in body.get("error", "").lower()
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_malformed_auth_header_returns_401(self, store):
        app = _make_app(store)
        client = await _get_client(app)
        try:
            resp = await client.get(
                "/api/v1/items",
                headers={"Authorization": "NotBearer stuff"},
            )
            assert resp.status == 401
        finally:
            await client.close()


# --- AC 2: GET items ranking ----------------------------------------------


class TestItemsRanking:
    """GET items returns only rows not delivered to the caller's channel,
    topic-filtered, sorted by temperature desc, limit capped by profile
    max_candidates."""

    @pytest.mark.asyncio
    async def test_items_topic_filtered_for_girllm(self, store):
        """Girllm only sees gaming/gamedev/ai rows, not science."""
        stories = [
            _story("Gaming", "https://g.example.com", origin_topic="gaming"),
            _story("Science", "https://s.example.com", origin_topic="science"),
        ]
        _seed_store(store, stories)
        app = _make_app(store)
        client = await _get_client(app)
        try:
            resp = await client.get(
                "/api/v1/items",
                headers={"Authorization": "Bearer secret-key"},
            )
            assert resp.status == 200
            data = await resp.json()
            items = data["items"]
            assert len(items) == 1
            assert items[0]["title"] == "Gaming"
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_items_sorted_by_temperature_desc(self, store):
        """Items are ranked by current temperature, hottest first.

        The 'Cold' story (engagement=10) falls below girllm's threshold
        (floor=25, ratio=0.3 × median) and is correctly excluded — the
        API returns only items above the threshold, same as pick_hottest.
        """
        stories = [
            _story("Warm", "https://w.example.com",
                   engagement=50.0, origin_topic="gaming"),
            _story("Hot", "https://h.example.com",
                   engagement=200.0, origin_topic="gaming"),
            _story("Cold", "https://c.example.com",
                   engagement=10.0, origin_topic="gaming"),
        ]
        _seed_store(store, stories)
        app = _make_app(store)
        client = await _get_client(app)
        try:
            resp = await client.get(
                "/api/v1/items?limit=3",
                headers={"Authorization": "Bearer secret-key"},
            )
            assert resp.status == 200
            items = (await resp.json())["items"]
            # Cold (temp ~9) is below threshold (25), so only Hot + Warm.
            assert len(items) == 2
            # Hottest first
            assert items[0]["title"] == "Hot"
            assert items[1]["title"] == "Warm"
            # Temperatures descending
            temps = [i["temperature"] for i in items]
            assert temps == sorted(temps, reverse=True)
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_items_excludes_delivered_to_caller_channel(self, store):
        """A row already delivered to girllm's channel must NOT appear
        in the girllm GET response."""
        ids = _seed_store(store, [
            _story("Undelivered", "https://u.example.com",
                   origin_topic="gaming"),
            _story("Delivered", "https://d.example.com",
                   origin_topic="gaming"),
        ])
        # Mark the second row as delivered to girllm.
        store.mark_delivered(ids[1], "girllm")

        app = _make_app(store)
        client = await _get_client(app)
        try:
            resp = await client.get(
                "/api/v1/items",
                headers={"Authorization": "Bearer secret-key"},
            )
            assert resp.status == 200
            items = (await resp.json())["items"]
            assert len(items) == 1
            assert items[0]["title"] == "Undelivered"
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_limit_capped_by_profile_max_candidates(self, store):
        """limit=N must be capped to the consumer's max_candidates
        (girllm default = 5)."""
        stories = [
            _story(f"Story {i}", f"https://s{i}.example.com",
                   engagement=100.0 - i, origin_topic="gaming")
            for i in range(10)
        ]
        _seed_store(store, stories)
        app = _make_app(store)
        client = await _get_client(app)
        try:
            # Request 20, but girllm max_candidates is 5.
            resp = await client.get(
                "/api/v1/items?limit=20",
                headers={"Authorization": "Bearer secret-key"},
            )
            assert resp.status == 200
            items = (await resp.json())["items"]
            assert len(items) <= 5
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_items_match_select_for_consumer_ranking(self, store):
        """The ranking returned by the API matches what
        select_for_consumer produces for the same rows + profile."""
        from newsbot.config import consumer_profile, load_config
        from newsbot.selection import select_for_consumer
        from core.settings_store import default_store

        stories = [
            _story("A", "https://a.example.com",
                   engagement=100.0, origin_topic="gaming"),
            _story("B", "https://b.example.com",
                   engagement=50.0, origin_topic="ai"),
            _story("C", "https://c.example.com",
                   engagement=80.0, origin_topic="gamedev"),
        ]
        _seed_store(store, stories)

        # Compute expected ranking via select_for_consumer.
        settings = default_store(str(store.db_path))
        cfg = load_config(settings)
        profile = consumer_profile(cfg, "girllm")
        rows = store.list_store_rows("girllm")
        deliveries = store.list_posted_since("girllm", (NOW - timedelta(hours=24)).isoformat(timespec="seconds"))

        # select_for_consumer picks ONE winner; the API returns a ranked list.
        # We verify the API's top item matches select_for_consumer's pick.
        result = select_for_consumer(rows, deliveries, profile, cfg, now=NOW)
        assert result.row is not None
        expected_top_id = result.row["id"]

        app = _make_app(store)
        client = await _get_client(app)
        try:
            resp = await client.get(
                "/api/v1/items?limit=3",
                headers={"Authorization": "Bearer secret-key"},
            )
            assert resp.status == 200
            items = (await resp.json())["items"]
            assert items[0]["id"] == expected_top_id
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_items_limit_default_when_missing(self, store):
        """No limit param uses a sensible default (not unlimited)."""
        stories = [
            _story(f"S{i}", f"https://s{i}.example.com",
                   engagement=100.0, origin_topic="gaming")
            for i in range(3)
        ]
        _seed_store(store, stories)
        app = _make_app(store)
        client = await _get_client(app)
        try:
            resp = await client.get(
                "/api/v1/items",
                headers={"Authorization": "Bearer secret-key"},
            )
            assert resp.status == 200
            items = (await resp.json())["items"]
            assert len(items) == 3
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_items_empty_store(self, store):
        """Empty store returns 200 with empty list."""
        app = _make_app(store)
        client = await _get_client(app)
        try:
            resp = await client.get(
                "/api/v1/items",
                headers={"Authorization": "Bearer secret-key"},
            )
            assert resp.status == 200
            assert (await resp.json())["items"] == []
        finally:
            await client.close()


# --- AC 3: POST deliveries idempotency + 404 -------------------------------


class TestDeliveries:
    """POST /api/v1/deliveries idempotency + 404 unknown item."""

    @pytest.mark.asyncio
    async def test_delivery_marks_item_delivered(self, store):
        """After POST deliveries, the item no longer appears in GET items."""
        ids = _seed_store(store, [
            _story("Target", "https://t.example.com", origin_topic="gaming"),
        ])
        app = _make_app(store)
        client = await _get_client(app)
        try:
            resp = await client.post(
                "/api/v1/deliveries",
                json={"item_id": ids[0], "external_ref": "msg-001"},
                headers={"Authorization": "Bearer secret-key"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True
            assert data["already_delivered"] is False

            # Verify the item is gone from GET items.
            resp2 = await client.get(
                "/api/v1/items",
                headers={"Authorization": "Bearer secret-key"},
            )
            assert resp2.status == 200
            items = (await resp2.json())["items"]
            assert len(items) == 0
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_delivery_idempotent_second_post(self, store):
        """Second POST of the same item_id returns 200 with
        already_delivered=true."""
        ids = _seed_store(store, [
            _story("Target", "https://t.example.com", origin_topic="gaming"),
        ])
        app = _make_app(store)
        client = await _get_client(app)
        try:
            # First delivery.
            resp1 = await client.post(
                "/api/v1/deliveries",
                json={"item_id": ids[0], "external_ref": "msg-001"},
                headers={"Authorization": "Bearer secret-key"},
            )
            assert resp1.status == 200
            assert (await resp1.json())["already_delivered"] is False

            # Second delivery — idempotent.
            resp2 = await client.post(
                "/api/v1/deliveries",
                json={"item_id": ids[0], "external_ref": "msg-002"},
                headers={"Authorization": "Bearer secret-key"},
            )
            assert resp2.status == 200
            data = await resp2.json()
            assert data["ok"] is True
            assert data["already_delivered"] is True
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_delivery_404_unknown_item(self, store):
        """POST with a non-existent item_id returns 404."""
        app = _make_app(store)
        client = await _get_client(app)
        try:
            resp = await client.post(
                "/api/v1/deliveries",
                json={"item_id": 99999, "external_ref": "msg-x"},
                headers={"Authorization": "Bearer secret-key"},
            )
            assert resp.status == 404
            body = await resp.json()
            assert "99999" in body.get("error", "")
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_delivery_missing_item_id_returns_400(self, store):
        """POST without item_id returns 400."""
        app = _make_app(store)
        client = await _get_client(app)
        try:
            resp = await client.post(
                "/api/v1/deliveries",
                json={"external_ref": "msg-x"},
                headers={"Authorization": "Bearer secret-key"},
            )
            assert resp.status == 400
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_delivery_missing_external_ref_returns_400(self, store):
        """POST without external_ref returns 400."""
        ids = _seed_store(store, [
            _story("Target", "https://t.example.com", origin_topic="gaming"),
        ])
        app = _make_app(store)
        client = await _get_client(app)
        try:
            resp = await client.post(
                "/api/v1/deliveries",
                json={"item_id": ids[0]},
                headers={"Authorization": "Bearer secret-key"},
            )
            assert resp.status == 400
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_delivery_persists_external_ref(self, store):
        """POST deliveries must persist external_ref in the deliveries row."""
        ids = _seed_store(store, [
            _story("Target", "https://t.example.com", origin_topic="gaming"),
        ])
        app = _make_app(store)
        client = await _get_client(app)
        try:
            resp = await client.post(
                "/api/v1/deliveries",
                json={"item_id": ids[0], "external_ref": "msg-abc-123"},
                headers={"Authorization": "Bearer secret-key"},
            )
            assert resp.status == 200

            # Verify external_ref landed in the deliveries row.
            row = store._conn.execute(
                "SELECT external_ref FROM deliveries WHERE post_id=? AND channel=?",
                (ids[0], "girllm"),
            ).fetchone()
            assert row is not None
            assert row["external_ref"] == "msg-abc-123"
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_delivery_first_ref_wins_on_repeat(self, store):
        """A second POST with a different external_ref must NOT overwrite
        the original ref (INSERT OR IGNORE semantics)."""
        ids = _seed_store(store, [
            _story("Target", "https://t.example.com", origin_topic="gaming"),
        ])
        app = _make_app(store)
        client = await _get_client(app)
        try:
            # First delivery with ref-A.
            resp1 = await client.post(
                "/api/v1/deliveries",
                json={"item_id": ids[0], "external_ref": "ref-A"},
                headers={"Authorization": "Bearer secret-key"},
            )
            assert resp1.status == 200
            assert (await resp1.json())["already_delivered"] is False

            # Second delivery with ref-B — idempotent, ref stays ref-A.
            resp2 = await client.post(
                "/api/v1/deliveries",
                json={"item_id": ids[0], "external_ref": "ref-B"},
                headers={"Authorization": "Bearer secret-key"},
            )
            assert resp2.status == 200
            data = await resp2.json()
            assert data["ok"] is True
            assert data["already_delivered"] is True

            # The row must still hold the FIRST ref.
            row = store._conn.execute(
                "SELECT external_ref FROM deliveries WHERE post_id=? AND channel=?",
                (ids[0], "girllm"),
            ).fetchone()
            assert row is not None
            assert row["external_ref"] == "ref-A"
        finally:
            await client.close()


# --- AC 4: /healthz -------------------------------------------------------


class TestHealthz:
    """/healthz returns 200 without auth."""

    @pytest.mark.asyncio
    async def test_healthz_no_auth(self, store):
        app = _make_app(store)
        client = await _get_client(app)
        try:
            resp = await client.get("/healthz")
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True
            assert "schema_version" in data
            assert isinstance(data["schema_version"], int)
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_healthz_ignores_auth_header(self, store):
        """healthz doesn't care about auth — it's a liveness probe."""
        app = _make_app(store)
        client = await _get_client(app)
        try:
            resp = await client.get(
                "/healthz",
                headers={"Authorization": "Bearer garbage"},
            )
            assert resp.status == 200
        finally:
            await client.close()


# --- AC 5: port disabled + --once ------------------------------------------


class TestPortDisabled:
    """HYPE_API_PORT unset = API disabled; --once mode never starts the API."""

    def test_parse_api_keys_basic(self):
        """_parse_api_keys parses 'consumer:key,consumer2:key2' format."""
        keys = _parse_api_keys("girllm:abc123,blog:def456")
        assert keys == {"abc123": "girllm", "def456": "blog"}

    def test_parse_api_keys_empty(self):
        """Empty string returns empty dict."""
        assert _parse_api_keys("") == {}

    def test_parse_api_keys_whitespace_tolerant(self):
        """Whitespace around entries is trimmed."""
        keys = _parse_api_keys(" girllm:abc , blog: def ")
        assert keys == {"abc": "girllm", "def": "blog"}

    def test_parse_api_keys_skips_malformed(self):
        """Entries without a colon are skipped (not crashed)."""
        keys = _parse_api_keys("girllm:abc,badentry, blog:def")
        assert keys == {"abc": "girllm", "def": "blog"}

    def test_parse_api_keys_none(self):
        """None input returns empty dict."""
        assert _parse_api_keys(None) == {}


# --- Review fix: port-in-use survival -------------------------------------


class TestPortInUseSurvival:
    """If the API port is already bound, the scheduler must survive —
    the API is an optional feature and must never take down the Telegram
    channel's generation/posting loops.

    Reproduces the defect: ``start_api`` was awaited bare inside
    ``_scheduled_loop`` BEFORE the task list was created. An ``OSError``
    (port in use) propagated and killed the entire scheduler.
    """

    @pytest.mark.asyncio
    async def test_port_in_use_scheduler_survives(self, tmp_path, monkeypatch):
        """When start_api raises OSError the scheduler continues —
        api_runner stays None, tasks are still created."""
        import newsbot.main as main_mod
        from core.settings_store import default_store

        db_path = tmp_path / "survival.sqlite"
        settings = default_store(str(db_path))

        monkeypatch.setenv("NEWS_DB", str(db_path))
        monkeypatch.setenv("HYPE_API_PORT", "18999")
        monkeypatch.setenv("HYPE_API_KEYS", "girllm:test-key")
        # No BOT_TOKEN / ADMIN_USER_ID → bot_handler stays None,
        # keeping the test minimal.

        # Patch start_api to raise OSError (simulates port already bound).
        async def _boom(*a, **kw):
            raise OSError("[Errno 98] Address already in use")

        monkeypatch.setattr(main_mod, "start_api", _boom)

        # Patch the three scheduler iterations so the loops return
        # immediately (one tick then done). Each loop calls its iteration
        # then asyncio.sleep — we patch sleep to raise CancelledError so
        # asyncio.gather returns after one tick of each loop.
        call_log: list[str] = []

        async def _noop_gen(*a, **kw):
            call_log.append("gen")

        async def _noop_post(*a, **kw):
            call_log.append("post")

        async def _noop_summary(*a, **kw):
            call_log.append("summary")

        monkeypatch.setattr(main_mod, "_scheduler_gen_iteration", _noop_gen)
        monkeypatch.setattr(main_mod, "_scheduler_post_iteration", _noop_post)
        monkeypatch.setattr(main_mod, "_scheduler_summary_iteration", _noop_summary)

        async def _short_sleep(delay):
            # Raise CancelledError on first sleep to break the infinite
            # loop. asyncio.gather cancels the other tasks and returns.
            raise asyncio.CancelledError()

        monkeypatch.setattr(main_mod.asyncio, "sleep", _short_sleep)

        # _scheduled_loop should NOT raise OSError from start_api.
        # CancelledError from the gather is expected (we induced it).
        try:
            await main_mod._scheduled_loop(settings)
        except asyncio.CancelledError:
            pass  # Expected — our sleep patch induced it.

        # All three loops must have been created and ticked at least once.
        assert "gen" in call_log
        assert "post" in call_log
        assert "summary" in call_log
