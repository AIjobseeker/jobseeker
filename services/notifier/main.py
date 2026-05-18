"""Notifier service — runs both the dedup republisher and the Telegram
dispatcher in a single asyncio process so they can share one DedupStore.

Pipeline:
    NATS jobs.scored
        -> dedup (drop if seen, else insert + republish jobs.new)
        -> NATS jobs.new
        -> telegram_dispatch (filter by score, send to Telegram, mark notified)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Optional

import nats

from services.notifier.bot_listener import run_listener
from services.notifier.dedup import DedupStore, compute_key
from services.notifier.telegram_dispatch import (
    TelegramDispatcher,
    env_min_score,
    run_dispatcher_loop,
)
from services.notifier.models import ScoredJob
from services.notifier.sheet_sync import SheetSyncer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("notifier")


def default_db_path() -> Path:
    raw = os.environ.get("JOBSEEKER_DB_PATH", "~/.jobseeker/seen.db")
    return Path(raw).expanduser()


class DedupRepublisher:
    """Subscribes to `jobs.scored`, dedups, republishes to `jobs.new`."""

    def __init__(self, nc, store: DedupStore, *, out_subject: str = "jobs.new") -> None:
        self.nc = nc
        self.store = store
        self.out_subject = out_subject
        self.seen_total = 0
        self.new_total = 0
        self.dropped_total = 0
        # Reset windows for the once-a-minute stats line.
        self._new_window = 0
        self._dropped_window = 0

    def snapshot_window(self) -> tuple[int, int]:
        new = self._new_window
        dropped = self._dropped_window
        self._new_window = 0
        self._dropped_window = 0
        return new, dropped

    async def handle(self, msg) -> None:
        try:
            data = json.loads(msg.data.decode("utf-8"))
            payload = ScoredJob.model_validate(data)
        except (json.JSONDecodeError, ValueError) as exc:
            log.error("malformed jobs.scored payload: %s", exc)
            return

        self.seen_total += 1
        company, source_id = payload.dedup_key_inputs
        if not source_id:
            log.warning("missing source_id, dropping: %s", payload.job.title)
            self.dropped_total += 1
            self._dropped_window += 1
            return

        key = compute_key(company, source_id)
        is_new = self.store.insert_if_new(
            key=key,
            company=payload.job.company,
            title=payload.job.title,
            url=payload.job.url,
            score=payload.score,
        )
        if not is_new:
            self.dropped_total += 1
            self._dropped_window += 1
            return

        self.new_total += 1
        self._new_window += 1
        await self.nc.publish(self.out_subject, msg.data)

    async def run(self, *, subject: str = "jobs.scored", queue: Optional[str] = "notifier-dedup") -> None:
        if queue:
            await self.nc.subscribe(subject, queue=queue, cb=self.handle)
        else:
            await self.nc.subscribe(subject, cb=self.handle)
        log.info("dedup subscribed to %s -> %s", subject, self.out_subject)


async def stats_loop(repub: DedupRepublisher, dispatcher: TelegramDispatcher) -> None:
    while True:
        await asyncio.sleep(60)
        new, dropped = repub.snapshot_window()
        # NOTE: kept the exact phrasing from the spec for grep-friendliness.
        log.info(
            "seen=%d new_last_min=%d dropped_last_min=%d "
            "telegram_sent=%d telegram_skipped_low=%d telegram_failed=%d",
            repub.seen_total, new, dropped,
            dispatcher.sent, dispatcher.skipped_low_score, dispatcher.failed,
        )


async def main() -> None:
    nats_url = os.environ.get("NATS_URL", "nats://localhost:4222")
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    db_path = default_db_path()

    # Multi-profile support — each notifier instance subscribes to its own
    # NATS subjects so two people (Sai + GF) get cleanly separated streams
    # with separate bots, sheets, and dedup DBs.
    scored_subject = os.environ.get("SCORED_SUBJECT", "jobs.scored")
    new_subject = os.environ.get("NEW_SUBJECT", "jobs.new")
    person = os.environ.get("NOTIFIER_PERSON", "sai")

    if not bot_token or not chat_id:
        log.error("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set")
        sys.exit(2)

    log.info("connecting to NATS %s", nats_url)
    nc = await nats.connect(nats_url)
    store = DedupStore(db_path)
    log.info("dedup store opened at %s (current size=%d)", db_path, store.count())

    sheet_person = os.environ.get("NOTIFIER_SHEET_PERSON", person)
    sheet_syncer = SheetSyncer.from_env(person=sheet_person)
    if sheet_syncer:
        log.info("sheet sync enabled for %s (sheet_id=%s)",
                 sheet_person, sheet_syncer.sheet_id)
    else:
        log.info("sheet sync disabled — set GOOGLE_SHEETS_ID_%s and "
                 "GOOGLE_SERVICE_ACCOUNT_JSON to enable", sheet_person.upper())

    log.info(
        "notifier starting: person=%s scored=%s new=%s db=%s",
        person, scored_subject, new_subject, db_path,
    )

    async with TelegramDispatcher(
        bot_token=bot_token,
        chat_id=chat_id,
        store=store,
        min_score=env_min_score(),
    ) as dispatcher:
        # IMPORTANT: subscribe the DISPATCHER first, then the republisher.
        # Order matters because NATS Core is fire-and-forget — if the
        # republisher publishes to jobs.new.{person} before any subscriber
        # exists on that subject, those messages are dropped silently and
        # the same jobs come back as dedup hits forever (notified=0
        # permanently in seen.db). This is what caused the original
        # "85 jobs ≥ 0.65 but telegram_sent=0" bug on first deployment.
        repub = DedupRepublisher(nc, store, out_subject=new_subject)
        await run_dispatcher_loop(
            nc, dispatcher, sheet_syncer=sheet_syncer,
            subject=new_subject, queue=f"notifier-{person}",
            person=sheet_person,
        )
        await repub.run(subject=scored_subject, queue=f"notifier-dedup-{person}")

        # Bot listener — handles inline-button callbacks and updates status
        # in seen.db + Google Sheet. Runs in its own task; never blocks the
        # main event loop.
        listener = await run_listener(
            bot_token=bot_token, store=store, sheet_syncer=sheet_syncer,
        )

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:
                # Windows / restricted envs — fall back to KeyboardInterrupt.
                pass

        stats_task = asyncio.create_task(stats_loop(repub, dispatcher))
        try:
            await stop.wait()
        finally:
            stats_task.cancel()
            try:
                await stats_task
            except asyncio.CancelledError:
                pass
            await listener.stop()
            await listener.__aexit__(None, None, None)
            await nc.drain()
            store.close()
            log.info("notifier shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
