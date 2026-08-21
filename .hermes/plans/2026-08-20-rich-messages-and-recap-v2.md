# Hype — Rich Messages + Recap v2

**Date:** 2026-08-20 (UTC+7)
**Status:** planned
**Predecessor:** OQ pass (2026-08-17) — commits 4b681e2, 89b2ab7, 798b886, 2cc623f

## Context

Telegram Bot API 10.1 (2026-06-11) + 10.2 (2026-07-14) added **Rich Messages**:
a new `sendRichMessage` method accepting GFM-flavored markdown (headings,
tables, ordered/unordered lists, blockquotes, footnotes, math, media blocks)
or an HTML/block equivalent. Works in channels (`chat_id` accepts `@username`).
Verified against core.telegram.org/bots/api (full spec cached at
~/.hermes/cache/web/core.telegram.org-a116457079.md). Interactive demo:
@RichTextDemoBot. Hermes Agent itself already delivers rich messages to
Telegram in production, so the path is proven.

Anton's recap decision (2026-08-20): recap becomes a **compact title-only
list** — summary headline on top, then a numbered list where each item is
just: number, original post title (linked to the channel post), source link.
No per-item summaries. This removes the recap-length problem at the root:
12 items ≈ 1.5 KB, always one message, no trim logic needed.

## Known bugs fixed along the way

1. `format_recap_message` trim loop is broken — each iteration re-assembles
   from the ORIGINAL item list, so multi-item overflows never resolve
   (observed: 3946-char recap split into two posts). Fix: delete the trim
   loop entirely; title-only recap can't overflow.
2. `message_id` capture already works (OQ-2) — verified day-3 recap links
   resolve to correct posts.

## Tasks

### RT-1: Rich transport layer

New in `newsbot/telegram_poster.py`:

- `post_rich_message(markdown: str, *, bot_token, chat_id) -> list[dict]`
  calling `POST {base}/bot{token}/sendRichMessage` with payload
  `{"chat_id": ..., "rich_message": {"markdown": ...}}`.
- Reuse `_send_with_retry` (429 + transient retry behavior stays identical).
- **Graceful degradation:** if the API rejects the rich call with 400
  (unsupported entity / bad markdown), retry ONCE after re-escaping; if it
  still fails, fall back to the existing HTML `sendMessage` path so the
  channel never gets nothing. Log the fallback at warning level.
- Probe + document the real rich-message char limit empirically at runtime
  (send an oversized message to Anton's DM, binary-search the cap) and put
  the number in a module constant. Spec doesn't state it explicitly; assume
  4096 until proven.
- Existing `post_digest` (HTML path) stays untouched as the fallback and for
  any non-rich callers.

AC:
- Unit tests: payload shape, retry on 429, fallback triggered on 400.
- Manual: one rich probe to Anton's DM renders headings + list + link.

### RT-2: Rich Markdown renderers

New module `newsbot/richmd.py`:

- `escape_rich_md(text) -> str` — backslash-escape user/LLM content per the
  spec's rich-markdown escape rules (at minimum: `\`, `*`, `_`, `~`, `` ` ``,
  `[`, `]`, `|`, `#`, `>`; keep `$` unescaped only inside intended math).
- `render_post(title, body, url) -> str`
  ```
  ## {title}

  {body paragraphs}

  [Source: {domain}]({url})
  ```
  H2 heading, plain paragraphs (double newline between), one link line.
  Body truncation budget moves from 3000 (HTML) to the probed rich limit.
- `render_recap(title, items, chat_id) -> str`
  ```
  ## {title}

  1. [{item title}]({channel post url}) — [{domain}]({source url})
  2. ...
  ```
  Numbered ordered list. Title links to the channel post via existing
  `_build_channel_link` (items without message_id → plain title, source link
  still shown). No summaries. Hard guard: if items > 30, cut at 30 + log.
- Both functions are pure (testable without network).

AC:
- Unit tests: escaping round-trips (titles with `*`, `[`, `_`, URLs with
  query params), recap shape exact match, channel-link reuse, 30-item guard.

### RT-3: Recap pipeline simplification

- `summarizer.py`: recap prompt (settings `news.recap_prompt`, default in
  config.py) now asks the LLM for ONLY a one-line headline for the day —
  no per-item summaries. Response contract: `{"title": "..."}`. Keep JSON
  parsing defensive (empty/invalid → generic "Daily recap — {date}").
- `jobs.py`: `format_recap_message` replaced by `richmd.render_recap`;
  delete the broken trim loop and the HTML recap renderer.
- Delivery: recap goes through RT-1's `post_rich_message` with HTML
  fallback (fallback renders the same title-only list as HTML `<b>` +
  `<a>` lines — trivial variant, kept inline).
- Update the recap system prompt text in config defaults; note migration:
  existing DB `news.recap_prompt` overrides remain in effect (user-owned via
  /setrecap) — document in task note that Anton's current prompt override
  should be reset to the new default via `/setrecap default` after deploy.

AC:
- Unit tests: headline-only parse, defensive fallback title.
- `/recap` command (admin) produces title-only rich message.

### RT-4: Previews go rich

- `/preview` (admin DM) renders the next store row via `richmd.render_post`
  and sends with `sendRichMessage` to Anton's DM — what you preview is what
  the channel gets.
- `/recap` admin command sends the rich recap to DM (already wired via RT-3).
- `/digest dry` output adds the generated markdown source in a code block
  (for debugging the LLM→render boundary).

AC:
- `/preview` in DM shows heading + paragraph + source link natively.
- `/digest dry` shows markdown source.

## Out of scope

- Media blocks, tables, math in posts (posts stay text; can add later).
- `sendRichMessageDraft` streaming (not needed for scheduled posts).
- EditMessageText rich path (posts are never edited).

## Rollout

1. RT-1 → RT-2 → RT-3 → RT-4, one commit each, squash-clean on main.
2. After RT-1: probe char limit on DM, confirm rendering on Anton's phone.
3. After RT-4: run `/digest dry` + `/preview` + `/recap` end-to-end; then
   let the next scheduled post go rich in the channel and verify client-side
   rendering (mobile + desktop).
4. Fallback path guarantees zero downtime if Telegram rejects rich calls.

## Open questions

- Rich message char limit (probe in RT-1).
- Whether old Telegram clients render rich messages with graceful
  degradation (check on Anton's devices after first live post).
