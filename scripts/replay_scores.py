#!/usr/bin/env python3
"""Score replay tool — rescoring a candidate fixture under the current config.

Usage:
    python scripts/replay_scores.py                    # score the current store
    python scripts/replay_scores.py fixture            # score the GTA6 fixture
    python scripts/replay_scores.py path/to/cands.json
    python scripts/replay_scores.py --weights '{"reddit": 0.8}'

No argument (the default) scores the current SQLite store via the db
module's own list_store_rows() accessor — the canonical path that the
poster uses. Store rows are mapped to candidate dicts carrying their raw
engagement fields (upvotes, comments, stars, reposts) so score_all
recomputes a fresh breakdown under the current config.

The 'fixture' keyword or any path arg loads a JSON list of Candidate dicts.
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
from newsbot.db import NewsStore
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
    """Load candidates from the current SQLite store via NewsStore.

    Uses list_store_rows() — the same accessor the poster uses — so the
    replay never references a table name or column set that could drift
    from the schema. Store rows are mapped to candidate dicts carrying
    their raw engagement fields (upvotes, comments, stars, reposts) and
    the source_name for origin_topic lookup.
    """
    store = NewsStore(Path(db_path))
    try:
        rows = store.list_store_rows()
    finally:
        store.close()

    candidates: list[dict[str, Any]] = []
    for row in rows:
        # Map store row → candidate dict for dedupe + score_all.
        # The store persists score_breakdown components; we carry the
        # raw engagement fields so score_all recomputes a fresh score
        # under the current config (not the stale queue-time snapshot).
        raw_json: dict[str, Any] = {}
        # RSS items: source_weight was derived from the feed weight.
        # Store it in raw_json so score_breakdown can apply it.
        sw = row.get("source_weight")
        if sw is not None and row.get("source") == "rss":
            raw_json["weight"] = float(sw)

        candidates.append({
            "title": row.get("title") or "",
            "url": row.get("url") or "",
            "source": row.get("source") or "hn",
            "source_name": row.get("source_name") or "",
            "snippet": row.get("snippet"),
            "published_at": row.get("published_at"),
            "upvotes": row.get("upvotes"),
            "comments": row.get("comments"),
            "stars": row.get("stars"),
            "reposts": row.get("reposts"),
            "crosspost_count": row.get("crosspost_count") or 1,
            "penalty": row.get("penalty") if row.get("penalty") is not None else 1.0,
            "raw_json": raw_json,
        })
    return candidates


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
    as_candidates: list = [Candidate.from_dict(c) for c in candidates]

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
        description="Replay scoring on the current store or a candidate fixture."
    )
    parser.add_argument(
        "candidates",
        nargs="?",
        default=None,
        help="Path to a JSON list of candidates, 'fixture' for the GTA6 "
        "week fixture, or omit to score the current SQLite store (default).",
    )
    parser.add_argument(
        "--weights",
        default=None,
        help='JSON object of source_weights overrides, e.g. \'{"reddit": 0.8}\'.',
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Path to the SQLite DB (for store mode). Default: data/newsbot.sqlite.",
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
    # No arg = score the current store (the brief's spec).
    # 'fixture' = the GTA6 week fixture.
    # Anything else = a path to a JSON file.
    if args.candidates is None:
        candidates = _load_store_candidates(db_path)
        print(f"  Loaded {len(candidates)} candidates from store", file=sys.stderr)
    elif args.candidates == "fixture":
        fixture_path = str(repo_root / "tests" / "fixtures" / "gta6_week.json")
        candidates = _load_candidates(fixture_path)
        print(
            f"  Loaded {len(candidates)} candidates from {fixture_path}",
            file=sys.stderr,
        )
    else:
        candidates = _load_candidates(args.candidates)
        print(
            f"  Loaded {len(candidates)} candidates from {args.candidates}",
            file=sys.stderr,
        )

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
    topics: set[str] = set()
    for item in top14:
        bd = item.get("score_breakdown") or {}
        ot = bd.get("origin_topic")
        if ot:
            topics.add(ot)
    print(f"  Distinct origin topics in top 14: {len(topics)} — {sorted(topics)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
