# Hype — Topic Packs + Real Engagement (v3)

**Date:** 2026-08-22 (UTC+7)
**Status:** planned
**Predecessor:** Nyx's report "HypeNewsBot — Issues, Research & Proposed Redesign" (2026-08-22); rich-messages v2 (2026-08-20)
**Principles:** KISS first, SOLID where it applies. Extend existing seams; no new layers.

## Context

The report established three facts (all verified in code):

1. **Reddit RSS carries no vote/comment counts.** `collectors/reddit.py::_extract_engagement`
   is dead code — every Reddit item scores `engagement = 0`.
2. **The output is AI-only**, but the root cause is the scoring config, not only the
   source pool: with engagement 0 a Reddit item's score *equals its topic bonus*, and
   `min_score = 35`. AI keys stack (`ai` 20 + `llm` 20 + `local_llm` 25 + `coding_agents` 25)
   while gaming caps at `gamedev` 15 + `unity` 12 = 27 — **below the floor**. A gaming
   Reddit post cannot reach the LLM filter today.
3. **No viral signal, and a slow clock.** Generation runs at 05:00/17:00, so a leak
   breaking at 10:00 is collected seven hours later. Cadence is the cheapest lever.

Anton's product goal (2026-08-22): **topics are the unit the app is configured by.**
The bot should scrape a set of topics (science, gaming, ai, design, art, hardware, …)
and any topic can be switched on or off. The missed August GTA6 leak is the acceptance test.

Decisions already taken: drop Product Hunt; X/Twitter rejected on cost; Lobste.rs not added.

## Architecture (v3)

```
TOPIC PACKS (config)                      one pack per topic, switchable
  ai | gaming | gamedev | science | hardware | vr_ar | robotics | design | art
  each: enabled, boost, keywords, subreddits, feeds, github_queries
                 │
                 ▼  load_config() derives the flat source blocks from ENABLED packs
  sources.reddit.subreddits / sources.rss.feeds / sources.github.queries / topic_boost
                 │
                 ▼  collectors UNCHANGED in shape (Candidate contract)
  HN · Reddit (JSON API, real score+comments) · GitHub · HF papers · RSS · Google Trends
                 │
                 ▼  existing pipeline
  filter_seen → dedupe_and_merge (URL / title / fuzzy / +containment for trends)
             → score (engagement·recency·weight + topic + crosspost)
             → LLM Pass A → store → style-at-pick → Telegram
```

What is **not** built: a separate "hype layer", a merge component, embeddings, a
`viral_boost` term, YouTube velocity. Google Trends is just a collector whose
candidates carry traffic as engagement; the existing dedupe merges them into the
matching article and the existing crosspost bonus does the boosting.

### 1. Topic packs

`newsbot/topics.py` owns the pack table (`DEFAULT_TOPIC_PACKS`). Shape:

```python
"gaming": {
    "enabled": True,
    "boost": 20,
    "keywords": ["gta", "playstation", "xbox", "nintendo", "steam", "leak", "trailer", ...],
    "subreddits": ["gaming", "Games", "GamingLeaksAndRumours"],
    "feeds": [{"name": "IGN", "url": "https://feeds.ign.com/ign/all", "weight": 1.1},
              {"name": "Eurogamer", "url": "https://www.eurogamer.net/feed", "weight": 1.1}],
    "github_queries": [],
},
```

Initial packs: `ai` (merges today's ai/llm/local_llm/coding_agents — ONE key, boost 20),
`gaming`, `gamedev` (unity/unreal/godot fold in), `science`, `hardware`, `vr_ar`,
`robotics`, `new_research` (HF papers live here). `design` and `art` ship as empty,
disabled placeholders so enabling them later is config, not code.

Runtime override lives in the settings table as `news.topics` →
`{"gaming": {"enabled": true}, "ai": {"enabled": false}, ...}` (partial; merged over defaults).
`load_config()` builds `sources.reddit.subreddits`, `sources.rss.feeds`,
`sources.github.queries`, `topic_boost` and the keyword table **from enabled packs only**.
Collectors never see topics (SRP): they keep receiving the flat `sources.*` blocks.

Sources that are not topic-specific (HN front page) stay as they are.

### 2. Scoring changes

- **Origin topic.** After collection, each candidate is stamped `origin_topic` by
  looking up its `source_name` (`r/gaming`, feed name) in the pack table. `score_breakdown`
  treats the origin topic as matched even when no keyword hits — an r/gaming post titled
  "Leaked footage" needs no keyword to count as gaming.
- **Bonus cap.** `topic_bonus = max(boost of matched topics)`, not the sum. Stacking
  stops deciding rankings; engagement does.
- **Reddit weight** starts at 0.8 — real Reddit counts dwarf HN/GitHub even under `log1p`.
  Tune with the replay (task H-5), not by feel.
- Formula otherwise unchanged: `(eng·rec·w + topic + crosspost)·penalty`.

### 3. Reddit via JSON API

Same batching as today (`/r/a+b+c/hot?limit=N` on `oauth.reddit.com`), same sequential
groups + delay, same retry-once-on-429. New: client-credentials token
(`REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET`, cached for its TTL), parse
`data.children[].data` → `score`, `num_comments`, `subreddit`, `url` (external link),
`permalink`, `preview` image, `over_18`, `is_self`. Drop `over_18`; keep self-posts
(discussion threads are hype too).

### 4. Google Trends as a collector

`collectors/trends.py`: poll `https://trends.google.com/trending/rss?geo=US`
(`geo` configurable, list). Each story → one Candidate per related news link
(cap 3): title = news headline, url = news link, source=`trends`,
source_name=`trends/<topic title>`, `reposts` = traffic mapped
`200+→200, 1000+→1000, …, 1000000+→1000000, Breakout→1000000` (Breakout is the top signal). The existing dedupe merges it with the
same article from IGN/Reddit via canonical URL or title; crosspost bonus fires.

One new dedupe rule, scoped to `source == "trends"`: the trend title's tokens
(minus stopwords, ≥2 tokens) all contained in a candidate title ⇒ same story.
Logged as `dedupe_trends_match` so boosts are auditable.

### 5. Cadence and registry

- `NEWS_GEN_HOURS` default → `5,9,13,17,21`. Cost: one Pass-A call per extra slot.
- `collect_all` if-chain → `COLLECTORS: dict[name, module]` (OCP): adding a collector
  is one line.

### 6. Removed

Product Hunt collector, `PH_API_KEY`, `producthunt`/`lobsters` weights, their config
validation and tests. Old flat `DEFAULT_TOPIC_BOOST` / `TOPIC_KEYWORDS` replaced by packs.

### 7. Admin surface

`/topics` — list packs with on/off, boost, source counts.
`/topic on <name>` / `/topic off <name>` — flip `news.topics` in settings.
`/sources` — show the *effective* derived source blocks (what the next /digest will fetch).

**Live-config gotcha:** prod has `news.sources` set in the settings DB (8 feeds vs 4 in
defaults). Rollout must migrate that row into `news.topics` overrides or delete it, or
the topic packs never take effect.

## Testing

### Unit (per task, pytest)

- Reddit: fixture JSON response → candidates with real `upvotes`/`comments`, correct
  sub attribution, NSFW dropped, 429 retry path, token refresh.
- Topics: enabling/disabling a pack adds/removes its subs, feeds, queries and boost from
  the derived config; unknown topic names rejected by validation; `design`/`art` disabled
  by default produce no sources.
- Scoring: origin-topic bonus applies without keywords; cap = max not sum; old stacked
  AI bonus is gone.
- Trends: RSS fixture → candidates with traffic→reposts mapping; containment rule merges
  "GTA 6 leak" trend with "GTA 6 gameplay leaks online ahead of…" article and does NOT
  merge with "Leak in GTA 5 RP server" (needs all trend tokens).
- Registry: every name in `COLLECTORS` has a `collect(config)` coroutine.

### Replay (the GTA6 acceptance test)

`scripts/replay_scores.py` rescoring a JSON list of candidates under the current config
and printing the ranking with breakdown. `tests/fixtures/gta6_week.json` holds a
captured week of real candidates (Aug 17–21: Reddit gaming subs with real counts, IGN,
Eurogamer, HN, a trends snapshot) fetched once with the new collectors.

Acceptance: in that replay the GTA6 leak story ranks **#1**, clears the threshold, and
at least **three distinct topics** appear in the top 14. Encoded as a pytest so the
tuning can't regress silently.

**H-5 final weights (2026-08-23):** `reddit: 1.0` (was 0.8). At 0.8, r/science
items with 7064 upvotes scored below HN items with 200–300 upvotes, starving
the top 14 of topic diversity. 1.0 lets real engagement dominate as intended
while HN keeps its 1.2 edge for lower-volume but high-signal stories.

### Live validation (after rollout)

1. `/topics` shows packs; `/topic off ai` → `/sources` no longer lists AI subs/queries.
2. `/digest dry` → report shows Reddit items with non-zero upvotes/comments and a
   `trends` collector line with >0 items.
3. Logs: `score_candidate` events show `origin_topic` and capped `topic_bonus`;
   Reddit 429 count per run ≤ 1.
4. One week of posts: count posted stories per topic — target ≥3 topics/day,
   AI ≤ 50% of posts.
5. Next real viral event (any topic): it is posted within one generation slot
   (≤ 4 h) of trending.

## Tasks

| # | Task | Depends on |
|---|---|---|
| H-1 | Reddit JSON API collector (OAuth, batched, real counts) | credentials |
| H-2 | Topic packs: config model, derived sources, scoring (origin topic, cap) | — |
| H-3 | Google Trends collector + trends containment rule in dedupe | — |
| H-4 | Admin surface (/topics, /topic, /sources) + Product Hunt/Lobsters removal | H-2 |
| H-5 | Score replay tool + GTA6 week fixture + acceptance test | H-1, H-2, H-3 |
| H-6 | Rollout: cadence, collector registry, live-settings migration, rebuild, week watch | H-1..H-5 |

Credentials needed from Anton before H-1: Reddit script app `client_id` + `client_secret`
(reddit.com/prefs/apps). Nothing else — Trends RSS is keyless.

## Addendum 2026-08-22: Reddit auth — Devvit token, not script app

Self-service script-app creation is dead (Responsible Builder Policy; `prefs/apps` is
gated). Anton registered a **Devvit app** (`cybercream-hypebot`) instead. Verified
working from the hype host the same day:

- **API calls:** `Bearer <access_token>` against `oauth.reddit.com`. Batched
  `/r/a+b+c/hot.json?limit=N&raw_json=1` returns full `score`, `num_comments`,
  `subreddit`, `preview`, `over_18`, `is_self`, `permalink`, `url`. Real counts confirmed
  (e.g. score 21,675 / 1,113 comments on r/gaming).
- **Token:** Devvit-issued permanent user token, stored base64-JSON at `~/.devvit/token`
  (keys: `refreshToken`, `accessToken`, `expiresAt`, `scope`, `tokenType`). Access token
  is a JWT, TTL 86400 s, scope `*`, tied to Anton's account.
- **Refresh (verified):** `POST https://www.reddit.com/api/v1/access_token` with
  `grant_type=refresh_token`, `Authorization: Basic base64("TWTsqXa53CexlrYGBWaesQ:")`
  (Devvit CLI's public copy-paste client id — no secret), `User-Agent: devvit-cli`.
  Refresh token is permanent and non-rotating; access TTL 86400 s.

**Change to §3 (Reddit via JSON API):** no `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET`.
The collector takes `REDDIT_REFRESH_TOKEN` env, gets/refreshes the access token itself
using the public client id above, caches for TTL, refreshes on 401. Everything else in
§3 (batching, sequential groups + delay, retry-once-on-429, field mapping) stands.
