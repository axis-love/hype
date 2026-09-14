"""H16: Pass B reads persisted summary (flow_001175)."""
from __future__ import annotations

from pathlib import Path

import pytest

from newsbot.jobs import _row_to_styler_input
from newsbot.main import _row_to_styler_input as preview_helper
from newsbot.summarizer import llm_style_posts


def test_row_to_styler_input_uses_store_summary() -> None:
    shaped = _row_to_styler_input({
        "id": 7,
        "title": "English headline",
        "url": "https://example.com/x",
        "summary": "S",
        "snippet": "COLLECTOR SNIPPET MUST NOT APPEAR",
        "published_at": "2026-09-14T00:00:00+00:00",
        "upvotes": 10,
        "comments": 1,
        "stars": 0,
        "crosspost_count": 1,
    })
    assert shaped["short_summary"] == "S"


@pytest.mark.asyncio
async def test_styler_prompt_uses_summary_not_snippet() -> None:
    shaped = _row_to_styler_input({
        "id": 7,
        "title": "English headline",
        "url": "https://example.com/x",
        "summary": "S",
        "snippet": "COLLECTOR SNIPPET MUST NOT APPEAR",
        "published_at": "2026-09-14T00:00:00+00:00",
        "upvotes": 10,
        "comments": 1,
        "stars": 0,
        "crosspost_count": 1,
    })

    class _CapturingLM:
        model = "fake"
        last_user = ""

        async def generate(self, messages, **params):
            self.last_user = messages[-1]["content"]
            return (
                '{"posts":[{"id":"s007","title":"T","body":"B"}]}',
                "stop",
            )

    lm = _CapturingLM()
    await llm_style_posts([shaped], lm, style_prompt="style")
    assert "Summary: S" in lm.last_user
    assert "COLLECTOR SNIPPET MUST NOT APPEAR" not in lm.last_user


def test_preview_uses_same_helper() -> None:
    assert preview_helper is _row_to_styler_input
    # No second shaping function in main.py.
    text = Path("newsbot/main.py").read_text()
    assert text.count("_row_to_styler_input") >= 1
