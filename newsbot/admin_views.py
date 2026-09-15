"""Admin /status /scores /store /digest-dry formatters."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from newsbot.config import consumer_profile, load_config
from newsbot.db import NewsStore
from newsbot.generation import GenerationPipelineResult
from newsbot.scoring import current_temperature, merge_multiplier
from newsbot.selection import select_for_consumer

log = logging.getLogger(__name__)

def _pick_snapshot(store: NewsStore, config: dict[str, Any]) -> tuple[Any, float, float, float, float]:
    """Run per-consumer selection over the current store rows (pure — no delivery).

    Routes through select_for_consumer with the telegram profile (flow_001162
    item 9) so /scores and /status share the exact same numbers the poster
    uses — one source of truth, including the 24h same-topic cooldown.

    Returns (PickResult, floor, ratio, merge_bonus, merge_cap) so callers
    (/scores, /status) share the exact same numbers the poster uses.
    """
    profile = consumer_profile(config, "telegram")
    rows = store.list_store_rows("telegram")
    now = datetime.now(timezone.utc)
    since = (now - timedelta(hours=24)).isoformat(timespec="seconds")
    deliveries_for_channel = store.list_posted_since("telegram", since)
    result = select_for_consumer(rows, deliveries_for_channel, profile, config, now=now)
    return (
        result,
        float(profile.get("floor", 35.0)),
        float(profile.get("ratio", 0.5)),
        float(profile.get("merge_bonus", 0.2)),
        float(profile.get("merge_cap", 2.0)),
    )


def _format_scores(store: NewsStore, config: dict[str, Any]) -> str:
    """Format hype scores for all store rows (for /scores command).

    Built on the same pick_hottest call the poster uses: one pass yields
    current temperatures, the live threshold, and the median. Rows are
    sorted hottest-first by EFFECTIVE temperature (raw × merge multiplier);
    legacy rows (NULL score columns) have no reconstructable temperature
    and sink to the bottom marked 'score unavailable'.
    """
    result, floor, ratio, merge_bonus, merge_cap = _pick_snapshot(store, config)
    rows = [row for row in store.list_store_rows("telegram")]
    if not rows:
        return "Store is empty."

    now = datetime.now(timezone.utc)
    lines = [
        f"Store temperatures ({len(rows)} rows)",
        f"As of: {now.isoformat(timespec='seconds')}",
        f"Threshold: {result.threshold:.1f} (floor {floor:.1f}, {ratio:.2f}× median {result.median:.1f})",
        "",
    ]

    ordered = sorted(
        rows,
        key=lambda row: result.temps.get(row["id"], 0.0)
        * merge_multiplier(row.get("merge_count"), bonus=merge_bonus, cap=merge_cap),
        reverse=True,
    )
    for i, row in enumerate(ordered, start=1):
        title = str(row.get("title") or "")[:60]
        raw_temp = result.temps.get(row["id"], 0.0)
        if row.get("engagement_score") is None:
            lines.append(f"{i}. score unavailable — queued before scoring update")
            lines.append(title)
            lines.append("")
            continue
        mult = merge_multiplier(row.get("merge_count"), bonus=merge_bonus, cap=merge_cap)
        effective = raw_temp * mult
        merge = row.get("merge_count") or 1
        source = row.get("source") or "?"
        merge_note = f" merge={merge}×{mult:.2f}" if merge > 1 else ""
        lines.append(f"{i}. {effective:.1f} eff ({raw_temp:.1f} raw{merge_note})")
        lines.append(title)
        origin = row.get("origin_topic")
        origin_str = f" | topic={origin}" if origin else ""
        lines.append(f"source={source}{origin_str} | published={(row.get('published_at') or '')[:10] or 'unknown'}")
        lines.append("")

    return "\n".join(lines).strip()


def _format_store_browse(store: NewsStore, config: dict[str, Any]) -> str:
    """Browse the store: all rows hottest-first, compact 3-line format.

    Per row: rank + effective temp, title (truncated), meta line
    (source, published, signals, merge count, raw/styled flag, snippet
    excerpt). Capped to fit a sane Telegram message.
    """
    result, floor, ratio, merge_bonus, merge_cap = _pick_snapshot(store, config)
    rows = store.list_store_rows("telegram")
    if not rows:
        return "Store is empty."

    ordered = sorted(
        rows,
        key=lambda row: result.temps.get(row["id"], 0.0)
        * merge_multiplier(row.get("merge_count"), bonus=merge_bonus, cap=merge_cap),
        reverse=True,
    )

    lines = [f"Store browse ({len(rows)} rows, hottest first):", ""]

    for i, row in enumerate(ordered, start=1):
        title = str(row.get("title") or "")[:60]
        raw_temp = result.temps.get(row["id"], 0.0)
        mult = merge_multiplier(row.get("merge_count"), bonus=merge_bonus, cap=merge_cap)
        effective = raw_temp * mult
        source = row.get("source") or "?"
        published = (row.get("published_at") or "")[:10] or "?"
        merge = row.get("merge_count") or 1

        # Signal bits: upvotes/comments/stars/reposts
        signals = []
        for sig_key, sig_label in [("upvotes", "↑"), ("comments", "💬"), ("stars", "★"), ("reposts", "↻")]:
            val = row.get(sig_key)
            if val is not None:
                signals.append(f"{sig_label}{val}")
        signal_str = " ".join(signals) if signals else "—"

        snippet = (row.get("snippet") or "")[:100]
        if len(row.get("snippet") or "") > 100:
            snippet += "…"

        merge_note = f" merge×{merge}" if merge > 1 else ""
        lines.append(f"{i}. [{row['id']}] {effective:.1f}°{merge_note}")
        lines.append(f"   {title}")
        lines.append(f"   {source} | {published} | {signal_str} | {snippet}")
        lines.append("")

    return "\n".join(lines).strip()


def _format_store_detail(store: NewsStore, row_id: int) -> str:
    """Full dump of one store row: score components, merge trail, styled content.

    Returns a helpful error with valid id hints if the row is not found.
    """
    row = store.get_story(row_id)
    if row is None:
        valid_ids = store.list_store_ids("telegram")
        if valid_ids:
            id_str = ", ".join(str(i) for i in valid_ids[:20])
            suffix = f" … ({len(valid_ids)} total)" if len(valid_ids) > 20 else ""
            return f"Row id {row_id} not found in store.\n\nValid ids: {id_str}{suffix}"
        return f"Row id {row_id} not found — store is empty."

    lines = [f"Store row {row_id}", ""]

    title = str(row.get("title") or "") or "(untitled)"
    lines.append(f"Title: {title}")
    summary = str(row.get("summary") or "")
    if summary:
        lines.append(f"Summary: {summary}")
    lines.append("")

    # Score breakdown
    lines.append("Score components:")
    score_keys = [
        ("score_at_queue", "Score at queue"),
        ("engagement_score", "Engagement"),
        ("recency_at_queue", "Recency at queue"),
        ("source_weight", "Source weight"),
        ("topic_bonus", "Topic bonus"),
        ("crosspost_bonus", "Crosspost bonus"),
        ("penalty", "Penalty"),
        ("matched_topics", "Matched topics"),
        ("origin_topic", "Origin topic"),
    ]
    for key, label in score_keys:
        val = row.get(key)
        if val is not None:
            lines.append(f"  {label}: {val}")
        else:
            lines.append(f"  {label}: —")
    lines.append("")

    # Raw signals
    lines.append("Raw signals:")
    for key, label in [("upvotes", "Upvotes"), ("comments", "Comments"),
                       ("stars", "Stars"), ("reposts", "Reposts"),
                       ("crosspost_count", "Crossposts")]:
        val = row.get(key)
        if val is not None:
            lines.append(f"  {label}: {val}")
    lines.append("")

    # Meta
    lines.append("Metadata:")
    lines.append(f"  Source: {row.get('source') or '?'}")
    lines.append(f"  URL: {row.get('url') or '(none)'}")
    lines.append(f"  Category: {row.get('category') or '(none)'}")
    lines.append(f"  Published: {row.get('published_at') or '?'}")
    lines.append(f"  Merge count: {row.get('merge_count') or 1}")

    merged_urls_raw = row.get("merged_urls")
    if merged_urls_raw:
        try:
            merged = json.loads(merged_urls_raw) if isinstance(merged_urls_raw, str) else merged_urls_raw
            if isinstance(merged, list) and merged:
                lines.append(f"  Merged URLs ({len(merged)}):")
                for url in merged[:5]:
                    lines.append(f"    {url}")
                if len(merged) > 5:
                    lines.append(f"    … ({len(merged)} total)")
        except Exception:
            lines.append(f"  Merged URLs: (parse error)")

    snippet = row.get("snippet") or ""
    if snippet:
        lines.append(f"  Snippet: {snippet[:200]}{'…' if len(snippet) > 200 else ''}")

    lines.append(f"  Scored at: {row.get('scored_at') or '(unscored)'}")
    lines.append(f"  Lookback hours: {row.get('lookback_hours') or '?'}")

    delivery = store.get_delivery(row_id, "telegram")
    if delivery is not None:
        lines.append("")
        lines.append("Telegram delivery:")
        if delivery["styled_title"]:
            lines.append(f"  styled_title: {delivery['styled_title']}")
        if delivery["styled_body"]:
            body = str(delivery["styled_body"])
            lines.append(f"  styled_body: {body[:200]}{'…' if len(body) > 200 else ''}")
        if delivery["message_id"] is not None:
            lines.append(f"  message_id: {delivery['message_id']}")

    return "\n".join(lines)


def _format_dry_run_report(result: GenerationPipelineResult) -> str:
    """Format the /digest dry-run funnel report + per-item classification."""
    lines = [
        f"Dry-run generation funnel:",
        f"  collected {result.collected}",
        f"  → unseen {result.unseen}",
        f"  → deduped {result.deduped}",
        f"  → above_min_score {result.above_min_score}",
        f"  → sent_to_filter {result.sent_to_filter}",
        f"  → llm_kept {result.llm_kept}",
        f"  → final {result.final_count}",
        "",
    ]
    if result.failed_collectors:
        lines.append(f"Failed collectors: {', '.join(result.failed_collectors)}")
        lines.append("")

    for idx, item in enumerate(result.items, start=1):
        title = (item.get("title") or "(untitled)")[:60]
        source = item.get("source") or "?"
        score = float(item.get("score") or 0.0)
        action = item.get("action") or "add"
        merge_id = item.get("merge_row_id")
        category = item.get("category") or ""
        importance = item.get("importance") or ""
        action_str = f"MERGE→row {merge_id}" if action == "merge" and merge_id else "ADD"
        cat_str = f" [{category} imp={importance}]" if category or importance else ""
        lines.append(f"{idx}. {title}")
        lines.append(f"   {source} | score={score:.1f} | {action_str}{cat_str}")
        lines.append("")

    return "\n".join(lines).strip()

