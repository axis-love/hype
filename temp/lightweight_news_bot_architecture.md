# Lightweight Hype-Driven News Bot — Concept & Architecture

## 1. General Idea

We are building a lightweight server-side news bot that automatically finds the most hyped and important IT-related news, summarizes them using our own local/custom LLM endpoint, and posts a readable digest to a Telegram channel.

This is not a generic RSS reader. The bot should behave more like a small **hype detector**:

1. Collect candidate news from sources where engagement can be measured.
2. Rank items by hype signals such as upvotes, comments, stars, reposts, source quality, recency, and cross-source appearance.
3. Filter and summarize the best items using our custom LLM endpoint.
4. Generate a readable Telegram article with 5–10 diverse news items.
5. Post the final digest to a Telegram channel.

The application should stay simple: one small Python or Go service, preferably Python for the first version.

---

## 2. Main Product Goal

The bot should produce a short, useful, readable IT/tech digest focused on topics like:

- AI and LLMs
- Local LLMs
- Coding agents and developer tools
- Programming and software engineering
- Game development
- Unity, Unreal, Godot
- Video games industry and game technology
- VR / AR / XR
- Robotics
- New research and inventions
- New GitHub trending repositories
- Useful new products and tools

The output should be suitable for a Telegram channel: short, dense, and readable.

Example final output shape:

```md
🔥 Tech / AI Digest — 21 Jun 2026

1. New local LLM tool is exploding on GitHub
What happened: ...
Why it matters: ...
Signal: 2,400 GitHub stars, 530 HN points, 190 comments
Link: ...

2. Major AI research paper introduces ...
What happened: ...
Why it matters: ...
Signal: trending on Hugging Face Papers, discussed on Reddit
Link: ...
```

---

## 3. Core Architecture

The bot has three main modules.

```text
┌─────────────────────────┐
│ 1. Hype Collector        │
│ Scrape / collect / rank  │
└────────────┬────────────┘
             ↓
┌─────────────────────────┐
│ 2. LLM Summarizer        │
│ Filter / summarize /     │
│ write final digest       │
└────────────┬────────────┘
             ↓
┌─────────────────────────┐
│ 3. Telegram Poster       │
│ Format / split / post    │
└─────────────────────────┘
```

The service can run by cron or as a long-running process with a simple scheduler.

---

## 4. Suggested Tech Stack

### Recommended: Python

Python is the better first choice because the project needs scraping, text extraction, LLM calls, YAML config, and Telegram posting. These are easier and faster to build in Python.

Suggested dependencies:

```text
httpx                 HTTP requests
feedparser            RSS / Atom parsing
beautifulsoup4        simple HTML parsing
trafilatura           article text extraction
PyYAML                config file
python-dotenv         environment variables
openai                OpenAI-compatible LLM endpoint client
python-telegram-bot   Telegram posting
```

Optional later:

```text
praw                  official Reddit API client
playwright            fallback for JS-heavy scraping only
rapidfuzz             fuzzy title deduplication
```

### Go Alternative

Go can work if we want a single compiled binary, but scraping and article extraction will be more annoying.

Possible Go libraries:

```text
gofeed       RSS parsing
goquery      HTML parsing
resty        HTTP client
tgbotapi     Telegram bot API
```

Recommended decision: **start with Python**.

---

## 5. File Structure

Keep the app simple.

```text
newsbot/
  main.py
  config.yaml
  state.json

  collectors/
    hackernews.py
    reddit.py
    github.py
    producthunt.py
    huggingface_papers.py
    rss.py

  summarizer.py
  telegram_poster.py
  scoring.py
  dedupe.py
  models.py
```

No database is required for the first version. Use `state.json` for seen links, seen titles, and posted digests.

---

## 6. Module 1 — Hype Collector

The collector does not just fetch articles. It collects **candidates** from sources where popularity can be measured.

A candidate is a normalized news item:

```python
Candidate:
    title: str
    url: str
    source: str
    source_type: str
    published_at: datetime
    score: float
    comments: int | None
    upvotes: int | None
    reposts: int | None
    stars: int | None
    velocity: float | None
    category: str | None
    raw_text: str | None
    extracted_text: str | None
```

The collector should gather more candidates than needed, score them, deduplicate them, and only send the top candidates to the LLM.

Target flow:

```text
collect 100–300 raw candidates
↓
remove already-seen URLs/titles
↓
deduplicate similar items
↓
score by hype
↓
keep top 30–80 candidates
↓
send to LLM filtering/summarization pass
```

---

## 7. Best Source Types

### 7.1 Hacker News

Very useful for:

- AI tools
- LLM releases
- coding agents
- dev tools
- startups
- Show HN projects
- technical controversy

Signals:

- points
- comment count
- age
- title keywords
- whether it is frontpage / Show HN

Example scoring idea:

```python
hn_score = points * 1.0 + comments * 2.5 + recency_bonus
```

Comments should be weighted strongly because they often indicate discussion, controversy, or importance.

---

### 7.2 Reddit

Useful subreddits:

```text
r/LocalLLaMA
r/MachineLearning
r/artificial
r/singularity
r/programming
r/gamedev
r/Unity3D
r/unrealengine
r/Godot
r/virtualreality
r/OculusQuest
r/SteamDeck
r/technology
r/webdev
r/selfhosted
r/opensource
```

Signals:

- score / upvotes
- comments
- upvote ratio
- subreddit weight
- recency

Example scoring:

```python
reddit_score = upvotes * 0.7 + comments * 3.0 + upvote_ratio_bonus + recency_bonus
```

---

### 7.3 GitHub Trending / GitHub Search

Useful for:

- new open-source tools
- local LLM projects
- agents
- coding tools
- game dev libraries
- VR / AR / WebXR libraries
- robotics tools

Signals:

- stars
- forks
- stars gained recently
- repo age
- README quality
- cross-posting on HN/Reddit

Example queries:

```text
llm
agent
coding-agent
rag
local-llm
ai-coding
unity
game-engine
unreal
godot
webxr
vr
robotics
```

Important: GitHub stars can be gamed, so stars should not be trusted alone.

Safety filters:

```text
penalize repos with no README
penalize repos with almost no code
penalize suspiciously new repos with huge stars but no issues/forks
penalize crypto / cheat / piracy / scam keywords
boost repos also discussed on HN or Reddit
```

---

### 7.4 Product Hunt

Useful for:

- new AI products
- dev tools
- productivity tools
- design tools
- SaaS launches

Signals:

- votes
- comments
- topic match
- launch date

Product Hunt has a lot of marketing noise, so it should be weighted lower than HN, Reddit, or GitHub unless the same product appears on multiple sources.

---

### 7.5 Hugging Face Papers

Useful for:

- trending AI papers
- LLM research
- vision models
- robotics
- benchmarks
- text-to-video / multimodal models

Signals:

- paper upvotes
- linked GitHub repositories
- linked models/datasets/spaces
- cross-source discussion

This is better than raw arXiv for hype detection because it already has community attention signals.

---

### 7.6 RSS Feeds

RSS should be used as a credibility and official-source layer, not as the main hype detector.

Good RSS sources:

```text
OpenAI blog
Anthropic news
Google DeepMind blog
Meta AI blog
NVIDIA developer blog
Unity blog
Unreal Engine blog
Godot blog
Steam blog
Road to VR
UploadVR
Game Developer
The Verge AI
Ars Technica
MIT Technology Review AI
```

RSS items should rank high only if:

```text
the source is official / highly important
or the same story appears on HN / Reddit / Product Hunt
or the title matches a high-priority topic
```

---

## 8. Hype Scoring

The LLM should not be responsible for discovering what is trending. The code should do the first pass mechanically.

Simple scoring function:

```python
from math import log1p


def hype_score(item):
    engagement = (
        log1p(item.upvotes or 0) * 10 +
        log1p(item.comments or 0) * 25 +
        log1p(item.stars or 0) * 15 +
        log1p(item.reposts or 0) * 20
    )

    recency = recency_decay(item.published_at)
    source_weight = SOURCE_WEIGHTS.get(item.source_type, 1.0)
    topic = topic_bonus(item)
    crosspost = cross_source_bonus(item)

    return engagement * recency * source_weight + topic + crosspost
```

Example source weights:

```yaml
source_weights:
  hackernews: 1.2
  reddit: 1.0
  github: 1.1
  producthunt: 0.8
  huggingface_papers: 1.2
  lobsters: 1.0
  official_rss: 1.3
  normal_rss: 0.5
```

Example topic boosts:

```yaml
topic_boost:
  ai: 20
  llm: 20
  local_llm: 25
  coding_agents: 25
  gamedev: 15
  unity: 12
  unreal: 12
  godot: 12
  vr_ar: 18
  robotics: 18
  github_trending: 15
  new_research: 20
```

Cross-source bonus:

```python
if same_url_or_title_seen_on_multiple_sources:
    score += 30
```

This is one of the strongest signals. A GitHub repo with stars is interesting. A GitHub repo that is also discussed on HN and Reddit is probably actually trending.

---

## 9. Deduplication

Keep deduplication simple.

Use:

```text
canonical URL match
title lowercase match
title fuzzy similarity
same GitHub repo URL
same domain + very similar title
```

For the first version, avoid embeddings. A lightweight fuzzy title comparison is enough.

Suggested logic:

```python
if canonical_url_a == canonical_url_b:
    duplicate = True

if normalized_title_a == normalized_title_b:
    duplicate = True

if fuzzy_title_similarity > 0.90:
    duplicate = True
```

When duplicates are found, merge their engagement signals instead of deleting the weaker item.

Example:

```text
same story appears on:
- Hacker News: 430 points, 180 comments
- Reddit: 2,100 upvotes, 340 comments
- GitHub: 8,000 stars

Result: one candidate with stronger cross-source signal.
```

---

## 10. Module 2 — LLM Summarizer

We already have a custom endpoint hosting:

```text
gemma4:26b
qwen3.6:27b
qwen3.6:35b-a3b
```

The bot should call this endpoint through an OpenAI-compatible API if possible.

Suggested usage:

```text
qwen3.6:27b       fast filtering, relevance checks, short summaries
gemma4:26b        final readable digest writing
qwen3.6:35b-a3b   fallback for harder synthesis or if quality is better in tests
```

The LLM should do two passes.

---

### Pass A — Candidate Filtering

Input: top 30–80 scored candidates.

Task:

- remove garbage
- remove low-value promo
- remove duplicates not caught by code
- classify category
- estimate importance
- produce a short summary

Expected JSON output:

```json
{
  "items": [
    {
      "keep": true,
      "title": "...",
      "url": "...",
      "category": "AI / Coding",
      "importance": 8,
      "reason": "Strong HN and GitHub engagement; useful developer tool.",
      "short_summary": "..."
    }
  ]
}
```

Reject items like:

```text
old news
thin marketing launch
pure drama
crypto spam
obvious repost
low-quality GitHub repo
low-value Product Hunt launch
```

---

### Pass B — Final Digest Writer

Input: top 5–10 cleaned items.

Task:

- write one readable Telegram article
- keep topics diverse
- keep each item short
- explain why each item matters
- include source links
- include engagement signal when useful

Output format:

```md
🔥 Tech / AI Digest — 21 Jun 2026

1. Headline
What happened: ...
Why it matters: ...
Signal: ...
Link: ...

2. Headline
...
```

The final article should be clear, compact, and human-readable.

---

## 11. Module 3 — Telegram Poster

The Telegram poster should be a simple adapter.

Responsibilities:

```text
post final digest to channel
use Markdown or HTML formatting
split long messages if needed
avoid duplicate posts
log successful posts
store posted URLs/titles in state.json
```

State example:

```json
{
  "seen_urls": [],
  "seen_titles": [],
  "posted_digests": [],
  "last_run": "2026-06-21T09:00:00+07:00"
}
```

The Telegram module should not know anything about scraping or summarization. It only receives final text and posts it.

---

## 12. Config Example

```yaml
llm:
  endpoint: "https://your-endpoint/v1/chat/completions"
  api_key: "${LLM_API_KEY}"
  classifier_model: "qwen3.6:27b"
  writer_model: "gemma4:26b"
  fallback_model: "qwen3.6:35b-a3b"

telegram:
  bot_token: "${TELEGRAM_BOT_TOKEN}"
  channel_id: "@your_channel"

run:
  max_candidates: 80
  max_final_news: 10
  lookback_hours: 48
  min_score: 35

topics:
  - ai
  - llm
  - local_llm
  - coding
  - coding_agents
  - gamedev
  - unity
  - unreal
  - godot
  - vr_ar
  - robotics
  - github_trending
  - research
  - developer_tools

sources:
  hackernews:
    enabled: true
    queries:
      - "AI"
      - "LLM"
      - "local LLM"
      - "coding agent"
      - "game engine"
      - "VR"
      - "AR"
      - "robotics"
      - "Show HN"

  reddit:
    enabled: true
    subreddits:
      - LocalLLaMA
      - MachineLearning
      - artificial
      - singularity
      - programming
      - gamedev
      - Unity3D
      - unrealengine
      - Godot
      - virtualreality
      - OculusQuest
      - selfhosted
      - opensource

  github:
    enabled: true
    queries:
      - "llm"
      - "agent"
      - "coding-agent"
      - "rag"
      - "unity"
      - "game-engine"
      - "webxr"
      - "vr"
      - "robotics"

  producthunt:
    enabled: true
    topics:
      - artificial-intelligence
      - developer-tools
      - productivity

  huggingface_papers:
    enabled: true

  rss:
    enabled: true
    feeds:
      - name: "OpenAI"
        url: "https://openai.com/news/rss.xml"
        weight: 1.3
      - name: "Google DeepMind"
        url: "https://deepmind.google/discover/blog/rss.xml"
        weight: 1.3
      - name: "Unity"
        url: "https://blog.unity.com/feed"
        weight: 1.1
      - name: "Unreal Engine"
        url: "https://www.unrealengine.com/en-US/feed"
        weight: 1.1
```

---

## 13. Runtime Flow

```python
def main():
    config = load_config()
    state = load_state()

    candidates = []

    candidates += collect_hackernews(config)
    candidates += collect_reddit(config)
    candidates += collect_github(config)
    candidates += collect_producthunt(config)
    candidates += collect_huggingface_papers(config)
    candidates += collect_rss(config)

    candidates = filter_seen(candidates, state)
    candidates = dedupe_and_merge(candidates)
    candidates = score_and_sort(candidates)

    top_candidates = candidates[:config.run.max_candidates]

    cleaned_items = llm_filter_and_summarize(top_candidates)
    final_items = select_diverse_top_items(cleaned_items, max_items=10)

    article = llm_write_digest(final_items)

    post_to_telegram(article)
    update_state(state, final_items)
```

---

## 14. Scheduling

Simplest scheduling: cron.

Twice per day:

```bash
0 9,18 * * * cd /opt/newsbot && python main.py
```

Every four hours:

```bash
0 */4 * * * cd /opt/newsbot && python main.py
```

This is enough for the first version.

---

## 15. MVP Plan

### Version 0.1

Build the smallest useful version:

```text
HN collector
Reddit collector
GitHub collector
RSS collector
score candidates
dedupe by URL/title
LLM filter pass
LLM final digest pass
Telegram posting
state.json dedupe
```

This version should already produce a useful digest.

---

### Version 0.2

Add better source coverage:

```text
Product Hunt
Hugging Face Papers
cross-source merge improvements
category balancing
bad keyword/source blacklist
```

---

### Version 0.3

Add quality improvements:

```text
Telegram reaction feedback
manual pinned/blocked keywords
source reliability tuning
separate digest styles
weekly digest mode
more aggressive duplicate detection
```

---

## 16. Design Principles

### Keep the bot lightweight

Avoid unnecessary systems:

```text
no database at first
no dashboard
no worker queue
no microservices
no embeddings unless needed
no browser automation unless required
```

### Code discovers hype, LLM writes the digest

The code should decide what is probably important using measurable signals:

```text
upvotes
comments
stars
votes
recency
cross-posts
source weight
topic match
```

The LLM should do:

```text
filter garbage
merge obvious duplicates
explain why it matters
write a readable digest
```

### Make everything replaceable

Each collector should be independent. Telegram should be just a delivery adapter. The LLM endpoint should be configured, not hardcoded.

### Start useful, then improve

The first version should not be perfect. It should simply produce a decent digest every day. After real posts, tune source weights, topic boosts, and prompt style based on actual quality.

---

## 17. Final Recommended Architecture

```text
Lightweight Python bot
├── config.yaml
├── state.json
├── collectors
│   ├── Hacker News
│   ├── Reddit
│   ├── GitHub
│   ├── RSS
│   ├── Product Hunt, later
│   └── Hugging Face Papers, later
├── scoring + dedupe
├── LLM summarizer using custom endpoint
│   ├── qwen3.6:27b for filtering
│   ├── gemma4:26b for final writing
│   └── qwen3.6:35b-a3b as fallback / quality test
└── Telegram poster
```

The core idea: **collect many candidates, rank by real engagement, let the LLM turn the best 5–10 into a clean Telegram digest.**
