# Hype pipeline notes

## 5. Story title vs Telegram headline (fixed 2026-09-08, flow_001165)

**Incident:** `set_styled_content` overwrote `pending_posts.title` with the
Russian Telegram headline. `GET /api/v1/items` returned that headline to
GirlLM. The hype gate then required English drafts to overlap Russian
tokens (item #1118, slot 12:52Z, 34 failed jobs).

**Fix:** `title` is immutable after insert (Pass A English). Migration 9
adds `styled_title`. The styler writes `styled_title` + `body` + `styled_at`
and never touches `title`. Telegram + recap render
`COALESCE(styled_title, title)`. The consumer API still returns `title`.
