# newsbot

A lightweight hype-driven tech news bot. Collects candidate news from
engagement-bearing sources (Hacker News, Reddit, GitHub, Product Hunt,
Hugging Face Papers, RSS), ranks by hype signals, deduplicates across
sources, summarizes via an OpenAI-compatible LLM, and posts a daily
digest to a Telegram channel.

## What it does

```text
collect (HN/Reddit/GitHub/PH/HF Papers/RSS)
  → filter already-seen
  → cross-source dedupe + merge engagement
  → score by hype (upvotes, comments, stars, recency, topic, crossposts)
  → LLM Pass A: filter garbage, classify, importance, one-line summary (JSON)
  → LLM Pass B: write the Telegram digest (What / Why / Signal / Link)
  → post to Telegram
  → mark seen
```

The code decides what's trending; the LLM only writes the digest.

## Quick start

```bash
cp .env.example .env        # fill in BOT_TOKEN, NEWS_CHANNEL_ID, LM_*
pip install -r requirements.txt
python -m newsbot.main      # one run; cron handles scheduling
```

If `BOT_TOKEN` or `NEWS_CHANNEL_ID` is unset, the bot prints the digest
to stdout instead of posting — useful for testing.

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
`llm_max_tokens_digest`. Default: 8000 / 8000.

## Layout

```text
newsbot/
  main.py              # linear pipeline entrypoint (cron-driven)
  config.py            # load_config + defaults
  db.py                # NewsStore (news_items, seen, news_digests)
  scoring.py           # hype_score (engagement × recency × weight + topics + crosspost)
  dedupe.py            # canonical URL + fuzzy title + GitHub repo + merge
  summarizer.py        # two-pass: llm_filter (JSON) + llm_write_digest (Markdown)
  telegram_poster.py   # httpx Bot API sendMessage, 429 retry, 4096 split
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