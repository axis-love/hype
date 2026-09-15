"""Single LLM client factory."""
from __future__ import annotations

from typing import Literal

from lm_client import LMClient
from newsbot.env import Env, resolve

Role = Literal["filter", "style"]


_clients: dict[Role, LMClient] = {}


def build_lm_client(role: Role = "style", env: Env | None = None) -> LMClient:
    """Build (or reuse) an LMClient from Env."""
    cached = _clients.get(role)
    if cached is not None:
        return cached
    env = resolve(env)
    base = env.lm_base
    if role == "filter":
        model = env.lm_filter_model or env.lm_model
        if not base or not model:
            raise RuntimeError(
                "LM_BASE and LM_MODEL (or LM_FILTER_MODEL) must be set in the environment"
            )
    else:
        model = env.lm_model
        if not base or not model:
            raise RuntimeError("LM_BASE and LM_MODEL must be set in the environment")
    timeout = env.lm_timeout
    headers: dict[str, str] = {}
    if env.lm_api_key:
        headers["Authorization"] = f"Bearer {env.lm_api_key}"
    client = LMClient(base, model, timeout, headers=headers, endpoint_path="/chat/completions")
    _clients[role] = client
    return client


async def aclose_clients() -> None:
    for client in _clients.values():
        await client.aclose()
    _clients.clear()


def validate_llm_env(env: Env | None = None) -> None:
    """Fail fast on missing/invalid LLM env at startup."""
    try:
        env = resolve(env)
    except ValueError as exc:
        raise RuntimeError(f"LLM configuration error: {exc}") from exc
    errors: list[str] = []
    if not env.lm_base.strip():
        errors.append("LM_BASE is not set")
    if not env.lm_model.strip():
        errors.append("LM_MODEL is not set")
    if not env.lm_api_key.strip():
        errors.append("LM_API_KEY is not set")
    if env.lm_timeout <= 0:
        errors.append(f"LM_TIMEOUT must be positive, got {env.lm_timeout}")
    admin_id = env.admin_user_id
    if admin_id and not admin_id.lstrip("-").isdigit():
        errors.append(f"ADMIN_USER_ID must be numeric, got {admin_id!r}")
    if errors:
        raise RuntimeError("LLM configuration error: " + "; ".join(errors))
