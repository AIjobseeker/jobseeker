"""Tests for the notifier service.

Run from the repo root: pytest services/notifier/test_notifier.py -q
"""
from __future__ import annotations

import asyncio
import re
import time
import unicodedata
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from services.notifier.dedup import DedupStore, compute_key
from services.notifier.telegram_dispatch import (
    TelegramDispatcher,
    TokenBucket,
    format_message,
)
from services.notifier.models import ScoredJob, ScoredJobInner


# ─────────────────────────── fixtures ───────────────────────────


def make_payload(score: float = 0.83, source_id: str = "stripe-123") -> ScoredJob:
    return ScoredJob(
        job=ScoredJobInner(
            id="uuid-changes-each-scrape",
            source_id=source_id,
            company="Stripe",
            title="Staff Site Reliability Engineer, Platform",
            url="https://jobs.stripe.com/123",
            location="San Francisco, Remote OK",
            remote=True,
            scraped_at="2026-05-17T15:23:00Z",
        ),
        score=score,
        embedding_score=score - 0.05,
        matched_skills=["kubernetes", "terraform", "aws"],
        missing_skills=["snowflake"],
        reason="Strong match: SRE title aligns with target. Concern: mentions 24/7 on-call.",
    )


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "seen.db"


@pytest.fixture
def store(db_path: Path) -> DedupStore:
    s = DedupStore(db_path)
    yield s
    s.close()


# ─────────────────────────── dedup ───────────────────────────


def test_dedup_blocks_repeats(store: DedupStore) -> None:
    payload = make_payload()
    company, source_id = payload.dedup_key_inputs
    key = compute_key(company, source_id)

    first = store.insert_if_new(
        key, payload.job.company, payload.job.title, payload.job.url, payload.score
    )
    assert first is True

    # Same identity, even with a different UUID, must NOT insert again.
    second = store.insert_if_new(
        key, payload.job.company, payload.job.title, payload.job.url, payload.score
    )
    assert second is False
    assert store.count() == 1


def test_dedup_persists(db_path: Path) -> None:
    payload = make_payload(source_id="persist-1")
    company, source_id = payload.dedup_key_inputs
    key = compute_key(company, source_id)

    s1 = DedupStore(db_path)
    assert s1.insert_if_new(
        key, payload.job.company, payload.job.title, payload.job.url, payload.score
    )
    s1.close()

    s2 = DedupStore(db_path)
    try:
        assert s2.has_seen(key) is True
        assert s2.insert_if_new(
            key, payload.job.company, payload.job.title, payload.job.url, payload.score
        ) is False
        assert s2.count() == 1
    finally:
        s2.close()


# ─────────────────────────── formatting ───────────────────────────


# Detect emoji codepoints. Telegram's API accepts emoji, but the user dislikes
# them, so we want a hard guarantee they never appear in our messages.
def _has_emoji(text: str) -> bool:
    for ch in text:
        if unicodedata.category(ch) == "So":
            return True
        cp = ord(ch)
        # Common emoji ranges.
        if 0x1F300 <= cp <= 0x1FAFF:
            return True
        if 0x2600 <= cp <= 0x27BF:
            return True
    return False


def test_telegram_format_no_emojis() -> None:
    payload = make_payload()
    msg = format_message(payload)
    assert not _has_emoji(msg), f"emoji leaked into message: {msg!r}"
    # Spec checks.
    assert "*NEW MATCH — Stripe*" in msg
    assert "Staff Site Reliability Engineer, Platform" in msg
    assert "Score: 83%" in msg
    assert "Remote: yes" in msg
    assert "kubernetes, terraform, aws" in msg
    assert "snowflake" in msg
    assert "[Apply](https://jobs.stripe.com/123)" in msg
    assert "scraped 2026-05-17 15:23 UTC" in msg


# ─────────────────────────── dispatcher ───────────────────────────


@pytest.mark.asyncio
async def test_telegram_skips_low_score(store: DedupStore) -> None:
    transport = httpx.MockTransport(
        lambda req: pytest.fail("should not call Telegram for low-score job")
    )
    async with httpx.AsyncClient(transport=transport) as client:
        dispatcher = TelegramDispatcher(
            bot_token="t",
            chat_id="c",
            store=store,
            min_score=0.65,
            client=client,
        )
        payload = make_payload(score=0.4)
        result = await dispatcher.handle(payload)
        assert result is False
        assert dispatcher.sent == 0
        assert dispatcher.skipped_low_score == 1


@pytest.mark.asyncio
async def test_telegram_marks_notified_on_success(store: DedupStore) -> None:
    payload = make_payload()
    company, source_id = payload.dedup_key_inputs
    key = compute_key(company, source_id)
    store.insert_if_new(
        key, payload.job.company, payload.job.title, payload.job.url, payload.score
    )

    captured: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append({"url": str(req.url), "json": req.read().decode()})
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        dispatcher = TelegramDispatcher(
            bot_token="abc",
            chat_id="42",
            store=store,
            min_score=0.65,
            client=client,
        )
        result = await dispatcher.handle(payload)
        assert result is True
        assert dispatcher.sent == 1

    # mark_notified should have flipped the bit.
    cur = store.conn.execute("SELECT notified FROM seen_jobs WHERE key = ?", (key,))
    row = cur.fetchone()
    assert row is not None and row[0] == 1
    assert "abc/sendMessage" in captured[0]["url"]


@pytest.mark.asyncio
async def test_telegram_rate_limit(store: DedupStore) -> None:
    """Burst of 25 msgs must take >= 15 seconds.

    Bucket is 20 capacity, refill 1 every 3s. After draining 20 instantly, the
    21st waits 3s, the 22nd 6s, ... so the 25th finishes ~15s after start.
    """
    handler = lambda req: httpx.Response(200, json={"ok": True})
    transport = httpx.MockTransport(handler)

    async with httpx.AsyncClient(transport=transport) as client:
        dispatcher = TelegramDispatcher(
            bot_token="t",
            chat_id="c",
            store=store,
            min_score=0.0,
            client=client,
            bucket=TokenBucket(capacity=20, refill_seconds=3.0),
        )

        # Insert all 25 keys up front so mark_notified has a row to update.
        payloads = [make_payload(source_id=f"rl-{i}") for i in range(25)]
        for p in payloads:
            company, source_id = p.dedup_key_inputs
            store.insert_if_new(
                compute_key(company, source_id),
                p.job.company, p.job.title, p.job.url, p.score,
            )

        start = time.monotonic()
        for p in payloads:
            ok = await dispatcher.handle(p)
            assert ok is True
        elapsed = time.monotonic() - start

    assert elapsed >= 15.0, f"rate limit not enforced: only took {elapsed:.2f}s"
    assert dispatcher.sent == 25
