# Design Note: Multi-Consumer Hype Store

**Plan:** aug-27-dedupe-diversity
**Task:** flow_001126
**Date:** 2026-08-27
**Status:** Design note only — no code, no migrations.

## 1. Current Contract Recap: posted_at = "delivered to the Telegram channel"

**Definition** (db.py:24): `posted_at` means "delivered to the Telegram
channel" — nothing else. The module docstring explicitly reserves a
future `deliveries(post_id, channel, delivered_at)` table for other
consumers and forbids overloading `posted_at` as a general consumption
marker (db.py:25-27).

**Where posted_at is SET:**

- `mark_posted(post_id, message_id=None)` (db.py:467-476) — the sole
  write path. Sets `posted_at = _utc_now_iso()` after the Telegram
  `sendMessage` / `sendRichMessage` call succeeds. Called exclusively
  from `jobs.py` (`_send_and_mark`, lines 474, 513, 540) in three
  paths: dry-run (stdout), partial-delivery fallback, and full success.

**Where posted_at is READ (current code paths):**

| Path | File:Line | Semantics |
|------|-----------|-----------|
| `list_store_rows()` | db.py:606 | `WHERE posted_at IS NULL` — pick_hottest, eviction, /scores, /store see unposted only |
| `list_merge_target_rows(days)` | db.py:632-633 | `WHERE posted_at IS NULL OR posted_at >= ?` — classification merge window (unposted + recently posted) |
| `list_posted_since(since_iso)` | db.py:817-818 | `WHERE posted_at IS NOT NULL AND posted_at >= ?` — recap input items + topic cooldown counting |
| `count_pending()` | db.py:845 | `WHERE posted_at IS NULL` — queue depth |
| `get_store_row(id)` | db.py:866 | `WHERE id=? AND posted_at IS NULL` — single unposted row fetch for /store detail |
| `list_store_ids()` | db.py:874 | `WHERE posted_at IS NULL` — valid id hints |
| `prune_posted_posts(max_age_days)` | db.py:890 | `WHERE posted_at IS NOT NULL AND posted_at < ?` — retention cleanup |
| `evict_coldest(temps, cap)` | db.py:803 | `WHERE posted_at IS NULL` — only unposted rows are evictable |
| Score replay (`_recap_input_items`) | main.py:582 | reads `posted_at` for display in dry-run report |
| Recap input sheet | summarizer.py:454-455 | `if item.get("posted_at")` — shown in preview |

**Classification merge window** (main.py:330-335):
`merge_window_days = int(os.getenv("NEWS_MERGE_WINDOW_DAYS", "7"))`
then `store.list_merge_target_rows(merge_window_days)`. A story arriving
from a different source can merge into a posted row within this window.
This is the ONE place where posted rows are visible to the pipeline
beyond pick_hottest — and it is scoped to a bounded window.

## 2. Deliveries Table Sketch

**Decision:** build this table when the second consumer actually arrives,
not before. This note captures the design so it's ready.

### Schema

```sql
CREATE TABLE deliveries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id     INTEGER NOT NULL REFERENCES pending_posts(id),
    channel     TEXT    NOT NULL,    -- 'telegram', 'girllm:gaming', 'blog'
    delivered_at TEXT   NOT NULL,    -- ISO UTC timestamp
    message_id  INTEGER,             -- channel-specific (Telegram msg_id, etc.)
    UNIQUE(post_id, channel)          -- one delivery per (row, channel)
);
CREATE INDEX ix_deliveries_post ON deliveries(post_id);
CREATE INDEX ix_deliveries_channel ON deliveries(channel);
CREATE INDEX ix_deliveries_delivered ON deliveries(delivered_at);
```

### Which reads move onto it

1. **`list_posted_since`** (db.py:811) — currently the sole source for
   "recently delivered." When the second consumer arrives, this method
   gains a `channel: str` parameter: `WHERE channel = ? AND delivered_at
   >= ?`. The caller passes its own channel name. The topic cooldown
   (jobs.py:335) and recap input (summarizer.py) each pass their channel.

2. **`list_merge_target_rows`** (db.py:610) — the merge window query
   becomes "recently delivered to THIS channel OR unposted." The cutoff
   is per-channel: `WHERE posted_at IS NULL OR id IN (SELECT post_id FROM
   deliveries WHERE channel = ? AND delivered_at >= ?)`. This redefines
   the merge-window cutoff in the ONE db method that owns it — no
   scattered queries.

3. **`mark_posted`** (db.py:467) — renamed conceptually to
   `mark_delivered(post_id, channel, message_id=None)`. Inserts into
   `deliveries` instead of setting `posted_at`. The `posted_at` column
   stays as-is for backward compatibility (legacy rows) but new writes
   go to `deliveries`. The Telegram consumer passes `channel="telegram"`.

4. **`list_store_rows`** (db.py:602) — unchanged in semantics: "rows
   not yet delivered to THIS channel." The query adds `AND id NOT IN
   (SELECT post_id FROM deliveries WHERE channel = ?)`. Each consumer
   sees only its own undelivered rows.

5. **`prune_posted_posts`** (db.py:880) — becomes
   `prune_delivered(channel, max_age_days)` or, if global cleanup is
   desired, prunes rows with NO deliveries in any channel within the
   age window.

### Migration path

When the second consumer arrives:
1. Add migration 7: create `deliveries` table + indexes.
2. Backfill: `INSERT INTO deliveries(post_id, channel, delivered_at, message_id) SELECT id, 'telegram', posted_at, message_id FROM pending_posts WHERE posted_at IS NOT NULL`.
3. `mark_posted` starts writing to `deliveries` (dual-write during transition: also set `posted_at` for backward compat).
4. Read paths migrate one at a time, each tested.

## 3. Per-Consumer Selection Config

`selection.py` (selection.py:1-7) is deliberately dependency-free: no
db, no env, no Telegram. Every knob is a parameter to `pick_hottest`
(selection.py:30-39):

```python
def pick_hottest(
    rows, config, *,
    now, floor, ratio, merge_bonus, merge_cap,
    excluded_ids: set[int] | None = None,
) -> PickResult:
```

### Per-consumer knobs

| Knob | Current default | Source | Per-consumer value |
|------|----------------|--------|-------------------|
| `floor` | 35.0 | `NEWS_TEMP_FLOOR` env (jobs.py:348) | girllm: lower (hot_take wants volume); blog: higher (only the best) |
| `ratio` | 0.5 | `NEWS_THRESHOLD_RATIO` env (jobs.py:349) | girllm: 0.3-0.4; blog: 0.8 |
| `merge_bonus` | 0.2 | `NEWS_MERGE_BONUS` env (jobs.py:350) | shared (merge signals cross-source corroboration) |
| `merge_cap` | 2.0 | `NEWS_MERGE_CAP` env (jobs.py:351) | shared |
| `excluded_ids` | None | computed by caller (jobs.py:332-344) | per-consumer: each owns its exclusion set |
| `max_candidates` | config | `cfg["max_candidates"]` | per-consumer: girllm wants 3-5; Telegram stays 14 |
| `max_final_news` | config | `cfg["max_final_news"]` | per-consumer |

### Consumer ownership model

Each consumer:
1. Reads its own env namespace (e.g. `GIRLLM_TEMP_FLOOR`,
   `BLOG_THRESHOLD_RATIO`).
2. Computes its own `excluded_ids` (same-topic cooldown per consumer,
   not shared — the blog may want to post a topic the channel already
   covered, or vice versa).
3. Calls `pick_hottest(rows, cfg, now=now, floor=..., ratio=...,
   excluded_ids=...)` with its own knobs.
4. Calls `mark_delivered(post_id, channel="girllm:gaming")` after
   delivery.

The `origin_topic` / `matched_topics` fields on each row
(selection.py:254-264, scoring.py:263-270) enable per-consumer topic
filtering: a consumer can pass `excluded_ids` for rows whose
`origin_topic` doesn't match its scope (e.g. the blog excludes
`origin_topic="gaming"`).

### Config profile shape

```python
# In config.py, a consumer profile section:
"girllm": {
    "floor": float(os.getenv("GIRLLM_TEMP_FLOOR", "25")),
    "ratio": float(os.getenv("GIRLLM_THRESHOLD_RATIO", "0.3")),
    "channel": "girllm:gaming",
    "topic_filter": ["gaming", "gamedev"],
    "cooldown_max": int(os.getenv("GIRLLM_TOPIC_COOLDOWN_MAX", "2")),
    "max_candidates": 5,
}
```

`load_config` (config.py) returns the global config; each consumer
profile is a section within it. The profile is read by the consumer's
own `_deliver_one` equivalent (mirroring jobs.py:325-353).

## 4. Mixed-Topic Store Implications

When the store carries mixed-topic collections (science packs + gaming
packs + AI packs), the shared store sees:

### Global median skew

`pick_hottest` (selection.py:59) computes `median =
statistics.median(temps.values())` across ALL rows in the store. If a
science pack delivers high-engagement items (e.g. arxiv papers with
200+ upvotes on r/science), the median inflates, and lower-engagement
gaming items fall below `threshold = max(floor, ratio * median)`. This
is already a risk with the current single-topic gaming config — mixed
topics amplify it.

**Mitigation per consumer:** each consumer passes a topic-filtered row
list to `pick_hottest`, not the full store. `list_store_rows()` returns
all unposted rows; the consumer filters by `origin_topic` before
calling `pick_hottest`. The median is then computed over the consumer's
scope, not the global store. No code change to selection.py needed —
the caller owns the row list.

### Per-topic store caps vs global NEWS_STORE_CAP=36

Currently `NEWS_STORE_CAP=36` (main.py:528) is a single global cap.
Eviction (main.py:524-531) trims the coldest rows across ALL topics
back to 36. With mixed topics, a hot science cycle could evict
gaming items that a later gaming cycle needs.

**Options (for maintainer to decide, not implemented here):**

1. **Per-topic caps:** `NEWS_STORE_CAP_GAMING=20`,
   `NEWS_STORE_CAP_SCIENCE=16` — eviction runs per topic, trimming each
   to its own cap. More complex, but fairer.
2. **Global cap with topic floor:** each topic gets a guaranteed minimum
   (e.g. 8 slots per topic); eviction only touches topics above their
   floor. Simpler than per-topic caps.
3. **Keep global cap (KISS):** if mixed-topic collection isn't happening
   yet, don't pre-solve this. Revisit when the second consumer actually
   arrives.

### Eviction fairness

`evict_coldest` (db.py:803) deletes `WHERE posted_at IS NULL` — only
unposted rows. With a deliveries table, this becomes "not delivered to
ANY channel" — rows delivered to one consumer but not another stay
evictable by a different consumer's eviction pass. This needs careful
thought: should a row delivered to girllm but not to Telegram be
evictable by Telegram's cycle? Probably not — eviction should be
global (no consumer's delivery protects a row from another consumer's
eviction) OR per-consumer (each consumer evicts from its own
undelivered set). This is an open question (see §5).

## 5. Open Questions for the Maintainer

Each question is answerable in one line:

1. **Girllm consumption semantics:** Does girllm want
   channel-unposted stories (rows not yet delivered to Telegram), or
   already-posted stories (reuse the channel's picks), or its own
   independent selection from the full store?

2. **Girllm styling/cadence:** Does girllm need its own LLM styler
   (different prompt, different tone — clickbait vs. neutral news), or
   does it reuse the channel's styled body? What cadence — per-slot
   (5/9/13/17/21), or event-driven (immediate when a hot item lands)?

3. **One shared instance vs config profiles:** Should the hype bot run
   as one process with multiple consumer profiles (config sections), or
   should girllm be a separate process reading the same DB? The
   `JobCoordinator` (jobs.py:1-7) currently serializes generation +
   posting — a second consumer in the same process would need its own
   coordinator or a lock-free read path.

4. **Blog topic-pack config:** Does the blog need its own topic pack
   (science/AI feeds, different subreddits), or does it filter from the
   shared collection by `origin_topic`? If separate packs, `load_config`
   needs a per-consumer topic pack section.

5. **Eviction scope:** Should eviction be global (no consumer's
   delivery protects a row) or per-consumer (each consumer evicts from
   its own undelivered set only)?

6. **Deliveries table timing:** Build it when girllm arrives, or
   pre-build now so the migration is ready? The db.py:24 docstring says
   "when the second consumer actually arrives" — confirm this still
   holds.

## 6. Ops Appendix: The "Post No Matter What" Bias

### Current defaults (jobs.py:348-349)

```python
floor = _env_float("NEWS_TEMP_FLOOR", "35")     # line 348
ratio = _env_float("NEWS_THRESHOLD_RATIO", "0.5")  # line 349
```

`pick_hottest` (selection.py:61): `threshold = max(floor, ratio * median)`.

### The bias

When the store median is low (e.g. 40.0), `ratio * median = 20.0`, and
`floor = 35.0` wins. Any item with temp >= 35.0 is eligible — even if
the item is mediocre. The floor becomes the effective threshold, and
the median is ignored. This means a quiet news cycle still produces a
post, because the floor is low enough that SOMETHING always clears it.

The audit (Aug 24-27, 38 posts) showed mediocre items being posted
during slow periods. The floor of 35 is low enough that almost any
scored item with moderate engagement + recency clears it.

### Recommended candidate values (env-only change, no code)

| Tuning | NEWS_TEMP_FLOOR | NEWS_THRESHOLD_RATIO | Effect |
|--------|----------------|----------------------|--------|
| Conservative | 55 | 0.5 | Floor 55 requires genuinely warm items; median still matters when store is hot |
| Strict median | 40 | 0.8 | Floor 40 as backstop; ratio 0.8 means threshold = 80% of median — only top-quartile items post |
| No floor bias | 0 | 0.6 | Pure median-relative: threshold = 60% of median. Floor disabled. Risk: empty store → threshold = 0, everything posts |
| Recommended | 55-60 | 0.8 | Floor high enough to reject mediocre items; ratio high enough that only items above 80% of median clear |

### Why not remove the floor entirely

Without a floor (`floor=0`), an empty or near-empty store has
`median ≈ 0`, so `threshold ≈ 0`, and any item with temp > 0 is
eligible. This produces low-quality posts during genuinely slow periods.
A non-zero floor acts as a quality backstop. The recommended 55-60
keeps the backstop but raises it from the current 35.

### How to change

Env-only — no code change, no migration, no deploy needed beyond
restarting the container:

```bash
# In the container environment or .env:
NEWS_TEMP_FLOOR=55
NEWS_THRESHOLD_RATIO=0.8
```

The maintainer should try one tuning for a week, audit the posts, and
adjust. The env is read on every `_deliver_one` call (jobs.py:348-349),
so changes take effect on the next posting slot after restart.
