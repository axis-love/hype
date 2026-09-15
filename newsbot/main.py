"""News bot entrypoint — wiring only.

    python -m newsbot.main              # scheduled mode (default)
    python -m newsbot.main --once       # one-shot mode
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from core.logging_config import configure_logging
from core.log_sanitizer import redact_exception
from core.settings_store import SettingsStore, default_store
from newsbot.admin import AdminActions
from newsbot.api import start_api
from newsbot.bot_commands import BotCommandHandler
from newsbot.clock import DEFAULT_GEN_HOURS, gen_slots, latest_due_gen_slot, local_now, post_slot, summary_day
from newsbot.db import NewsStore
from newsbot.env import Env, resolve
from newsbot.generation import (
    GENERATION_TIMEOUT_SECONDS,
    _run_generation,
    _run_retention,
)
from newsbot.jobs import JobCoordinator, JobKind, exclusive
from newsbot.llm import aclose_clients, validate_llm_env
from newsbot.outcome import Outcome
from newsbot.poster import deliver_one, drain
from newsbot.recap import _run_summary

log = logging.getLogger(__name__)


async def _scheduler_summary_iteration(
    coordinator: JobCoordinator,
    store: NewsStore,
    settings: SettingsStore,
    *,
    now: datetime | None = None,
    env: Env | None = None,
) -> Outcome:
    env = resolve(env)
    if now is None:
        now = local_now(env.news_tz)
    if now.hour < 13:
        return Outcome.OK
    day = summary_day(now)
    last_summary_day = settings.get("scheduler", "last_summary_day", default="") or ""
    if last_summary_day == day:
        return Outcome.OK
    result = await exclusive(
        coordinator, JobKind.SUMMARY, lambda: _run_summary(store, settings, now, env)
    )
    if result is Outcome.OK or result is Outcome.NOTHING_TO_DO:
        settings.set("scheduler", "last_summary_day", day)
    else:
        log.warning("daily summary did not succeed (%s) — will retry for day %s", result, day)
    return result


async def _scheduler_gen_iteration(
    coordinator: JobCoordinator,
    store: NewsStore,
    settings: SettingsStore,
    gen_hours: list[int],
    *,
    now: datetime | None = None,
    timeout: float = 0,
    env: Env | None = None,
) -> Outcome:
    env = resolve(env)
    if now is None:
        now = local_now(env.news_tz)
    due_slot = latest_due_gen_slot(now, gen_hours)
    last_gen_slot = settings.get("scheduler", "last_gen_slot", default="") or ""
    if last_gen_slot == due_slot:
        return Outcome.OK
    log.info("generation cycle starting (slot=%s, now=%s)", due_slot, now.isoformat())
    gen_success = False
    result = Outcome.FAILED
    try:
        result = await exclusive(
            coordinator, JobKind.GENERATION,
            lambda: _run_generation(store, settings, env),
            timeout=timeout,
        )
        if result is Outcome.OK:
            gen_success = True
        elif result is Outcome.BUSY:
            log.info("generation skipped — already in progress")
        elif result is Outcome.NOTHING_TO_DO:
            log.info("generation no-progress — will retry on next tick")
        else:
            log.error("generation failed (%s)", result)
    except Exception as exc:
        log.error("generation cycle failed: %s", redact_exception(exc))
    finally:
        _run_retention(store, env)
    if gen_success:
        settings.set("scheduler", "last_gen_slot", due_slot)
        log.info("generation cycle complete (slot=%s)", due_slot)
    else:
        log.warning("generation did not succeed — will retry slot %s on next tick", due_slot)
    return Outcome.OK if gen_success else result


async def _scheduler_post_iteration(
    coordinator: JobCoordinator,
    store: NewsStore,
    settings: SettingsStore,
    *,
    now: datetime | None = None,
    env: Env | None = None,
) -> Outcome:
    env = resolve(env)
    if now is None:
        now = local_now(env.news_tz)
    slot = post_slot(now)
    if slot is None:
        return Outcome.OK
    last_post_slot = settings.get("scheduler", "last_post_slot", default="") or ""
    if last_post_slot == slot:
        return Outcome.OK
    post_success = False
    result = Outcome.FAILED
    try:
        result = await exclusive(
            coordinator, JobKind.POSTING, lambda: deliver_one(store, settings, env)
        )
        if result.consumes_slot:
            post_success = True
            if result is not Outcome.OK:
                log.debug("posting slot %s consumed by skip (%s)", slot, result)
        elif result is Outcome.BUSY:
            log.info("posting skipped — already in progress")
        else:
            log.error("posting failed (%s)", result)
    except Exception as exc:
        log.error("posting cycle failed: %s", redact_exception(exc))
    if post_success:
        settings.set("scheduler", "last_post_slot", slot)
    else:
        log.warning("posting did not succeed — will retry slot %s within the hour", slot)
    return result


async def _scheduled_loop(settings: SettingsStore, env: Env | None = None) -> None:
    env = resolve(env)
    store = NewsStore(Path(env.news_db))
    coordinator = JobCoordinator()
    gen_hours = gen_slots(env.gen_hours or DEFAULT_GEN_HOURS)

    bot_handler: BotCommandHandler | None = None
    if env.bot_token and env.admin_user_id:
        actions = AdminActions(store, settings, coordinator, gen_hours, env)
        bot_handler = BotCommandHandler(
            bot_token=env.bot_token,
            admin_user_id=env.admin_user_id,
            settings=settings,
            actions=actions,
        )

    async def generation_loop() -> None:
        log.info("generation scheduler started: slots=%s (%s)", gen_hours, local_now(env.news_tz).tzinfo)
        while True:
            await _scheduler_gen_iteration(
                coordinator, store, settings, gen_hours,
                timeout=GENERATION_TIMEOUT_SECONDS, env=env,
            )
            await asyncio.sleep(30)

    async def posting_loop() -> None:
        log.info("posting scheduler started: even hours (%s)", local_now(env.news_tz).tzinfo)
        while True:
            await _scheduler_post_iteration(coordinator, store, settings, env=env)
            await asyncio.sleep(30)

    async def summary_loop() -> None:
        log.info("daily summary scheduler started: 13:00 (%s)", local_now(env.news_tz).tzinfo)
        while True:
            await _scheduler_summary_iteration(coordinator, store, settings, env=env)
            await asyncio.sleep(60)

    api_runner = None
    if env.api_port and env.api_port > 0:
        try:
            api_runner = await start_api(store, env.api_port, settings=settings, env=env)
        except OSError as exc:
            log.error(
                "H4 consumer API failed to start on port %d — API disabled: %s",
                env.api_port, exc,
            )
            api_runner = None

    tasks = [
        asyncio.create_task(generation_loop()),
        asyncio.create_task(posting_loop()),
        asyncio.create_task(summary_loop()),
    ]
    if bot_handler:
        tasks.append(asyncio.create_task(bot_handler.poll_loop()))
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        log.info("shutting down")
    finally:
        if api_runner is not None:
            await api_runner.cleanup()
        if bot_handler:
            await bot_handler.close()
        await aclose_clients()
        store.close()


async def _once_or_dry(
    store: NewsStore, settings: SettingsStore, env: Env | None = None,
) -> int:
    env = resolve(env)
    coordinator = JobCoordinator()
    try:
        result = await exclusive(
            coordinator, JobKind.GENERATION,
            lambda: _run_generation(store, settings, env),
            timeout=GENERATION_TIMEOUT_SECONDS,
        )
        _run_retention(store, env)
        if result is Outcome.OK:
            result = await exclusive(
                coordinator, JobKind.POSTING, lambda: drain(store, settings, env)
            )
            return result.exit_code
        return result.exit_code if result is Outcome.FAILED else 0
    finally:
        await aclose_clients()


def main() -> None:
    parser = argparse.ArgumentParser(description="News bot pipeline")
    parser.add_argument("--once", action="store_true", help="Run generation + drain all posts and exit")
    args = parser.parse_args()

    load_dotenv()
    configure_logging(process_name="newsbot")
    env = Env.from_environ()
    validate_llm_env(env)

    settings: SettingsStore = default_store(env.news_db)

    if args.once or not env.bot_token:
        if not args.once:
            log.info("no BOT_TOKEN — running once (dry-run mode)")
        store = NewsStore(Path(env.news_db))
        try:
            code = asyncio.run(_once_or_dry(store, settings, env))
        finally:
            store.close()
        sys.exit(code)

    try:
        asyncio.run(_scheduled_loop(settings, env))
    except KeyboardInterrupt:
        log.info("shutting down")
        sys.exit(0)

if __name__ == "__main__":
    main()
