#!/usr/bin/env python3
"""Post replay: restyle recent Telegram posts through the current pipeline.

Usage (inside the news-bot container, against a DB snapshot):
    python scripts/replay_posts.py --db /tmp/snap.sqlite --limit 15 --out /tmp/replay.md
    python scripts/replay_posts.py --db /tmp/snap.sqlite --style-file new_style.txt
    python scripts/replay_posts.py --db /tmp/snap.sqlite --limit 15 --until-id 1181  # same window again
    python scripts/replay_posts.py --db /tmp/snap.sqlite --until-id 1181 --filter-compare 3

For each of the last N telegram deliveries it rebuilds the candidate from
the stored row, fetches the article (newsbot.article), reruns Pass A on
the batch for a fresh summary, and reruns Pass B per row the way
poster.deliver_one does. It prints the old post next to the new one.

Read-only: the DB opens with mode=ro; nothing is posted or written back.
It DOES call the configured LLM (LM_BASE / LM_MODEL), once for Pass A and
once per row for Pass B.

--filter-compare N runs only Pass A, N times with the article text and N
times without it (the old snippet-only input), and reports per post how
often it was kept and why it was dropped. No Pass B.

--style-file overrides the style prompt; default is the one in effect
(settings override, else config.DEFAULT_STYLE_PROMPT). Pass
--default-style to ignore the settings override and use the code default.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import newsbot.llm as llm
from core.settings_store import SettingsStore, SettingsStoreConfig
from newsbot.article import attach_extracted_text
from newsbot.config import DEFAULT_STYLE_PROMPT, load_config
from newsbot.env import resolve
from newsbot.poster import _row_to_styler_input
from newsbot.summarizer import _assign_candidate_ids, llm_filter, llm_style_posts

QUERY = """
    SELECT d.id AS d_id, d.styled_title AS old_title, d.styled_body AS old_body, p.*
    FROM deliveries d JOIN pending_posts p ON p.id = d.post_id
    WHERE d.channel = 'telegram' AND d.id <= ?
    ORDER BY d.delivered_at DESC, d.id DESC LIMIT ?
"""


def _candidate(row: dict[str, Any]) -> dict[str, Any]:
    """Rebuild a generation-time candidate from a stored row."""
    try:
        raw_json = json.loads(row.get("raw_json") or "null")
    except json.JSONDecodeError:
        raw_json = None
    if not isinstance(raw_json, dict):
        raw_json = {}
    if row.get("is_self"):
        raw_json["is_self"] = True
    if row.get("external_url"):
        raw_json.setdefault("external_url", row["external_url"])
    story_text = raw_json.get("story_text")  # HN keeps the whole Algolia hit
    return {
        "title": row["title"], "url": row["url"], "source": row.get("source") or "",
        "source_name": row.get("source_name") or "", "published_at": row.get("published_at"),
        "snippet": row.get("snippet"), "raw_text": story_text or row.get("snippet"),
        "raw_json": raw_json, "upvotes": row.get("upvotes"), "comments": row.get("comments"),
        "stars": row.get("stars"), "crosspost_count": row.get("crosspost_count") or 1,
        "score": row.get("score_at_queue") or 0.0, "_row": row,
    }


async def replay(args: argparse.Namespace) -> str:
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    until = args.until_id if args.until_id is not None else 2**62
    rows = [dict(r) for r in con.execute(QUERY, (until, args.limit))]
    con.close()

    settings = SettingsStore(SettingsStoreConfig(db_path=Path(args.db)))
    cfg = load_config(settings, resolve(None))
    if args.style_file:
        style_prompt = Path(args.style_file).read_text(encoding="utf-8").strip()
    elif args.default_style:
        style_prompt = DEFAULT_STYLE_PROMPT
    else:
        style_prompt = cfg["style_prompt"]

    cands = [_candidate(r) for r in rows]
    _assign_candidate_ids(cands)
    await attach_extracted_text(cands)
    if args.filter_compare:
        return await filter_compare(cands, cfg, args.filter_compare)
    kept = await llm_filter(
        [dict(c) for c in cands], llm.build_lm_client("filter"),
        temperature=cfg["llm_temperature"], max_tokens=cfg["llm_max_tokens_filter"],
    )
    summaries = {k["candidate_id"]: k.get("short_summary") for k in kept}

    out = [f"# Post replay: last {len(rows)} telegram posts\n", "Style prompt used:\n", f"> {style_prompt}\n"]
    style_lm = llm.build_lm_client("style")
    for c in cands:
        row = c["_row"]
        new_summary = summaries.get(c["candidate_id"])
        styled = await llm_style_posts(
            [_row_to_styler_input({**row, "summary": new_summary or row.get("summary"),
                                   "extracted_text": c.get("extracted_text")})],
            style_lm, style_prompt=style_prompt,
        )
        new = styled[0] if styled else {"title": "(styler returned nothing)", "body": ""}
        text = c.get("extracted_text") or ""
        out += [
            f"\n## d{row['d_id']} · {row.get('source_name')} · {row['url']}",
            f"Source text: {len(text)} chars"
            + ("" if new_summary else " · Pass A dropped it now (old summary used)"),
            f"New summary: {new_summary or row.get('summary')}",
            f"\nOLD: **{row['old_title']}**\n{row['old_body']}",
            f"\nNEW ({len(new['body'])} chars): **{new['title']}**\n{new['body']}",
        ]
    await llm.aclose_clients()
    return "\n".join(out) + "\n"


async def filter_compare(cands: list[dict[str, Any]], cfg: dict[str, Any], runs: int) -> str:
    """Pass A only: keep rate and drop reasons with vs without article text."""
    lm = llm.build_lm_client("filter")
    modes = {"with text": cands, "without text": [{**c, "extracted_text": None} for c in cands]}
    kept_n: dict[str, dict[str, int]] = {m: {} for m in modes}
    reasons: dict[str, dict[str, list[str]]] = {m: {} for m in modes}
    for mode, batch in modes.items():
        for _ in range(runs):
            dropped: list[dict[str, Any]] = []
            kept = await llm_filter(
                [dict(c) for c in batch], lm, dropped=dropped,
                temperature=cfg["llm_temperature"], max_tokens=cfg["llm_max_tokens_filter"],
            )
            for k in kept:
                kept_n[mode][k["candidate_id"]] = kept_n[mode].get(k["candidate_id"], 0) + 1
            for d in dropped:
                reasons[mode].setdefault(d["candidate_id"], []).append(d["reason"])
    await llm.aclose_clients()

    out = [f"# Pass A comparison: {len(cands)} posts, {runs} runs per mode\n",
           "| post | source text | kept with text | kept without text |", "|---|---|---|---|"]
    for c in cands:
        cid, row = c["candidate_id"], c["_row"]
        out.append(f"| d{row['d_id']} {row['title'][:60]} | {len(c.get('extracted_text') or '')} chars"
                   f" | {kept_n['with text'].get(cid, 0)}/{runs} | {kept_n['without text'].get(cid, 0)}/{runs} |")
    out.append("\n## Drop reasons")
    for c in cands:
        cid, row = c["candidate_id"], c["_row"]
        for mode in modes:
            for r in reasons[mode].get(cid, []):
                out.append(f"- d{row['d_id']} ({mode}): {r}")
    return "\n".join(out) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True, help="SQLite DB (use a snapshot)")
    ap.add_argument("--limit", type=int, default=15)
    ap.add_argument("--until-id", type=int, help="newest deliveries.id to include (repeat a window)")
    ap.add_argument("--style-file", help="text file with the style prompt to try")
    ap.add_argument("--default-style", action="store_true", help="use config.DEFAULT_STYLE_PROMPT")
    ap.add_argument("--filter-compare", type=int, metavar="N",
                    help="Pass A only, N runs with and N without article text")
    ap.add_argument("--out", help="write markdown here instead of stdout")
    args = ap.parse_args()
    report = asyncio.run(replay(args))
    if args.out:
        Path(args.out).write_text(report, encoding="utf-8")
    else:
        sys.stdout.write(report)


if __name__ == "__main__":
    main()
