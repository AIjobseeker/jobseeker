"""Telegram notification — rich HTML message + resume/cover as file attachments."""
from __future__ import annotations

import html
import logging
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

import httpx
from minio import Minio
from minio.error import S3Error
from temporalio import activity

from shared.config import settings
from shared.models import GeneratedDocuments, JobPost, MatchResult, UserProfile

log = logging.getLogger("worker.notify")

SEND_MESSAGE_URL = "https://api.telegram.org/bot{token}/sendMessage"
SEND_DOCUMENT_URL = "https://api.telegram.org/bot{token}/sendDocument"


def e(s: str) -> str:
    """HTML-escape a string for safe use in Telegram HTML mode."""
    return html.escape(str(s), quote=False)


def _minio_client() -> Minio:
    return Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_secure,
    )


def _download_temp(minio_path: str, suffix: str) -> Path | None:
    """Download a MinIO object to a named temp file. Returns path or None."""
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
) -> bool:
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
        return False
    return True


async def _send_document(
    client: httpx.AsyncClient,
    token: str,
    chat_id: str,
    file_path: Path,
    filename: str,
    caption: str,
) -> bool:
    with open(file_path, "rb") as f:
        resp = await client.post(
            SEND_DOCUMENT_URL.format(token=token),
            data={"chat_id": chat_id, "caption": caption},
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
    docs = GeneratedDocuments(**docs_dict)

    chat_id = profile.notification.telegram_chat_id
    if not chat_id:
        log.warning("No Telegram chat_id for %s — skipping", profile.id)
        return False

    bot_token = profile.notification.telegram_bot_token or settings.telegram_bot_token
    if not bot_token:
        log.warning("No Telegram bot token for %s — skipping", profile.id)
        return False

    matched_skills = e(", ".join(match.matched_skills[:6]) or "—")
    missing_skills = e(", ".join(match.missing_skills[:3]) or "None")
    visa_line = "Visa OK" if match.visa_ok else "Check Visa"
    score_icon = "🟢" if match.score >= 0.80 else "🟡" if match.score >= 0.65 else "🔴"
    reasoning = e((match.reasoning or "")[:250])

    # Trim study guide to keep total message under 4096 chars
    study = (docs.study_guide or "").strip()
    study_preview = e(study[:900]) + (" …" if len(study) > 900 else "")

    main_text = (
        f"{score_icon} <b>{e(job.company)} — {e(job.title)}</b>\n"
        f"{e(job.location or 'Location TBD')}\n"
        f"Match: <b>{match.score:.0%}</b> | {visa_line}\n"
        f"\n"
        f"<b>Matched:</b> {matched_skills}\n"
        f"<b>Gaps:</b> {missing_skills}\n"
        f"\n"
        f"<i>{reasoning}</i>\n"
        f"\n"
        f"<b>Study Guide:</b>\n"
        f"<pre>{study_preview}</pre>\n"
        f"\n"
        f"<i>Generated {datetime.utcnow().strftime('%b %d %H:%M')} UTC</i>"
    )

    apply_button = {"inline_keyboard": [[{"text": "Apply Now", "url": job.url}]]}

    safe_company = "".join(c if c.isalnum() or c in "-_" else "_" for c in job.company)
    safe_title = "".join(c if c.isalnum() or c in "-_" else "_" for c in job.title)

    async with httpx.AsyncClient(timeout=30) as http:
        await _send_message(http, bot_token, chat_id, main_text, reply_markup=apply_button)

        # Resume as DOCX file attachment
        resume_tmp = _download_temp(docs.resume_minio_path, suffix=".docx")
        if resume_tmp:
            resume_filename = f"{safe_company}_{safe_title}_resume.docx"
            await _send_document(
                http, bot_token, chat_id, resume_tmp,
                filename=resume_filename,
                caption=f"Tailored resume — {job.title} @ {job.company}",
            )
            resume_tmp.unlink(missing_ok=True)
        else:
            log.warning("Resume not available for %s @ %s", job.title, job.company)

        # Cover letter — inline if fits, else as file
        cover_tmp = _download_temp(docs.cover_letter_minio_path, suffix=".txt")
        if cover_tmp:
            cover_text = cover_tmp.read_text(encoding="utf-8").strip()
            cover_tmp.unlink(missing_ok=True)
            if len(cover_text) <= 3500:
                cover_msg = f"<b>Cover Letter — {e(job.company)}</b>\n\n{e(cover_text)}"
                await _send_message(http, bot_token, chat_id, cover_msg)
            else:
                cover_file = Path(tempfile.mktemp(suffix=".txt"))
                cover_file.write_text(cover_text, encoding="utf-8")
                cover_filename = f"{safe_company}_{safe_title}_cover.txt"
                await _send_document(
                    http, bot_token, chat_id, cover_file,
                    filename=cover_filename,
                    caption=f"Cover letter — {job.title} @ {job.company}",
                )
                cover_file.unlink(missing_ok=True)
        else:
            log.warning("Cover letter not available for %s @ %s", job.title, job.company)

    log.info("Notification sent to %s for %s @ %s", profile.id, job.title, job.company)
    return True
