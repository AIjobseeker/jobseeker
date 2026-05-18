"""Claude Sonnet cover letter generator — human-sounding, strategically targeted prose."""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from temporalio import activity

from shared.config import settings
from shared.llm_client import claude_chat
from shared.models import JobPost, MatchResult, UserProfile

log = logging.getLogger("worker.cover_letter")

COVER_LETTER_PROMPT = """\
You are an expert career coach writing a cover letter for a highly skilled engineer.
This letter must get the candidate an interview. Every sentence must earn its place.

JOB:
Company: {company}
Title: {title}
Description:
{description}

CANDIDATE:
Name: {name}
Experience: {experience_years} years in {target_roles}
Key Skills: {skills}
Strongest match points for THIS role: {matched_skills}
Visa: {visa_note}

STRATEGY — think through this before writing:
1. What are the top 2-3 things this hiring team cares about most (from the JD)?
2. Which 2 of the candidate's achievements best prove they can deliver those things?
3. What is one specific, non-obvious thing about this company that shows genuine interest?

WRITING RULES:
- Opening: hook with a specific insight about the company or role — not "I am excited to apply"
- Paragraph 2: 2 concrete achievements that directly prove the JD's top needs, with numbers
- Paragraph 3: Why THIS company specifically — one real detail (tech stack, product, mission)
- Closing: confident ask for a conversation, one sentence
- Tone: senior engineer who knows their worth, not a supplicant
- Length: exactly 3 paragraphs, under 300 words
- Do NOT start with "I" as the first word
- BANNED words: passionate, excited, dream, team player, results-driven, leverage, synergy

Output ONLY the letter body. No subject line, no date, no headers, no preamble.
"""


@activity.defn
async def generate_cover_letter(job_dict: dict, profile_dict: dict, match_dict: dict) -> str:
    """Generate cover letter text. Returns local path of the saved text file."""
    job = JobPost(**job_dict)
    profile = UserProfile(**profile_dict)
    match = MatchResult(**match_dict)

    visa_note = ""
    if profile.h1b_transfer_ok:
        visa_note = "Currently on H1B, open to transfer"
    elif profile.needs_sponsorship:
        visa_note = f"On {profile.visa_status.value}, needs sponsorship"
    else:
        visa_note = "No sponsorship needed"

    letter_text = await asyncio.to_thread(
        claude_chat,
        COVER_LETTER_PROMPT.format(
            company=job.company,
            title=job.title,
            description=job.description_text[:3000],
            name=profile.name,
            experience_years=profile.experience_years,
            target_roles=", ".join(profile.target_roles),
            skills=", ".join(profile.skills[:15]),
            matched_skills=", ".join(match.matched_skills[:8]),
            visa_note=visa_note,
        ),
        model=settings.claude_model,
        max_tokens=1000,
    )

    safe_company = re.sub(r"[^\w-]", "-", job.company)
    safe_title = re.sub(r"[^\w-]", "-", job.title)
    output_path = Path("/tmp") / f"{profile.id}_{safe_company}_{safe_title}_cover.txt"
    output_path.write_text(letter_text, encoding="utf-8")

    log.info("Cover letter generated → %s", output_path)
    return str(output_path)
