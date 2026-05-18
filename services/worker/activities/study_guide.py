"""Study guide generator — Ollama primary, Claude fallback."""
from __future__ import annotations

import asyncio
import logging

from temporalio import activity

from shared.config import settings
from shared.models import JobPost, MatchResult, UserProfile

log = logging.getLogger("worker.study_guide")

STUDY_PROMPT = """\
You are a senior technical interviewer. Give a concise interview prep guide based on this job and candidate.

JOB: {title} at {company}
JD KEY POINTS:
{description}

CANDIDATE SKILLS: {skills}
SKILL GAPS: {missing_skills}

Write a markdown guide with these sections (max 300 words total):

## Must Know
- 3-5 technical topics central to this JD

## Brush Up
- 2-3 skills the candidate has but should review

## Likely Interview Questions
- 3 system design or technical questions from this JD

## Quick Tips
- 1-2 company-specific things to research before the interview
"""


@activity.defn
async def generate_study_guide(job_dict: dict, profile_dict: dict, match_dict: dict) -> str:
    """Return study guide as markdown string. Uses Ollama with Claude fallback."""
    job = JobPost(**job_dict)
    profile = UserProfile(**profile_dict)
    match = MatchResult(**match_dict)

    prompt = STUDY_PROMPT.format(
        title=job.title,
        company=job.company,
        description=job.description_text[:2000],
        skills=", ".join(profile.skills[:20]),
        missing_skills=", ".join(match.missing_skills[:10]),
    )

    # Try Ollama first (fast, free)
    try:
        from shared.ollama_client import chat
        guide = await chat(settings.ollama_model_study, prompt, max_tokens=600)
        log.info("Study guide (ollama) for %s @ %s", job.title, job.company)
        return guide
    except Exception as exc:
        log.warning("Ollama study guide failed (%s), falling back to Claude", exc)

    # Claude fallback
    try:
        from shared.llm_client import claude_chat
        guide = await asyncio.to_thread(
            claude_chat, prompt, model=settings.claude_model, max_tokens=600
        )
        log.info("Study guide (claude) for %s @ %s", job.title, job.company)
        return guide
    except Exception as exc:
        log.error("Study guide fallback also failed: %s", exc)
        return f"## Study Guide\n\nCould not generate guide for {job.title} @ {job.company}.\nReview the job description manually."
