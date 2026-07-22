"""Lightweight hype-driven news bot.

Collects candidate news from engagement-bearing sources, ranks by hype
signals, deduplicates across sources, filters and summarizes via an
OpenAI-compatible LLM, and posts a digest to a Telegram channel.
"""