"""qwen2.5:7b study guide — fast factual markdown for interview prep."""
from __future__ import annotations

import logging

from temporalio import activity

from shared.config import settings
from shared.models import JobPost, MatchResult, UserProfile
from shared.ollama_client import chat

log = logging.getLogger("worker.study_guide")

STUDY_PROMPT = """\
You are a senior technical interviewer. Based on this job description and candidate profile, \
give a concise interview preparation guide.

JOB: {title} at {company}
JD KEY POINTS:
{description}

CANDIDATE SKILLS: {skills}
MISSING SKILLS: {missing_skills}

Output a markdown guide with these sections (keep it tight — max 300 words total):

## Must Know for This Role
- 3-5 technical topics central to this JD that the candidate must be solid on

## Brush Up
- 2-3 things from the JD that are in the candidate's skills but may need refreshing

## Likely Interview Questions
- 3 system design or technical questions likely from this JD

## Quick Tips
- 1-2 company-specific things to research before the interview
"""


@activity.defn
async def generate_study_guide(job_dict: dict, profile_dict: dict, match_dict: dict) -> str:
    """Return study guide as markdown string."""
    job = JobPost(**job_dict)
    profile = UserProfile(**profile_dict)
    match = MatchResult(**match_dict)

    guide = await chat(
        settings.ollama_model_study,
        STUDY_PROMPT.format(
            title=job.title,
            company=job.company,
            description=job.description_text[:2000],
            skills=", ".join(profile.skills[:20]),
            missing_skills=", ".join(match.missing_skills[:10]),
        ),
        max_tokens=600,
    )
    log.info("Study guide generated for %s @ %s", job.title, job.company)
    return guide
