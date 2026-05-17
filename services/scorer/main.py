from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
from pathlib import Path

import nats
from nats.aio.client import Client as NATSClient

from services.scorer.scorer import ProfileScorer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("scorer.main")

NATS_URL = os.getenv("NATS_URL", "nats://localhost:4222")
SUBJECT_RAW = os.getenv("SCORER_SUBJECT_RAW", "jobs.raw")
SUBJECT_SCORED = os.getenv("SCORER_SUBJECT_SCORED", "jobs.scored")
PROFILE_PATH = os.getenv("PROFILE_PATH", "profiles/sai/profile.parsed.yaml")
BATCH_SIZE = int(os.getenv("SCORER_BATCH_SIZE", "32"))
BATCH_FLUSH_INTERVAL = float(os.getenv("SCORER_BATCH_FLUSH_SECONDS", "1.0"))
QUEUE_GROUP = os.getenv("SCORER_QUEUE_GROUP", "scorer-workers")


class ScorerService:
    def __init__(
        self,
        scorer: ProfileScorer,
        nc: NATSClient,
        subject_raw: str = SUBJECT_RAW,
        subject_scored: str = SUBJECT_SCORED,
        batch_size: int = BATCH_SIZE,
        flush_interval: float = BATCH_FLUSH_INTERVAL,
    ) -> None:
        self.scorer = scorer
        self.nc = nc
        self.subject_raw = subject_raw
        self.subject_scored = subject_scored
        self.batch_size = batch_size
        self.flush_interval = flush_interval

        self._queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=batch_size * 8)
        self._stop = asyncio.Event()
        self._sub = None
        self._worker_task: asyncio.Task | None = None

    async def start(self) -> None:
        self._worker_task = asyncio.create_task(self._batch_worker(), name="scorer-batch-worker")
        self._sub = await self.nc.subscribe(
            self.subject_raw, queue=QUEUE_GROUP, cb=self._on_message
        )
        log.info("subscribed subject=%s queue=%s", self.subject_raw, QUEUE_GROUP)

    async def _on_message(self, msg) -> None:
        try:
            payload = json.loads(msg.data.decode("utf-8"))
        except Exception as e:
            log.warning("dropping invalid JSON message: %s", e)
            return
        await self._queue.put(payload)

    async def _batch_worker(self) -> None:
        while not self._stop.is_set() or not self._queue.empty():
            batch = await self._collect_batch()
            if not batch:
                continue
            try:
                results = await asyncio.to_thread(self.scorer.score_batch, batch)
            except Exception as e:
                log.exception("scoring batch failed (size=%d): %s", len(batch), e)
                continue
            for scored in results:
                try:
                    await self.nc.publish(
                        self.subject_scored, scored.model_dump_json().encode("utf-8")
                    )
                except Exception as e:
                    log.exception("publish failed: %s", e)
            log.info("scored batch=%d published=%s", len(results), self.subject_scored)

    async def _collect_batch(self) -> list[dict]:
        batch: list[dict] = []
        try:
            first = await asyncio.wait_for(self._queue.get(), timeout=self.flush_interval)
            batch.append(first)
        except asyncio.TimeoutError:
            return batch
        while len(batch) < self.batch_size:
            try:
                batch.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return batch

    async def stop(self) -> None:
        log.info("draining: queue=%d", self._queue.qsize())
        self._stop.set()
        if self._sub is not None:
            try:
                await self._sub.unsubscribe()
            except Exception:
                pass
        if self._worker_task is not None:
            try:
                await asyncio.wait_for(self._worker_task, timeout=30.0)
            except asyncio.TimeoutError:
                log.warning("worker did not drain in time; cancelling")
                self._worker_task.cancel()
        try:
            await self.nc.flush(timeout=5)
        except Exception:
            pass
        await self.nc.drain()


async def run() -> None:
    profile_path = Path(PROFILE_PATH)
    if not profile_path.exists():
        log.warning("profile not found at %s; nothing to score against. exiting.", profile_path)
        return

    scorer = ProfileScorer(profile_yaml_path=profile_path, embedding_batch_size=BATCH_SIZE)
    log.info("scorer ready: model=%s profile=%s", scorer.model_name, profile_path)

    nc = await nats.connect(NATS_URL, name="jobseeker-scorer", max_reconnect_attempts=-1)
    log.info("connected NATS at %s", NATS_URL)

    service = ScorerService(scorer=scorer, nc=nc)
    await service.start()

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _signal_handler() -> None:
        log.info("signal received; shutting down")
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            pass

    await stop_event.wait()
    await service.stop()
    log.info("scorer stopped cleanly")


if __name__ == "__main__":
    asyncio.run(run())
