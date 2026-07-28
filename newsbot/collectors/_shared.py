"""Shared concurrency primitives for collectors.

A single shared semaphore bounds concurrent leaf HTTP requests across
ALL collectors (RSS, Reddit, GitHub, Hacker News, Product Hunt, HuggingFace)
to prevent overwhelming the event loop or hitting connection limits.
"""
from __future__ import annotations

import asyncio

# Maximum concurrent HTTP requests across all collectors.
# 10 is a safe default that balances throughput with resource limits.
_MAX_CONCURRENT = 10

# Single shared semaphore instance — imported by all collectors.
# Using one instance (not per-module) ensures aggregate concurrency
# is truly bounded to _MAX_CONCURRENT.
_shared_semaphore: asyncio.Semaphore | None = None


def get_shared_semaphore() -> asyncio.Semaphore:
    """Return the shared collector semaphore (lazily initialized).

    Must be called from within an async context (event loop running).
    """
    global _shared_semaphore
    if _shared_semaphore is None:
        _shared_semaphore = asyncio.Semaphore(_MAX_CONCURRENT)
    return _shared_semaphore