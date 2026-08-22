# newsbot

A lightweight hype-driven tech news bot. Collects candidate news from
engagement-bearing sources (Hacker News, Reddit, GitHub, Product Hunt,
Hugging Face Papers, RSS), ranks by hype signals, deduplicates across
sources, filters via an OpenAI-compatible LLM, and posts the hottest
stories to a Telegram channel — styled on demand, gated by a live
temperature threshold.

## What it does

```text
GENERATION (wall-clock slots, default 05:00 + 17:00 NEWS_TZ):
  collect (HN/Reddit/GitHub/PH/HF Papers/RSS)
    → filter already-seen
    → cross-source dedupe + merge engagement (merged rows re-score hotter)
    → score by hype (upvotes, comments, stars, recency, topic, crossposts)
    → LLM Pass A: filter garbage, classify, importance, one-line summary (JSON)
    → append RAW stories to the store (no styling yet); evict coldest past cap

POSTING (even hours, NEWS_TZ):
  pick hottest store row above the live threshold
    (threshold = max(floor, ratio × median store temperature))
    → LLM Pass B: style the single winner (title + body)
    → post to Telegram → mark posted

DAILY SUMMARY (13:00 NEWS_TZ):
  recap the last 24h of posted news → post as one recap message
```

The code decides what's trending; the LLM filters, and styles each
story exactly once, at the moment it is picked. Stories that never
reach the threshold are never styled — no wasted LLM calls.

## Quick start

```bash
cp .env.example .env        # fill in BOT_TOKEN, NEWS_CHANNEL_ID, LM_*, ADMIN_USER_ID
pip install -e ".[dev]"
python -m newsbot.main      # scheduled mode (stays alive, generates + posts on timers)
python -m newsbot.main --once  # one-shot mode (generates + posts all immediately)
```

Without `BOT_TOKEN` and `NEWS_CHANNEL_ID`, the bot runs in dry-run mode
(printing posts to stdout). Scheduled mode runs forever — use `--once`
for a single generate-and-drain pass (e.g. from cron or CI).

## Bot Commands

The bot accepts commands via Telegram DM (long polling). Set
`ADMIN_USER_ID` to your Telegram user ID to enable. Only the admin
user can issue commands.

**Preview** — sent to your DM only; nothing is posted, no DB writes:

| Command | Action |
|---|---|
| `/preview` | Style the hottest store story and show it here |
| `/recap` | Preview the daily recap here |

**Inspect:**

| Command | Action |
|---|---|
| `/status` | Show store counts, threshold, slots and schedule info |
| `/scores` | Show hype scores for all store rows (hottest first, with live threshold) |
| `/style` | Show the current style prompt |

**Run** — posts to the channel:

| Command | Action |
|---|---|
| `/digest` | Trigger a generation cycle immediately (collect → filter → store raw; styling happens at pick) |
| `/post` | Pick the hottest store story, style and post it now |
| `/summary` | Run the daily recap job now |

**Configure:**

| Command | Action |
|---|---|
| `/setstyle <text>` | Update the style prompt for post writing |
| `/help` | List commands |

## Scheduling

The bot has a **built-in scheduler** — do NOT use external cron for
scheduled mode. The container runs four concurrent async loops
(generation, posting, daily summary, bot commands) and stays alive as
long as needed. All times are wall-clock in `NEWS_TZ` (default
`Asia/Bangkok`).

| Job | Schedule | Notes |
|---|---|---|
| Generation | `NEWS_GEN_HOURS` (default `5,17`) | One digest per listed hour. **Catch-up:** if the process was down past a slot, the most recent due slot fires exactly once when it comes back. |
| Posting | Even hours (00, 02, …, 22) | One pick per slot; skipped (never backfilled) if the bot was down. |
| Daily summary | 13:00 | Recaps the last 24h of posted news. |

```bash
# Scheduled mode (recommended) — stays alive, handles its own timing
python -m newsbot.main
```

If you need one-shot execution (e.g. from a cron job or CI):

```cron
0 9 * * * cd /opt/newsbot && python -m newsbot.main --once
```

`--once` generates one digest, then drains the store (posting every row
hot enough) and exits.

> **Warning:** Do NOT run `python -m newsbot.main` (without `--once`)
> from cron — it starts a long-lived process that never exits. Multiple
> invocations will create duplicate bot instances posting to the same channel.

## Store semantics

Generation appends **raw** scored stories to a persistent store;
styling happens at pick time on the single winner (style-at-pick), so
each story costs at most one styling LLM call.

- **Merge:** when a new candidate duplicates an existing store story,
  its engagement merges into the stored row (per-field max), and the
  row re-scores hotter — a hot story seen on two sources beats the
  same story seen once. Merged rows rank higher via the merge
  multiplier (ranking only; it never makes a cold row eligible).
- **Posting gate:** each posting slot picks the hottest row whose raw
  temperature ≥ `max(NEWS_TEMP_FLOOR, NEWS_THRESHOLD_RATIO × median)`.
  Below-threshold slots post nothing — quiet hours stay quiet.
- **Eviction:** after each digest, the coldest rows past
  `NEWS_STORE_CAP` are evicted. Known trade-off: an evicted story
  stays in `seen` for `NEWS_RETENTION_SEEN_DAYS` and cannot re-enter
  on the same URL.

## Environment

| Var | Purpose |
|---|---|
| `BOT_TOKEN` | Telegram bot token for posting |
| `NEWS_CHANNEL_ID` | `@channel_username` or `-100…` chat id |
| `LM_BASE` | OpenAI-compatible endpoint base including `/v1` (e.g. `https://host/v1`) |
| `LM_MODEL` | LLM model name (digest writer) |
| `LM_FILTER_MODEL` | LLM model name (filter pass; defaults to `LM_MODEL`) |
| `LM_API_KEY` | Bearer token (required) |
| `LM_TIMEOUT` | Request timeout, seconds (default 300) |
| `NEWS_DB` | SQLite path (default `data/newsbot.sqlite`) |
| `PH_API_KEY` | Product Hunt API token (optional; skips PH if unset) |
| `GITHUB_TOKEN` | GitHub token for higher rate limits (optional) |
| `REDDIT_REFRESH_TOKEN` | Reddit refresh token (Devvit app; required for real vote/comment counts) |
| `NEWS_TZ` | Wall-clock timezone for all schedules (default `Asia/Bangkok`) |
| `NEWS_GEN_HOURS` | Comma-separated local hours for digests (default `5,17`; catch-up fires once after downtime) |
| `NEWS_STORE_CAP` | Max unposted rows kept in the store (default 36). After each digest the coldest rows are evicted. Known trade-off: an evicted story stays in `seen` for `NEWS_RETENTION_SEEN_DAYS` and cannot re-enter on the same URL. |
| `NEWS_TEMP_FLOOR` | Minimum raw temperature to post (default 35) |
| `NEWS_THRESHOLD_RATIO` | Threshold = max(floor, ratio × median) (default 0.5) |
| `NEWS_MERGE_BONUS` | Ranking multiplier bonus per extra merge (default 0.2) |
| `NEWS_MERGE_CAP` | Cap on the merge multiplier (default 2.0) |
| `ADMIN_USER_ID` | Telegram user ID allowed to send bot commands (optional) |
| `LOG_LEVEL` | `INFO` (default) / `DEBUG` |

## Configuration

Source lists, weights, topic boosts, and runtime parameters live in the
SQLite `settings` table under the `news` namespace. Sensible defaults ship
in `newsbot/config.py` so the bot runs with an empty settings table. To
override, set keys via SQL or seed the table:

```sql
INSERT OR REPLACE INTO settings(namespace, key, value_json, updated_at)
VALUES ('news', 'max_final_news', '8', datetime('now'));
```

Recognized keys: `sources`, `source_weights`, `topic_boost`,
`lookback_hours`, `max_candidates`, `max_final_news`, `min_score`,
`source_quota`, `item_prune_hours`, `llm_temperature`,
`llm_max_tokens_filter`, `llm_max_tokens_digest`, `style_prompt`.

## Database Migrations & Backup

The bot uses a lightweight SQLite migration system (`db.py`) that tracks
applied migrations in a `schema_version` table. Migrations run
automatically on startup.

### Backup

Before upgrading or running manual migrations:

```bash
# Stop the bot
docker compose down

# Back up the database
cp data/newsbot.sqlite data/newsbot.sqlite.bak.$(date +%Y%m%d)
```

### Rollback

If a migration fails or causes issues:

```bash
# Stop the bot
docker compose down

# Restore from backup
cp data/newsbot.sqlite.bak.YYYYMMDD data/newsbot.sqlite

# Start the bot — it will detect the older schema and apply any missing migrations
docker compose up -d
```

### Retention

Retention cleanup runs after every generation cycle (scheduled, manual
`/digest`, `--once`, and dry-run). Posted posts, seen entries, and digests
older than configurable thresholds are pruned:

| Env Var | Default | Description |
|---------|---------|-------------|
| `NEWS_RETENTION_POSTED_DAYS` | 30 | Days to keep posted posts |
| `NEWS_RETENTION_SEEN_DAYS` | 14 | Days to keep seen entries |
| `NEWS_RETENTION_DIGEST_DAYS` | 90 | Days to keep old digests |

## Reusing the engine (girllm, blog writer)

The store and pick logic are deliberately consumer-agnostic. Other
agents (girllm hot takes, the blog writer) can consume the same store
instead of re-collecting:

- Read candidates with `NewsStore.list_store_rows()` and pick with
  `newsbot.selection.pick_hottest()` — the same pure function the
  Telegram poster uses. Style the winner for your own medium with
  `summarizer.llm_style_posts()`.
- `posted_at` marks delivery **to the Telegram channel only**. It is
  not a global "consumed" flag — other consumers may still pick a
  TG-posted row.
- Multi-consumer cursors (per-consumer "seen" tracking) are planned as
  the migration-5 `deliveries` table; until then, consumers track
  their own consumption.

## Layout

```text
newsbot/
  main.py              # wall-clock schedulers (gen/post/summary) + handlers
  clock.py             # NEWS_TZ wall-clock helpers (slots, day keys)
  selection.py         # pure pick_hottest (temperature gate + merge ranking)
  config.py            # load_config + defaults + validation (incl. style_prompt)
  db.py                # NewsStore (store, seen, summaries, migrations, retention)
  scoring.py           # hype_score (engagement × recency × weight + topics + crosspost)
  dedupe.py            # canonical URL + fuzzy title + GitHub repo + merge
  summarizer.py        # llm_filter + llm_style_posts + llm_daily_summary
  telegram_poster.py   # httpx Bot API sendMessage, 429 retry, tag-safe 4096 split
  bot_commands.py      # long-polling command handler (/preview, /recap, /digest, /post, /scores, /status, /summary, …)
  jobs.py              # JobCoordinator (serializes gen + posting + summary via asyncio lock)
  collectors/
    hackernews.py reddit.py github.py rss.py producthunt.py huggingface_papers.py
lm_client.py           # OpenAI-compatible HTTP client with bounded retries
core/                  # settings_store, text_utils, logging_config, log_sanitizer
```

## Tests

```bash
pip install -e ".[dev]"
pytest
```

## Reproducible Builds

- **Base image**: Pinned to `python:3.11.15-slim` in `Dockerfile`.
  Update by changing the version and verifying CI passes.
- **Dependencies**: Exact pins in `pyproject.toml` (no lower-bound
  ranges). `constraints.txt` captures all transitive dependencies with
  exact versions. The Dockerfile installs with
  `pip install . --constraint constraints.txt` to ensure the same
  resolved versions every build — no fresh resolution.
- **CI**: GitHub Actions (`.github/workflows/ci.yml`) runs the test suite
  and verifies the Docker image builds and all modules import on every PR
  and push to main. Actions are pinned to immutable commit SHAs.

## Docker

```bash
cd deploy/docker
cp env.example .env   # fill in secrets
docker compose up -d
```