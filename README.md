# newsbot

A lightweight hype-driven tech news bot. Collects candidate news from
engagement-bearing sources (Hacker News, Reddit, GitHub, Hugging Face
Papers, RSS, Google Trends), ranks by hype signals, deduplicates across
sources, filters via an OpenAI-compatible LLM, and posts the hottest
stories to a Telegram channel — styled on demand, gated by a live
temperature threshold.

## What it does

```text
GENERATION (wall-clock slots, default 05:00/09:00/13:00/17:00/21:00 NEWS_TZ):
  collect (HN/Reddit/GitHub/HF Papers/RSS/Trends)
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
| `/setrecap <text>` | Update the recap prompt |
| `/topics` | List all topic packs (on/off, boost, source counts) |
| `/topic on <name>` | Enable a topic pack (writes `news.topics` override) |
| `/topic off <name>` | Disable a topic pack (writes `news.topics` override) |
| `/sources` | Show the effective derived source blocks (what /digest will fetch) |
| `/help` | List commands |

## Scheduling

The bot has a **built-in scheduler** — do NOT use external cron for
scheduled mode. The container runs four concurrent async loops
(generation, posting, daily summary, bot commands) and stays alive as
long as needed. All times are wall-clock in `NEWS_TZ` (default
`Asia/Bangkok`).

| Job | Schedule | Notes |
|---|---|---|
| Generation | `NEWS_GEN_HOURS` (default `5,9,13,17,21`) | One digest per listed hour. **Catch-up:** if the process was down past a slot, the most recent due slot fires exactly once when it comes back. |
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
| `GITHUB_TOKEN` | GitHub token for higher rate limits (optional) |
| `REDDIT_REFRESH_TOKEN` | Reddit refresh token (Devvit app; required for real vote/comment counts) |
| `REDDIT_CLIENT_ID` | Client id for the Reddit token endpoint (optional; defaults to the Devvit CLI's public id) |
| `NEWS_TZ` | Wall-clock timezone for all schedules (default `Asia/Bangkok`) |
| `NEWS_GEN_HOURS` | Comma-separated local hours for digests (default `5,9,13,17,21`; catch-up fires once after downtime) |
| `NEWS_STORE_CAP` | Max unposted rows kept in the store (default 36). After each digest the coldest rows are evicted. Known trade-off: an evicted story stays in `seen` for `NEWS_RETENTION_SEEN_DAYS` and cannot re-enter on the same URL. |
| `NEWS_TEMP_FLOOR` | Minimum raw temperature to post (default 35) |
| `NEWS_THRESHOLD_RATIO` | Threshold = max(floor, ratio × median) (default 0.5) |
| `NEWS_MERGE_BONUS` | Ranking multiplier bonus per extra merge (default 0.2) |
| `NEWS_MERGE_CAP` | Cap on the merge multiplier (default 2.0) |
| `NEWS_TOPIC_COOLDOWN_MAX` | Max posts per `origin_topic` in the last 24h before that topic is excluded from posting (default 3, 0 disables) |
| `ADMIN_USER_ID` | Telegram user ID allowed to send bot commands (optional) |
| `LOG_LEVEL` | `INFO` (default) / `DEBUG` |
| `HYPE_API_PORT` | Port for the H4 consumer HTTP API. Unset = disabled (default). When set, consumers fetch ranked items and record deliveries via aiohttp on the same event loop. |
| `HYPE_API_KEYS` | Comma-separated `consumer:key` pairs (e.g. `girllm:abc,blog:def`). Each key is a bearer token; consumers must have a profile in config. |

## Configuration

Source lists, weights, topic boosts, and runtime parameters live in the
SQLite `settings` table under the `news` namespace. Sensible defaults ship
in `newsbot/config.py` so the bot runs with an empty settings table.

Topic packs (`newsbot/topics.py`) are the source of truth for which
subreddits, RSS feeds, GitHub queries, and topic boosts are active.
The operator can override individual packs via the settings key
`news.topics` (partial dict merged over defaults, e.g.
`{"gaming": {"enabled": true}, "ai": {"enabled": false}}`). Use
`/topics`, `/topic on|off`, and `/sources` to manage them at runtime
without a deploy.

To override other settings, set keys via SQL or seed the table:

```sql
INSERT OR REPLACE INTO settings(namespace, key, value_json, updated_at)
VALUES ('news', 'max_final_news', '8', datetime('now'));
```

Recognized keys: `topics`, `sources`, `source_weights`, `topic_boost`,
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

## Consumer API (H4)

Other agents (girllm hot takes, the blog writer) consume the same
store instead of re-collecting. The API runs in-process on the
scheduler's event loop (aiohttp) — no second DB connection, no
separate process. Started automatically when `HYPE_API_PORT` is set
and `HYPE_API_KEYS` is non-empty.

### Deployment topology

Hype stays on Nyx's server. Same-host consumers (GirlLM) use the
loopback port (`127.0.0.1:<HYPE_API_PORT>`). No public hostname,
reverse proxy, or TLS yet. Hype does not move to the axis server and
does not join the GPUBox tailnet.

When a remote consumer exists (e.g. a blog writer hosted in
Singapore), the API will be exposed via a vhost on Nyx's own server
plus a DNS record, using the same bearer keys. Consumers read
`HYPE_API_URL` from env so switching from loopback to a public hostname
is a config change, not a code change.

The server binds `0.0.0.0` inside the container; `compose.yml`
publishes on `127.0.0.1` only — so the port is reachable from the
host but not from outside.

### Authentication

`HYPE_API_KEYS` maps `consumer:key` pairs (comma-separated). Each key
is a bearer token. The API resolves `Authorization: Bearer <token>`
to a consumer name, then looks up that consumer's profile in
`config.py`.

- **401 Unauthorized** — missing `Authorization` header, malformed
  header, or the token doesn't match any key in `HYPE_API_KEYS`.
- **403 Forbidden** — the token is valid but the consumer name has no
  profile in `_consumer_profiles()` (e.g. a key was added to
  `HYPE_API_KEYS` without a matching profile section).

`/healthz` is exempt from auth — it's a liveness probe.

### Endpoints

**GET /healthz** — liveness probe, no auth required.

```bash
curl http://127.0.0.1:${HYPE_API_PORT}/healthz
# {"ok": true, "schema_version": 8}
```

**GET /api/v1/items?limit=N** — ranked eligible items for the
bearer's consumer profile. Items are topic-filtered, cooldown-excluded,
and ranked by effective temperature (raw temperature x merge
multiplier) descending. The `temperature` field in the response is the
raw temperature; the ranking applies the merge multiplier on top.
`limit` is capped by the profile's `max_candidates`.

```bash
curl -H "Authorization: Bearer ${HYPE_API_KEY}" \
     "http://127.0.0.1:${HYPE_API_PORT}/api/v1/items?limit=5"
# {"items": [{"id": 1, "title": "...", ...}, ...]}
```

**POST /api/v1/deliveries** — record a delivery. Idempotent: a repeat
POST returns 200 with `already_delivered: true`; the first `external_ref`
persists (INSERT OR IGNORE). Unknown `item_id` returns 404.

```bash
curl -X POST -H "Authorization: Bearer ${HYPE_API_KEY}" \
     -H "Content-Type: application/json" \
     -d '{"item_id": 42, "external_ref": "msg-001"}' \
     http://127.0.0.1:${HYPE_API_PORT}/api/v1/deliveries
# {"ok": true, "already_delivered": false}
```

### Item JSON shape

Every item in the `GET /api/v1/items` response:

```json
{
  "id": 42,
  "title": "Story title",
  "snippet": "One-line summary from the LLM filter pass",
  "url": "https://example.com/story",
  "source_name": "r/gaming",
  "origin_topic": "gaming",
  "matched_topics": ["gaming"],
  "temperature": 82.5,
  "upvotes": 120,
  "comments": 34,
  "published_at": "2026-09-05T08:00:00+00:00",
  "merge_count": 2,
  "collected_at": "2026-09-05T09:00:00+00:00"
}
```

`temperature` is the raw hype score; items are ranked by
`temperature * merge_multiplier(merge_count)` descending (the merge
multiplier is `min(1 + (merge_count - 1) * merge_bonus, merge_cap)`).
A row already delivered to the caller's channel is excluded from the
response.

### Per-consumer profiles

Each consumer profile lives in `_consumer_profiles()` in
`newsbot/config.py`. A profile carries its own selection knobs
(floor, ratio, cooldown, max_candidates) and an optional topic filter.
The `deliveries` table (migration 7) tracks per-consumer delivery so
each consumer sees only its own undelivered rows.

| Consumer | Channel | Topics | Floor | Ratio | Cooldown | Max |
|----------|---------|--------|-------|-------|----------|-----|
| telegram | `telegram` | all | `NEWS_TEMP_FLOOR` (35) | `NEWS_THRESHOLD_RATIO` (0.5) | `NEWS_TOPIC_COOLDOWN_MAX` (3) | `NEWS_MAX_CANDIDATES` (20) |
| girllm | `girllm` | gaming, gamedev, ai | `HYPE_CONSUMER_GIRLLM_FLOOR` (25) | `HYPE_CONSUMER_GIRLLM_RATIO` (0.3) | `HYPE_CONSUMER_GIRLLM_COOLDOWN_MAX` (2) | `HYPE_CONSUMER_GIRLLM_MAX_CANDIDATES` (5) |
| blog | `blog` | science, new_research, ai | `HYPE_CONSUMER_BLOG_FLOOR` (55) | `HYPE_CONSUMER_BLOG_RATIO` (0.8) | `HYPE_CONSUMER_BLOG_COOLDOWN_MAX` (3) | `HYPE_CONSUMER_BLOG_MAX_CANDIDATES` (5) |

Eviction is global (`evict_coldest` never deletes a row with ANY
delivery in any channel — delivered rows are protected).

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
  bot_commands.py      # long-polling command handler (/preview, /recap, /topics, /topic, /sources, /digest, /post, /scores, /status, /summary, …)
  jobs.py              # JobCoordinator (serializes gen + posting + summary via asyncio lock)
  collectors/
    hackernews.py reddit.py github.py rss.py huggingface_papers.py trends.py
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