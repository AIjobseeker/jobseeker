"""Telegram notification activity — sends formatted match alert."""
from __future__ import annotations

import logging
from datetime import datetime

import httpx
from temporalio import activity

from shared.config import settings
from shared.models import GeneratedDocuments, JobPost, MatchResult, UserProfile

log = logging.getLogger("worker.notify")

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def _format_message(
    job: JobPost,
    profile: UserProfile,
    match: MatchResult,
    docs: GeneratedDocuments,
) -> str:
    visa_flag = "OK" if match.visa_ok else "CHECK VISA"
    matched_skills = ", ".join(match.matched_skills[:6]) or "-"
    missing_skills = ", ".join(match.missing_skills[:4]) or "None"
    posted = job.posted_at.strftime("%b %d") if job.posted_at else "recent"

    return (
        f"*NEW MATCH — {job.company}*\n"
        f"*{job.title}*\n"
        f"{job.location or 'Location TBD'} | Posted: {posted}\n"
        f"Match: {match.score:.0%} | Visa: {visa_flag}\n"
        f"\n"
        f"*Skills match:* {matched_skills}\n"
        f"*Gaps:* {missing_skills}\n"
        f"\n"
        f"[Apply]({job.url}) | [Resume]({docs.resume_url}) | [Cover Letter]({docs.cover_letter_url})\n"
        f"\n"
        f"*Study Guide:*\n"
        f"```\n{docs.study_guide[:800]}\n```\n"
        f"\n"
        f"_Generated {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC_"
    )


@activity.defn
async def notify_telegram(
    job_dict: dict,
    profile_dict: dict,
    match_dict: dict,
    docs_dict: dict,
) -> bool:
    """Send Telegram notification. Returns True on success."""
    job = JobPost(**job_dict)
    profile = UserProfile(**profile_dict)
    match = MatchResult(**match_dict)
    docs = GeneratedDocuments(**docs_dict)

    chat_id = profile.notification.telegram_chat_id
    if not chat_id:
        log.warning("No Telegram chat_id for %s — skipping notification", profile.id)
        return False

    # Use per-profile bot token if available, fall back to global TELEGRAM_BOT_TOKEN.
    bot_token = profile.notification.telegram_bot_token or settings.telegram_bot_token
    if not bot_token:
        log.warning("No Telegram bot token for %s — skipping notification", profile.id)
        return False

    text = _format_message(job, profile, match, docs)
    url = TELEGRAM_API.format(token=bot_token)

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(url, json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": False,
        })

    if resp.status_code == 200:
        log.info("Telegram notification sent to %s for %s @ %s",
                 profile.id, job.title, job.company)
        return True
    else:
        log.error("Telegram send failed: %s %s", resp.status_code, resp.text)
        return False
