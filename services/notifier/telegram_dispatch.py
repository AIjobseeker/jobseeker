"""Telegram dispatcher — subscribes to `jobs.new`, formats Markdown messages,
sends to Telegram, and marks the dedup row as notified on success.

Rate-limited to 20 msgs/min/chat via a token bucket. Telegram's official limit
is ~30/sec total but a tighter cap avoids triggering their spam heuristics.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Optional

import httpx

from services.notifier.dedup import DedupStore, compute_key
from services.notifier.models import ScoredJob

log = logging.getLogger("notifier.telegram")

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
DEFAULT_MIN_SCORE = 0.65

# Markdown special chars we strip from interpolated text. The full MarkdownV2
# spec escapes far more, but we use legacy "Markdown" mode which only requires
# us to keep `*` `_` `[` `]` `(` `)` from appearing inside dynamic strings.
_MD_SPECIALS = re.compile(r"[*_\[\]()]")


def _strip_md(text: str) -> str:
    return _MD_SPECIALS.sub("", text)


def _short_dedup_id(company: str, source_id: str) -> str:
    """Return the 16-char prefix of the dedup key for use in callback_data.
    Telegram caps callback_data at 64 bytes; we encode 'applied:<16hex>' = 24b.
    """
    return compute_key(company, source_id)[:16]


def _fmt_scraped_at(raw: str) -> str:
    """Best-effort ISO -> 'YYYY-MM-DD HH:MM UTC'. Falls back to raw."""
    if not raw:
        return "unknown"
    try:
        # Tolerate trailing Z
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return raw


def format_message(payload: ScoredJob) -> str:
    job = payload.job
    score_pct = int(round(payload.score * 100))
    remote = "yes" if job.remote else "no"
    location = _strip_md(job.location) if job.location else "Location TBD"

    matched = ", ".join(_strip_md(s) for s in payload.matched_skills) or "-"
    gaps = ", ".join(_strip_md(s) for s in payload.missing_skills) or "none"
    reason = _strip_md(payload.reason) if payload.reason else "no reason provided"

    company = _strip_md(job.company)
    title = _strip_md(job.title)

    # Block G — short legitimacy badge in the alert. Don't show if not assessed.
    legitimacy_line = ""
    tier = getattr(payload, "legitimacy_tier", "") or ""
    if tier:
        badge = {
            "HIGH_CONFIDENCE": "Posting: HIGH confidence (likely real)",
            "PROCEED_WITH_CAUTION": "Posting: caution (mixed signals)",
            "SUSPICIOUS": "Posting: SUSPICIOUS (possible ghost job)",
        }.get(tier, "")
        if badge:
            legitimacy_line = f"\n_{_strip_md(badge)}_"

    return (
        f"*NEW MATCH — {company}*\n"
        f"*{title}*\n"
        f"Score: {score_pct}% | Remote: {remote} | {location}{legitimacy_line}\n"
        f"\n"
        f"*Why match:* {reason}\n"
        f"\n"
        f"*Skills you have:* {matched}\n"
        f"*Gaps:* {gaps}\n"
        f"\n"
        f"[Apply]({job.url})\n"
        f"\n"
        f"_scraped {_fmt_scraped_at(job.scraped_at)}_"
    )


class TokenBucket:
    """Async token bucket. Default config: 20 tokens, refill 1 per 3s."""

    def __init__(self, capacity: int = 20, refill_seconds: float = 3.0) -> None:
        self.capacity = capacity
        self.refill_seconds = refill_seconds
        self.tokens = float(capacity)
        self.last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self.last_refill
        added = elapsed / self.refill_seconds
        if added > 0:
            self.tokens = min(self.capacity, self.tokens + added)
            self.last_refill = now

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                self._refill()
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                deficit = 1 - self.tokens
                wait = deficit * self.refill_seconds
            await asyncio.sleep(max(wait, 0.05))


class TelegramDispatcher:
    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        store: DedupStore,
        *,
        min_score: float = DEFAULT_MIN_SCORE,
        bucket: Optional[TokenBucket] = None,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        if not bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN required")
        if not chat_id:
            raise ValueError("TELEGRAM_CHAT_ID required")
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.store = store
        self.min_score = min_score
        self.bucket = bucket or TokenBucket()
        self._client = client
        self._owns_client = client is None
        self.sent = 0
        self.skipped_low_score = 0
        self.failed = 0

    async def __aenter__(self) -> "TelegramDispatcher":
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=15)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=15)
        return self._client

    async def handle(self, payload: ScoredJob) -> bool:
        """Process one scored job. Returns True if a Telegram send succeeded."""
        if payload.score < self.min_score:
            self.skipped_low_score += 1
            log.debug(
                "skip low-score job: %s @ %s (%.2f < %.2f)",
                payload.job.title, payload.job.company, payload.score, self.min_score,
            )
            return False

        await self.bucket.acquire()

        url = TELEGRAM_API.format(token=self.bot_token)
        text = format_message(payload)

        # Inline buttons let the user tap once to mark applied / skipped.
        # Telegram forwards the press as a callback_query that bot_listener.py
        # consumes and turns into a status update in seen.db + Google Sheet.
        short_id = _short_dedup_id(payload.job.company, payload.job.source_id)
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "Open Job", "url": payload.job.url},
                    {"text": "Mark Applied", "callback_data": f"applied:{short_id}"},
                ],
                [
                    {"text": "Skip", "callback_data": f"skip:{short_id}"},
                    {"text": "Save for Later", "callback_data": f"saved:{short_id}"},
                ],
            ]
        }

        body = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": False,
            "reply_markup": keyboard,
        }

        # Retry loop for 429s.
        for attempt in range(3):
            resp = await self.client.post(url, json=body)
            if resp.status_code == 200:
                self.sent += 1
                key = compute_key(payload.job.company, payload.job.source_id)
                # Capture message_id so bot_listener can edit/delete the
                # alert when the user taps a button.
                msg_id = None
                try:
                    msg_id = int(resp.json().get("result", {}).get("message_id"))
                except (ValueError, TypeError, KeyError):
                    pass
                self.store.mark_notified(key, message_id=msg_id)
                log.info("telegram sent: %s @ %s (msg_id=%s)",
                         payload.job.title, payload.job.company, msg_id)
                return True

            if resp.status_code in (401, 403):
                log.error(
                    "telegram auth failed (%s): %s — exiting",
                    resp.status_code, resp.text,
                )
                # Auth failures are unrecoverable; better to crash loud than
                # silently drop every notification from now on.
                sys.exit(1)

            if resp.status_code == 429:
                retry_after = 1.0
                try:
                    data = resp.json()
                    retry_after = float(data.get("parameters", {}).get("retry_after", 1))
                except (ValueError, KeyError, TypeError):
                    pass
                log.warning("telegram 429: sleeping %.1fs", retry_after)
                await asyncio.sleep(retry_after)
                continue

            log.error("telegram send failed: %s %s", resp.status_code, resp.text[:200])
            self.failed += 1
            return False

        self.failed += 1
        return False


async def run_dispatcher_loop(
    nc,
    dispatcher: TelegramDispatcher,
    *,
    subject: str = "jobs.new",
    queue: Optional[str] = "notifier",
    sheet_syncer=None,
    person: str = "sai",
) -> None:
    """Subscribe to NATS `jobs.new` and dispatch each message to Telegram.

    If `sheet_syncer` is provided, every message that meets the score
    threshold also gets appended/updated in the user's tracking sheet —
    independent of whether Telegram succeeds. Sheet writes are best-effort
    and never fail the Telegram leg.

    Per-job markdown reports are also written to the artifact store
    (~/.jobseeker/docs/<dedup_id>/report.md) — audit trail for every alert.
    """

    async def _handler(msg) -> None:
        try:
            data = json.loads(msg.data.decode("utf-8"))
            payload = ScoredJob.model_validate(data)
        except (json.JSONDecodeError, ValueError) as exc:
            log.error("malformed jobs.new payload: %s", exc)
            return

        # Write the per-job report BEFORE Telegram, so even if Telegram
        # fails the report is available locally and via the dedup_id.
        if payload.score >= dispatcher.min_score:
            try:
                from services.notifier.reports import write_report

                short_id = compute_key(payload.job.company, payload.job.source_id)[:16]
                await asyncio.to_thread(write_report, payload, short_id)
            except Exception:
                log.exception("report write failed (non-fatal)")

        # Telegram first (latency-critical), sheet second (best-effort).
        try:
            await dispatcher.handle(payload)
        except SystemExit:
            raise
        except Exception:
            log.exception("dispatcher.handle failed")
        if sheet_syncer is not None and payload.score >= dispatcher.min_score:
            try:
                await sheet_syncer.upsert(payload, person=person)
            except Exception:
                log.exception("sheet upsert failed (non-fatal)")

    if queue:
        await nc.subscribe(subject, queue=queue, cb=_handler)
    else:
        await nc.subscribe(subject, cb=_handler)
    log.info("subscribed to %s (min_score=%.2f, sheet_sync=%s)",
             subject, dispatcher.min_score, sheet_syncer is not None)


def env_min_score() -> float:
    raw = os.environ.get("TELEGRAM_MIN_SCORE", "")
    if not raw:
        return DEFAULT_MIN_SCORE
    try:
        return float(raw)
    except ValueError:
        log.warning("invalid TELEGRAM_MIN_SCORE=%r, using default", raw)
        return DEFAULT_MIN_SCORE
