"""Telegram callback-query listener.

Long-polls /getUpdates and converts inline-button presses into status
updates on seen.db and the Google Sheet. No public webhook needed.

Callback data format:  "<action>:<short_dedup_id>"
  action  -> applied | skip | saved
  short_dedup_id -> first 16 hex chars of sha256(company.lower()|source_id)

We don't trust the user to send a valid callback — both DB and sheet
updates are no-ops if the dedup row isn't found.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

import httpx

from services.notifier.dedup import (
    DedupStore,
    STATUS_APPLIED,
    STATUS_SKIPPED,
)
from services.notifier.sheet_sync import SheetSyncer

log = logging.getLogger("notifier.bot_listener")

POLL_TIMEOUT_S = 25
HTTP_TIMEOUT_S = POLL_TIMEOUT_S + 10  # must exceed long-poll timeout

ACTION_TO_STATUS = {
    "applied": STATUS_APPLIED,
    "skip": STATUS_SKIPPED,
    "saved": "SAVED",
}


class BotListener:
    """Long-polls getUpdates, dispatches callback_query to handlers."""

    def __init__(
        self,
        bot_token: str,
        store: DedupStore,
        sheet_syncer: Optional[SheetSyncer] = None,
        poll_timeout: int = POLL_TIMEOUT_S,
    ) -> None:
        self.bot_token = bot_token
        self.store = store
        self.sheet_syncer = sheet_syncer
        self.poll_timeout = poll_timeout
        self.offset = 0
        self.processed = 0
        self.errors = 0
        self._client: Optional[httpx.AsyncClient] = None
        self._stop = asyncio.Event()

    @property
    def base(self) -> str:
        return f"https://api.telegram.org/bot{self.bot_token}"

    async def __aenter__(self) -> "BotListener":
        self._client = httpx.AsyncClient(timeout=HTTP_TIMEOUT_S)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def stop(self) -> None:
        self._stop.set()

    async def _get_updates(self) -> list[dict]:
        assert self._client is not None
        params = {
            "offset": self.offset,
            "timeout": self.poll_timeout,
            "allowed_updates": '["callback_query"]',
        }
        try:
            resp = await self._client.get(f"{self.base}/getUpdates", params=params)
        except httpx.RequestError as e:
            self.errors += 1
            log.warning("getUpdates network error (will retry): %s", e)
            await asyncio.sleep(2)
            return []
        if resp.status_code != 200:
            self.errors += 1
            log.warning("getUpdates HTTP %s: %s", resp.status_code, resp.text[:200])
            await asyncio.sleep(2)
            return []
        data = resp.json()
        if not data.get("ok"):
            self.errors += 1
            log.warning("getUpdates not ok: %s", data)
            return []
        return data.get("result", []) or []

    async def _answer_callback(self, callback_id: str, text: str) -> None:
        assert self._client is not None
        try:
            await self._client.post(
                f"{self.base}/answerCallbackQuery",
                json={"callback_query_id": callback_id, "text": text},
            )
        except httpx.RequestError as e:
            log.warning("answerCallbackQuery failed (non-fatal): %s", e)

    async def _edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
    ) -> None:
        """Replace the original alert text to show the new status. Best-effort."""
        assert self._client is not None
        try:
            await self._client.post(
                f"{self.base}/editMessageText",
                json={
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "text": text,
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": True,
                },
            )
        except httpx.RequestError as e:
            log.warning("editMessageText failed (non-fatal): %s", e)

    async def _handle_callback(self, cb: dict) -> None:
        cb_id = cb.get("id", "")
        data = cb.get("data", "") or ""
        message = cb.get("message", {}) or {}
        chat_id = (message.get("chat") or {}).get("id")
        message_id = message.get("message_id")
        original_text = message.get("text", "") or ""

        if ":" not in data:
            await self._answer_callback(cb_id, "unknown action")
            return

        action, _, short_id = data.partition(":")
        status = ACTION_TO_STATUS.get(action)
        if status is None:
            await self._answer_callback(cb_id, "unknown action")
            return

        row = self.store.get_by_dedup_id_short(short_id)
        if row is None:
            await self._answer_callback(cb_id, "job no longer in DB")
            return

        full_key, company, title, url, _prev_status = row

        # Update DB
        try:
            self.store.update_status(full_key, status)
        except Exception as e:
            log.exception("DB status update failed for %s: %s", full_key, e)
            await self._answer_callback(cb_id, "DB update failed")
            return

        # Update sheet (best-effort; never fails the user feedback)
        if self.sheet_syncer is not None:
            try:
                await self.sheet_syncer.update_status_by_dedup_id(short_id, status)
            except Exception:
                log.exception("sheet status update failed (non-fatal)")

        await self._answer_callback(cb_id, f"Marked: {status}")

        # Edit the original message to reflect the new state.
        if chat_id and message_id:
            tag = {"APPLIED": "[APPLIED]", "SKIPPED": "[SKIPPED]", "SAVED": "[SAVED]"}.get(
                status, f"[{status}]"
            )
            new_text = f"{tag}\n\n{original_text}"
            await self._edit_message(chat_id, message_id, new_text)

        self.processed += 1
        log.info("status update: %s @ %s -> %s", title, company, status)

    async def run(self) -> None:
        log.info("bot listener started (long-poll timeout=%ds)", self.poll_timeout)
        while not self._stop.is_set():
            updates = await self._get_updates()
            for upd in updates:
                # Always advance offset so we don't reprocess.
                upd_id = upd.get("update_id", 0)
                if upd_id >= self.offset:
                    self.offset = upd_id + 1
                cb = upd.get("callback_query")
                if cb:
                    try:
                        await self._handle_callback(cb)
                    except Exception:
                        log.exception("callback handler crashed (continuing)")
        log.info("bot listener stopped")


async def run_listener(
    bot_token: str,
    store: DedupStore,
    sheet_syncer: Optional[SheetSyncer] = None,
) -> BotListener:
    listener = BotListener(bot_token=bot_token, store=store, sheet_syncer=sheet_syncer)
    await listener.__aenter__()
    asyncio.create_task(listener.run(), name="bot-listener")
    return listener
