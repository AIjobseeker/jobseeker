"""Qwen3 cover letter generator — local LLM for human-sounding prose."""
from __future__ import annotations

import logging
import re
from pathlib import Path

from temporalio import activity

from shared.config import settings
from shared.models import JobPost, MatchResult, UserProfile
from shared.ollama_client import chat

log = logging.getLogger("worker.cover_letter")

COVER_LETTER_PROMPT = """\
You are an expert career coach writing a cover letter for a highly skilled engineer.
This letter must be specific, impressive, and never sound AI-generated or generic.

JOB:
Company: {company}
Title: {title}
Description:
{description}

CANDIDATE:
Name: {name}
Experience: {experience_years} years in {target_roles}
Key Skills: {skills}
Strongest match points: {matched_skills}
Visa: {visa_note}

WRITING RULES:
- Opening: hook with a specific insight about the company or role — not "I am excited to apply"
- Paragraph 2: 2 concrete achievements from their experience that directly address the JD's top needs
- Paragraph 3: Why THIS company specifically — reference something real about the company (tech stack, known product, engineering blog if notable)
- Closing: confident, brief, with a specific ask
- Tone: confident engineer, not a supplicant. No filler phrases.
- Length: exactly 3-4 paragraphs, under 350 words
- Do NOT start with "I" as the first word
- Do NOT use: "passionate", "excited to apply", "dream company", "team player", "results-driven"

Output ONLY the letter body (no subject line, no date, no address headers).
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

    letter_text = await chat(
        settings.ollama_model_cover,
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
        max_tokens=1000,
        think=False,  # qwen3: direct prose, no CoT overhead
    )

    safe_company = re.sub(r"[^\w-]", "-", job.company)
    safe_title = re.sub(r"[^\w-]", "-", job.title)
    output_path = Path("/tmp") / f"{profile.id}_{safe_company}_{safe_title}_cover.txt"
    output_path.write_text(letter_text, encoding="utf-8")

    log.info("Cover letter generated → %s", output_path)
    return str(output_path)
