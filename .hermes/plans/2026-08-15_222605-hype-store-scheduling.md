# Hype: News Store, Temperature-Gated Posting & Wall-Clock Schedule — Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Replace the wipe-and-replace queue with an additive news store that is filled by a 12h digest, drained by hourly temperature-ranked threshold-gated posting, plus a daily 13:00 summary post — all on a wall-clock Bangkok schedule.

**Architecture:** `pending_posts` becomes a *store*: the digest appends (never clears), duplicates merge into existing rows (`merge_count`), and the poster recalculates every row's temperature at pick time, applies a merge multiplier, gates on an adaptive threshold, and posts the hottest eligible row — or skips the slot. The interval-based scheduler is replaced with slot-based wall-clock scheduling in Asia/Bangkok.

**Tech Stack:** Python 3.11, sqlite3 (migration 4), asyncio, zoneinfo + pip `tzdata` (Debian slim has no system tzdata), Telegram Bot API, pytest.

**Locked decisions (Anton, 2026-08-16):**
- Timezone: Asia/Bangkok
- Posting: 1 post per even hour → 12 posts/day (00:00, 02:00, …, 22:00 BKK)
- Summary: independent extra post at 13:00 BKK daily (13 total sends/day)
- Digest: every 12h, additive (unposted survivors keep their chance)
- Threshold & merge knobs: Nyx decides (below)

---

## 1. Current state (verified in code + live DB)

| Aspect | Today | Where |
|---|---|---|
| Generation | every 8h, `replace_unposted_batch()` **deletes all unposted rows** then inserts fresh 8 | `main.py:438-444`, `db.py:399-501` |
| Posting | every 60min, `get_next_pending_post()` = oldest-first FIFO, no temp recalc | `db.py:377-387`, `jobs.py:181-250` |
| Scheduler | interval-based: elapsed-since-last vs `NEWS_INTERVAL_HOURS` / `NEWS_POST_INTERVAL_MINUTES`, tick 60s/30s | `main.py:549-770` |
| Score persistence | migration 3 stores all components (`engagement_score`, `source_weight`, `topic_bonus`, `crosspost_bonus`, `penalty`, `lookback_hours`, `published_at`) → recalc is possible | `db.py:124-149` |
| Recalc today | duplicated inline in `_format_scores()` | `main.py:517-529` |
| Dedupe identity | canonical URL / normalized title / fuzzy>0.90 / GitHub repo key, all in `dedupe.py` (private fns) | `dedupe.py:114-181` |
| Cross-cycle dupes | **not prevented** — `seen` blocks exact URL/title only; same story with new URL re-enters store next cycle | `db.py:240-308` |
| Schema | version 3 | live DB |
| Live data | queue-time scores: min 35 / med 164 / max 279; recalculated at post time: min 35 / med 100 / max 222; 27% of posts delivered below temp 70, 15% below 50 | last 120 posted rows |

## 2. Target behavior

1. **Digest (05:00 & 17:00 BKK):** collect → filter-seen → dedupe/merge → score → LLM filter → diverse top-14 → **match survivors against store** (matches merge into existing rows, skip styling) → LLM style the rest → **append** to store → evict coldest above cap.
2. **Posting (every even hour BKK):** recalc all store temperatures → threshold gate → post hottest eligible (merge multiplier affects ranking only) → mark posted. Nothing eligible → skip slot (visible in `/status`).
3. **Summary (13:00 BKK):** LLM recap of rows posted in the last 24h (using their stored source data), sent as an extra post, recorded in `daily_summaries`.
4. Some hours may post nothing. That is intended: quality over filling the schedule.

## 3. Design decisions (Nyx-owned, with rationale)

| Knob | Value | Env override | Rationale |
|---|---|---|---|
| Generation slots | 05:00, 17:00 BKK | `NEWS_GEN_HOURS="5,17"` | Odd hours — never collide with post slots or summary |
| Post slots | even hours | — | Anton-locked |
| Store cap | 36 | `NEWS_STORE_CAP` | 2× daily inflow (≤14/cycle) vs ≤12/day outflow → cap reached in ~1 day; eviction keeps strongest. Insurance against unbounded growth |
| `max_final_news` | 8 → **14** | existing `news.max_final_news` | Store must feed 12 posts/day + threshold rejects |
| Threshold | `max(floor, ratio × median(store_raw_temps))` | — | Adaptive: tightens when store is hot, floor dominates when cold |
| Threshold floor | 35 | `NEWS_TEMP_FLOOR` | = today's `min_score`; historical data shows <35 never posted anyway |
| Threshold ratio | 0.5 | `NEWS_THRESHOLD_RATIO` | With median ~100 → gate ~50, blocking exactly the 15% of historically cold posts |
| Merge multiplier | `min(1 + 0.2×(merge_count−1), 2.0)` | `NEWS_MERGE_BONUS`, `NEWS_MERGE_CAP` | Applied **at pick time only**, ranking only (not threshold eligibility). A story resurfacing across cycles is gaining hype → jumps the line |
| Merge engagement | per-field **max** | — | Same story observed later/elsewhere — max avoids double-counting (cross-source summing already happened inside `dedupe_and_merge` within a batch) |
| Threshold input | **raw** recalc temperature | — | Keeps the two mechanisms independent |
| Summary window | rows with `posted_at` in last 24h | — | Source data (title/url/category/score) from those rows feeds the LLM, per Anton's "reuse sources from store" |
| Summary on 0 posts | skip, log | — | Quiet day → no recap |
| Slot bookkeeping | settings keys (persist, restart-safe) | — | See Task 7 |

**Merge = single row.** Merging collapses duplicates into one store row, so "posting removes all merged" holds by construction; `merged_urls` JSON keeps the audit trail.

## 4. Schema — migration 4

```sql
ALTER TABLE pending_posts ADD COLUMN merge_count INTEGER NOT NULL DEFAULT 1;
ALTER TABLE pending_posts ADD COLUMN merged_urls TEXT;  -- JSON list, audit trail

CREATE TABLE IF NOT EXISTS daily_summaries(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  day TEXT NOT NULL UNIQUE,          -- 'YYYY-MM-DD' in BKK
  posted_at TEXT NOT NULL,
  summary_text TEXT NOT NULL,
  model_used TEXT,
  item_count INTEGER
);
```

Legacy rows: `merge_count` defaults 1, `merged_urls` NULL — no special-casing needed beyond NULL-safe reads.

## 5. Slot-based scheduling model

Tick every 30s (existing loop cadence). Compute `now_bkk = datetime.now(ZoneInfo("Asia/Bangkok"))`:

- **Gen slot:** `slot = f"{date}T{hour:02d}"` for each hour in `NEWS_GEN_HOURS`; fire if `settings["scheduler.last_gen_slot"] != slot`. On success set the key; on failure leave unset (retry next tick within the hour).
- **Post slot:** `slot = f"{date}T{hour:02d}"` when `hour % 2 == 0`; fire if `settings["scheduler.last_post_slot"] != slot`.
  - Delivery **success** → set key.
  - **Skip** (empty store / nothing above threshold) → set key (slot consumed; no retry storm).
  - Delivery **failure** (Telegram error) → leave unset (retry within the hour).
- **Summary slot:** `day = f"{date}"`; fire if `hour >= 13` and `settings["scheduler.last_summary_day"] != day`. Set on success/skip(0 posts); leave unset on failure.

Restart-safe: keys persist in the settings table; container restart mid-hour cannot double-post. `NEWS_INTERVAL_HOURS` / `NEWS_POST_INTERVAL_MINUTES` are removed from compose/env and from `main()`'s dry-run check.

---

## Tasks

### Task 1: Timezone foundation — `tzdata` dep + `newsbot/clock.py`

**Objective:** Bangkok wall-clock helpers usable everywhere.

**Files:** Create `newsbot/clock.py`; modify `pyproject.toml` (deps), `constraints.txt`; test `tests/test_clock.py`.

**Steps:**
1. `pip install tzdata` into the dev venv, pin exact version into `constraints.txt` (must be in constraints — Dockerfile builds with `--constraint constraints.txt` and `pip check`).
2. Failing test:

```python
def test_bkk_now_is_bangkok():
    from newsbot.clock import bkk_now
    now = bkk_now()
    assert str(now.tzinfo) == "Asia/Bangkok"

def test_slot_keys():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from newsbot.clock import gen_slots, post_slot, summary_day
    dt = datetime(2026, 8, 16, 14, 30, tzinfo=ZoneInfo("Asia/Bangkok"))
    assert post_slot(dt) == "2026-08-16T14"
    assert post_slot(dt.replace(hour=13)) is None      # odd hour
    assert summary_day(dt) == "2026-08-16"
    assert gen_slots("5,17") == [5, 17]
```

3. Implement `clock.py`: `bkk_now()`, `gen_slots(env_str) -> list[int]` (parse, validate 0-23), `post_slot(dt) -> str | None` (even hours only), `summary_day(dt) -> str`.
4. Run `pytest tests/test_clock.py -v` → PASS. Commit `feat: Bangkok wall-clock helpers`.

**Pitfall:** `python:3.11-slim` has **no system tzdata** — `ZoneInfo("Asia/Bangkok")` raises `ZoneInfoNotFoundError` without the pip `tzdata` package. Verify inside the built image in Task 11.

### Task 2: Migration 4 + store methods in `db.py`

**Objective:** Additive store primitives.

**Files:** Modify `newsbot/db.py`; test `tests/test_store.py` (new).

**Methods (exact signatures):**

```python
def add_posts_to_store(self, posts: list[dict], seen_items: list[dict]) -> int
    # Append posts (same column set as replace_unposted_batch) WITHOUT deleting
    # unposted rows; mark seen_items; single transaction; returns inserted count.
    # Each post may carry merge_count (default 1).

def list_store_rows(self) -> list[dict]
    # All unposted rows (id, title, url + every score-component column + merge_count).

def merge_into_store_row(self, row_id: int, candidate: dict, extra_url: str) -> None
    # merge_count += 1; engagement fields = max(stored, candidate);
    # published_at = max; append extra_url to merged_urls JSON (dedup);
    # refresh score_at_queue & components from candidate["score_breakdown"].

def evict_coldest(self, temps: dict[int, float], cap: int) -> int
    # temps = {row_id: raw_current_temp} computed by caller; delete unposted rows
    # with lowest temps until count <= cap; returns evicted count. Never touches posted rows.

def list_posted_since(self, since_iso: str) -> list[dict]
    # posted_at >= since, ORDER BY posted_at ASC. Source rows for the summary.

def add_summary(self, day: str, text: str, model: str, item_count: int) -> None
def get_summary_for_day(self, day: str) -> dict | None
```

`replace_unposted_batch()` and `get_next_pending_post()` are deleted (callers rewritten in Tasks 5/6).

**Tests:** additive insert preserves existing unposted rows; merge increments count, takes max engagement, dedups merged_urls; eviction removes only coldest and never posted rows; list_posted_since boundary (inclusive). TDD each. Commit `feat: additive news store (migration 4)`.

### Task 3: Shared temperature recalculation in `scoring.py`

**Objective:** One recalc implementation, used by posting, eviction, `/scores`.

**Files:** Modify `newsbot/scoring.py`, `newsbot/main.py` (`_format_scores` refactored to call it); test `tests/test_scoring.py` (extend).

```python
def current_temperature(row: dict[str, Any], config: dict[str, Any], *, now: datetime) -> float:
    """Recompute hype score for a STORE ROW (DB dict) as of `now`.
    Reuses engagement_score/source_weight/topic_bonus/crosspost_bonus/penalty
    from the row; only recency is recomputed via recency_decay(row['published_at'],
    lookback_hours=row['lookback_hours'] or config default, now=now).
    Legacy rows (engagement_score NULL) → return 0.0.
    Formula identical to score_breakdown: (eng*rec*w + topic + crosspost)*penalty."""

def merge_multiplier(merge_count: int | None, *, bonus: float = 0.2, cap: float = 2.0) -> float:
    """min(1 + bonus*max(0,(count or 1)-1), cap)"""
```

**Test:** fixture row with known components; assert `current_temperature` equals hand-computed value at two different `now`s (recency differs); legacy row → 0.0; multiplier: None→1.0, 1→1.0, 3→1.4, 99→2.0. Commit `feat: shared store temperature recalculation`.

### Task 4: Store matching for incoming candidates in `dedupe.py`

**Objective:** Reuse story-identity logic to match candidates against existing store rows.

**Files:** Modify `newsbot/dedupe.py`; test `tests/test_dedupe.py` (extend).

```python
def match_candidate_to_store(candidate: dict, store_rows: list[dict]) -> dict | None:
    """Return the store row matching this candidate, or None.
    Identity checks, in order, mirroring dedupe_and_merge:
      1. GitHub repo key (candidate only; store rows from github source carry url — match via canonical URL)
      2. canonical URL (_canonical_url) against row['url'] AND each row's merged_urls
      3. normalized title exact match
      4. fuzzy title > FUZZY_THRESHOLD against row titles
    Store rows are dicts with 'title' and 'url'. First match wins."""
```

Expose nothing else; keep `_canonical_url`/`_normalize_title` private, wrapper is the public surface.

**Tests:** same URL with tracking params matches; fuzzy title match; no false positive below threshold; merged_urls entries match. Commit `feat: candidate-to-store identity matching`.

### Task 5: Additive generation pipeline in `main.py`

**Objective:** Digest fills the store instead of replacing the queue.

**Files:** Modify `newsbot/main.py` (`_run_generation` steps 7-10), `newsbot/config.py` (`DEFAULT_RUN["max_final_news"] = 14`); test `tests/test_generation_store.py` (new, mocks LLM passes like existing tests).

Rewrite the tail of `_run_generation` (after `select_diverse_top_items`):

```python
# 7b. Match kept-but-unstyled candidates against the store BEFORE styling.
store_rows = store.list_store_rows()
to_style, merges = [], []
for item in final:
    hit = match_candidate_to_store(item, store_rows)
    if hit:
        merges.append((hit, item))      # merge, skip styling (saves LLM cost)
    else:
        to_style.append(item)

# 8. Style only to_style (llm_style_posts unchanged).

# 9. Merges: for each (row, item): store.merge_into_store_row(row["id"], item, item["url"]);
#    mark item seen. Log each merge with merge_count.

# 10. store.add_posts_to_store(posts, seen_items)   # additive — NO delete
# 11. Eviction: temps = {r["id"]: current_temperature(r, cfg, now=now) for r in store.list_store_rows()}
#     evicted = store.evict_coldest(temps, cap=int(os.getenv("NEWS_STORE_CAP", "36")))
#     Log evicted titles+temps.
```

Return codes unchanged (0/1/3). Empty `to_style` but non-empty `merges` → still success (return 0).

**Tests:** (a) additive: pre-seeded unposted rows survive a generation run; (b) duplicate candidate merges instead of inserting (merge_count=2, no second styled post — assert styler mock called only with non-matching items); (c) eviction above cap drops coldest. Commit `feat: additive digest fills news store`.

### Task 6: Temperature-ranked, threshold-gated posting in `jobs.py`

**Objective:** Poster empties the store by hottest-first with adaptive threshold.

**Files:** Modify `newsbot/jobs.py` (`_deliver_one`), `newsbot/config.py` (floor/ratio/bonus/cap env reads); test `tests/test_posting_gate.py` (new).

`_deliver_one` becomes:

```python
cfg = load_config(self._settings)
rows = self._store.list_store_rows()
if not rows: return 3                                    # skip: empty store
now = datetime.now(timezone.utc)
temps = {r["id"]: current_temperature(r, cfg, now=now) for r in rows}
floor = float(os.getenv("NEWS_TEMP_FLOOR", "35"))
ratio = float(os.getenv("NEWS_THRESHOLD_RATIO", "0.5"))
medians = statistics.median(temps.values())
threshold = max(floor, ratio * medians)
eligible = [r for r in rows if temps[r["id"]] >= threshold]
if not eligible:
    log.info("posting skipped — hottest %.1f below threshold %.1f", max(temps.values()), threshold)
    return 4                                             # NEW code: threshold skip
# rank by effective temp = raw * merge_multiplier(merge_count)
bonus = float(os.getenv("NEWS_MERGE_BONUS", "0.2")); cap = float(os.getenv("NEWS_MERGE_CAP", "2.0"))
post = max(eligible, key=lambda r: temps[r["id"]] * merge_multiplier(r.get("merge_count"), bonus=bonus, cap=cap))
# ... format_post_message, deliver, mark_posted as today ...
```

Result code **4 = threshold skip** (distinct from 3=empty): scheduler treats both as slot-consumed (Task 7); `/status` shows last skip reason. Log threshold, median, hottest, and chosen row with merge_count at INFO as JSON (`event: post_pick`).

**Tests:** mock store with rows of known temps → hottest chosen; below-threshold store returns 4 and marks nothing posted; merge multiplier changes ranking without changing eligibility; legacy NULL-score row never selected (temp 0.0). Commit `feat: hottest-first threshold-gated posting`.

### Task 7: Slot-based scheduler in `main.py`

**Objective:** Wall-clock Bangkok slots replace elapsed-interval logic.

**Files:** Modify `newsbot/main.py` (`_scheduler_gen_iteration`, `_scheduler_post_iteration` → slot-based; new `_scheduler_summary_iteration`), `tests/test_scheduler_bookkeeping.py` (rewrite).

Per Section 5: each iteration computes `bkk_now()`, derives the slot key, compares against the settings key, fires or idles. Gen slots from `NEWS_GEN_HOURS` (default `"5,17"`). Post slot even-hours. Summary slot 13:00 (see Task 8 for the job itself). Settings keys: `scheduler.last_gen_slot`, `scheduler.last_post_slot`, `scheduler.last_summary_day`.

Remove: `NEWS_INTERVAL_HOURS`/`NEWS_POST_INTERVAL_MINUTES` reads, `DEFAULT_INTERVAL_HOURS`/`DEFAULT_POST_INTERVAL_MINUTES` constants, and the `main()` dry-run condition referencing them (dry-run check becomes: no BOT_TOKEN → run once).

**Tests** (inject a fake `now` — iterations must accept a `now` param for testability): fires once per slot; second tick same slot is a no-op; failure leaves slot unconsumed; threshold-skip (code 4) consumes slot; restart after success does not refire. Commit `feat: slot-based Bangkok scheduler`.

### Task 8: Daily summary job

**Objective:** 13:00 recap of the last 24h of posted news.

**Files:** Modify `newsbot/summarizer.py` (new `llm_daily_summary`), `newsbot/jobs.py` (`run_summary` on coordinator), `newsbot/main.py` (wire slot + `_run_summary`); test `tests/test_summary.py` (new).

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
    # items: title, category, url, score_at_queue, source. Returns {"title","body"} or None.

# jobs.py — JobCoordinator.run_summary() -> int  (0 success, 1 failure, 2 busy, 3 skipped <1 post)
# main.py — _run_summary(store, settings):
#   since = (bkk_now() - timedelta(hours=24)); rows = store.list_posted_since(since UTC iso)
#   if not rows: return 3
#   text = await llm_daily_summary(rows, _build_lm_client(), ...)
#   post via post_digest (same format_post_message, url="") then store.add_summary(day, ...)
```

Summary is sent to the same channel, formatted like a regular post. `daily_summaries.day` UNIQUE prevents duplicates even if the settings key were lost.

**Tests:** <1 posted row → skip; LLM mock → posted + recorded; double-run same day → second is no-op. Commit `feat: daily 13:00 summary post`.

### Task 9: Bot commands refresh

**Objective:** Operators can see the new mechanics.

**Files:** Modify `newsbot/bot_commands.py`, `newsbot/main.py` (handlers); extend `tests/test_jobs.py` pattern for commands if present.

- `/scores` → rebuilt on `current_temperature` + `merge_multiplier`: show effective temp, raw temp, threshold (recomputed live), merge_count, sorted hottest-first. Remove the inline recalc block (`main.py:517-529`).
- `/status` → add: current threshold, last slot keys, last skip reason, summary last-run day.
- `/summary` (new, admin) → manual trigger of `coordinator.run_summary()`.
- `/help` updated.

Commit `feat: bot commands for store mechanics`.

### Task 10: Config, deployment, docs

**Objective:** Ship-ready configuration.

**Files:** `deploy/docker/compose.yml`, `deploy/docker/.env`, `deploy/docker/env.example`, `README.md`.

- compose/env: remove `NEWS_INTERVAL_HOURS`, `NEWS_POST_INTERVAL_MINUTES`; add `NEWS_GEN_HOURS=5,17`, `NEWS_TZ` unused (Bangkok hard default in clock.py — YAGNI), `NEWS_STORE_CAP=36`, `NEWS_TEMP_FLOOR=35`, `NEWS_THRESHOLD_RATIO=0.5`, `NEWS_MERGE_BONUS=0.2`, `NEWS_MERGE_CAP=2.0` (all with `${VAR:-default}` in compose).
- README: new schedule section (digest 05/17, posts even hours, summary 13:00 BKK), store semantics, threshold & merge knobs table.
- Settings DB overrides: `news.max_final_news=14` set via settings table or DEFAULT_RUN change (code default is enough — document it).

Commit `chore: deployment config for store schedule`.

### Task 11: Full validation & rollout

**Objective:** Prove it works end-to-end before the live container switches over.

1. `pytest tests/ -v` → all green (including rewritten `test_scheduler_bookkeeping.py`, updated `test_transactional_queue.py` → rename/fold into `test_store.py`).
2. Dry-run inside the **built image** (catches tzdata): `docker compose build && docker compose run --rm newsbot python -c "from newsbot.clock import bkk_now; print(bkk_now())"` → prints Bangkok time.
3. `docker compose run --rm newsbot python -m newsbot.main --once` against a scratch DB → generation appends, drain posts hottest-first, threshold log lines visible.
4. Manual `/digest` + `/scores` against staging DB to eyeball effective temps & threshold.
5. **Rollout:** `git pull` on host → `docker compose up -d --build`. Watch logs for one full cycle: gen slot fires at 05:00 BKK, first even-hour post picks hottest, `/status` shows slot keys. Migration 4 applies on startup (check `Applied migration 4` in logs).
6. Verify in DB: `SELECT merge_count FROM pending_posts` default 1; existing unposted rows (currently 0) unaffected.

---

## Files changed (summary)

| File | Change |
|---|---|
| `newsbot/clock.py` | NEW — Bangkok slot helpers |
| `newsbot/db.py` | migration 4; add/merge/evict/list-posted/summary methods; drop `replace_unposted_batch`, `get_next_pending_post` |
| `newsbot/scoring.py` | `current_temperature`, `merge_multiplier` |
| `newsbot/dedupe.py` | `match_candidate_to_store` |
| `newsbot/main.py` | additive generation tail; slot scheduler; summary wiring; `/scores` refactor |
| `newsbot/jobs.py` | threshold-gated hottest pick; `run_summary` |
| `newsbot/summarizer.py` | `llm_daily_summary` |
| `newsbot/bot_commands.py` | `/scores`, `/status`, `/summary` |
| `newsbot/config.py` | `max_final_news` 14 |
| `pyproject.toml` / `constraints.txt` | + `tzdata` |
| `deploy/docker/*`, `README.md` | env knobs, docs |
| `tests/` | new: test_clock, test_store, test_generation_store, test_posting_gate, test_summary; rewritten: test_scheduler_bookkeeping |

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| Missing tzdata in slim image crashes scheduler at startup | Task 1 pins pip `tzdata`; Task 11 verifies inside built image |
| Threshold too aggressive → silent channel | Floor 35 = today's de-facto minimum; `/status` + JSON logs expose every skip; knobs env-tunable without rebuild |
| Store starvation (threshold blocks everything for days) | Eviction only removes coldest; digest keeps adding fresh high-temp items; skip-slot is logged, not hidden |
| Merge false positives merge distinct stories | Same conservative identity as existing dedupe (fuzzy 90) — already battle-tested in-batch |
| Double summary after settings loss | `daily_summaries.day UNIQUE` constraint is the second line of defense |
| Slot missed while container down | Slot fires on next startup if the hour is still current; missed hours are intentionally not backfilled (no post storms) |
| Migration on live DB | Migration 4 is additive-only (ALTER ADD + CREATE TABLE); existing rows untouched |

## Out of scope (explicit)

- Backfilling missed slots after downtime.
- Per-category thresholds, learned/ML threshold tuning.
- Summary styling variants (single fixed prompt for now).
