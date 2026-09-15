"""Shared httpx client helper.

When *existing* is passed, kwargs are ignored — per-request timeout/headers
must go on the get/post call. When *existing* is None, a new client is
opened with those kwargs and closed on exit.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import httpx


@asynccontextmanager
async def owned_client(
    existing: httpx.AsyncClient | None = None,
    *,
    client_cls: type[httpx.AsyncClient] = httpx.AsyncClient,
    **kwargs: Any,
) -> AsyncIterator[httpx.AsyncClient]:
    if existing is not None:
        yield existing
        return
    async with client_cls(**kwargs) as client:
        yield client
