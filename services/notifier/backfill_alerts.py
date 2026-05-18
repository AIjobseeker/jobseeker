"""Backfill Telegram alerts for jobs that scored above threshold but were
never sent (notified=0 in seen_jobs).

Use case: the original notifier had a startup race condition where the
DedupRepublisher published to jobs.new.{person} before the dispatcher had
subscribed, so those messages were dropped (NATS Core = fire-and-forget).
Those jobs now sit in seen.db with score ≥ threshold but notified=0 forever.

This script reads seen.db directly, picks the highest-scoring un-notified
jobs, and sends a streaming-style Telegram alert per job. It does NOT run
the full tailor pipeline (use scripts/tailor_v2.py for that on a per-match
basis); the goal here is just to surface the existing matches that got
stranded.

Run from inside the notifier container:

    docker compose exec -T notifier-sai python3 -m services.notifier.backfill_alerts \
        --person sai --min-score 0.65 --limit 50

After running, those rows will have notified=1.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sqlite3
from pathlib import Path

from services.notifier.dedup import DedupStore

log = logging.getLogger("notifier.backfill")


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--person", required=True, choices=["sai", "gf"])
    ap.add_argument("--db", default=None,
                    help="Path to seen.db (default: from JOBSEEKER_DB_PATH env)")
    ap.add_argument("--min-score", type=float, default=0.65,
                    help="Only backfill jobs scoring >= this")
    ap.add_argument("--limit", type=int, default=50,
                    help="Max alerts to send (don't flood your phone)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print what would be sent, but don't send")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    # Resolve DB path
    db_path = args.db or os.environ.get("JOBSEEKER_DB_PATH")
    if not db_path:
        # Fall back to the per-person convention from the dev compose
        db_path = f"/data/seen_{args.person}.db"
    db_path = Path(db_path)
    if not db_path.exists():
        log.error("seen.db not found at %s", db_path)
        return 1
    log.info("seen.db: %s", db_path)

    # Telegram creds — same env layout as the notifier
    token = os.environ.get(f"TELEGRAM_BOT_TOKEN_{args.person.upper()}", "").strip() \
            or os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get(f"TELEGRAM_CHAT_ID_{args.person.upper()}", "").strip() \
           or os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not args.dry_run:
        if not token or not chat or chat in ("987654321", "0"):
            log.error("missing TELEGRAM_BOT_TOKEN_%s or TELEGRAM_CHAT_ID_%s",
                      args.person.upper(), args.person.upper())
            return 1

    # Pick rows to backfill
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute(
        """
        SELECT key, company, title, url, score, first_seen_at
        FROM seen_jobs
        WHERE notified = 0 AND score >= ?
        ORDER BY score DESC, first_seen_at DESC
        LIMIT ?
        """,
        (args.min_score, args.limit),
    ).fetchall()
    conn.close()
    log.info("found %d un-notified jobs above %.2f", len(rows), args.min_score)
    if not rows:
        log.info("nothing to backfill — all high-score jobs already notified")
        return 0

    if args.dry_run:
        for r in rows:
            print(f"  [score={r[4]:.3f}] {r[1]} - {r[2]}")
            print(f"     {r[3]}")
        return 0

    # Send each — reuse the streaming-notifier alert format (Markdown, 4-button keyboard).
    import httpx

    sent = 0
    failed = 0
    async with httpx.AsyncClient(timeout=20) as c:
        for key, company, title, url, score, _ts in rows:
            dedup_id = key[:16]
            text = (
                f"*BACKFILL — {company}*\n"
                f"*{title}*\n"
                f"Score: {int(round(score * 100))}%\n"
                f"\n"
                f"_Stranded by an earlier startup race; surfacing now._"
            )
            kb = {
                "inline_keyboard": [
                    [
                        {"text": "Apply (open job)", "url": url},
                        {"text": "Mark Applied", "callback_data": f"applied:{dedup_id}"},
                    ],
                    [
                        {"text": "Skip", "callback_data": f"skip:{dedup_id}"},
                        {"text": "Save for Later", "callback_data": f"saved:{dedup_id}"},
                    ],
                ],
            }
            try:
                r = await c.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={
                        "chat_id": chat, "text": text, "parse_mode": "Markdown",
                        "disable_web_page_preview": True, "reply_markup": kb,
                    },
                )
                if r.status_code != 200 or not r.json().get("ok"):
                    log.warning("send failed for %s: %s", dedup_id, r.text[:200])
                    failed += 1
                    continue
                msg_id = r.json()["result"]["message_id"]
                # Mark notified in db so we don't duplicate
                with DedupStore(db_path) as store:
                    store.mark_notified(key, message_id=msg_id)
                sent += 1
                log.info("sent  msg_id=%s  score=%.2f  %s — %s",
                         msg_id, score, company, title[:50])
            except Exception as e:
                log.warning("error sending %s: %s", dedup_id, e)
                failed += 1
            # Telegram rate limit: 30 msg/s to a chat. We're well under.
            await asyncio.sleep(0.4)

    log.info("\nbackfill complete: sent=%d failed=%d / %d", sent, failed, len(rows))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
