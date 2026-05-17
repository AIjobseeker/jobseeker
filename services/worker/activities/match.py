"""Qwen3 job-profile matcher — fast JSON scoring with thinking disabled."""
from __future__ import annotations

import json
import logging

from temporalio import activity

from shared.config import settings
from shared.models import JobPost, MatchResult, UserProfile
from shared.ollama_client import chat

log = logging.getLogger("worker.match")

MATCH_PROMPT = """\
You are an expert technical recruiter. Evaluate how well this job posting matches this candidate.

JOB POSTING:
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

TASK:
1. Check visa compatibility first. If the job says "no visa sponsorship" AND the candidate needs sponsorship, score = 0.0 and visa_ok = false.
2. Check role/seniority fit.
3. Score the technical skills match.

Respond ONLY with a valid JSON object (no markdown):
{{
  "score": 0.0,
  "matched": false,
  "visa_ok": true,
  "seniority_match": true,
  "matched_skills": [],
  "missing_skills": [],
  "reasoning": "2-3 sentence explanation",
  "recommended_variant": "infra|cicd|reliability|leadership|fresher"
}}
"""


@activity.defn
async def match_job(job_dict: dict, profile_dict: dict) -> dict:
    """Match a job against a user profile. Returns MatchResult as dict."""
    job = JobPost(**job_dict)
    profile = UserProfile(**profile_dict)

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
    )

    try:
        raw = await chat(settings.ollama_model_match, prompt, max_tokens=600, think=False)
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
        result_dict = json.loads(raw)
    except Exception as e:
        log.error("Match failed for %s @ %s: %s", job.title, job.company, e)
        result_dict = {
            "score": 0.0,
            "matched": False,
            "visa_ok": True,
            "seniority_match": True,
            "matched_skills": [],
            "missing_skills": [],
            "reasoning": f"Matching error: {e}",
            "recommended_variant": "",
        }

    result = MatchResult(job_id=job.id, person_id=profile.id, **result_dict)
    return result.model_dump()
