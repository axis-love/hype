"""flow_001165 / H15: immutable Pass A title; channel copy on deliveries."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterator

import pytest

from newsbot.db import NewsStore, _migration_9
from newsbot.dedupe import match_candidate_to_store
from newsbot.recap import _recap_input_items

from tests.test_items_api import NOW, _get_client, _make_app, _seed_store, _story


@pytest.fixture
def store(tmp_path: Path) -> Iterator[NewsStore]:
    s = NewsStore(tmp_path / "h9.sqlite")
    yield s
    s.close()


@pytest.fixture(autouse=True)
def _freeze_time(monkeypatch: pytest.MonkeyPatch) -> None:
    from newsbot import api as api_module
    monkeypatch.setattr(api_module, "_now", lambda: NOW)


class TestMarkPostedLeavesTitle:
    def test_title_stays_english(self, store: NewsStore) -> None:
        store.add_stories_to_store(
            [_story(title="Mistral raises €3B", url="https://mistral.ai/x")],
            [],
        )
        rid = store._conn.execute("SELECT id FROM pending_posts").fetchone()["id"]
        store.mark_posted(
            rid,
            message_id=7,
            styled_title="Три миллиарда евро, чтобы Франция перестала быть ИИ-колбасой",
            styled_body="body",
        )
        row = store._conn.execute(
            "SELECT title FROM pending_posts WHERE id=?", (rid,)
        ).fetchone()
        assert row["title"] == "Mistral raises €3B"
        d = store._conn.execute(
            "SELECT styled_title, styled_body FROM deliveries WHERE post_id=?",
            (rid,),
        ).fetchone()
        assert d["styled_title"].startswith("Три миллиарда")
        assert d["styled_body"] == "body"

    @pytest.mark.asyncio
    async def test_girllm_api_returns_raw_title_after_style(self, store: NewsStore) -> None:
        ids = _seed_store(store, [_story(title="Mistral raises €3B", url="https://mistral.ai/x")])
        store.mark_posted(ids[0], styled_title="Три миллиарда евро", styled_body="ru body")
        app = _make_app(store)
        client = await _get_client(app)
        try:
            resp = await client.get(
                "/api/v1/items?limit=5",
                headers={"Authorization": "Bearer secret-key"},
            )
            assert resp.status == 200
            payload = await resp.json()
            titles = [it["title"] for it in payload["items"]]
            assert "Mistral raises €3B" in titles
            assert "Три миллиарда евро" not in titles
        finally:
            await client.close()


class TestRecapUsesStyledTitle:
    def test_recap_prefers_styled_title(self) -> None:
        rows = [{
            "title": "Mistral raises €3B",
            "styled_title": "Три миллиарда евро",
            "styled_body": "Styled body.",
            "summary": "English snippet about Mistral.",
            "category": "AI",
            "url": "https://mistral.ai/x",
            "source": "hn",
            "posted_at": "2026-09-08T12:00:00+00:00",
            "message_id": 1,
        }]
        items = _recap_input_items(rows)
        assert items[0]["title"] == "Три миллиарда евро"
        assert items[0]["body"] == "Styled body."

    def test_recap_falls_back_to_title_and_summary(self) -> None:
        rows = [{
            "title": "English raw",
            "styled_title": None,
            "styled_body": "",
            "summary": "English summary.",
            "category": "AI",
            "url": "https://x.io",
            "source": "hn",
            "posted_at": "",
            "message_id": None,
        }]
        items = _recap_input_items(rows)
        assert items[0]["title"] == "English raw"
        assert items[0]["body"] == "English summary."


class TestMigration9:
    def test_backfill_restores_from_raw_json(self, tmp_path: Path) -> None:
        db = tmp_path / "m9.sqlite"
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.executescript(
            """
            CREATE TABLE pending_posts(
              id INTEGER PRIMARY KEY,
              title TEXT NOT NULL,
              body TEXT NOT NULL,
              url TEXT,
              raw_json TEXT,
              styled_at TEXT
            );
            CREATE TABLE seen(
              url TEXT PRIMARY KEY,
              title TEXT,
              first_seen_at TEXT NOT NULL
            );
            """
        )
        cur.execute(
            "INSERT INTO pending_posts(id, title, body, url, raw_json, styled_at) VALUES(?,?,?,?,?,?)",
            (
                1,
                "Три миллиарда евро",
                "ru body",
                "https://mistral.ai/x",
                json.dumps({"title": "Mistral raises €3B"}),
                "2026-09-08T12:00:00+00:00",
            ),
        )
        cur.execute(
            "INSERT INTO pending_posts(id, title, body, url, raw_json, styled_at) VALUES(?,?,?,?,?,?)",
            (2, "Unstyled English", "", "https://u.example/1", json.dumps({"title": "Unstyled English"}), None),
        )
        cur.execute(
            "INSERT INTO pending_posts(id, title, body, url, raw_json, styled_at) VALUES(?,?,?,?,?,?)",
            (3, "Seen restore RU", "b", "https://seen.example/1", None, "2026-09-08T12:00:00+00:00"),
        )
        cur.execute(
            "INSERT INTO seen(url, title, first_seen_at) VALUES(?,?,?)",
            ("https://seen.example/1", "Seen English title", "2026-09-08T11:00:00+00:00"),
        )
        conn.commit()
        _migration_9(cur)
        conn.commit()

        r1 = cur.execute("SELECT title, styled_title FROM pending_posts WHERE id=1").fetchone()
        assert r1["styled_title"] == "Три миллиарда евро"
        assert r1["title"] == "Mistral raises €3B"

        r2 = cur.execute("SELECT title, styled_title FROM pending_posts WHERE id=2").fetchone()
        assert r2["title"] == "Unstyled English"
        assert r2["styled_title"] is None

        r3 = cur.execute("SELECT title, styled_title FROM pending_posts WHERE id=3").fetchone()
        assert r3["styled_title"] == "Seen restore RU"
        assert r3["title"] == "Seen English title"
        conn.close()

    def test_newsstore_second_open_is_noop(self, tmp_path: Path) -> None:
        path = tmp_path / "twice.sqlite"
        s1 = NewsStore(path)
        v1 = s1._conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()["v"]
        s1.close()
        s2 = NewsStore(path)
        v2 = s2._conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()["v"]
        s2.close()
        assert v1 == v2 == 11


class TestMatchUsesRawTitle:
    def test_english_recollection_matches_row(self, store: NewsStore) -> None:
        store.add_stories_to_store(
            [_story(title="Mistral raises €3B", url="https://mistral.ai/x")],
            [],
        )
        rid = store._conn.execute("SELECT id FROM pending_posts").fetchone()["id"]
        rows = store.list_store_rows("telegram")
        hit = match_candidate_to_store(
            {"title": "Mistral raises €3B", "url": "https://other.example/mistral"},
            rows,
        )
        assert hit is not None
        assert hit["id"] == rid
        assert hit["title"] == "Mistral raises €3B"


def test_mark_posted_does_not_write_pending_posts_title() -> None:
    from newsbot import db as db_mod
    src = Path(db_mod.__file__).read_text(encoding="utf-8")
    assert "def mark_posted(" in src
    assert "UPDATE pending_posts SET posted_at" not in src
    assert "SET title=?, body=?, styled_at=?" not in src
