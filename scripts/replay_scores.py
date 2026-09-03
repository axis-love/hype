#!/usr/bin/env python3
"""Score replay tool — rescoring a candidate fixture under the current config.

Usage:
    python scripts/replay_scores.py                    # score the current store
    python scripts/replay_scores.py fixture            # score the GTA6 fixture
    python scripts/replay_scores.py path/to/cands.json
    python scripts/replay_scores.py fixture --weights '{"reddit": 0.8}'
    python scripts/replay_scores.py fixture --now 2026-08-23T12:00:00+00:00

No argument (the default) scores the current SQLite store via the db
module's own list_store_rows() accessor — the canonical path that the
poster uses. Store rows are mapped to candidate dicts carrying their raw
engagement fields (upvotes, comments, stars, reposts) so score_all
recomputes a fresh breakdown under the current config.

The 'fixture' keyword or any path arg loads a JSON list of candidate dicts.
Runs the FULL production pipeline: _set_pre_merge_weights → dedupe_and_merge
→ score_all → select_diverse_candidates. Prints BOTH the raw ranking and
the actual selected set, with the FULL score breakdown per item:
engagement, recency, source_weight, topic_bonus, crosspost_bonus, origin_topic,
matched_topics, and the pick threshold (min_score).

The --weights flag accepts a JSON object of source_weights overrides, e.g.
'{"reddit": 0.8, "hn": 1.5}'. This lets you try tunings without editing config.

The --now flag overrides the scoring timestamp. In fixture mode, --now defaults
to the max published_at in the fixture file — wall-clock today makes the frozen
Aug-2026 fixture decay to ~0 while the pinned test stays green.
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

from newsbot.config import load_config
from newsbot.db import NewsStore
from newsbot.dedupe import dedupe_and_merge, _set_pre_merge_weights
from newsbot.scoring import score_all
from newsbot.selection import select_diverse_candidates
from core.settings_store import default_store


def _load_candidates(path: str) -> list[dict[str, Any]]:
    """Load a JSON list of Candidate dicts from *path*."""
    with open(path) as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"Expected a JSON list, got {type(data).__name__}")
    return data


def _fixture_now(candidates: list[dict[str, Any]]) -> datetime:
    """Derive the scoring timestamp from the fixture's max published_at.

    Wall-clock today makes frozen Aug-2026 fixtures decay to ~0 recency.
    Defaulting to the fixture's max published_at keeps scores pinned to
    the capture window — the same timestamp the acceptance tests use.
    """
    max_ts: str | None = None
    for c in candidates:
        ts = c.get("published_at")
        if ts:
            s = str(ts).strip()
            if not max_ts or s > max_ts:
                max_ts = s
    if max_ts:
        try:
            return datetime.fromisoformat(max_ts.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _load_store_candidates(db_path: str) -> list[dict[str, Any]]:
    """Load candidates from a SQLite store via NewsStore.

    Uses list_store_rows() — the same accessor the poster uses — so the
    replay never references a table name or column set that could drift
    from the schema. Store rows are mapped to candidate dicts carrying
    their raw engagement fields (upvotes, comments, stars, reposts) and
    the source_name for origin_topic lookup.

    Raises FileNotFoundError if the DB file does not exist — never
    silently creates an empty store.
    """
    if not Path(db_path).exists():
        raise FileNotFoundError(
            f"Store DB not found: {db_path}. "
            f"Set NEWS_DB or pass --db <path>."
        )
    store = NewsStore(Path(db_path))
    try:
        rows = store.list_store_rows("telegram")
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
    """Run the production pipeline on *candidates* under *config*.

    Mirrors _run_generation_pipeline in main.py:
      1. _set_pre_merge_weights(config["source_weights"])
      2. dedupe_and_merge(candidates)   — takes plain dicts
      3. score_all(merged, config, now=now)
      4. sort by score descending

    Returns the scored and ranked list (highest score first).
    Each item carries its 'score_breakdown' dict.

    Does NOT run the selection stage — call select_diverse_candidates
    separately for that. Use replay_with_selection() to get both.
    """
    # 1. Set pre-merge weights from config — without this, --weights
    #    overrides don't affect primary-source selection in dedupe.
    _set_pre_merge_weights(config.get("source_weights") or {})

    # 2. Dedupe + merge. dedupe_and_merge takes plain dicts and uses
    #    .get() — no Candidate.from_dict roundtrip needed. Skipping the
    #    roundtrip also tolerates legacy source ids (e.g. "producthunt"
    #    in migrated DBs) that Candidate.from_dict would reject.
    merged = dedupe_and_merge(list(candidates))

    # 3. Score.
    effective_now = now or datetime.now(timezone.utc)
    scored = score_all(merged, config, now=effective_now)

    # 4. Rank by score descending.
    scored.sort(key=lambda c: c.get("score", 0.0), reverse=True)
    return scored


def replay_with_selection(
    candidates: list[dict[str, Any]],
    config: dict[str, Any],
    *,
    now: datetime | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Run the full pipeline including the production selection stage.

    Returns (raw_ranking, selected_set) — the raw ranking is the full
    scored list sorted by score; the selected set is what
    select_diverse_candidates picks (source quota, round-robin).
    """
    scored = replay(candidates, config, now=now)

    min_score = float(config.get("min_score") or 0.0)
    above = [c for c in scored if float(c.get("score") or 0.0) >= min_score]
    max_candidates = int(config.get("max_candidates") or 8)
    selected = select_diverse_candidates(above, max_candidates, config)
    return scored, selected


def _print_item(rank: int, item: dict[str, Any], threshold: float) -> None:
    """Print one scored item with full breakdown."""
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


def print_ranking(
    scored: list[dict[str, Any]],
    selected: list[dict[str, Any]],
    config: dict[str, Any],
) -> None:
    """Print the raw ranking and the selected set with full breakdowns."""
    min_score = float(config.get("min_score") or 35)
    threshold = min_score

    # --- Raw ranking ---
    print()
    print("=" * 72)
    print(f"  SCORE REPLAY — {len(scored)} candidates after dedupe")
    print(f"  Pick threshold (min_score): {threshold:.1f}")
    print("=" * 72)
    print()

    for rank, item in enumerate(scored, 1):
        _print_item(rank, item, threshold)

    # --- Selected set (production selection stage) ---
    print("=" * 72)
    print(f"  SELECTED SET — {len(selected)} items (source quota + round-robin)")
    print("=" * 72)
    print()

    for rank, item in enumerate(selected, 1):
        _print_item(rank, item, threshold)

    # --- Summary stats ---
    above = [c for c in scored if c.get("score", 0.0) >= min_score]
    print(f"  Above threshold ({min_score}): {len(above)}/{len(scored)}")
    print(f"  Selected: {len(selected)}/{len(above)} above-threshold")

    # Topic diversity in top 14 (raw ranking)
    top14 = scored[:14]
    topics: set[str] = set()
    for item in top14:
        bd = item.get("score_breakdown") or {}
        ot = bd.get("origin_topic")
        if ot:
            topics.add(ot)
    print(f"  Distinct origin topics in top 14 (raw ranking): {len(topics)} — {sorted(topics)}")

    # Topic diversity in selected set
    sel_topics: set[str] = set()
    for item in selected:
        bd = item.get("score_breakdown") or {}
        ot = bd.get("origin_topic")
        if ot:
            sel_topics.add(ot)
    print(f"  Distinct origin topics in selected set: {len(sel_topics)} — {sorted(sel_topics)}")


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
        help="Path to the SQLite DB (for store mode). Default: NEWS_DB env or data/newsbot.sqlite.",
    )
    parser.add_argument(
        "--now",
        default=None,
        help="ISO 8601 timestamp for scoring. Fixture mode defaults to the "
        "max published_at in the fixture file. Store mode defaults to now().",
    )
    args = parser.parse_args()

    # --- Load config via default_store (honors NEWS_DB env) ---
    settings = default_store(args.db)
    config = load_config(settings)

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
        db_path = args.db or __import__("os").getenv("NEWS_DB", "data/newsbot.sqlite")
        candidates = _load_store_candidates(db_path)
        print(f"  Loaded {len(candidates)} candidates from store ({db_path})", file=sys.stderr)
        effective_now = None  # store mode uses wall-clock
    elif args.candidates == "fixture":
        fixture_path = str(REPO / "tests" / "fixtures" / "gta6_week.json")
        candidates = _load_candidates(fixture_path)
        print(
            f"  Loaded {len(candidates)} candidates from {fixture_path}",
            file=sys.stderr,
        )
        effective_now = None  # will default to fixture max below
    else:
        candidates = _load_candidates(args.candidates)
        print(
            f"  Loaded {len(candidates)} candidates from {args.candidates}",
            file=sys.stderr,
        )
        effective_now = None

    # --- Resolve --now ---
    if args.now:
        effective_now = datetime.fromisoformat(args.now.replace("Z", "+00:00"))
    elif args.candidates is not None:
        # Fixture/file mode: default to max published_at in the file.
        effective_now = _fixture_now(candidates)

    # --- Replay with selection ---
    scored, selected = replay_with_selection(
        candidates, config, now=effective_now
    )

    # --- Print ranking + selected set ---
    print_ranking(scored, selected, config)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
