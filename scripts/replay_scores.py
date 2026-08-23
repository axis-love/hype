#!/usr/bin/env python3
"""Score replay tool — rescoring a candidate fixture under the current config.

Usage:
    python scripts/replay_scores.py [candidates.json] [--weights '{"reddit": 0.8}']
    python scripts/replay_scores.py                    # score the current store

Loads a JSON list of Candidate dicts (default: tests/fixtures/gta6_week.json),
runs dedupe_and_merge + score_all under the active config (or a --weights
override), and prints the ranking with the FULL score breakdown per item:
engagement, recency, source_weight, topic_bonus, crosspost_bonus, origin_topic,
matched_topics, and the pick threshold (min_score).

The --weights flag accepts a JSON object of source_weights overrides, e.g.
'{"reddit": 0.8, "hn": 1.5}'. This lets you try tunings without editing config.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Ensure the repo root is importable.
REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from newsbot.collectors.base import Candidate
from newsbot.config import load_config
from newsbot.dedupe import dedupe_and_merge
from newsbot.scoring import score_all
from core.settings_store import SettingsStore, SettingsStoreConfig


def _load_candidates(path: str) -> list[dict[str, Any]]:
    """Load a JSON list of Candidate dicts from *path*."""
    with open(path) as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"Expected a JSON list, got {type(data).__name__}")
    return data


def _load_store_candidates(db_path: str) -> list[dict[str, Any]]:
    """Load candidates from the current SQLite store (no scoring, raw rows)."""
    # The store schema stores candidates as rows; we reconstruct dicts.
    # This path is for the "no arg = dump/score the current store" case.
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT title, url, source, source_name, published_at, "
        "upvotes, comments, stars, reposts, crosspost_count, "
        "engagement_score, source_weight, topic_bonus, crosspost_bonus, "
        "penalty, lookback_hours, score as score_at_queue, merged_urls "
        "FROM store ORDER BY score_at_queue DESC"
    ).fetchall()
    conn.close()

    candidates = []
    for row in rows:
        c = {
            "title": row["title"],
            "url": row["url"],
            "source": row["source"] or "hn",
            "source_name": row["source_name"] or "",
            "published_at": row["published_at"],
            "upvotes": row["upvotes"],
            "comments": row["comments"],
            "stars": row["stars"],
            "reposts": row["reposts"],
            "crosspost_count": row["crosspost_count"] or 1,
            "penalty": row["penalty"] if row["penalty"] is not None else 1.0,
            "raw_json": {},
        }
        # Restore raw_json weight for RSS items (source_weight was derived
        # from the feed weight; store it back so score_breakdown can use it).
        sw = row["source_weight"]
        if sw is not None and row["source"] == "rss":
            c["raw_json"]["weight"] = float(sw)
        candidates.append(c)
    return candidates


def _to_candidate_dicts(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ensure items are plain dicts (convert Candidate objects if needed)."""
    result = []
    for item in items:
        if isinstance(item, Candidate):
            result.append(item.to_dict())
        elif isinstance(item, dict):
            result.append(dict(item))
        else:
            raise ValueError(f"Unexpected item type: {type(item).__name__}")
    return result


def replay(
    candidates: list[dict[str, Any]],
    config: dict[str, Any],
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Run dedupe_and_merge + score_all on *candidates* under *config*.

    Returns the scored and ranked list (highest score first).
    Each item carries its 'score_breakdown' dict.
    """
    effective_now = now or datetime.now(timezone.utc)

    # Convert plain dicts to Candidate instances for dedupe (which expects
    # dict-like objects with .get()/.setitem()). Candidate.from_dict gives us
    # the validation + dict-like interface dedupe needs.
    as_candidates = [Candidate.from_dict(c) for c in candidates]

    # Dedupe + merge (cross-source aggregation, trends containment, etc.)
    merged = dedupe_and_merge(as_candidates)

    # Convert back to dicts for scoring (which works on dicts).
    scored = score_all([c.to_dict() for c in merged], config, now=effective_now)

    # Rank by score descending.
    scored.sort(key=lambda c: c.get("score", 0.0), reverse=True)
    return scored


def print_ranking(scored: list[dict[str, Any]], config: dict[str, Any]) -> None:
    """Print the ranking with full score breakdown per item."""
    min_score = float(config.get("min_score") or 35)
    threshold = min_score

    print()
    print("=" * 72)
    print(f"  SCORE REPLAY — {len(scored)} candidates after dedupe")
    print(f"  Pick threshold (min_score): {threshold:.1f}")
    print("=" * 72)
    print()

    for rank, item in enumerate(scored, 1):
        bd = item.get("score_breakdown") or {}
        score = item.get("score", 0.0)
        passes = "PASS" if score >= threshold else "----"

        title = str(item.get("title") or "")[:70]
        source = item.get("source") or ""
        source_name = item.get("source_name") or ""
        origin = bd.get("origin_topic") or "—"
        matched = bd.get("matched_topics") or []

        print(f"#{rank:2d} [{passes}] score={score:8.2f}  {title}")
        print(f"     source={source}  source_name={source_name}")
        print(
            f"     eng={bd.get('engagement', 0):.2f}  "
            f"rec={bd.get('recency', 0):.4f}  "
            f"weight={bd.get('source_weight', 0):.2f}  "
            f"topic={bd.get('topic_bonus', 0)}  "
            f"crosspost={bd.get('crosspost_bonus', 0):.0f}  "
            f"penalty={bd.get('penalty', 1):.2f}"
        )
        print(
            f"     origin_topic={origin}  matched_topics={matched}  "
            f"crosspost_count={bd.get('crosspost_count', 1)}"
        )
        # Show raw engagement inputs
        up = bd.get("upvotes") or 0
        cm = bd.get("comments") or 0
        st = bd.get("stars") or 0
        rp = bd.get("reposts") or 0
        print(f"     upvotes={up}  comments={cm}  stars={st}  reposts={rp}")
        print()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Replay scoring on a candidate fixture or the current store."
    )
    parser.add_argument(
        "candidates",
        nargs="?",
        default=None,
        help="Path to a JSON list of candidates. "
        "Default: tests/fixtures/gta6_week.json. "
        "Use 'store' to score the current SQLite store.",
    )
    parser.add_argument(
        "--weights",
        default=None,
        help='JSON object of source_weights overrides, e.g. \'{"reddit": 0.8}\'.',
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Path to the SQLite DB (for 'store' mode). Default: data/newsbot.sqlite.",
    )
    args = parser.parse_args()

    # --- Load config ---
    repo_root = Path(__file__).resolve().parents[1]
    db_path = args.db or str(repo_root / "data" / "newsbot.sqlite")
    store = SettingsStore(SettingsStoreConfig(db_path=Path(db_path)))
    config = load_config(store)

    # --- Apply --weights override ---
    if args.weights:
        overrides = json.loads(args.weights)
        weights = dict(config.get("source_weights") or {})
        weights.update(overrides)
        config["source_weights"] = weights
        print(f"  --weights override: {overrides}", file=sys.stderr)

    # --- Load candidates ---
    if args.candidates is None or args.candidates == "fixture":
        fixture_path = str(repo_root / "tests" / "fixtures" / "gta6_week.json")
    elif args.candidates == "store":
        fixture_path = None
    else:
        fixture_path = args.candidates

    if fixture_path:
        candidates = _load_candidates(fixture_path)
        print(
            f"  Loaded {len(candidates)} candidates from {fixture_path}",
            file=sys.stderr,
        )
    else:
        candidates = _load_store_candidates(db_path)
        print(f"  Loaded {len(candidates)} candidates from store", file=sys.stderr)

    # --- Replay ---
    scored = replay(candidates, config)

    # --- Print ranking ---
    print_ranking(scored, config)

    # --- Summary stats ---
    min_score = float(config.get("min_score") or 35)
    above = [c for c in scored if c.get("score", 0.0) >= min_score]
    print(f"  Above threshold ({min_score}): {len(above)}/{len(scored)}")

    # Topic diversity in top 14
    top14 = scored[:14]
    topics = set()
    for item in top14:
        bd = item.get("score_breakdown") or {}
        ot = bd.get("origin_topic")
        if ot:
            topics.add(ot)
    print(f"  Distinct origin topics in top 14: {len(topics)} — {sorted(topics)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
