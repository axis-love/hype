"""Two-pass LLM summarizer.

Pass A — filter:  take 30-80 scored candidates, ask the LLM to keep/drop,
                  assign category + importance + a one-line summary.
                  Returns JSON {items:[{keep,title,url,category,importance,
                  reason,short_summary}]}.

Pass B — style:   take the top 8 kept items and ask the LLM to write
                  individual styled posts (one per item). Returns JSON
                  {posts:[{title,body}]}.

Both passes use response_format=json_object to force content output from
reasoning models. Output is run through core.text_utils.strip_think to
remove reasoning-model <think> blocks.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from core.text_utils import strip_think

log = logging.getLogger(__name__)

FILTER_SYSTEM = (
    "You are a tech-news filter. For each candidate below, decide whether to keep it "
    "for a Telegram digest. Drop: old news, thin marketing launches, pure drama, "
    "crypto spam, obvious reposts, low-quality GitHub repos, low-value Product Hunt "
    "launches. Check the Published date — if the item is older than 7 days, drop it "
    "unless it has extraordinary engagement (10K+ stars, 1K+ upvotes) AND is still "
    "actively discussed. For kept items, assign a category (e.g. 'AI / Coding', 'LLM', 'Game Dev', "
    "'VR/AR', 'Robotics', 'Research', 'Tools'), an importance score from 1 to 10, a "
    "one-line reason, and a one-line short_summary. "
    "Return STRICT JSON: {\"items\":[{\"keep\":true,\"title\":...,\"url\":...,"
    "\"category\":...,\"importance\":8,\"reason\":...,\"short_summary\":...}]}. "
    "Include every input item in the response, with keep=false for dropped ones."
)

STYLE_SYSTEM = (
    "You write individual posts for a Telegram tech-news channel. "
    "You receive {n} selected news items. Write one post per item. "
    "Return STRICT JSON: {{\"posts\":[{{\"title\":\"...\",\"body\":\"...\"}}]}}. "
    "The title is a short headline. The body is 2-4 sentences in Telegram Markdown. "
    "Use the Published date to accurately describe timing — do not assume an item "
    "is new or 'just released' unless the date is recent. Do not use phrases like "
    "'finally dropped' or 'just launched' unless the published date confirms it. "
    "Follow the style instructions exactly."
)


def _format_candidate(index: int, item: dict[str, Any]) -> str:
    """Render one candidate for the filter prompt."""
    parts = [f"{index}. [{item.get('source_name','?')}] {item.get('title','')}"]
    if item.get("published_at"):
        parts.append(f"   Published: {item['published_at']}")
    if item.get("url"):
        parts.append(f"   URL: {item['url']}")
    if item.get("snippet"):
        parts.append(f"   Snippet: {item['snippet']}")
    parts.append(
        f"   Signals: upvotes={item.get('upvotes') or 0}, "
        f"comments={item.get('comments') or 0}, stars={item.get('stars') or 0}, "
        f"forks={item.get('forks') or 0}, crosspost={item.get('crosspost_count') or 1}"
    )
    parts.append(f"   Score: {float(item.get('score') or 0.0):.1f}")
    return "\n".join(parts)


async def llm_filter(
    items: list[dict[str, Any]],
    lm_client: Any,
    *,
    temperature: float = 0.4,
    max_tokens: int = 800,
) -> list[dict[str, Any]]:
    """Pass A: ask the LLM to filter and annotate candidates.

    Returns the kept items (keep=true), merged with the original
    candidate's engagement fields so the digest writer has signals.
    """
    if not items:
        return []

    numbered = "\n".join(_format_candidate(i, item) for i, item in enumerate(items, start=1))
    messages = [
        {"role": "system", "content": FILTER_SYSTEM},
        {"role": "user", "content": f"Candidates:\n{numbered}"},
    ]

    raw_text, _ = await lm_client.generate(
        messages,
        temperature=temperature,
        max_tokens=max_tokens,
        response_format={"type": "json_object"},
        chat_template_kwargs={"enable_thinking": False},
    )

    # Reasoning models can emit 6 blocks before the JSON.
    cleaned = strip_think(raw_text)
    if not cleaned:
        log.warning("LLM filter returned empty visible output")
        return []

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        log.warning("LLM filter returned invalid JSON: %s\nraw: %s", exc, cleaned[:300])
        return []

    kept_raw = data.get("items") if isinstance(data, dict) else None
    if not isinstance(kept_raw, list):
        log.warning("LLM filter JSON has no 'items' list")
        return []

    # Build a title -> original-candidate index so we can carry engagement
    # signals into the filter output (the LLM only echoes title/url).
    by_title: dict[str, dict[str, Any]] = {}
    for item in items:
        key = str(item.get("title") or "").strip().lower()
        if key:
            by_title.setdefault(key, item)

    kept: list[dict[str, Any]] = []
    for entry in kept_raw:
        if not isinstance(entry, dict):
            continue
        if not entry.get("keep"):
            continue
        title = str(entry.get("title") or "").strip()
        original = by_title.get(title.lower())
        merged = dict(original) if original else {}
        # Overlay the LLM's annotations.
        merged["category"] = entry.get("category") or merged.get("category")
        merged["importance"] = entry.get("importance")
        merged["reason"] = entry.get("reason")
        merged["short_summary"] = entry.get("short_summary")
        merged["title"] = title or merged.get("title")
        merged["url"] = entry.get("url") or merged.get("url")
        kept.append(merged)

    log.info("LLM filter: %d in, %d kept", len(items), len(kept))
    return kept


def select_diverse_top_items(items: list[dict[str, Any]], max_items: int) -> list[dict[str, Any]]:
    """Pick the top N kept items, balancing categories.

    Greedy by importance (descending), but cap each category at
    ceil(max_items / 2) + 1 so one topic can't dominate the digest.
    """
    if not items:
        return []

    def importance_of(item: dict[str, Any]) -> int:
        try:
            return int(item.get("importance") or 0)
        except (TypeError, ValueError):
            return 0

    sorted_items = sorted(items, key=importance_of, reverse=True)
    max_items = max(1, int(max_items))
    cat_cap = max(2, max_items // 2 + 1)

    selected: list[dict[str, Any]] = []
    cat_counts: dict[str, int] = {}
    for item in sorted_items:
        if len(selected) >= max_items:
            break
        cat = str(item.get("category") or "Other").strip() or "Other"
        if cat_counts.get(cat, 0) >= cat_cap:
            continue
        selected.append(item)
        cat_counts[cat] = cat_counts.get(cat, 0) + 1

    # If category caps left slots unfilled, fill them with the highest-importance
    # remaining items regardless of category.
    if len(selected) < max_items:
        chosen_ids = {id(item) for item in selected}
        for item in sorted_items:
            if len(selected) >= max_items:
                break
            if id(item) in chosen_ids:
                continue
            selected.append(item)

    return selected[:max_items]


async def llm_style_posts(
    items: list[dict[str, Any]],
    lm_client: Any,
    *,
    style_prompt: str = "",
    temperature: float = 0.5,
    max_tokens: int = 8000,
) -> list[dict[str, Any]]:
    """Pass B: style individual posts from the filter output.

    Returns a list of dicts with keys: title, body, category, importance, url,
    plus the original engagement signals from the filter pass.
    """
    if not items:
        return []

    def signal_line(item: dict[str, Any]) -> str:
        bits = []
        if item.get("stars"):
            bits.append(f"{item['stars']:,} GitHub stars")
        if item.get("upvotes"):
            bits.append(f"{item['upvotes']:,} upvotes")
        if item.get("comments"):
            bits.append(f"{item['comments']:,} comments")
        if int(item.get("crosspost_count") or 1) >= 2:
            bits.append(f"cross-posted on {item.get('crosspost_count')} sources")
        return ", ".join(bits) if bits else "n/a"

    def item_block(index: int, item: dict[str, Any]) -> str:
        title = item.get("title") or "(untitled)"
        url = item.get("url") or ""
        summary = item.get("short_summary") or item.get("snippet") or ""
        reason = item.get("reason") or ""
        published = item.get("published_at") or ""
        lines = [f"{index}. Title: {title}"]
        if published:
            lines.append(f"   Published: {published}")
        if summary:
            lines.append(f"   Summary: {summary}")
        if reason:
            lines.append(f"   Why it matters: {reason}")
        lines.append(f"   Signal: {signal_line(item)}")
        if url:
            lines.append(f"   URL: {url}")
        return "\n".join(lines)

    system_content = (
        style_prompt + "\n\n"
        + STYLE_SYSTEM.replace("{n}", str(len(items)))
    )
    user_body = "\n\n".join(item_block(i, item) for i, item in enumerate(items, start=1))

    messages = [
        {"role": "system", "content": system_content},
        {"role": "user", "content": f"Selected items for styling:\n\n{user_body}"},
    ]

    raw_text, _ = await lm_client.generate(
        messages,
        temperature=temperature,
        max_tokens=max_tokens,
        response_format={"type": "json_object"},
        chat_template_kwargs={"enable_thinking": False},
    )
    cleaned = strip_think(raw_text)
    if not cleaned:
        log.warning("LLM styler returned empty visible output")
        return []

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        log.warning("LLM styler returned invalid JSON: %s\nraw: %s", exc, cleaned[:300])
        return []

    posts_raw = data.get("posts") if isinstance(data, dict) else None
    if not isinstance(posts_raw, list):
        log.warning("LLM styler JSON has no 'posts' list")
        return []

    # Merge styled posts with original item data (engagement signals, url, category).
    # Match by index: the styler receives items in order and writes posts in order.
    # Title-based matching fails because the LLM rewrites headlines.
    result: list[dict[str, Any]] = []
    for idx, entry in enumerate(posts_raw):
        if not isinstance(entry, dict):
            continue
        title = str(entry.get("title") or "").strip()
        body = str(entry.get("body") or "").strip()
        if not body:
            continue
        original = items[idx] if idx < len(items) else {}
        result.append({
            "title": title or original.get("title", ""),
            "body": body,
            "category": original.get("category", ""),
            "importance": original.get("importance"),
            "url": original.get("url", ""),
        })

    log.info("LLM styler: %d items in, %d posts out", len(items), len(result))
    return result