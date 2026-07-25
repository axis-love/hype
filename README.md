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
    → store 8 posts in pending_posts table
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
pip install -r requirements.txt
python -m newsbot.main      # scheduled mode (stays alive, generates + posts on timers)
python -m newsbot.main --once  # one-shot mode (generates + posts all immediately)
```

Without `BOT_TOKEN` and `NEWS_CHANNEL_ID`, the bot runs in dry-run mode
(printing posts to stdout). Without `NEWS_INTERVAL_HOURS`, it runs once
and exits.

## Bot Commands

The bot accepts commands via Telegram DM (long polling). Set
`ADMIN_USER_ID` to your Telegram user ID to enable. Only the admin
user can issue commands.

| Command | Action |
|---|---|
| `/setstyle <text>` | Update the style prompt for Pass B (post writing) |
| `/style` | Show the current style prompt |
| `/run` | Trigger a generation cycle immediately |
| `/status` | Show pending posts count + schedule info |
| `/help` | List commands |

## Cron

```cron
0 9,18 * * * cd /opt/newsbot && python -m newsbot.main
```

## Environment

| Var | Purpose |
|---|---|
| `BOT_TOKEN` | Telegram bot token for posting |
| `NEWS_CHANNEL_ID` | `@channel_username` or `-100…` chat id |
| `LM_BASE` | OpenAI-compatible endpoint base (no `/v1` suffix) |
| `LM_MODEL` | LLM model name (digest writer) |
| `LM_FILTER_MODEL` | LLM model name (filter pass; defaults to `LM_MODEL`) |
| `LM_API_KEY` | Bearer token (optional) |
| `LM_TIMEOUT` | Request timeout, seconds (default 300) |
| `NEWS_DB` | SQLite path (default `data/newsbot.sqlite`) |
| `PH_API_KEY` | Product Hunt API token (optional; skips PH if unset) |
| `GITHUB_TOKEN` | GitHub token for higher rate limits (optional) |
| `NEWS_INTERVAL_HOURS` | Hours between generation cycles (default 8) |
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
`item_prune_hours`, `llm_temperature`, `llm_max_tokens_filter`,
`llm_max_tokens_digest`, `style_prompt`.

## Layout

```text
newsbot/
  main.py              # split generation + posting scheduler + bot command handler
  config.py            # load_config + defaults (incl. style_prompt)
  db.py                # NewsStore (news_items, seen, news_digests, pending_posts)
  scoring.py           # hype_score (engagement × recency × weight + topics + crosspost)
  dedupe.py            # canonical URL + fuzzy title + GitHub repo + merge
  summarizer.py        # two-pass: llm_filter (JSON) + llm_style_posts (JSON per-item)
  telegram_poster.py   # httpx Bot API sendMessage, 429 retry, 4096 split
  bot_commands.py      # long-polling command handler (/setstyle, /run, /status, /help)
  collectors/
    hackernews.py reddit.py github.py rss.py producthunt.py huggingface_papers.py
lm_client.py           # OpenAI-compatible HTTP client
core/                  # settings_store, text_utils, logging_config
```

## Tests

```bash
pip install -e ".[dev]"
pytest
```