"""Coordinator lock/admission tests (H12)."""
from __future__ import annotations

import asyncio

import pytest

from newsbot.jobs import Busy, JobCoordinator, JobKind
from newsbot.outcome import Outcome


@pytest.mark.asyncio
async def test_run_exclusive_returns_callable_value() -> None:
    coord = JobCoordinator()

    async def fn():
        return "report"

    assert await coord.run_exclusive(JobKind.GENERATION, fn) == "report"


@pytest.mark.asyncio
async def test_same_kind_concurrent_raises_busy() -> None:
    coord = JobCoordinator()
    started = asyncio.Event()
    release = asyncio.Event()

    async def hold():
        started.set()
        await release.wait()
        return Outcome.OK

    task = asyncio.create_task(coord.run_exclusive(JobKind.GENERATION, hold))
    await started.wait()
    with pytest.raises(Busy):
        await coord.run_exclusive(JobKind.GENERATION, hold)
    release.set()
    assert await task is Outcome.OK


@pytest.mark.asyncio
async def test_different_kinds_serialise_on_one_lock() -> None:
    coord = JobCoordinator()
    order: list[str] = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def gen():
        order.append("gen-start")
        started.set()
        await release.wait()
        order.append("gen-end")
        return Outcome.OK

    async def post():
        order.append("post")
        return Outcome.OK

    t1 = asyncio.create_task(coord.run_exclusive(JobKind.GENERATION, gen))
    await started.wait()
    t2 = asyncio.create_task(coord.run_exclusive(JobKind.POSTING, post))
    await asyncio.sleep(0.05)
    assert "post" not in order
    release.set()
    await t1
    await t2
    assert order == ["gen-start", "gen-end", "post"]
