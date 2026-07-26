import asyncio
import json
import random
import re
from typing import Any, Dict, Optional, Tuple

import httpx


class LLMError(Exception):
    """Base for all LLM errors."""


class LLMTransientError(LLMError):
    """Retryable: transport failures, timeouts, 408, 429, 5xx, and specific 400s indicating temporary provider unavailability."""


class LLMPermanentError(LLMError):
    """Non-retryable: auth failures, validation errors, model not found, etc."""


class LMClient:
    """Minimal OpenAI-compatible chat client.

    Records the last request payload in `last_request` so the control plane
    can show exact JSON in Telegram (Debug button).
    """

    ALLOWED_PARAMS = {
        "temperature",
        "top_p",
        "max_tokens",
        "min_tokens",
        "top_k",
        "presence_penalty",
        "frequency_penalty",
        "repeat_penalty",
        "stop",
        "n",
        "seed",
        "stream",
        "logit_bias",
        "response_format",
        "chat_template_kwargs",
    }

    def __init__(
        self,
        base_url: str,
        model: str,
        timeout: float,
        *,
        headers: Optional[Dict[str, str]] = None,
        endpoint_path: str = "/v1/chat/completions",
        max_retries: int = 5,
    ):
        self.base_url = (base_url or "").rstrip("/")
        self.model = model
        self.timeout = timeout
        self.headers = dict(headers or {})
        self.endpoint_path = endpoint_path
        self.max_retries = max_retries
        # Debug export must NEVER leak secrets.
        # Keep last_request header-free by default.
        self.last_request: Optional[Dict[str, Any]] = None

    @staticmethod
    def _looks_like_html_error(body: str) -> bool:
        """True when the error body is a generic web-server HTML page, not JSON.

        A strong signal the base_url/endpoint_path points at something that is
        NOT an OpenAI-compatible API (e.g. a plain http.server). Such pages echo
        the request body, so they must never be spliced into the raised error."""
        head = (body or "").lstrip()[:256].lower()
        return (
            head.startswith("<!doctype html")
            or head.startswith("<html")
            or "<title>error response</title>" in (body or "").lower()
        )

    def _classify_http_error(self, status_code: int, response_body: str) -> Tuple[bool, type[LLMError], str]:
        # Non-JSON HTML error page => the endpoint is almost certainly misconfigured
        # (not an OpenAI-compatible API). Do NOT echo the body: it contains the full
        # request payload (the prompt), which would leak into logs/Telegram.
        if self._looks_like_html_error(response_body):
            msg = (
                f"LLM endpoint returned a non-JSON HTTP {status_code} HTML error page; "
                "base_url/endpoint_path is likely not an OpenAI-compatible API — check configuration"
            )
            if status_code in {500, 502, 503, 504}:
                return True, LLMTransientError, msg
            return False, LLMPermanentError, msg

        if status_code in {408, 423, 429, 500, 502, 503, 504}:
            return True, LLMTransientError, f"LLM request failed with HTTP {status_code}"

        if status_code in {401, 403}:
            return False, LLMPermanentError, f"LLM request failed with HTTP {status_code}"

        if status_code == 400:
            body_lower = response_body.lower()
            transient_markers = (
                "overloaded",
                "temporarily unavailable",
                "temporary unavailability",
                "try again later",
                "server is busy",
                "service unavailable",
            )
            if any(marker in body_lower for marker in transient_markers):
                # Do NOT include response body — providers may echo prompts or article text.
                return True, LLMTransientError, "LLM provider temporarily unavailable (HTTP 400)"
            # Do NOT include response body — providers may echo request fields including
            # article text, prompts, or other sensitive content.
            return False, LLMPermanentError, "LLM request failed with HTTP 400"

        return False, LLMPermanentError, f"LLM request failed with HTTP {status_code}"

    async def generate(self, messages, **params) -> Tuple[str, str]:
        filtered = {k: v for k, v in params.items() if v is not None and k in self.ALLOWED_PARAMS}

        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            **filtered,
        }

        url = f"{self.base_url}{self.endpoint_path}"
        # Record request for debugging — URL only (no auth headers, no body).
        self.last_request = {"url": url, "model": self.model}

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            last_error: Optional[LLMTransientError] = None
            attempts = max(1, self.max_retries)
            for attempt in range(attempts):
                try:
                    r = await client.post(url, json=payload, headers=(self.headers or None))
                    r.raise_for_status()

                    # Caddy/muse can intermittently return 200 with an empty body.
                    # Retry on empty responses.
                    if not r.content:
                        last_error = LLMTransientError("LLM returned 200 with empty body")
                        if attempt == attempts - 1:
                            break
                        delay = 1.0 * (2 ** attempt) + random.uniform(0.0, 0.5)
                        await asyncio.sleep(delay)
                        continue

                    data = r.json()
                    if not data or not data.get("choices"):
                        return "", "empty_response"

                    content = data["choices"][0]["message"]["content"]
                    return (
                        content if content is not None else "",
                        data["choices"][0].get("finish_reason", "")
                    )
                except httpx.TimeoutException:
                    last_error = LLMTransientError(f"LLM request timed out after {self.timeout}s")
                except httpx.TransportError:
                    last_error = LLMTransientError("LLM transport error (connection failed)")
                except httpx.HTTPStatusError as exc:
                    response = exc.response
                    response_body = ""
                    try:
                        response_body = response.text
                    except Exception:
                        try:
                            response_body = json.dumps(response.json())
                        except Exception:
                            response_body = ""
                    is_transient, error_class, message = self._classify_http_error(
                        response.status_code,
                        response_body,
                    )
                    error = error_class(message)
                    if not is_transient:
                        raise error from exc
                    last_error = error

                if attempt == attempts - 1:
                    break
                delay = 1.0 * (2 ** attempt) + random.uniform(0.0, 0.5)
                await asyncio.sleep(delay)

            if last_error is not None:
                raise last_error
            raise LLMTransientError("LLM request failed after retries with no error detail")