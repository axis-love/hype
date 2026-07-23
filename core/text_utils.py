"""Text processing utilities for news-bot."""

from __future__ import annotations

import re

THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
FINAL_MARKER_RE = re.compile(r"\b(?:so\s+)?final(?:\s+answer)?\s*[:.\-\n]+\s*", re.IGNORECASE)
REASONING_PREFIX_RE = re.compile(
    r"^\s*(?:"
    r"(?:we|i)\s+(?:need|should|must|have|want)\s+to\b|"
    r"(?:we|i)'ll\b.*\b(?:write|draft|reply|respond|generate|produce|return|post)\b|"
    r"let'?s\b.*\b(?:write|draft|reply|respond|generate|produce|return|post)\b|"
    r"(?:sure|here(?:'s| is))\b.*\b(?:post|reply|caption|draft)\b|"
    r"(?:output|caption|post|reply)\s*:|"
    r"the user\b|"
    r"(?:analysis|reasoning|plan|scratchpad|thinking)\b"
    r")",
    re.IGNORECASE,
)


def strip_think(text: str) -> str:
    """Remove <think>...</think> blocks from LLM output (reasoning models)."""
    text = THINK_RE.sub("", text or "")
    text = re.sub(r"</?think>", "", text, flags=re.IGNORECASE)
    return text.strip()


def extract_final_text(text: str) -> str:
    """Best-effort extraction of the final user-facing text from leaked scratchpad output."""
    cleaned = strip_think(text)
    if not cleaned:
        return ""

    matches = list(FINAL_MARKER_RE.finditer(cleaned))
    if matches:
        candidate = cleaned[matches[-1].end():].strip().strip("\"'")
        if candidate:
            return candidate
    return cleaned.strip()


def looks_like_reasoning_leak(text: str) -> bool:
    """Return True when output still looks like model planning instead of final text."""
    cleaned = strip_think(text)
    if not cleaned:
        return False

    match = FINAL_MARKER_RE.search(cleaned)
    if match and match.start() > 0:
        return True

    for line in cleaned.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        return bool(REASONING_PREFIX_RE.match(stripped))
    return False
