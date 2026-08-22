# Praxis Dispatch — Hype H-1 → H-2 → H-3 (sequential, one session)

**Repo:** /home/nyx/.hermes/projects/hype (work here)
**Spec:** read `.hermes/plans/2026-08-22-topic-packs-and-hype-v3.md` FIRST — it is the
authoritative design (incl. the 2026-08-22 Addendum on Reddit auth). Sections referenced
below map to that file. Plan principles: KISS first, SOLID where applicable; extend
existing seams, no new layers.

## Execution order

Do these **one at a time, in order**: H-1, then H-2, then H-3. For each task:
1. `git pull --rebase origin main` before you start touching files.
2. Read the current code first (collectors/, pipeline, config loading, existing tests)
   and follow existing conventions.
3. Write tests alongside/before code per plan §Testing.
4. Run the full test suite until green.
5. Commit as ONE squash commit per task. Author/committer:
   `Nyx Prime <nyx@axis.love>`. Commit subject: `hype: H-1 Reddit JSON API collector ...` etc.
   Explain *why* in the body.
6. Push to main only after tests are green.

**Never commit secrets.** The Reddit refresh token is a secret.

---

## H-1 — Reddit JSON API collector (plan §3 + Addendum)

Replace the RSS fetch in `newsbot/collectors/reddit.py` with Reddit's JSON API.
KEEP the current structure: multi-subreddit batching
(`oauth.reddit.com/r/a+b+c/hot.json?limit=N&raw_json=1`), sequential groups with delay,
retry once on 429.

Auth (verified working 2026-08-22 — do not redesign it):
- Env var `REDDIT_REFRESH_TOKEN` (permanent, non-rotating).
- Access token: `POST https://www.reddit.com/api/v1/access_token`,
  form `grant_type=refresh_token&refresh_token=***
  header `Authorization: Basic <base64("TWTsqXa53CexlrYGBWaesQ:")>` — that is Devvit
  CLI's PUBLIC client id, there is no secret. `User-Agent: devvit-cli`.
- Response: `access_token` (JWT, TTL `expires_in`=86400), scope `*`. Cache it; refresh on
  expiry or on 401. Use a descriptive UA (e.g. `cybercream-hypebot/0.1`) on API calls.

Mapping `data.children[].data` → Candidate:
- `upvotes` = `score`, `comments` = `num_comments` (real numbers now!)
- `source_name` from `data.subreddit` (as `r/<name>`); url = permalink; keep external
  link + preview image available for the media extractor; drop `over_18`; keep self-posts.
- Delete the dead `_extract_engagement` regex code.

Plumbing: wire `REDDIT_REFRESH_TOKEN` through env.example (placeholder only),
deploy/docker/compose.yml, and the README source list. Do NOT put the real token in any
file you commit.

Tests: fixture JSON response → candidates with real upvotes/comments, correct sub
attribution, NSFW dropped, 429 retry path, token refresh path (mock HTTP).

**Live smoke test (do this once, proves the whole chain):** the token lives at
`/home/nyx/.hermes/home/.devvit/token` — it is base64-encoded JSON with key
`refreshToken`. In a throwaway script (NOT committed): parse it, refresh, then fetch
`r/gaming+Games+GamingLeaksAndRumours/hot.json?limit=5` and print titles + scores.
Expected: real counts, HTTP 200. Never print or persist the token value itself.

## H-2 — Topic packs (plan §1, §2)

- New `newsbot/topics.py` with `DEFAULT_TOPIC_PACKS`: packs `ai` (merges today's
  ai/llm/local_llm/coding_agents into ONE key, boost 20), `gaming`, `gamedev`, `science`,
  `hardware`, `vr_ar`, `robotics`, `new_research` (HF papers), plus `design` and `art`
  as empty DISABLED placeholders. Each pack: enabled, boost, keywords, subreddits, feeds,
  github_queries. Use the plan's gaming example as the shape reference and port existing
  keywords/subs/feeds into the right packs.
- Runtime override: settings key `news.topics` → partial dict merged over defaults.
- `load_config()` derives `sources.reddit.subreddits`, `sources.rss.feeds`,
  `sources.github.queries`, `topic_boost` and keyword table **from enabled packs only**.
  Collectors keep receiving flat `sources.*` blocks — collectors never see topics (SRP).
  Non-topic sources (HN front page) unchanged.
- Scoring: stamp each candidate `origin_topic` by looking up its `source_name` in the pack
  table; origin topic counts as matched even with zero keyword hits.
  `topic_bonus = max(boost of matched topics)` — NOT sum. Reddit source weight starts 0.8.
  Formula otherwise unchanged.
- Remove the old flat `DEFAULT_TOPIC_BOOST` / `TOPIC_KEYWORDS`.

Tests (from plan): enabling/disabling a pack adds/removes its subs, feeds, queries, boost
from derived config; unknown topic names rejected by validation; disabled design/art
produce no sources; origin-topic bonus applies without keywords; bonus cap = max not sum;
old stacked AI bonus gone.

## H-3 — Google Trends collector (plan §4)

- New `newsbot/collectors/trends.py`: poll
  `https://trends.google.com/trending/rss?geo=US` (geo configurable, list of geos).
  Each story → up to 3 Candidates (one per related news link): title = news headline,
  url = news link, source=`trends`, source_name=`trends/<topic title>`,
  `reposts` = traffic mapped `200+→200, 1000+→1000, ..., Breakout→5000`.
- Register it wherever collectors are enumerated (note: H-6 later turns the if-chain into
  a registry — for now wire it the way existing collectors are wired).
- One new dedupe rule, scoped to `source == "trends"`: trend title's tokens (minus
  stopwords, ≥2 tokens) ALL contained in a candidate title ⇒ same story → merge.
  Log as `dedupe_trends_match`.

Tests: RSS fixture → candidates with correct traffic→reposts mapping; containment rule
merges "GTA 6 leak" trend with "GTA 6 gameplay leaks online ahead of…" article and does
NOT merge with "Leak in GTA 5 RP server" (needs ALL trend tokens).

---

## Final report

When all three are done (or you hit a hard blocker), report: per task — commit SHA,
test results (counts), what changed, anything deviating from the plan and why. If blocked,
commit what's green, push, and state the blocker precisely. Do not ask questions mid-run;
make the reasonable engineering call and note it.
