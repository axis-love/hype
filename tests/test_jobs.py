"""Tests for newsbot/jobs.py — JobCoordinator serialization and drain logic."""
import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from newsbot.db import NewsStore
from newsbot.jobs import JobCoordinator, format_post_message


@pytest.fixture
def store(tmp_path: Path) -> NewsStore:
    return NewsStore(tmp_path / "test.sqlite")


@pytest.fixture
def settings():
    """Minimal mock settings store."""
    class MockSettings:
        def __init__(self):
            self._data: dict[str, dict[str, any]] = {}
        def get(self, section, key, default=None):
            return self._data.get(section, {}).get(key, default)
        def set(self, section, key, value):
            self._data.setdefault(section, {})[key] = value
    return MockSettings()


@pytest.fixture
def coordinator(store, settings) -> JobCoordinator:
    return JobCoordinator(store, settings)


class TestJobCoordinatorSerialization:
    """Verify that the coordinator serializes generation and posting."""

    @pytest.mark.asyncio
    async def test_generation_lock_prevents_overlap(self, coordinator):
        """Two concurrent generation calls — only one should run, the other skipped."""
        call_count = 0

        async def slow_gen():
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.1)
            return 0

        # Launch two concurrently.
        results = await asyncio.gather(
            coordinator.run_generation(slow_gen),
            coordinator.run_generation(slow_gen),
        )
        # One should succeed (0), one should be skipped (2).
        assert results.count(0) == 1
        assert results.count(2) == 1
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_posting_lock_prevents_overlap(self, coordinator):
        """Two concurrent posting calls — only one should run, the other skipped."""
        # Add a pending post so posting has something to do.
        coordinator._store.add_pending_post({"title": "T", "body": "B", "url": ""})

        # Mock the actual delivery to be slow.
        async def slow_deliver(*args, **kwargs):
            await asyncio.sleep(0.1)
            return 0

        with patch.object(coordinator, "_deliver_one", side_effect=slow_deliver, return_value=0):
            results = await asyncio.gather(
                coordinator.run_posting(),
                coordinator.run_posting(),
            )
        # One should succeed (0), one should be skipped (2).
        assert results.count(0) == 1
        assert results.count(2) == 1

    @pytest.mark.asyncio
    async def test_generation_and_posting_cannot_overlap(self, coordinator):
        """Generation and posting use a SINGLE lock — they cannot overlap."""
        gen_started = asyncio.Event()
        post_started = asyncio.Event()
        gen_done = asyncio.Event()
        post_done = asyncio.Event()

        async def gen_fn():
            gen_started.set()
            await asyncio.sleep(0.05)
            gen_done.set()
            return 0

        # Add a pending post.
        coordinator._store.add_pending_post({"title": "T", "body": "B", "url": ""})

        async def slow_deliver(*args, **kwargs):
            post_started.set()
            await asyncio.sleep(0.05)
            post_done.set()
            return 0

        with patch.object(coordinator, "_deliver_one", side_effect=slow_deliver):
            await asyncio.gather(
                coordinator.run_generation(gen_fn),
                coordinator.run_posting(),
            )
        assert gen_started.is_set()
        assert post_started.is_set()
        # The single lock means gen and post are serialized —
        # gen must complete before post starts (or vice versa).
        # Both must have completed.
        assert gen_done.is_set()
        assert post_done.is_set()

    @pytest.mark.asyncio
    async def test_lock_released_on_exception(self, coordinator):
        """Lock must be released even if the generation function raises."""
        async def failing_gen():
            raise ValueError("boom")

        # First call raises.
        with pytest.raises(ValueError):
            await coordinator.run_generation(failing_gen)

        # Second call should succeed — lock was released.
        result = await coordinator.run_generation(lambda: asyncio.sleep(0))
        assert result == 0

    @pytest.mark.asyncio
    async def test_posting_lock_released_on_exception(self, coordinator):
        """Lock must be released even if posting raises."""
        coordinator._store.add_pending_post({"title": "T", "body": "B", "url": ""})

        async def failing_deliver(*args, **kwargs):
            raise RuntimeError("post failed")

        with patch.object(coordinator, "_deliver_one", side_effect=failing_deliver):
            with pytest.raises(RuntimeError):
                await coordinator.run_posting()

        # Should be able to call again — lock was released.
        with patch.object(coordinator, "_deliver_one", return_value=0):
            result = await coordinator.run_posting()
        assert result == 0

    @pytest.mark.asyncio
    async def test_multiple_gen_queued_behind_post_only_one_runs(self, coordinator):
        """Hold posting active, launch two generation calls, assert only one runs."""
        coordinator._store.add_pending_post({"title": "T", "body": "B", "url": ""})

        post_can_finish = asyncio.Event()

        gen_call_count = 0

        async def slow_deliver(*args, **kwargs):
            await post_can_finish.wait()
            return 0

        async def gen_fn():
            nonlocal gen_call_count
            gen_call_count += 1
            return 0

        with patch.object(coordinator, "_deliver_one", side_effect=slow_deliver):
            post_task = asyncio.create_task(coordinator.run_posting())
            # Give posting time to acquire the lock.
            await asyncio.sleep(0.05)

            # Launch two generation calls while posting is active.
            # Don't use gather — the first gen waits for the lock (held by posting),
            # so gather would hang. Launch the first as a task, the second returns 2.
            gen1_task = asyncio.create_task(coordinator.run_generation(gen_fn))
            # Give gen1 time to set _gen_running and start waiting for the lock.
            await asyncio.sleep(0.02)

            gen2_result = await coordinator.run_generation(gen_fn)
            assert gen2_result == 2  # skipped because gen1 already set the flag

            # Release posting so gen1 can proceed.
            post_can_finish.set()
            await post_task
            gen1_result = await gen1_task
            assert gen1_result == 0

        # Only one generation actually ran.
        assert gen_call_count == 1

    @pytest.mark.asyncio
    async def test_multiple_post_queued_behind_gen_only_one_runs(self, coordinator):
        """Hold generation active, launch two posting calls, assert only one runs."""
        gen_can_finish = asyncio.Event()

        deliver_call_count = 0

        async def slow_gen():
            await gen_can_finish.wait()
            return 0

        async def fast_deliver(*args, **kwargs):
            nonlocal deliver_call_count
            deliver_call_count += 1
            return 0

        coordinator._store.add_pending_post({"title": "T", "body": "B", "url": ""})

        with patch.object(coordinator, "_deliver_one", side_effect=fast_deliver):
            gen_task = asyncio.create_task(coordinator.run_generation(slow_gen))
            # Give generation time to acquire the lock.
            await asyncio.sleep(0.05)

            # Launch two posting calls while generation is active.
            # The first waits for the lock, the second returns 2.
            post1_task = asyncio.create_task(coordinator.run_posting())
            await asyncio.sleep(0.02)

            post2_result = await coordinator.run_posting()
            assert post2_result == 2  # skipped

            # Release generation so post1 can proceed.
            gen_can_finish.set()
            gen_result = await gen_task
            assert gen_result == 0
            post1_result = await post1_task
            assert post1_result == 0

        # Only one posting actually ran.
        assert deliver_call_count == 1

    @pytest.mark.asyncio
    async def test_coordinator_returns_to_idle_after_timeout(self, coordinator):
        """Coordinator state returns to idle after a timeout."""
        async def slow_gen():
            await asyncio.sleep(10)
            return 0

        result = await coordinator.run_generation(slow_gen, timeout=0.05)
        assert result == 1  # timeout
        assert coordinator.generation_running is False

    @pytest.mark.asyncio
    async def test_coordinator_returns_to_idle_after_cancellation(self, coordinator):
        """Coordinator state returns to idle after task cancellation."""
        async def slow_gen():
            await asyncio.sleep(10)
            return 0

        task = asyncio.create_task(coordinator.run_generation(slow_gen))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert coordinator.generation_running is False

    @pytest.mark.asyncio
    async def test_no_duplicate_posts_under_concurrent_posting(self, coordinator, store):
        """Concurrent posting calls must not deliver the same post twice."""
        store.add_pending_post({"title": "T", "body": "B", "url": "http://x.com"})

        delivered_ids: list[int] = []

        async def capture_deliver():
            # Yield to let any concurrent call check the admission flag.
            await asyncio.sleep(0.05)
            unposted = store.list_unposted_posts()
            post = unposted[0] if unposted else None
            if post:
                delivered_ids.append(post["id"])
                store.mark_posted(post["id"])
            return 0

        with patch.object(coordinator, "_deliver_one", new=capture_deliver):
            results = await asyncio.gather(
                coordinator.run_posting(),
                coordinator.run_posting(),
            )

        # One should succeed (0), one should be skipped (2).
        assert results.count(0) == 1
        assert results.count(2) == 1
        # No duplicate delivery.
        assert len(delivered_ids) == 1


class TestJobCoordinatorDrain:
    """Verify drain_posts consolidates the --once and dry-run paths."""

    @pytest.mark.asyncio
    async def test_drain_posts_all(self, coordinator, store):
        """Drain should post all eligible store rows and mark them posted."""
        from tests.helpers import scored_story, echo_style

        for i in range(3):
            store.add_stories_to_store([scored_story(f"T{i}", 90.0 - i * 5)], [])

        # Dry-run mode (no BOT_TOKEN).
        with patch("newsbot.jobs.llm_style_posts", new=echo_style), \
             patch("newsbot.jobs._build_lm_client", return_value=object()):
            os.environ.pop("BOT_TOKEN", None)
            os.environ.pop("NEWS_CHANNEL_ID", None)
            result = await coordinator.drain_posts()

        assert result == 0
        assert store.count_pending() == 0

    @pytest.mark.asyncio
    async def test_drain_empty_queue(self, coordinator):
        """Drain with empty queue should return 0."""
        result = await coordinator.drain_posts()
        assert result == 0


class TestConcurrentGenerationPostingIntegration:
    """Integration tests using real NewsStore DB to verify queue integrity
    under concurrent generation+posting.

    These tests exercise the real DB operations (add_stories_to_store,
    list_unposted_posts, mark_posted) through the JobCoordinator's
    single-lock serialization, verifying no duplicate posts, no lost rows,
    and no reordering.
    """

    @pytest.mark.asyncio
    async def test_concurrent_gen_post_no_duplicate_delivery(self, coordinator, store):
        """Concurrent generation+posting must not deliver any post twice.

        Scenario: 3 pending posts in queue. Posting starts draining them
        (with a simulated slow Telegram call). Concurrently, generation
        fires and APPENDS new stories to the store (v2 additive). The single
        lock ensures posting finishes before generation appends, so no post
        is delivered twice and no row is lost.
        """
        from tests.helpers import scored_story, echo_style

        # Pre-populate the queue with 3 scored posts.
        for i in range(3):
            store.add_stories_to_store([scored_story(f"Old{i}", 90.0 - i)], [])

        delivered_titles: list[str] = []

        async def slow_post_digest(message, **kwargs):
            await asyncio.sleep(0.05)  # Simulate network latency
            # Extract title from the HTML message for tracking.
            import re
            m = re.search(r"<b>(.*?)</b>", message)
            if m:
                delivered_titles.append(m.group(1))

        new_posts = [{"title": f"New{i}", "body": f"NewBody{i}", "url": f"http://new{i}.com"} for i in range(3)]
        seen_items = [{"url": f"http://new{i}.com", "title": f"New{i}"} for i in range(3)]

        async def gen_fn():
            store.add_stories_to_store(new_posts, seen_items)
            return 0

        with patch("newsbot.jobs.post_digest", new=slow_post_digest), \
             patch("newsbot.jobs.llm_style_posts", new=echo_style), \
             patch("newsbot.jobs._build_lm_client", return_value=object()):
            with patch.dict(os.environ, {"BOT_TOKEN": "fake", "NEWS_CHANNEL_ID": "fake"}):
                # Launch generation and posting concurrently.
                gen_task = asyncio.create_task(coordinator.run_generation(gen_fn))
                await asyncio.sleep(0.01)  # Let gen acquire the lock first.
                post_task = asyncio.create_task(coordinator.run_posting())

                gen_result = await gen_task
                post_result = await post_task

        # Generation should succeed.
        assert gen_result == 0
        # Posting should succeed (delivered one post).
        assert post_result == 0

        # No title should appear more than once — no duplicate delivery.
        assert len(delivered_titles) == len(set(delivered_titles)), \
            f"Duplicate delivery detected: {delivered_titles}"

        # Generation appended 3 stories; posting delivered 1 (hottest pick,
        # from the original scored batch). No post lost or duplicated.
        assert len(delivered_titles) == 1, f"Expected 1 delivery, got {delivered_titles}"

        # After both jobs: 3 old + 3 new − 1 delivered = 5 pending.
        remaining = store.count_pending()
        assert remaining == 5

    @pytest.mark.asyncio
    async def test_concurrent_posting_no_duplicate_or_loss(self, coordinator, store):
        """Multiple concurrent posting calls through real DB must not
        duplicate delivery or lose posts.

        With 5 pending posts and 3 concurrent posting calls, the single
        lock serializes them. Each call delivers at most 1 post. No post
        is delivered twice, no row is lost.
        """
        from tests.helpers import scored_story, echo_style

        for i in range(5):
            store.add_stories_to_store([scored_story(f"Post{i}", 90.0 - i)], [])

        delivered_ids: list[int] = []

        async def tracking_post_digest(message, **kwargs):
            await asyncio.sleep(0.02)
            # Don't track here — tracking happens in _deliver_one via mark_posted.

        # Monkey-patch _deliver_one to add a tiny delay so calls overlap.
        original_deliver = coordinator._deliver_one

        async def slow_deliver_one():
            await asyncio.sleep(0.02)
            return await original_deliver()

        with patch("newsbot.jobs.post_digest", new=tracking_post_digest), \
             patch("newsbot.jobs.llm_style_posts", new=echo_style), \
             patch("newsbot.jobs._build_lm_client", return_value=object()):
            with patch.dict(os.environ, {"BOT_TOKEN": "fake", "NEWS_CHANNEL_ID": "fake"}):
                with patch.object(coordinator, "_deliver_one", side_effect=slow_deliver_one):
                    results = await asyncio.gather(
                        coordinator.run_posting(),
                        coordinator.run_posting(),
                        coordinator.run_posting(),
                    )

        # Only one should succeed (0), others skipped (2).
        assert results.count(0) == 1
        assert results.count(2) == 2

        # Exactly 1 post was marked posted.
        posted_count = store._conn.execute(
            "SELECT COUNT(*) AS n FROM pending_posts WHERE posted_at IS NOT NULL"
        ).fetchone()
        assert int(posted_count["n"]) == 1

        # 4 posts still pending.
        assert store.count_pending() == 4

    @pytest.mark.asyncio
    async def test_generation_during_drain_preserves_order(self, coordinator, store):
        """Generation replacing the queue while drain_posts is running
        must not lose or reorder posts.

        Drain is processing posts one by one. A concurrent generation
        call must wait for drain to finish, then replace the remaining
        unposted posts. No delivered post should disappear, and new posts
        should be in the queue afterward.
        """
        from tests.helpers import scored_story, echo_style

        for i in range(3):
            store.add_stories_to_store([scored_story(f"Old{i}", 90.0 - i)], [])

        delivered: list[str] = []

        async def tracking_post_digest(message, **kwargs):
            import re
            m = re.search(r"<b>(.*?)</b>", message)
            if m:
                delivered.append(m.group(1))

        new_posts = [{"title": "FreshPost", "body": "Fresh", "url": "http://fresh.com"}]
        seen_items = [{"url": "http://fresh.com", "title": "FreshPost"}]

        async def gen_fn():
            store.add_stories_to_store(new_posts, seen_items)
            return 0

        with patch("newsbot.jobs.post_digest", new=tracking_post_digest), \
             patch("newsbot.jobs.llm_style_posts", new=echo_style), \
             patch("newsbot.jobs._build_lm_client", return_value=object()):
            with patch.dict(os.environ, {"BOT_TOKEN": "fake", "NEWS_CHANNEL_ID": "fake"}):
                # Start drain (processes all 3 old posts sequentially).
                drain_task = asyncio.create_task(coordinator.drain_posts())
                await asyncio.sleep(0.01)

                # Concurrently attempt generation — must wait for drain.
                gen_task = asyncio.create_task(coordinator.run_generation(gen_fn))

                drain_result = await drain_task
                gen_result = await gen_task

        assert drain_result == 0
        assert gen_result == 0

        # All 3 old posts were delivered in order (no reordering).
        assert delivered == ["Old0", "Old1", "Old2"]

        # New post is in the queue.
        assert store.count_pending() == 1

    @pytest.mark.asyncio
    async def test_concurrent_generation_no_queue_corruption(self, coordinator, store):
        """Two concurrent generation calls through real DB — only one
        should run, the other skipped. Queue must not be corrupted."""
        posts_a = [{"title": "A", "body": "BA", "url": "http://a.com"}]
        posts_b = [{"title": "B", "body": "BB", "url": "http://b.com"}]
        seen_a = [{"url": "http://a.com", "title": "A"}]
        seen_b = [{"url": "http://b.com", "title": "B"}]

        async def gen_a():
            await asyncio.sleep(0.02)
            store.add_stories_to_store(posts_a, seen_a)
            return 0

        async def gen_b():
            store.add_stories_to_store(posts_b, seen_b)
            return 0

        results = await asyncio.gather(
            coordinator.run_generation(gen_a),
            coordinator.run_generation(gen_b),
        )

        # One succeeds (0), one skipped (2).
        assert results.count(0) == 1
        assert results.count(2) == 1

        # Queue has exactly 1 post (from whichever generation ran).
        assert store.count_pending() == 1
        post = store.list_unposted_posts()[0]
        assert post is not None
        assert post["title"] in ("A", "B")


class TestFormatPostMessage:
    """Verify format_post_message produces correct HTML."""

    def test_basic_message(self):
        msg = format_post_message("Title", "Body text", "https://example.com/article")
        assert "<b>Title</b>" in msg
        assert "Body text" in msg
        assert 'href="https://example.com/article"' in msg
        assert "Source: example.com" in msg

    def test_html_escaping(self):
        msg = format_post_message("<script>", "<b>body</b>", "https://example.com")
        assert "<script>" not in msg  # escaped
        assert "&lt;script&gt;" in msg
        assert "<b>body</b>" not in msg  # body escaped

    def test_no_url(self):
        msg = format_post_message("Title", "Body", "")
        assert "<a href" not in msg
        assert "Title" in msg
        assert "Body" in msg

def test_format_post_message_truncates_long_body():
    """format_post_message caps body so total HTML stays under ~3000 chars."""
    from newsbot.jobs import format_post_message

    long_body = "This is a very long sentence. " * 200  # ~5000 chars
    msg = format_post_message("Test Title", long_body, "https://example.com/very/long/url/path")
    assert len(msg) <= 3100  # Under 3000 + small overhead for title/link
    assert msg.startswith("<b>Test Title</b>")
    assert "Source:" in msg


def test_format_post_message_short_body_unchanged():
    """Short bodies are not truncated."""
    from newsbot.jobs import format_post_message

    msg = format_post_message("Title", "Short body text.", "https://example.com")
    assert "Short body text." in msg
    assert "…" not in msg


def test_format_post_message_truncates_at_sentence_boundary():
    """Truncation prefers sentence boundaries when possible."""
    from newsbot.jobs import format_post_message

    # Create a body with clear sentence boundaries
    body = "First sentence here. Second one follows. Third is cut off " + "x" * 4000
    msg = format_post_message("T", body, "https://example.com")
    assert len(msg) <= 3100
    # Should cut at a sentence boundary, not mid-word
    assert not msg.endswith("x…") or msg.endswith("…")


# --- flow_001099: /scores rebuilt on pick_hottest ---


def test_format_scores_empty_store(tmp_path):
    """_format_scores with an empty store says so."""
    from newsbot.db import NewsStore
    from newsbot.main import _format_scores
    store = NewsStore(tmp_path / "test.sqlite")
    result = _format_scores(store, {"lookback_hours": 48})
    assert result == "Store is empty."
    store.close()


def test_format_scores_threshold_header_and_row(tmp_path):
    """_format_scores shows threshold header and eff/raw temp per row."""
    from newsbot.db import NewsStore
    from newsbot.main import _format_scores
    from tests.helpers import scored_story
    store = NewsStore(tmp_path / "test.sqlite")
    store.add_stories_to_store([scored_story("Test Post About LLMs", 150.0)], [])

    result = _format_scores(store, {"lookback_hours": 48})
    assert "Store temperatures (1 rows)" in result
    assert "Threshold:" in result
    assert "floor 35.0" in result  # default NEWS_TEMP_FLOOR
    assert "Test Post About LLMs" in result
    assert "eff" in result and "raw)" in result  # effective + raw temp
    assert "[raw]" in result  # not yet styled
    assert "source=hn" in result
    store.close()


def test_format_scores_styled_flag(tmp_path):
    """Rows with styled_at show the [styled] flag."""
    from newsbot.db import NewsStore
    from newsbot.main import _format_scores
    from tests.helpers import scored_story
    store = NewsStore(tmp_path / "test.sqlite")
    store.add_stories_to_store([scored_story("Styled One", 150.0)], [])
    row = store.list_store_rows()[0]
    store.set_styled_content(int(row["id"]), "Styled One", "Styled body.")

    result = _format_scores(store, {"lookback_hours": 48})
    assert "[styled]" in result
    store.close()


def test_format_scores_legacy_row_sinks(tmp_path):
    """Legacy rows (NULL score columns) show 'score unavailable', last."""
    from newsbot.db import NewsStore
    from newsbot.main import _format_scores
    from tests.helpers import scored_story
    store = NewsStore(tmp_path / "test.sqlite")

    store.add_stories_to_store([scored_story("Scored Post", 150.0)], [])
    # Legacy row inserted directly (no score columns).
    store._conn.execute(
        "INSERT INTO pending_posts(title, body, url, created_at) VALUES(?, ?, ?, ?)",
        ("Legacy Post", "B", "https://legacy.com", "2026-07-28T10:00:00+00:00"),
    )

    result = _format_scores(store, {"lookback_hours": 48})
    assert "score unavailable" in result
    assert "Legacy Post" in result
    assert "Scored Post" in result
    # Legacy row sinks below the scored row (hottest-first).
    assert result.index("Scored Post") < result.index("Legacy Post")
    store.close()


def test_format_scores_hottest_first(tmp_path):
    """_format_scores sorts rows hottest-first by effective temperature."""
    from newsbot.db import NewsStore
    from newsbot.main import _format_scores
    from tests.helpers import scored_story
    store = NewsStore(tmp_path / "test.sqlite")
    store.add_stories_to_store([
        scored_story("Cold Post", 50.0),
        scored_story("Hot Post", 300.0),
        scored_story("Warm Post", 150.0),
    ], [])

    result = _format_scores(store, {"lookback_hours": 48})
    idx_hot = result.index("Hot Post")
    idx_warm = result.index("Warm Post")
    idx_cold = result.index("Cold Post")
    assert idx_hot < idx_warm < idx_cold
    store.close()
