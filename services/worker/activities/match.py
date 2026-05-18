"""Claude Sonnet job matcher — structured JSON scoring with visa + seniority checks."""
from __future__ import annotations

import asyncio
import json
import logging

from temporalio import activity

from shared.config import settings
from shared.llm_client import claude_chat
from shared.models import JobPost, MatchResult, UserProfile

log = logging.getLogger("worker.match")

MATCH_PROMPT = """\
You are an expert technical recruiter evaluating job fit. Respond ONLY with a valid JSON object — no markdown, no commentary, no preamble.

JOB:
Company: {company}
Title: {title}
Location: {location}
Description (first 2500 chars):
{description}

CANDIDATE:
Name: {name}
Experience: {experience_years} years
Visa: {visa_status} | H1B Transfer OK: {h1b_transfer_ok} | Needs New Sponsorship: {needs_sponsorship}
Target Roles: {target_roles}
Skills: {skills}
Resume Variants: {variant_ids}

SCORING RULES:
1. If job says "no visa sponsorship" AND candidate needs_sponsorship=true → score=0.0, visa_ok=false
2. If experience gap is >3 years in either direction → penalise score heavily
3. Score 0.85-1.0 = strong match, 0.65-0.85 = good match, below 0.65 = weak
4. recommended_variant must be one of the Resume Variants listed above (pick the best fit)

Required JSON format (copy exactly, fill values):
{{
  "score": 0.0,
  "matched": false,
  "visa_ok": true,
  "seniority_match": true,
  "matched_skills": [],
  "missing_skills": [],
  "reasoning": "2-3 sentence explanation",
  "recommended_variant": ""
}}
"""


@activity.defn
async def match_job(job_dict: dict, profile_dict: dict) -> dict:
    """Match a job against a user profile using Claude. Returns MatchResult as dict."""
    job = JobPost(**job_dict)
    profile = UserProfile(**profile_dict)

    variant_ids = [v.id for v in profile.resume_variants] or ["base"]

    prompt = MATCH_PROMPT.format(
        company=job.company,
        title=job.title,
        location=job.location,
        description=job.description_text[:2500],
        name=profile.name,
        experience_years=profile.experience_years,
        visa_status=profile.visa_status,
        h1b_transfer_ok=profile.h1b_transfer_ok,
        needs_sponsorship=profile.needs_sponsorship,
        target_roles=", ".join(profile.target_roles),
        skills=", ".join(profile.skills),
        variant_ids=", ".join(variant_ids),
    )

    try:
        raw = await asyncio.to_thread(
            claude_chat,
            prompt,
            model=settings.claude_model,
            max_tokens=600,
        )
        # Strip markdown code fences if Claude wraps the JSON
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        result_dict = json.loads(raw)
    except Exception as exc:
        log.error("Match failed for %s @ %s: %s", job.title, job.company, exc)
        result_dict = {
            "score": 0.0,
            "matched": False,
            "visa_ok": True,
            "seniority_match": True,
            "matched_skills": [],
            "missing_skills": [],
            "reasoning": f"Matching error: {exc}",
            "recommended_variant": variant_ids[0] if variant_ids else "",
        }

    result = MatchResult(job_id=job.id, person_id=profile.id, **result_dict)
    return result.model_dump()
