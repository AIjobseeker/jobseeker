"""Telegram notification — rich HTML alert + 4 inline buttons + files as replies."""
from __future__ import annotations

import hashlib
import html
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path

import httpx
from minio import Minio
from minio.error import S3Error
from temporalio import activity

from shared.config import settings
from shared.models import JobPost, MatchResult, UserProfile

log = logging.getLogger("worker.notify")

SEND_MESSAGE_URL = "https://api.telegram.org/bot{token}/sendMessage"
SEND_DOCUMENT_URL = "https://api.telegram.org/bot{token}/sendDocument"


def e(s: str) -> str:
    return html.escape(str(s), quote=False)


def _short_id(company: str, job_id: str) -> str:
    raw = f"{company.lower()}|{job_id}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def _minio_client() -> Minio:
    return Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_secure,
    )


def _download_temp(minio_path: str, suffix: str) -> Path | None:
    if not minio_path:
        return None
    try:
        client = _minio_client()
        fd, tmp_path = tempfile.mkstemp(suffix=suffix)
        os.close(fd)
        client.fget_object(settings.minio_bucket, minio_path, tmp_path)
        return Path(tmp_path)
    except (S3Error, Exception) as exc:
        log.warning("Could not download %s from MinIO: %s", minio_path, exc)
        return None


async def _send_message(
    client: httpx.AsyncClient,
    token: str,
    chat_id: str,
    text: str,
    reply_markup: dict | None = None,
) -> int | None:
    """Send HTML message, return message_id or None."""
    payload: dict = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    resp = await client.post(SEND_MESSAGE_URL.format(token=token), json=payload)
    if resp.status_code != 200:
        log.error("sendMessage failed: %s — %s", resp.status_code, resp.text[:300])
        return None
    try:
        return int(resp.json()["result"]["message_id"])
    except (KeyError, ValueError, TypeError):
        return None


async def _send_document(
    client: httpx.AsyncClient,
    token: str,
    chat_id: str,
    file_path: Path,
    filename: str,
    caption: str,
    reply_to: int | None = None,
) -> bool:
    """Upload a file as a Telegram document, optionally as reply_to a message."""
    with open(file_path, "rb") as f:
        data: dict = {"chat_id": chat_id, "caption": caption}
        if reply_to is not None:
            data["reply_to_message_id"] = str(reply_to)
        resp = await client.post(
            SEND_DOCUMENT_URL.format(token=token),
            data=data,
            files={"document": (filename, f)},
            timeout=60,
        )
    if resp.status_code != 200:
        log.error("sendDocument failed: %s — %s", resp.status_code, resp.text[:300])
        return False
    return True


@activity.defn
async def notify_telegram(
    job_dict: dict,
    profile_dict: dict,
    match_dict: dict,
    docs_dict: dict,
) -> bool:
    job = JobPost(**job_dict)
    profile = UserProfile(**profile_dict)
    match = MatchResult(**match_dict)

    chat_id = profile.notification.telegram_chat_id
    if not chat_id:
        log.warning("No Telegram chat_id for %s — skipping", profile.id)
        return False

    bot_token = profile.notification.telegram_bot_token or settings.telegram_bot_token
    if not bot_token:
        log.warning("No Telegram bot token for %s — skipping", profile.id)
        return False

    # Build main alert text
    matched_skills = e(", ".join(match.matched_skills[:6]) or "—")
    missing_skills = e(", ".join(match.missing_skills[:3]) or "None")
    visa_line = "✅ Visa OK" if match.visa_ok else "⚠️ Check Visa"
    score_icon = "🟢" if match.score >= 0.80 else "🟡" if match.score >= 0.65 else "🔴"
    reasoning = e((match.reasoning or "")[:300])

    main_text = (
        f"{score_icon} <b>{e(job.company)} — {e(job.title)}</b>\n"
        f"{e(job.location or 'Location TBD')} | {visa_line}\n"
        f"Match: <b>{match.score:.0%}</b>\n"
        f"\n"
        f"<b>Matched:</b> {matched_skills}\n"
        f"<b>Gaps:</b> {missing_skills}\n"
        f"\n"
        f"<i>{reasoning}</i>\n"
        f"\n"
        f"<i>Tailored resume + cover + interview notes attached ↓</i>\n"
        f"<i>Generated {datetime.utcnow().strftime('%b %d %H:%M')} UTC</i>"
    )

    # 4-button inline keyboard
    dedup_id = _short_id(job.company, job.id)
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "Open Job", "url": job.url},
                {"text": "Mark Applied", "callback_data": f"applied:{dedup_id}"},
            ],
            [
                {"text": "Skip", "callback_data": f"skip:{dedup_id}"},
                {"text": "Save for Later", "callback_data": f"saved:{dedup_id}"},
            ],
        ]
    }

    safe_company = "".join(c if c.isalnum() or c in "-_" else "_" for c in job.company)
    safe_title = "".join(c if c.isalnum() or c in "-_" else "_" for c in job.title)

    async with httpx.AsyncClient(timeout=30) as http:
        # Main alert message — get message_id for reply_to
        msg_id = await _send_message(http, bot_token, chat_id, main_text, reply_markup=keyboard)

        # Resume as DOCX — reply to main message
        resume_tmp = _download_temp(docs_dict.get("resume_minio_path", ""), suffix=".docx")
        if resume_tmp:
            await _send_document(
                http, bot_token, chat_id, resume_tmp,
                filename=f"{safe_company}_{safe_title}_resume.docx",
                caption=f"📄 Tailored resume — {job.title} @ {job.company}",
                reply_to=msg_id,
            )
            resume_tmp.unlink(missing_ok=True)
        else:
            log.warning("Resume not available for %s @ %s", job.title, job.company)

        # Cover letter — inline if short, else as file (reply to main message)
        cover_tmp = _download_temp(docs_dict.get("cover_letter_minio_path", ""), suffix=".txt")
        if cover_tmp:
            cover_text = cover_tmp.read_text(encoding="utf-8").strip()
            cover_tmp.unlink(missing_ok=True)
            if len(cover_text) <= 3500:
                cover_msg = f"✉️ <b>Cover Letter — {e(job.company)}</b>\n\n{e(cover_text)}"
                await _send_message(http, bot_token, chat_id, cover_msg)
            else:
                cover_file = Path(tempfile.mktemp(suffix=".txt"))
                cover_file.write_text(cover_text, encoding="utf-8")
                await _send_document(
                    http, bot_token, chat_id, cover_file,
                    filename=f"{safe_company}_{safe_title}_cover.txt",
                    caption=f"✉️ Cover letter — {job.title} @ {job.company}",
                    reply_to=msg_id,
                )
                cover_file.unlink(missing_ok=True)
        else:
            log.warning("Cover letter not available for %s @ %s", job.title, job.company)

        # Interview defense notes — reply to main message
        defense_tmp = _download_temp(docs_dict.get("defense_minio_path", ""), suffix=".md")
        if defense_tmp:
            await _send_document(
                http, bot_token, chat_id, defense_tmp,
                filename=f"Interview_defense_{safe_company}.md",
                caption=f"🛡️ Interview defense notes — {job.company}",
                reply_to=msg_id,
            )
            defense_tmp.unlink(missing_ok=True)
        else:
            log.info("No defense notes for %s @ %s", job.title, job.company)

        # Interview prep guide — reply to main message
        prep_tmp = _download_temp(docs_dict.get("prep_minio_path", ""), suffix=".md")
        if prep_tmp:
            await _send_document(
                http, bot_token, chat_id, prep_tmp,
                filename=f"Interview_prep_{safe_company}.md",
                caption=f"📚 Interview prep — {job.company}",
                reply_to=msg_id,
            )
            prep_tmp.unlink(missing_ok=True)
        else:
            log.info("No prep guide for %s @ %s", job.title, job.company)

    log.info(
        "Notification sent to %s for %s @ %s (msg_id=%s)",
        profile.id, job.title, job.company, msg_id,
    )
    return True
