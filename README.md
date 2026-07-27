# newsbot

A lightweight hype-driven tech news bot. Collects candidate news from
engagement-bearing sources (Hacker News, Reddit, GitHub, Product Hunt,
Hugging Face Papers, RSS), ranks by hype signals, deduplicates across
sources, filters and styles via an OpenAI-compatible LLM, and posts
individual news items to a Telegram channel on an hourly schedule.

## What it does

```text
GENERATION (every 8h):
  collect (HN/Reddit/GitHub/PH/HF Papers/RSS)
    → filter already-seen
    → cross-source dedupe + merge engagement
    → score by hype (upvotes, comments, stars, recency, topic, crossposts)
    → LLM Pass A: filter garbage, classify, importance, one-line summary (JSON)
    → LLM Pass B: style individual posts (JSON: title + body per item)
    → store 8 posts in pending_posts table (atomic replacement)
    → mark items seen

POSTING (every 1h):
  pull oldest unposted post from pending_posts
    → post to Telegram
    → mark as posted
```

The code decides what's trending; the LLM filters and writes the posts.
Posts are delivered one at a time, spread over the interval.

## Quick start

```bash
cp .env.example .env        # fill in BOT_TOKEN, NEWS_CHANNEL_ID, LM_*, ADMIN_USER_ID
pip install -e ".[dev]"
python -m newsbot.main      # scheduled mode (stays alive, generates + posts on timers)
python -m newsbot.main --once  # one-shot mode (generates + posts all immediately)
```

Without `BOT_TOKEN` and `NEWS_CHANNEL_ID`, the bot runs in dry-run mode
(printing posts to stdout). Without `NEWS_INTERVAL_HOURS`, it runs once
and exits.

> **Note:** `NEWS_INTERVAL_HOURS=0` does NOT mean one-shot — it causes
> generation every 60 seconds. Use `--once` for one-shot mode, or leave
> `NEWS_INTERVAL_HOURS` unset for dry-run single execution.

## Bot Commands

The bot accepts commands via Telegram DM (long polling). Set
`ADMIN_USER_ID` to your Telegram user ID to enable. Only the admin
user can issue commands.

| Command | Action |
|---|---|
| `/setstyle <text>` | Update the style prompt for Pass B (post writing) |
| `/style` | Show the current style prompt |
| `/digest` | Trigger a generation cycle immediately (collect → filter → style → queue) |
| `/post` | Post the next pending post to the channel immediately |
| `/status` | Show pending posts count + schedule info |
| `/help` | List commands |

## Scheduling

The bot has a **built-in scheduler** — do NOT use external cron for
scheduled mode. The container runs three concurrent async loops
(generation, posting, bot commands) and stays alive as long as needed.

```bash
# Scheduled mode (recommended) — stays alive, handles its own timing
python -m newsbot.main
```

If you need one-shot execution (e.g. from a cron job or CI):

```cron
0 9 * * * cd /opt/newsbot && python -m newsbot.main --once
```

> **Warning:** Do NOT run `python -m newsbot.main` (without `--once`)
> from cron — it starts a long-lived process that never exits. Multiple
> invocations will create duplicate bot instances posting to the same channel.

## Environment

| Var | Purpose |
|---|---|
| `BOT_TOKEN` | Telegram bot token for posting |
| `NEWS_CHANNEL_ID` | `@channel_username` or `-100…` chat id |
| `LM_BASE` | OpenAI-compatible endpoint base including `/v1` (e.g. `https://host/v1`) |
| `LM_MODEL` | LLM model name (digest writer) |
| `LM_FILTER_MODEL` | LLM model name (filter pass; defaults to `LM_MODEL`) |
| `LM_API_KEY` | Bearer token (optional) |
| `LM_TIMEOUT` | Request timeout, seconds (default 300) |
| `NEWS_DB` | SQLite path (default `data/newsbot.sqlite`) |
| `PH_API_KEY` | Product Hunt API token (optional; skips PH if unset) |
| `GITHUB_TOKEN` | GitHub token for higher rate limits (optional) |
| `NEWS_INTERVAL_HOURS` | Hours between generation cycles (default 8; 0 = every 60s, NOT one-shot — use `--once`) |
| `NEWS_POST_INTERVAL_MINUTES` | Minutes between individual post deliveries (default 60) |
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

## Layout

```text
newsbot/
  main.py              # split generation + posting scheduler + bot command handler
  config.py            # load_config + defaults + validation (incl. style_prompt)
  db.py                # NewsStore (pending_posts, seen, migrations, retention)
  scoring.py           # hype_score (engagement × recency × weight + topics + crosspost)
  dedupe.py            # canonical URL + fuzzy title + GitHub repo + merge
  summarizer.py        # two-pass: llm_filter (JSON) + llm_style_posts (JSON per-item)
  telegram_poster.py   # httpx Bot API sendMessage, 429 retry, tag-safe 4096 split
  bot_commands.py      # long-polling command handler (/setstyle, /digest, /post, /status)
  jobs.py              # JobCoordinator (serializes generation + posting via asyncio locks)
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
- **Dependencies**: Declared in `pyproject.toml` with lower bounds.
  The Docker build runs `pip freeze --all > constraints.txt` to capture
  exact resolved versions. For fully reproducible builds, install from
  `constraints.txt` instead of resolving fresh.
- **CI**: GitHub Actions (`.github/workflows/ci.yml`) runs the test suite
  and verifies the Docker image builds and all modules import on every PR
  and push to main.

## Docker

```bash
cd deploy/docker
cp env.example .env   # fill in secrets
docker compose up -d
```