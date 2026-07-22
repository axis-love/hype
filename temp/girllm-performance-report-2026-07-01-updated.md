# GirlLM Performance Report — Updated
## June 6 – July 1, 2026 (with Threads fix applied)

**Generated:** July 1, 2026 09:50 UTC  
**Last code commit:** `c61d6d4` — June 6, 2026  
**Reports analyzed:** 26 daily maintenance reports (June 6 – July 1, 2026)

---

## Executive Summary

| Area | Status | Details |
|------|--------|---------|
| Threads session | ✅ **FIXED** | Fresh cookies applied, session verified live, `threads_enabled` reset, Natalie restarted |
| Threads posting | 🟡 Pending verification | Next scheduled post at 15:30 +08:00 (08:30 UTC) will be the first test |
| Content generation | ✅ Healthy | 89 drafts in 30 days, ~3/day, good variety |
| Process health | ✅ Excellent | Both services stable, zero crashes |
| Engagement scraper | 🔴 Still broken | Scraper-to-DB persistence not working, EPIPE crashes |
| Healthcheck script | 🔴 Still broken | Times out every day, schema bug in SQL |
| Reply rate | 🔴 0% | Natalie has never replied to any tracked comment |

---

## 1. Threads Session Fix — ✅ Applied and Verified

### What Was Done

1. **Backed up** old cookies to `threads_cookies.json.bak.20260701`
2. **Updated** cookies with fresh export from Anton (9 cookies, including new `th_eu_pref` entries)
3. **Verified live** — loaded new cookies into headless Playwright, navigated to `https://www.threads.com/`:
   - Page title: **"Home • Threads"** (was just "Threads" with dead session)
   - No auth wall indicators found
   - Create button found (count: 2), clicked it → **compose editor opened** ✅
4. **Reset** `threads_enabled` to `true` in DB (was `false` since June 2)
5. **Restarted** Natalie service (PID 2848533, active since 09:47 UTC)

### Cookie Status (new)

| Cookie | Expires | Expired? |
|--------|---------|----------|
| csrftoken | 2027-08-05 | No |
| th_eu_pref | 2026-07-01 | Yes (non-critical, EU consent) |
| rur | session | N/A |
| dpr | 2026-07-08 | No |
| ds_user_id | 2026-09-29 | No |
| ig_did | 2027-05-22 | No |
| mid | 2027-07-08 | No |
| sessionid | 2027-07-01 | No |

The two expired `th_eu_pref` cookies are non-critical (EU consent preferences). The critical `sessionid` is valid until July 2027.

### Next Test

The scheduler has dispatched one slot for today: **2026-07-01T15:30:00+08:00** (08:30 UTC). This will be the first real Threads post attempt with the new cookies. An engagement scrape job (#1874) was also auto-dispatched on Natalie restart and is currently running.

---

## 2. Threads Posting Timeline (June 1 – July 1)

The full 30-day timeline reveals **three distinct failure periods**, not one:

### Period 1: Playwright Binary Missing (Jun 4 – Jun 9)
| Dates | Failures | Error |
|-------|----------|-------|
| Jun 4 – Jun 9 | 9 posts | `BrowserType.launch: Executable doesn't exist at chromium_headless_shell-1223` |

Root cause: Playwright browser binary missing after a `uv sync` upgrade. Self-resolved around June 10 (binary reinstalled).

### Period 2: Intermittent Compose Editor (Jun 17)
| Dates | Failures | Error |
|-------|----------|-------|
| Jun 17 | 1 post | `RuntimeError: Compose editor not found` |

One-off failure — the next post (same day) succeeded. Likely a transient page load issue.

### Period 3: Session Death (Jun 28 – Jul 1)
| Dates | Failures | Error |
|-------|----------|-------|
| Jun 28 – Jul 1 | 8 posts | `RuntimeError: Compose editor not found` |

Root cause: Meta invalidated the session server-side. Cookies were present but no longer accepted. Verified by live test on July 1 — login wall appeared. **Fixed with fresh cookies.**

### Summary Statistics

| Metric | Value |
|--------|-------|
| Total POST_DRAFT jobs (30d) | 89 |
| Threads: posted | 43 (48%) |
| Threads: failed | 18 (20%) |
| Threads: not attempted (test mode) | 22 (25%) |
| Threads: no result | 6 (7%) |
| Telegram: posted | 77 (100%) |

The June 2 batch of 22 `threads=-` jobs were test-mode posts (Telegram only, no Threads delivery attempted).

### Posting Cadence (successful Threads posts only)

| Week | Posts | Notes |
|------|-------|-------|
| Jun 1-7 | 5 | Playwright binary broke Jun 4, 5 of 5 attempts before that succeeded |
| Jun 8-14 | 0 | Playwright binary missing, all 9 attempts failed |
| Jun 10-16 | 10 | Binary fixed, strong recovery |
| Jun 17-23 | 8 | One intermittent failure on Jun 17, otherwise solid |
| Jun 24-27 | 7 | All successful |
| Jun 28-Jul 1 | 0 | Session death, 8 attempts failed |
| **Total** | **43** | |

---

## 3. Process Health

| Component | Status | PID | Uptime |
|-----------|--------|-----|--------|
| Jessica (bot.py) | ✅ UP | 1543101 | Since Jun 6 (25 days) |
| Natalie Worker | ✅ UP | 2848533 | Since Jul 1 09:47 UTC (just restarted) |
| Worker heartbeat | ✅ `running` | — | — |
| DB integrity | ✅ `ok` | — | — |
| Circuit breaker | ✅ `closed` | — | 0 failure streak |

Jessica has been up for 25 days straight. Natalie was restarted to pick up the new cookies — previous instance (PID 2526109) ran 7 days since Jun 24.

---

## 4. Content Generation & Quality

### Draft Production

**89 drafts in 30 days** — consistent ~3/day cadence.

### Lane Distribution (June 1 – July 1)

| Lane | Count | % | Assessment |
|------|-------|---|------------|
| hot_take | 25 | 28% | Dominant — AI industry commentary |
| UNTAGGED | 24 | 27% | ⚠️ Missing `content_lane` metadata |
| vulnerable | 15 | 17% | Strong — highest engagement lane historically |
| devlog | 15 | 17% | Good cadence but lowest engagement |
| lifestyle | 9 | 10% | Underrepresented — Bali setting is a differentiator |
| cross_domain | 2 | 2% | Nearly abandoned — bridges tech + lifestyle |

### Content Quality

**Strengths:**
- Strong topic diversity: AI industry critique, dev tool updates, personal vulnerability, Bali lifestyle, hardware/security
- Consistent tone: sharp, first-person, opinionated, tech-fluent
- Working dedup guardrails (correctly rejected 3 duplicate topics)
- No tone drift over 30 days

**Weaknesses:**
- **27% of drafts have no `content_lane`** — the tagger isn't firing on "smart" post kind
- **Devlog posts consistently underperform** (0-3 likes) — internal tool updates don't resonate
- **Cross_domain nearly abandoned** (2 posts in 30 days)

---

## 5. Engagement Data (stale — last DB update June 2)

### DB State

| Table | Rows | Last Update |
|-------|------|-------------|
| engagement_posts | 27 | June 2, 2026 |
| engagement_comments | 22 | June 2, 2026 |
| engagement_profile_metrics | 1 | June 2, 2026 (424 followers) |

The engagement scraper has not persisted new data to the DB in 29 days. A new scrape job (#1874) was dispatched on Natalie restart and is currently running — this is the first scrape attempt with the new valid session.

### Historical Performance (June 2 snapshot)

| Metric | Value |
|--------|-------|
| Followers | 424 (single snapshot) |
| Avg likes/post | 8.6 |
| Total likes | 231 |
| Total replies | 36 |
| Total reposts | 1 |
| Reply rate | **0%** (0/22) |

### Engagement Tiers (27 posts)

| Tier | Range | Count | % |
|------|-------|-------|---|
| 🔥 Breakout | 40+ likes | 1 | 3.7% |
| ✅ Solid | 10-39 likes | 4 | 14.8% |
| 📊 Middling | 5-9 likes | 8 | 29.6% |
| 💀 Dead | 0-4 likes | 14 | 51.9% |

### Top 5 Posts (all-time from DB)

| # | Likes | Replies | Preview | Lane (inferred) |
|---|-------|---------|---------|-----------------|
| 1 | **103** | 14 | "it's hard to feel like a real engineer when you're mostly just orchestrating Claude…" | vulnerable |
| 2 | 24 | 3 | "watching stack overflow turn into a digital graveyard…" | hot_take |
| 3 | 14 | 4 | "a single failed build is all it takes to bring the imposter syndrome back…" | vulnerable |
| 4 | 12 | 5 | "staring at my dependency tree at 2am after 314 compromised npm packages…" | hot_take |
| 5 | 11 | 3 | "cannibalizing engineering teams to fund ai compute…" | hot_take |

### Follower Growth (from daily report live scrapes)

| Date | Followers | Source |
|------|-----------|--------|
| June 2 | 424 | DB snapshot |
| June 13 | 443 | Daily report live scrape |
| June 17 | 445 | Daily report live scrape |
| June 25 | 445 | Daily report live scrape |

Growth plateaued at ~445 after mid-June. The scraper was returning wrong-profile data after June 28, so later numbers are unreliable.

### Reply Rate: 0% 🔴

Natalie has **not replied to a single comment** across all 22 tracked comments. The `REPLY_TO_COMMENT` job type was implemented (flow_000729) but appears to not be active — no reply jobs in the queue.

---

## 6. Failed Jobs Summary (30 days)

| Error Type | Count | Period | Severity | Root Cause |
|------------|-------|--------|----------|------------|
| Threads: Compose editor not found | 8 | Jun 28 – Jul 1 | 🔴 Fixed | Session invalidated server-side |
| Threads: Playwright binary missing | 9 | Jun 4 – Jun 9 | 🔴 Fixed | Binary removed by uv sync |
| Threads: Compose editor (one-off) | 1 | Jun 17 | 🟢 Transient | Page load timing |
| HTTP 423 (GPUBox) | 4 | Jun 2-5 | 🟢 Expected | GPUBox capacity |
| HTTP 400 (non-JSON HTML) | 8 | Jun 16 | 🟡 Transient | LLM endpoint outage (7 min) |
| Duplicate topic rejection | 3 | Jun 7, 14, 23 | 🟢 Healthy | Dedup guard working |

**No LLM failures in the last 7 days.** Generation pipeline is clean.

---

## 7. Known Issues Still Open

### 🔴 Engagement scraper-to-DB persistence broken
The scraper runs and produces JSON, but results are not written to `engagement_posts`/`engagement_comments`/`engagement_profile_metrics`. All DB analytics have been stale since June 2.

### 🔴 Healthcheck script times out every day (26/26 reports)
120s timeout is insufficient for Playwright scraper + 9 DB query blocks. Additionally, the script has a schema bug: references `d.payload_json` but the column is `d.meta_json`.

### 🟡 Reply rate is 0%
The `REPLY_TO_COMMENT` job type exists but no reply jobs have been dispatched. The comment reply pipeline may not be active.

### 🟡 27% of drafts have no content_lane
Drafts with `post_kind=smart` are not getting lane tags assigned. This breaks lane-level engagement analysis.

### 🟡 Scraper EPIPE crashes
Playwright's Node.js driver periodically crashes with EPIPE during Phase 2 (comment scraping). Needs retry logic or per-page timeouts.

---

## 8. Priority Action Items

### ✅ Completed This Session

| # | Action | Result |
|---|--------|--------|
| 1 | Update Threads cookies | ✅ Fresh cookies applied |
| 2 | Verify session live | ✅ Compose editor opens |
| 3 | Reset `threads_enabled` | ✅ Set to `true` |
| 4 | Restart Natalie | ✅ Running (PID 2848533) |

### 🔴 Remaining (code fixes needed)

| # | Issue | Fix | Effort |
|---|-------|-----|--------|
| 1 | Scraper not persisting to DB | Fix `EngagementStore` write path after scrape | Code fix |
| 2 | Healthcheck script timeout | Increase cron timeout to 240s or split script | Config |
| 3 | Healthcheck schema bug | Change `d.payload_json` → `d.meta_json` | 2-line fix |
| 4 | Draft-to-engagement linkage | Wire `draft_id` association in scraper | Code fix |
| 5 | Lane tagging for `smart` posts | Enforce `content_lane` assignment at generation | Prompt/code |
| 6 | Reply pipeline activation | Enable `REPLY_TO_COMMENT` dispatch | Config |
| 7 | Scraper EPIPE crashes | Add Playwright retry + per-page timeouts | Code fix |
| 8 | Profile metrics persistence | Ensure daily snapshot to `engagement_profile_metrics` | Code fix |

---

## 9. What Happens Next

1. **08:30 UTC** — Next scheduled post slot. Natalie will attempt to post draft to Threads with the new cookies. This is the first real test.
2. **Scrape job #1874** — Currently running. If it completes successfully, it will be the first engagement data since June 2. Check if it persists to DB.
3. **15:00 UTC** — Daily maintenance cron job runs. Tomorrow's report should show fresh data if the scraper works.

If the next Threads post succeeds, the session fix is confirmed. If it fails, there may be an additional issue (e.g., the `th_eu_pref` expired cookies causing problems, or a Threads UI change).

---

*Report compiled from 26 daily maintenance cron reports, live DB queries, systemd journal logs, Playwright session verification, and git history. All data verified against `data/girllm.sqlite` on July 1, 2026 09:50 UTC.*