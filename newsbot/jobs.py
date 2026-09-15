"""Job coordinator: one lock, per-kind admission, no I/O.

Telegram delivery lives in newsbot.poster. Generation/recap/LLM live
in their own modules. This file only serialises work.
"""
from __future__ import annotations

import asyncio
import enum
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

from newsbot.outcome import Outcome

log = logging.getLogger(__name__)

T = TypeVar("T")


class JobKind(enum.Enum):
    GENERATION = "generation"
    POSTING = "posting"
    SUMMARY = "summary"


class Busy(Exception):
    """A second concurrent call of the same JobKind was refused."""


class JobCoordinator:
    """Serialise jobs on one lock with per-kind admission flags."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._running: dict[JobKind, bool] = {kind: False for kind in JobKind}
        self.last_outcome: dict[JobKind, Outcome] = {}

    def running(self, kind: JobKind) -> bool:
        return self._running[kind]

    async def run_exclusive(
        self,
        kind: JobKind,
        fn: Callable[[], Awaitable[T]],
        *,
        timeout: float = 0,
    ) -> T:
        """Run *fn* under the lock. Same-kind overlap raises Busy.

        A timeout > 0 wraps fn in wait_for. TimeoutError propagates —
        callers (generation.py) map it to Outcome.FAILED.
        """
        if self._running[kind]:
            log.info("%s already in progress — skipping", kind.value)
            raise Busy
        self._running[kind] = True
        try:
            async with self._lock:
                if timeout > 0:
                    result = await asyncio.wait_for(fn(), timeout=timeout)
                else:
                    result = await fn()
                if isinstance(result, Outcome):
                    self.last_outcome[kind] = result
                return result
        finally:
            self._running[kind] = False


async def exclusive(
    coordinator: JobCoordinator,
    kind: JobKind,
    fn: Callable[[], Awaitable[T]],
    *,
    timeout: float = 0,
) -> Outcome:
    """run_exclusive with Busy/timeout mapped to Outcome."""
    try:
        result = await coordinator.run_exclusive(kind, fn, timeout=timeout)
    except Busy:
        return Outcome.BUSY
    except asyncio.TimeoutError:
        log.error("%s timed out after %ss", kind.value, timeout)
        return Outcome.FAILED
    return result if isinstance(result, Outcome) else Outcome.OK
