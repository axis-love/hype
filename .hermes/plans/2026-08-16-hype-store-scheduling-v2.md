# Hype v2: Medium-Neutral News Store, Style-at-Pick, Temperature-Gated Posting & Wall-Clock Schedule

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.
> Supersedes `2026-08-15_222605-hype-store-scheduling.md` (v1). Reviewed 2026-08-16; all v1
> adjustments from the review are folded in.

**Goal:** Replace the wipe-and-replace queue with an additive, **medium-neutral** news store
filled by a 12h digest and drained by hourly temperature-ranked, threshold-gated posting that
**styles at pick time**, plus a daily 13:00 summary — all on a wall-clock schedule (default
Asia/Bangkok, env-overridable).

**Why medium-neutral (changed from v1):** hype is becoming a shared engine — Telegram channel
today, girllm hot_take feeder and the feed.axis.love blog writer next. The store therefore holds
**scored raw stories** (title, url, snippet, source, engagement, score components), not styled
Telegram prose. Each consumer styles for its own medium at delivery time. Side effects, all good:

- LLM styling cost drops from ≤28 styled items/day to ≤12 (only stories that actually post);
  evicted / threshold-rejected stories never pay styling cost.
- v1's "merge but skip styling" special case disappears — a merge is just a data update.
- girllm and the blog get raw material (snippet, source_name, raw_json) instead of nothing.

**Architecture:** `pending_posts` becomes a *store* of raw scored stories: the digest appends
(never clears), duplicates merge into existing rows (`merge_count`), and the poster recalculates
every row's temperature at pick time, applies a merge multiplier, gates on an adaptive threshold,
**styles the winner with one LLM call**, posts it — or skips the slot. Interval scheduling is
replaced with slot-based wall-clock scheduling.

**Tech stack:** Python 3.11, sqlite3 (migration 4), asyncio, zoneinfo + pip `tzdata` (Debian slim
has no system tzdata), Telegram Bot API, pytest.

**Locked decisions (Anton, 2026-08-16):**
- Timezone: Asia/Bangkok **default**, overridable via `NEWS_TZ` (changed from v1 hardcode)
- Posting: 1 post per even hour → 12 posts/day (00:00, 02:00, …, 22:00 local)
- Summary: independent extra post at 13:00 local daily (13 total sends/day)
- Digest: every 12h, additive (unposted survivors keep their chance)
- Store is medium-neutral; styling happens at pick time (v2 review)
- Threshold & merge knobs: as below

---

## 1. Current state (verified in code + live DB)

| Aspect | Today | Where |
|---|---|---|
| Generation | every 8h, `replace_unposted_batch()` **deletes all unposted rows** then inserts fresh 8 styled posts | `main.py:438-444`, `db.py:399-501` |
| Posting | every 60min, `get_next_pending_post()` = oldest-first FIFO, no temp recalc | `db.py:377-387`, `jobs.py:181-250` |
| Scheduler | interval-based: elapsed-since-last vs `NEWS_INTERVAL_HOURS` / `NEWS_POST_INTERVAL_MINUTES` | `main.py:549-770` |
| Score persistence | migration 3 stores all components → recalc possible | `db.py:124-149` |
| Recalc today | duplicated inline in `_format_scores()` | `main.py:517-529` |
| Dedupe identity | canonical URL / normalized title / fuzzy>0.90 / GitHub repo key | `dedupe.py:114-181` |
| Cross-cycle dupes | not prevented — same story with new URL re-enters next cycle | `db.py:240-308` |
| Raw material | discarded — store keeps only styled title/body/url + score columns | `db.py:399-501` |
| Schema | version 3 | live DB |
| Live data | queue-time scores min 35 / med 164 / max 279; recalc at post time min 35 / med 100 / max 222; 27% posted below temp 70, 15% below 50 | last 120 posted rows |

## 2. Target behavior

1. **Digest (05:00 & 17:00 local):** collect → filter-seen → dedupe/merge → score → LLM filter →
   diverse top-14 → **match survivors against store** (matches merge into existing rows) →
   **append the rest as raw stories** (no styling) → mark all survivors seen → evict coldest above cap.
2. **Posting (every even hour local):** recalc all store temperatures → threshold gate → pick
   hottest eligible (merge multiplier affects ranking only) → **LLM-style the single winner** →
   save styled title/body onto the row → deliver → mark posted. Nothing eligible → skip slot.
3. **Summary (13:00 local):** LLM recap of rows posted in the last 24h (from their stored raw
   source data), sent as an extra post, recorded in `daily_summaries`.
4. Some hours post nothing. Intended: quality over filling the schedule.
5. **Future consumers (documented, not built):** girllm hot_take and the blog writer read the same
   store via `selection.pick_hottest()` and style for their own medium. `posted_at` means
   "delivered to the Telegram channel"; a `deliveries(post_id, channel, delivered_at)` table is the
   planned migration-5 shape when a second consumer lands — nothing in v2 may assume `posted_at`
   is the only consumption marker beyond the TG paths that already do.

## 3. Design decisions

| Knob | Value | Env override | Rationale |
|---|---|---|---|
| Timezone | Asia/Bangkok | `NEWS_TZ` | Free parameter; girllm runs Asia/Makassar, blog may differ |
| Generation slots | 05:00, 17:00 local | `NEWS_GEN_HOURS="5,17"` | Odd hours — never collide with post slots or summary |
| Post slots | even hours | — | Anton-locked |
| Store cap | 36 | `NEWS_STORE_CAP` | 2× daily inflow vs outflow; insurance against unbounded growth |
| `max_final_news` | 8 → **14** | existing `news.max_final_news` | Store must feed 12 posts/day + threshold rejects |
| Threshold | `max(floor, ratio × median(store_raw_temps))` | — | Adaptive: tightens when store is hot, floor dominates when cold |
| Threshold floor | 35 | `NEWS_TEMP_FLOOR` | = today's `min_score`; <35 historically never posted |
| Threshold ratio | 0.5 | `NEWS_THRESHOLD_RATIO` | Median ~100 → gate ~50, blocks the historically cold 15% |
| Merge multiplier | `min(1 + 0.2×(merge_count−1), 2.0)` | `NEWS_MERGE_BONUS`, `NEWS_MERGE_CAP` | Pick-time only, ranking only (not eligibility) |
| Merge engagement | per-field **max**, then **recompute** `engagement_score` via `engagement()` from merged fields | — | v1 said "copy candidate components", which could *lower* a hot row; recompute keeps `current_temperature` consistent with raw fields |
| Threshold input | raw recalc temperature | — | Keeps the two mechanisms independent |
| Styling | at pick time, winner only | — | v2 core change; see preamble |
| Summary window | rows with `posted_at` in last 24h | — | Their stored raw source data feeds the LLM |
| Summary on 0 posts | skip, log | — | Quiet day → no recap |
| Slot bookkeeping | settings keys (persist, restart-safe) | — | Task 7 |
| Gen catch-up | on startup / after downtime, fire if last gen slot older than most recent scheduled slot | — | v1 skipped this; a missed 05:00 digest would starve the store 12h and silence the channel. Generation is additive → catch-up is safe. Post/summary slots still never backfill |
| Pick logic location | `newsbot/selection.py` | — | Pure function, importable by girllm/blog without Telegram deps |

**Merge = single row.** Merging collapses duplicates into one store row; `merged_urls` JSON keeps
the audit trail.

## 4. Schema — migration 4

```sql
ALTER TABLE pending_posts ADD COLUMN merge_count INTEGER NOT NULL DEFAULT 1;
ALTER TABLE pending_posts ADD COLUMN merged_urls TEXT;   -- JSON list, audit trail
-- Raw material for style-at-pick and for future consumers (girllm, blog):
ALTER TABLE pending_posts ADD COLUMN snippet TEXT;
ALTER TABLE pending_posts ADD COLUMN source_name TEXT;
ALTER TABLE pending_posts ADD COLUMN raw_json TEXT;      -- JSON, collector payload
ALTER TABLE pending_posts ADD COLUMN styled_at TEXT;     -- set when the styler ran

CREATE TABLE IF NOT EXISTS daily_summaries(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  day TEXT NOT NULL UNIQUE,          -- 'YYYY-MM-DD' local
  posted_at TEXT NOT NULL,
  summary_text TEXT NOT NULL,
  model_used TEXT,
  item_count INTEGER
);
```

`body` stays `NOT NULL` (SQLite can't relax it without a table rebuild): **new raw rows insert
`body=''`**; the poster fills `body` + `styled_at` when it styles the winner. `body='' AND
styled_at IS NULL` = "raw, not yet styled" — document in db.py docstring. Legacy rows:
`merge_count` 1, new columns NULL — NULL-safe reads only, no special-casing.

## 5. Slot-based scheduling model

Tick every 30s (existing loop cadence). Compute `now_local = local_now()` (Task 1):

- **Gen slot:** `slot = f"{date}T{hour:02d}"` for each hour in `NEWS_GEN_HOURS`; fire if
  `settings["scheduler.last_gen_slot"] != slot`, **or** (catch-up) if `last_gen_slot` is lexically
  older than the most recent scheduled gen slot ≤ now. On success set the key to the slot that was
  due; on failure leave unset (retry next tick).
- **Post slot:** `slot = f"{date}T{hour:02d}"` when `hour % 2 == 0`; fire if
  `settings["scheduler.last_post_slot"] != slot`.
  - Delivery **success** → set key.
  - **Skip** (3 = empty store, 4 = nothing above threshold) → set key (slot consumed; no retry storm).
  - **Failure** (styler error or Telegram error) → leave unset (retry within the hour; slot lapses
    naturally at hour end — persistent LLM outage costs the slot, the story stays in the store).
  - Never backfills missed hours.
- **Summary slot:** `day = f"{date}"`; fire if `hour >= 13` and
  `settings["scheduler.last_summary_day"] != day`. Set on success/skip(0 posts); leave unset on failure.

Restart-safe: keys persist in the settings table; restart mid-hour cannot double-post.
`NEWS_INTERVAL_HOURS` / `NEWS_POST_INTERVAL_MINUTES` are removed from compose/env and from
`main()`'s dry-run check.

---

## Tasks

### Task 1: Timezone foundation — `tzdata` dep + `newsbot/clock.py`

**Objective:** Local wall-clock helpers usable everywhere, timezone from env.

**Files:** Create `newsbot/clock.py`; modify `pyproject.toml`, `constraints.txt`; test `tests/test_clock.py`.

**Steps:**
1. `pip install tzdata` into the dev venv, pin exact version into `constraints.txt` (Dockerfile
   builds with `--constraint constraints.txt` and `pip check`).
2. Failing tests:

```python
def test_local_now_default_bangkok(monkeypatch):
    monkeypatch.delenv("NEWS_TZ", raising=False)
    from newsbot.clock import local_now
    assert str(local_now().tzinfo) == "Asia/Bangkok"

def test_local_now_env_override(monkeypatch):
    monkeypatch.setenv("NEWS_TZ", "Asia/Makassar")
    from newsbot.clock import local_now
    assert str(local_now().tzinfo) == "Asia/Makassar"

def test_slot_keys():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from newsbot.clock import gen_slots, post_slot, summary_day, latest_due_gen_slot
    dt = datetime(2026, 8, 16, 14, 30, tzinfo=ZoneInfo("Asia/Bangkok"))
    assert post_slot(dt) == "2026-08-16T14"
    assert post_slot(dt.replace(hour=13)) is None          # odd hour
    assert summary_day(dt) == "2026-08-16"
    assert gen_slots("5,17") == [5, 17]
    # catch-up: most recent scheduled gen slot at or before 14:30 is 05:00 today
    assert latest_due_gen_slot(dt, [5, 17]) == "2026-08-16T05"
    assert latest_due_gen_slot(dt.replace(hour=18), [5, 17]) == "2026-08-16T17"
    assert latest_due_gen_slot(dt.replace(hour=3), [5, 17]) == "2026-08-15T17"
```

3. Implement `clock.py`: `local_now()` (reads `NEWS_TZ`, default `Asia/Bangkok`),
   `gen_slots(env_str) -> list[int]` (parse, validate 0-23), `post_slot(dt) -> str | None`
   (even hours only), `summary_day(dt) -> str`, `latest_due_gen_slot(dt, hours) -> str`
   (most recent scheduled gen slot ≤ dt, may be yesterday).
4. `pytest tests/test_clock.py -v` → PASS. Commit `feat: local wall-clock helpers (NEWS_TZ)`.

**Pitfall:** `python:3.11-slim` has **no system tzdata** — `ZoneInfo` raises
`ZoneInfoNotFoundError` without pip `tzdata`. Verify inside the built image in Task 11.

### Task 2: Migration 4 + store methods in `db.py`

**Objective:** Additive raw-story store primitives.

**Files:** Modify `newsbot/db.py`; test `tests/test_store.py` (new).

**Methods (exact signatures):**

```python
def add_stories_to_store(self, stories: list[dict], seen_items: list[dict]) -> int
    # Append RAW stories (no body — insert body=''): title, url, category, snippet,
    # source_name, raw_json (json.dumps), plus every score-component column from
    # story["score_breakdown"], merge_count default 1. Marks seen_items.
    # Single transaction, NO delete of unposted rows. Returns inserted count.

def list_store_rows(self) -> list[dict]
    # All unposted rows: id, title, url, snippet, source_name, raw_json +
    # every score-component column + merge_count, merged_urls.

def merge_into_store_row(self, row_id: int, candidate: dict, extra_url: str) -> None
    # merge_count += 1; raw engagement fields (upvotes/comments/stars/reposts) =
    # per-field max(stored, candidate); published_at = max; append extra_url to
    # merged_urls JSON (dedup). Then RECOMPUTE engagement_score via
    # scoring.engagement() from the merged raw fields (never copy either side's value),
    # refresh source_weight/topic_bonus/crosspost_*/penalty/lookback_hours from
    # candidate["score_breakdown"], and recompute score_at_queue from the rebuilt
    # components. Keeps current_temperature consistent with the stored raw fields.

def set_styled_content(self, row_id: int, title: str, body: str) -> None
    # Fill styled title/body + styled_at (utc iso). Called by the poster after styling.

def evict_coldest(self, temps: dict[int, float], cap: int) -> int
    # temps = {row_id: raw_current_temp} computed by caller; delete unposted rows
    # with lowest temps until count <= cap; returns evicted count. Never touches posted rows.

def list_posted_since(self, since_iso: str) -> list[dict]
    # posted_at >= since, ORDER BY posted_at ASC. Source rows for the summary.

def add_summary(self, day: str, text: str, model: str, item_count: int) -> None
def get_summary_for_day(self, day: str) -> dict | None
```

`replace_unposted_batch()` and `get_next_pending_post()` are deleted (callers rewritten in
Tasks 5/6). Document in the module docstring: `posted_at` = "delivered to the Telegram channel";
future consumers get a `deliveries` table (migration 5), do not overload `posted_at`.

**Tests:** additive insert preserves existing unposted rows; raw insert has `body=''`,
`styled_at NULL`; merge increments count, takes per-field max, **recomputes engagement_score
from merged fields** (regression test: stored row hotter than candidate → engagement does not
drop); merged_urls dedup; eviction removes only coldest, never posted rows; `set_styled_content`
fills body+styled_at; `list_posted_since` boundary (inclusive). TDD each.
Commit `feat: additive raw news store (migration 4)`.

### Task 3: Shared temperature recalc in `scoring.py` + pick logic in `newsbot/selection.py`

**Objective:** One recalc implementation and one pure pick function, importable by any consumer
(TG poster today; girllm hot_take and blog writer later) with zero Telegram deps.

**Files:** Modify `newsbot/scoring.py`; create `newsbot/selection.py`; refactor
`main.py::_format_scores` to call them; tests `tests/test_scoring.py` (extend),
`tests/test_selection.py` (new).

```python
# scoring.py
def current_temperature(row: dict[str, Any], config: dict[str, Any], *, now: datetime) -> float:
    """Recompute hype score for a STORE ROW (DB dict) as of `now`.
    Reuses engagement_score/source_weight/topic_bonus/crosspost_bonus/penalty from
    the row; only recency is recomputed via recency_decay(row['published_at'],
    lookback_hours=row['lookback_hours'] or config default, now=now).
    Legacy rows (engagement_score NULL) → 0.0.
    Formula identical to score_breakdown: (eng*rec*w + topic + crosspost)*penalty."""

def merge_multiplier(merge_count: int | None, *, bonus: float = 0.2, cap: float = 2.0) -> float:
    """min(1 + bonus*max(0,(count or 1)-1), cap)"""

# selection.py  (pure — no db, no telegram, no env reads; knobs are parameters)
@dataclass
class PickResult:
    row: dict | None          # chosen story, or None
    reason: str               # "picked" | "empty" | "below_threshold"
    threshold: float
    median: float
    hottest: float
    temps: dict[int, float]   # row_id -> raw temp (reusable for eviction / /scores)

def pick_hottest(rows: list[dict], config: dict, *, now: datetime,
                 floor: float, ratio: float,
                 merge_bonus: float, merge_cap: float) -> PickResult:
    """temps = current_temperature per row; threshold = max(floor, ratio*median(temps));
    eligible = raw temp >= threshold; winner = max by raw_temp * merge_multiplier.
    Merge multiplier affects RANKING only, never eligibility."""
```

**Tests:** fixture row with known components; `current_temperature` equals hand-computed value at
two different `now`s; legacy row → 0.0; multiplier None→1.0, 1→1.0, 3→1.4, 99→2.0.
`pick_hottest`: empty → "empty"; all below floor → "below_threshold" with correct threshold;
merge multiplier flips ranking between two eligible rows without making an ineligible row
eligible. Commit `feat: shared temperature recalc + pure pick logic`.

### Task 4: Store matching for incoming candidates in `dedupe.py`

**Objective:** Reuse story-identity logic to match candidates against existing store rows.

**Files:** Modify `newsbot/dedupe.py`; test `tests/test_dedupe.py` (extend).

```python
def match_candidate_to_store(candidate: dict, store_rows: list[dict]) -> dict | None:
    """Return the store row matching this candidate, or None.
    Identity checks, in order, mirroring dedupe_and_merge:
      1. GitHub repo key (via canonical URL for store rows)
      2. canonical URL (_canonical_url) against row['url'] AND each row's merged_urls
      3. normalized title exact match
      4. fuzzy title > FUZZY_THRESHOLD against row titles
    First match wins."""
```

Expose nothing else; `_canonical_url`/`_normalize_title` stay private, wrapper is the public surface.

**Tests:** same URL with tracking params matches; fuzzy title match; no false positive below
threshold; merged_urls entries match. Commit `feat: candidate-to-store identity matching`.

### Task 5: Additive raw-story generation pipeline in `main.py`

**Objective:** Digest fills the store with raw scored stories — **no styling pass**.

**Files:** Modify `newsbot/main.py` (`_run_generation` tail), `newsbot/config.py`
(`DEFAULT_RUN["max_final_news"] = 14`); test `tests/test_generation_store.py` (new; mocks the
LLM filter like existing tests — there is no styler to mock anymore).

Rewrite the tail of `_run_generation` (after `select_diverse_top_items`):

```python
# 8. Match survivors against the store.
store_rows = store.list_store_rows()
to_add, merges = [], []
for item in final:
    hit = match_candidate_to_store(item, store_rows)
    (merges if hit else to_add).append((hit, item) if hit else item)

# 9. Merges: for each (row, item): store.merge_into_store_row(row["id"], item, item["url"]).
#    Log each merge with resulting merge_count.

# 10. store.add_stories_to_store(to_add, seen_items=final)   # additive — NO delete, NO styling.
#     ALL survivors (added + merged) are marked seen: they live in the store now;
#     the v1 "styler omitted → don't mark seen" rule is obsolete.

# 11. Eviction: temps = {r["id"]: current_temperature(r, cfg, now=now) for r in store.list_store_rows()}
#     evicted = store.evict_coldest(temps, cap=int(os.getenv("NEWS_STORE_CAP", "36")))
#     Log evicted titles+temps. (Known trade-off, document in README: an evicted story
#     stays in `seen` for NEWS_RETENTION_SEEN_DAYS and cannot re-enter on the same URL.)
```

`llm_style_posts` / `_build_lm_client` are no longer called from generation (styler client moves
to Task 6). The LLM *filter* pass is unchanged. Return codes unchanged (0/1/3); empty `to_add`
with non-empty `merges` → still 0.

**Tests:** (a) additive: pre-seeded unposted rows survive a run; (b) duplicate candidate merges
instead of inserting (merge_count=2, no new row); (c) survivors marked seen; (d) eviction above
cap drops coldest; (e) no styler call anywhere in generation.
Commit `feat: additive raw digest fills news store`.

### Task 6: Temperature-gated, style-at-pick posting in `jobs.py`

**Objective:** Poster picks the hottest eligible raw story, styles it, delivers it.

**Files:** Modify `newsbot/jobs.py` (`_deliver_one`, `drain_posts`), `newsbot/config.py`
(floor/ratio/bonus/cap env reads); test `tests/test_posting_gate.py` (new).

`_deliver_one` becomes:

```python
cfg = load_config(self._settings)
rows = self._store.list_store_rows()
now = datetime.now(timezone.utc)
result = pick_hottest(
    rows, cfg, now=now,
    floor=float(os.getenv("NEWS_TEMP_FLOOR", "35")),
    ratio=float(os.getenv("NEWS_THRESHOLD_RATIO", "0.5")),
    merge_bonus=float(os.getenv("NEWS_MERGE_BONUS", "0.2")),
    merge_cap=float(os.getenv("NEWS_MERGE_CAP", "2.0")),
)
if result.reason == "empty": return 3
if result.reason == "below_threshold":
    log.info(json.dumps({"event": "post_skip", "threshold": result.threshold,
                         "median": result.median, "hottest": result.hottest}))
    return 4                                       # threshold skip (slot consumed)
row = result.row
# Style the single winner (reuse llm_style_posts with a one-item list; row dict carries
# title/url/snippet/category — same fields the styler consumed at digest time in v1).
styled = await llm_style_posts([_row_to_candidate(row)], _build_lm_client(),
                               style_prompt=cfg["style_prompt"], ...)
if not styled:
    log.error("styler failed for row id=%d — will retry within the hour", row["id"])
    return 1                                       # failure: slot NOT consumed
self._store.set_styled_content(row["id"], styled[0]["title"], styled[0]["body"])
message = format_post_message(styled[0]["title"], styled[0]["body"], row.get("url") or "")
# ... deliver via post_digest, mark_posted — same error handling as today ...
# Log post_pick JSON: threshold, median, hottest, chosen id, raw temp, merge_count.
```

Result codes: **3 = empty store, 4 = threshold skip** — the scheduler treats both as
slot-consumed; **1 = styler or delivery failure** — slot unconsumed, retry within the hour.
**`drain_posts` must treat 4 as terminal success** (like 3) — otherwise `--once`/dry-run exits
nonzero on a healthy "nothing hot enough" state.

`_build_lm_client` moves (or is imported) so jobs.py can build the styler client; keep the
dry-run stdout path working (style, print, mark posted).

**Tests:** hottest chosen from known temps; below-threshold → 4, nothing styled/posted; styler
returns [] → 1 and row stays raw+unposted; successful path fills styled_at and marks posted;
merge multiplier changes ranking not eligibility; legacy NULL-score row never selected;
`drain_posts` returns 0 when `_deliver_one` yields 4. Commit `feat: style-at-pick threshold-gated posting`.

### Task 7: Slot-based scheduler in `main.py`

**Objective:** Wall-clock slots replace elapsed-interval logic, with generation catch-up.

**Files:** Modify `newsbot/main.py` (`_scheduler_gen_iteration`, `_scheduler_post_iteration` →
slot-based; new `_scheduler_summary_iteration`); rewrite `tests/test_scheduler_bookkeeping.py`.

Per Section 5. Each iteration computes `local_now()`, derives the slot key, compares against the
settings key, fires or idles. Gen uses `latest_due_gen_slot()` for catch-up: if
`scheduler.last_gen_slot` != the most recent due slot, fire and (on success) set the key to that
due slot. Post slot even-hours, no backfill. Summary at `hour >= 13`, once per day. Settings
keys: `scheduler.last_gen_slot`, `scheduler.last_post_slot`, `scheduler.last_summary_day`.

Remove: `NEWS_INTERVAL_HOURS`/`NEWS_POST_INTERVAL_MINUTES` reads,
`DEFAULT_INTERVAL_HOURS`/`DEFAULT_POST_INTERVAL_MINUTES`, and the `main()` dry-run condition
referencing them (dry-run check becomes: no BOT_TOKEN → run once).

**Tests** (iterations accept an injected `now`): fires once per slot; second tick same slot no-op;
failure leaves slot unconsumed; codes 3 and 4 consume the post slot; restart after success does
not refire; **catch-up: last_gen_slot two days old + now=14:00 → gen fires for today's 05 slot;
post slot missed during downtime is NOT backfilled**. Commit `feat: slot-based scheduler with gen catch-up`.

### Task 8: Daily summary job

**Objective:** 13:00 recap of the last 24h of posted news.

**Files:** Modify `newsbot/summarizer.py` (new `llm_daily_summary`), `newsbot/jobs.py`
(`run_summary`), `newsbot/main.py` (wire slot + `_run_summary`); test `tests/test_summary.py` (new).

```python
# summarizer.py
SUMMARY_SYSTEM = (
    "You write the daily recap for a Telegram tech-news channel. "
    "You receive the news items posted in the last 24 hours. Write ONE post: "
    "a short headline and a body of 4-8 sentences that groups related items, "
    "highlights the biggest story first, and ends with one lesser-known gem. "
    "Same style rules as regular posts: no hype words, no emojis, plain text. "
    "Return STRICT JSON: {\"title\": \"...\", \"body\": \"...\"}."
)
async def llm_daily_summary(items: list[dict], lm_client, *, temperature, max_tokens) -> dict | None
    # items: title, category, url, snippet, score_at_queue, source. Returns {"title","body"} or None.

# jobs.py — JobCoordinator.run_summary() -> int  (0 success, 1 failure, 2 busy, 3 skipped <1 post)
# main.py — _run_summary(store, settings):
#   since = local_now() - timedelta(hours=24) → UTC iso; rows = store.list_posted_since(...)
#   if not rows: return 3
#   text = await llm_daily_summary(rows, _build_lm_client(), ...)
#   post via post_digest (format_post_message, url="") then store.add_summary(day, ...)
```

Sent to the same channel, formatted like a regular post. `daily_summaries.day` UNIQUE prevents
duplicates even if the settings key is lost.

**Tests:** 0 posted rows → skip; LLM mock → posted + recorded; double-run same day → second no-op.
Commit `feat: daily 13:00 summary post`.

### Task 9: Bot commands refresh

**Objective:** Operators can see the new mechanics.

**Files:** Modify `newsbot/bot_commands.py`, `newsbot/main.py` (handlers).

- `/scores` → rebuilt on `pick_hottest`'s PickResult (one call gives temps, threshold, median):
  effective temp, raw temp, merge_count, styled/raw flag, sorted hottest-first; threshold line on
  top. Remove the inline recalc block (`main.py:517-529`).
- `/status` → add: store count (raw vs styled), current threshold, last slot keys, last skip
  reason, summary last-run day, `NEWS_TZ` in effect.
- `/summary` (new, admin) → manual `coordinator.run_summary()`.
- `/digest` → note in reply that generation no longer styles (cheaper, faster).
- `/post` → unchanged UX; now styles at pick.
- `/help` updated.

Commit `feat: bot commands for store mechanics`.

### Task 10: Config, deployment, docs

**Objective:** Ship-ready configuration.

**Files:** `deploy/docker/compose.yml`, `deploy/docker/.env`, `deploy/docker/env.example`, `README.md`.

- compose/env: remove `NEWS_INTERVAL_HOURS`, `NEWS_POST_INTERVAL_MINUTES`; add
  `NEWS_TZ=Asia/Bangkok`, `NEWS_GEN_HOURS=5,17`, `NEWS_STORE_CAP=36`, `NEWS_TEMP_FLOOR=35`,
  `NEWS_THRESHOLD_RATIO=0.5`, `NEWS_MERGE_BONUS=0.2`, `NEWS_MERGE_CAP=2.0`
  (all `${VAR:-default}` in compose).
- README: new schedule section (digest 05/17 + catch-up, posts even hours, summary 13:00, all in
  `NEWS_TZ`); store semantics (raw stories, style-at-pick, merge, eviction, the
  evicted-story-stays-seen trade-off); threshold & merge knobs table; **engine-reuse section**:
  how girllm / the blog writer consume the store (`list_store_rows` + `selection.pick_hottest`,
  style for their own medium; `posted_at` = TG channel only; `deliveries` table is the migration-5
  plan for multi-consumer cursors).
- `news.max_final_news=14` code default (document; settings table can override).

Commit `chore: deployment config for store schedule`.

### Task 11: Full validation & rollout

**Objective:** Prove it works end-to-end before the live container switches over.

1. `pytest tests/ -v` → all green (rewritten `test_scheduler_bookkeeping.py`;
   `test_transactional_queue.py` folded into `test_store.py`).
2. Dry-run inside the **built image** (catches tzdata):
   `docker compose build && docker compose run --rm newsbot python -c "from newsbot.clock import local_now; print(local_now())"`.
3. `docker compose run --rm newsbot python -m newsbot.main --once` against a scratch DB →
   generation appends raw rows (`body=''`), drain styles + posts hottest-first, threshold log
   lines visible, drain exits 0 when the gate blocks everything.
4. Manual `/digest` + `/scores` against staging DB to eyeball temps & threshold; `/post` to
   verify style-at-pick output quality matches the old digest-time styling.
5. **Rollout:** `git pull` on host → `docker compose up -d --build`. Watch one full cycle:
   gen slot fires (or catches up) at startup, first even-hour post styles + posts the hottest,
   `/status` shows slot keys. `Applied migration 4` in logs.
6. Verify in DB: `SELECT merge_count, body, styled_at FROM pending_posts` — legacy rows
   untouched, new raw rows `body=''`, posted rows styled.

---

## Files changed (summary)

| File | Change |
|---|---|
| `newsbot/clock.py` | NEW — local-time slot helpers (`NEWS_TZ`), gen catch-up |
| `newsbot/selection.py` | NEW — pure `pick_hottest` (consumer-agnostic) |
| `newsbot/db.py` | migration 4 (merge + raw-material columns); add/merge/evict/styled/list-posted/summary methods; drop `replace_unposted_batch`, `get_next_pending_post` |
| `newsbot/scoring.py` | `current_temperature`, `merge_multiplier` |
| `newsbot/dedupe.py` | `match_candidate_to_store` |
| `newsbot/main.py` | raw additive generation tail (no styler); slot scheduler + gen catch-up; summary wiring; `/scores` refactor |
| `newsbot/jobs.py` | pick → style → deliver; codes 3/4; drain treats 4 as done; `run_summary` |
| `newsbot/summarizer.py` | `llm_daily_summary` |
| `newsbot/bot_commands.py` | `/scores`, `/status`, `/summary`, `/help` |
| `newsbot/config.py` | `max_final_news` 14; gate/merge env reads |
| `pyproject.toml` / `constraints.txt` | + `tzdata` |
| `deploy/docker/*`, `README.md` | env knobs, schedule + engine-reuse docs |
| `tests/` | new: test_clock, test_store, test_selection, test_generation_store, test_posting_gate, test_summary; rewritten: test_scheduler_bookkeeping |

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| Missing tzdata in slim image crashes scheduler at startup | Task 1 pins pip `tzdata`; Task 11 verifies inside built image |
| Styler outage at post time → missed slots | Failure leaves slot unconsumed → retries all hour; story stays in store and remains hottest next slot. Cheaper than v1's failure mode (v1 lost the whole styled batch on digest-time LLM failure) |
| Style quality drift (single-item styling vs batch) | Same prompt + same `llm_style_posts` path with a 1-item list; Task 11 step 4 eyeballs output before rollout |
| Threshold too aggressive → silent channel | Floor 35 = today's de-facto minimum; `/status` + `post_skip` JSON logs expose every skip; knobs env-tunable without rebuild |
| Store starvation after downtime | Gen catch-up fires the missed digest on startup (new in v2) |
| Merge false positives merge distinct stories | Same conservative identity as existing dedupe (fuzzy 90) — battle-tested in-batch |
| Merge lowers a hot row's temperature | Fixed by design: engagement_score recomputed from per-field maxima, never copied |
| Double summary after settings loss | `daily_summaries.day` UNIQUE is the second line of defense |
| Migration on live DB | Migration 4 is additive-only (ALTER ADD + CREATE TABLE); existing rows untouched |
| Evicted story can't re-enter on same URL for `NEWS_RETENTION_SEEN_DAYS` | Accepted; documented in README |

## Out of scope (explicit)

- Backfilling missed post/summary slots after downtime (gen catch-up IS in scope).
- The `deliveries` multi-consumer table (documented as migration-5 shape; built when girllm or
  the blog writer actually lands).
- girllm hot_take integration and the feed.axis.love writer themselves — this plan only keeps the
  store and pick logic consumable by them.
- Per-category thresholds, learned/ML threshold tuning.
- Summary styling variants (single fixed prompt for now).
