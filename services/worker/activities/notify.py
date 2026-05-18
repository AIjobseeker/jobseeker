"""Telegram notification activity — sends rich match alert with resume + cover as attachments."""
from __future__ import annotations

import logging
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


def _minio_client() -> Minio:
    return Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_secure,
    )


def _download_temp(minio_path: str, suffix: str) -> Path | None:
    """Download a MinIO object to a temp file. Returns path or None on failure."""
    try:
        client = _minio_client()
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        tmp.close()
        client.fget_object(settings.minio_bucket, minio_path, tmp.name)
        return Path(tmp.name)
    except (S3Error, Exception) as e:
        log.warning("Could not download %s from MinIO: %s", minio_path, e)
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
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    resp = await client.post(SEND_MESSAGE_URL.format(token=token), json=payload)
    if resp.status_code != 200:
        log.error("sendMessage failed: %s %s", resp.status_code, resp.text[:200])
        return False
    return True


async def _send_document(
    client: httpx.AsyncClient,
    token: str,
    chat_id: str,
    file_path: Path,
    caption: str,
) -> bool:
    with open(file_path, "rb") as f:
        resp = await client.post(
            SEND_DOCUMENT_URL.format(token=token),
            data={"chat_id": chat_id, "caption": caption},
            files={"document": (file_path.name, f)},
        )
    if resp.status_code != 200:
        log.error("sendDocument failed: %s %s", resp.status_code, resp.text[:200])
        return False
    return True


@activity.defn
async def notify_telegram(
    job_dict: dict,
    profile_dict: dict,
    match_dict: dict,
    docs_dict: dict,
) -> bool:
    """Send Telegram notification: job alert message + resume file + cover letter."""
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

    matched_skills = ", ".join(match.matched_skills[:6]) or "—"
    missing_skills = ", ".join(match.missing_skills[:3]) or "None"
    visa_flag = "✅ Visa OK" if match.visa_ok else "⚠️ Check Visa"
    score_bar = "🟢" if match.score >= 0.80 else "🟡" if match.score >= 0.65 else "🔴"

    # Truncate study guide to fit Telegram's 4096 char message limit
    study_preview = docs.study_guide[:1200].strip()
    if len(docs.study_guide) > 1200:
        study_preview += "\n…"

    main_text = (
        f"{score_bar} *{job.company} — {job.title}*\n"
        f"{job.location or 'Location TBD'}\n"
        f"Match: *{match.score:.0%}* | {visa_flag}\n"
        f"\n"
        f"*Skills matched:* {matched_skills}\n"
        f"*Gaps:* {missing_skills}\n"
        f"\n"
        f"*Reasoning:* _{match.reasoning[:300]}_\n"
        f"\n"
        f"📚 *Study Guide:*\n"
        f"```\n{study_preview}\n```\n"
        f"\n"
        f"_Generated {datetime.utcnow().strftime('%b %d %H:%M')} UTC_"
    )

    apply_button = {
        "inline_keyboard": [[{"text": "🔗 Apply Now", "url": job.url}]]
    }

    async with httpx.AsyncClient(timeout=30) as http:
        # 1. Main message
        await _send_message(http, bot_token, chat_id, main_text, reply_markup=apply_button)

        # 2. Resume DOCX as file attachment
        resume_file = _download_temp(docs.resume_minio_path, suffix=".docx")
        if resume_file:
            # Rename to a human-readable filename before sending
            readable = resume_file.parent / f"{job.company}_{job.title}_resume.docx".replace(" ", "_")
            resume_file.rename(readable)
            await _send_document(
                http, bot_token, chat_id, readable,
                caption=f"📄 Tailored resume for {job.title} @ {job.company}",
            )
            readable.unlink(missing_ok=True)

        # 3. Cover letter — send inline if short enough, otherwise as file
        cover_file = _download_temp(docs.cover_letter_minio_path, suffix=".txt")
        if cover_file:
            cover_text = cover_file.read_text(encoding="utf-8").strip()
            cover_file.unlink(missing_ok=True)

            if len(cover_text) <= 3800:
                cover_msg = f"✉️ *Cover Letter — {job.company}*\n\n{cover_text}"
                await _send_message(http, bot_token, chat_id, cover_msg)
            else:
                # Too long for a message; send as a text file
                tmp_cover = Path(tempfile.mktemp(suffix=".txt"))
                tmp_cover.write_text(cover_text, encoding="utf-8")
                await _send_document(
                    http, bot_token, chat_id, tmp_cover,
                    caption=f"✉️ Cover letter for {job.title} @ {job.company}",
                )
                tmp_cover.unlink(missing_ok=True)

    log.info("Notification sent to %s for %s @ %s", profile.id, job.title, job.company)
    return True
