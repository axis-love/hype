"""Single LLM client factory."""
from __future__ import annotations

import os
from typing import Literal

from lm_client import LMClient

Role = Literal["filter", "style"]


_clients: dict[Role, LMClient] = {}


def build_lm_client(role: Role = "style") -> LMClient:
    """Build (or reuse) an LMClient from env."""
    cached = _clients.get(role)
    if cached is not None:
        return cached
    base = os.getenv("LM_BASE", "").rstrip("/")
    if role == "filter":
        model = os.getenv("LM_FILTER_MODEL", "") or os.getenv("LM_MODEL", "")
        if not base or not model:
            raise RuntimeError(
                "LM_BASE and LM_MODEL (or LM_FILTER_MODEL) must be set in the environment"
            )
    else:
        model = os.getenv("LM_MODEL", "")
        if not base or not model:
            raise RuntimeError("LM_BASE and LM_MODEL must be set in the environment")
    timeout = float(os.getenv("LM_TIMEOUT", "300"))
    headers: dict[str, str] = {}
    api_key = os.getenv("LM_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    client = LMClient(base, model, timeout, headers=headers, endpoint_path="/chat/completions")
    _clients[role] = client
    return client


async def aclose_clients() -> None:
    for client in _clients.values():
        await client.aclose()
    _clients.clear()


def validate_llm_env() -> None:
    """Fail fast on missing/invalid LLM env at startup."""
    errors: list[str] = []
    if not os.getenv("LM_BASE", "").strip():
        errors.append("LM_BASE is not set")
    if not os.getenv("LM_MODEL", "").strip():
        errors.append("LM_MODEL is not set")
    if not os.getenv("LM_API_KEY", "").strip():
        errors.append("LM_API_KEY is not set")
    lm_timeout = os.getenv("LM_TIMEOUT", "300")
    try:
        t = float(lm_timeout)
        if t <= 0:
            errors.append(f"LM_TIMEOUT must be positive, got {t}")
    except ValueError:
        errors.append(f"LM_TIMEOUT must be numeric, got {lm_timeout!r}")
    admin_id = os.getenv("ADMIN_USER_ID", "").strip()
    if admin_id and not admin_id.lstrip("-").isdigit():
        errors.append(f"ADMIN_USER_ID must be numeric, got {admin_id!r}")
    if errors:
        raise RuntimeError("LLM configuration error: " + "; ".join(errors))
