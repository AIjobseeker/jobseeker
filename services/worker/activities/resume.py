"""Resume tailor — resume.yaml + Claude REWRITE_PROMPT → DOCX + defense notes + prep guide."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Optional

import yaml
from temporalio import activity

from shared.config import settings
from shared.llm_client import claude_chat
from shared.models import JobPost, MatchResult, UserProfile

log = logging.getLogger("worker.resume")

PROFILES_DIR = Path("/app/profiles")


# ── REWRITE_PROMPT (same as tailor_v2.py — Claude gets the full JD + resume) ──
REWRITE_PROMPT = """You are simultaneously a SENIOR TECHNICAL RECRUITER, an
ATS-optimization specialist, and a hiring manager who has seen 10,000 resumes.
You're tailoring this candidate's resume for ONE specific JD.

GOAL: Maximize interview callbacks while keeping every claim defensible in a
technical interview. Aggressive POSITIONING is allowed. Inventing employment
history is NOT.

JOB:
Company: {company}
Title: {title}
Full posting (8000 chars max):
{jd}

CANDIDATE PROFILE SUMMARY (background facts — never contradict these):
{profile_summary}

ARCHETYPES (different framings of the same career):
{archetypes_block}

CANDIDATE'S RAW RESUME (numbered roles + numbered bullets — these are the
ONLY employment facts you may reference; do not invent new ones):
{numbered_resume}

────────────────────────────────────────────────────────────────────────
PROCESS — do all of this internally before emitting the JSON.
────────────────────────────────────────────────────────────────────────

STEP 1 — JD intelligence
Read the posting end-to-end. Extract:
  • company name + business domain
  • role title + seniority signal (junior/mid/senior/staff/principal)
  • core responsibilities (bullet list)
  • required skills (must-haves)
  • preferred skills (nice-to-haves)
  • "what you will do" / day-to-day work
  • culture / mission language (e.g. "ownership", "scale", "moving fast")
  • hidden ATS keywords — terms in the responsibilities/preferred sections
    that the recruiter's ATS will boost (e.g. specific tools, frameworks,
    methodologies even if mentioned only once)

STEP 2 — Infer company-type priorities (pick ONE that fits best)
  • startup        → ownership, speed, breadth, scrappy execution
  • enterprise     → process, scale, reliability, compliance
  • platform team  → automation, architecture, internal-tools
  • SRE            → reliability, incident response, SLO/SLA, on-call
  • release eng    → CI/CD, deployment stability, rollout safety
  • applied ML     → model performance, latency, production deployment
  • research       → publications, methodology, novelty
  • data eng       → pipeline scale, data quality, latency

STEP 3 — Multi-perspective check (mental pass; don't emit)
  • ATS parser: does this resume contain the JD's required + preferred terms,
    naturally placed (not stuffed)?
  • Recruiter (10-second scan): does the top half of the resume make it
    obvious this person fits THIS role?
  • Hiring manager: do the bullets prove the candidate has solved similar
    problems at relevant scale?
  • Technical interviewer: can the candidate explain every claim in detail?
  • Culture: does the language match the company's stated values?

STEP 4 — Repositioning (the work)
For each role in the candidate's resume, pick which bullets to keep, drop,
or rewrite to match THIS role. Aggressive reframing is allowed:

  ALLOWED:
    • Reframe real work using JD vocabulary verbatim.
    • Expand a thin bullet with industry-standard context the candidate
      plausibly performed.
    • Use transferable-skill language (mark MEDIUM/LOW confidence).
    • Estimate impact in a defensible range when source has "improved" /
      "reduced" without a number.
    • Convert operational tasks into business-impact framing.
    • Reorder skills so JD-relevant ones appear first.

  NOT ALLOWED:
    • Inventing employers, titles, dates, degrees, or certifications.
    • Claiming senior ownership of tools the candidate has zero exposure to.
    • Numbers that imply a different scale than the candidate operated at.
    • Stories the candidate cannot defend under follow-up questions.
    • Mentioning visa status, work authorization, F1, OPT, CPT, H1B, green
      card, sponsorship, or any immigration topic ANYWHERE in the summary,
      bullets, or skills. Strip it completely.

STEP 5 — Per-bullet confidence tag
Tag each rewritten bullet with one of:
  • HIGH    — candidate directly performed this work; can defend deeply
  • MEDIUM  — adjacent / practical exposure; can talk about it credibly
  • LOW     — limited exposure; phrase as "familiarity / working knowledge"

STEP 6 — Bullet pattern
Every rewritten bullet: Action verb + Technology/Tool + Problem solved + Business impact.
Banned first-words: Worked on, Helped with, Assisted, Was responsible for.

STEP 7 — Defense note (per bullet)
For every rewritten bullet, write a one-line defense note in plain English:
"If a technical interviewer asks me to elaborate, here's the honest version."

────────────────────────────────────────────────────────────────────────
OUTPUT — exactly this JSON shape, plain integers for indices, no fences:
────────────────────────────────────────────────────────────────────────
{{
  "archetype": "exact archetype name from list above",
  "jd_intelligence": {{
    "company_type": "startup|enterprise|platform|sre|release|applied_ml|research|data|other",
    "priority_frame": "one short sentence — what this team really wants",
    "core_responsibilities": ["..."],
    "required_skills": ["..."],
    "preferred_skills": ["..."],
    "hidden_keywords": ["terms ATS will boost — extracted from posting"]
  }},
  "summary": "3-4 sentences. Lead with the EXACT target title (mirror JD). Reference 2-3 specific technologies the JD asks for. Include one differentiator. Reads as written FOR THIS company and role.",
  "experience": [
    {{
      "role_index": 1,
      "rewrites": [
        {{
          "original_index": 3,
          "rewritten": "Strong action verb + tech + problem solved + impact",
          "confidence": "HIGH|MEDIUM|LOW",
          "defense_note": "How I'd honestly explain this in an interview",
          "jd_alignment": "Which JD requirement this serves"
        }}
      ],
      "dropped_indices": [2, 4],
      "drop_rationale": "brief reason these are off-topic for THIS JD"
    }}
  ],
  "skills_reordered": {{
    "lead_with": ["the 4-6 most JD-relevant skills, in order"],
    "deprioritize": ["skills present in resume but irrelevant to this JD"]
  }},
  "missing_skill_risks": [
    {{
      "skill": "skill the JD asks for that the candidate doesn't have",
      "severity": "blocker|moderate|minor",
      "mitigation": "How candidate can address this"
    }}
  ],
  "interview_callback_estimate": 75
}}

INDEX FORMAT — role_index and original_index MUST be plain integers.
The numbered resume above uses dotted labels like (1.3) for display only —
when you reference bullet 3 of role 1, output "role_index": 1 and "original_index": 3.
Output ONLY the JSON. No markdown fences, no commentary."""


# ── Helper functions (ported from tailor_v2.py) ──────────────────────────────

_VISA_SENTENCE_RE = re.compile(
    r"(?:^|(?<=[\.\!\?]))\s*[^\.\!\?]*\b("
    r"f-?1|opt|cpt|h-?1b|h1b|green\s*card|sponsorship|sponsor\s+visa|"
    r"work\s+authorization|authorized\s+to\s+work|visa\s+status"
    r")\b[^\.\!\?]*[\.\!\?]?",
    re.IGNORECASE,
)


def _scrub_visa(text: str) -> str:
    if not text:
        return text
    cleaned = _VISA_SENTENCE_RE.sub(" ", text)
    return re.sub(r"\s+", " ", cleaned).strip()


def _coerce_int(x) -> Optional[int]:
    if isinstance(x, bool):
        return None
    if isinstance(x, int):
        return x
    if isinstance(x, float):
        s = f"{x}"
        if "." in s:
            tail = s.split(".", 1)[1].rstrip("0") or "0"
            try:
                return int(tail)
            except ValueError:
                return None
        return int(x)
    if isinstance(x, str):
        s = x.strip()
        if s.isdigit():
            return int(s)
        if "." in s:
            tail = s.split(".", 1)[1].strip()
            if tail.isdigit():
                return int(tail)
    return None


def _number_resume(resume: dict) -> str:
    lines: list[str] = []
    for ridx, role in enumerate(resume.get("experience", []) or [], start=1):
        company = role.get("company", "?")
        title = role.get("title", "?")
        start = role.get("start", "")
        end = role.get("end", "")
        lines.append(f"\n[role {ridx}] {title} at {company} ({start} - {end})")
        for bidx, b in enumerate(role.get("bullets", []) or [], start=1):
            lines.append(f"  ({ridx}.{bidx}) {b}")
    return "\n".join(lines)


def _archetypes_block(resume: dict) -> str:
    archs = resume.get("archetypes") or []
    if not archs:
        return "- default: (no archetypes defined; use the summary as guide)"
    out: list[str] = []
    for a in archs:
        name = a.get("name", "?")
        signals = a.get("jd_signals", []) or []
        proof = a.get("primary_proof_points", []) or []
        out.append(f"- {name}")
        if signals:
            out.append(f"    pick this when JD mentions: {', '.join(signals[:8])}")
        if proof:
            out.append(f"    prioritize bullets about: {', '.join(proof[:6])}")
    return "\n".join(out)


def _extract_companies(resume: dict) -> set[str]:
    return {
        (r.get("company", "") or "").strip().lower()
        for r in resume.get("experience", []) or []
        if r.get("company")
    }


def validate_rewrites(plan: dict, resume: dict) -> list[str]:
    warns: list[str] = []
    roles = resume.get("experience", []) or []
    companies_lower = _extract_companies(resume)

    for entry in plan.get("experience", []) or []:
        ridx = _coerce_int(entry.get("role_index"))
        if ridx is None or ridx < 1 or ridx > len(roles):
            warns.append(f"role_index {entry.get('role_index')!r} out of range")
            continue
        role = roles[ridx - 1]
        original_bullets = role.get("bullets", []) or []
        for r in entry.get("rewrites", []) or []:
            orig_idx = _coerce_int(r.get("original_index"))
            new_text = r.get("rewritten", "") or ""
            if orig_idx is None or not (1 <= orig_idx <= len(original_bullets)):
                warns.append(
                    f"role {ridx}: invalid original_index {r.get('original_index')!r}"
                )
                continue
            for word in re.findall(r"\b[A-Z][a-zA-Z0-9]{3,}\b", new_text):
                wl = word.lower()
                if wl in companies_lower:
                    continue
                if wl in {"google", "meta", "facebook", "amazon", "microsoft",
                          "netflix", "uber", "airbnb", "linkedin", "tesla",
                          "salesforce", "oracle", "ibm", "twitter", "snap",
                          "stripe", "square", "robinhood", "coinbase"}:
                    if re.search(rf"\bat\s+{re.escape(word)}\b", new_text, re.IGNORECASE):
                        warns.append(
                            f"role {ridx}, bullet {orig_idx}: rewrite "
                            f"references employer '{word}'"
                        )
    return warns


def _parse_json_loose(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*\n?|\n?```$", "", text, flags=re.MULTILINE).strip()
    return json.loads(text)


def _profile_summary(profile: UserProfile) -> str:
    parts = [
        f"Name: {profile.name}",
        f"Experience: {profile.experience_years} years",
        f"Target roles: {', '.join(profile.target_roles[:5])}",
        f"Skills: {', '.join(profile.skills[:20])}",
        f"Visa: {profile.visa_status.value if profile.visa_status else 'N/A'}"
        + (", H1B transfer OK" if profile.h1b_transfer_ok else "")
        + (", needs sponsorship" if profile.needs_sponsorship else ""),
    ]
    return "\n".join(parts)


@activity.defn
async def tailor_resume(job_dict: dict, profile_dict: dict, match_dict: dict) -> dict:
    """Tailor resume via resume.yaml + Claude. Returns paths for resume, defense, and prep."""
    job = JobPost(**job_dict)
    profile = UserProfile(**profile_dict)
    match = MatchResult(**match_dict)

    # Load resume.yaml
    resume_yaml_path = PROFILES_DIR / profile.id / "resume.yaml"
    if not resume_yaml_path.exists():
        raise FileNotFoundError(
            f"resume.yaml not found at {resume_yaml_path}. "
            f"Ensure profiles/{profile.id}/resume.yaml is present in the container."
        )
    resume = yaml.safe_load(resume_yaml_path.read_text(encoding="utf-8"))

    prompt = REWRITE_PROMPT.format(
        company=job.company,
        title=job.title,
        jd=job.description_text[:8000],
        profile_summary=_profile_summary(profile),
        archetypes_block=_archetypes_block(resume),
        numbered_resume=_number_resume(resume),
    )

    raw = await asyncio.to_thread(
        claude_chat,
        prompt,
        model=settings.claude_model,
        max_tokens=12000,
    )

    try:
        plan = _parse_json_loose(raw)
    except json.JSONDecodeError as exc:
        log.error("Claude returned invalid JSON for %s @ %s: %s", job.title, job.company, exc)
        log.error("Raw output (first 500 chars): %s", raw[:500])
        raise

    # Scrub visa mentions from summary and bullets
    plan["summary"] = _scrub_visa((plan.get("summary") or "").strip())
    for entry in plan.get("experience") or []:
        for r in entry.get("rewrites") or []:
            if r.get("rewritten"):
                r["rewritten"] = _scrub_visa(r["rewritten"])

    warns = validate_rewrites(plan, resume)
    if warns:
        log.warning(
            "Resume validation warnings for %s @ %s: %s",
            job.title, job.company, warns[:5],
        )

    jdi = plan.get("jd_intelligence") or {}
    log.info(
        "Resume plan ready — archetype=%s company_type=%s callback_est=%s",
        plan.get("archetype"), jdi.get("company_type"),
        plan.get("interview_callback_estimate"),
    )
    safe_company = re.sub(r"[^\w-]", "-", job.company)
    safe_title = re.sub(r"[^\w-]", "-", job.title)
    out_dir = Path("/tmp") / f"{profile.id}_{safe_company}_{safe_title}"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Render DOCX (ATS-friendly)
    docx_path = out_dir / "resume.docx"
    from scripts.render_docx import render_docx
    render_docx(resume=resume, plan=plan, out_path=docx_path, show_highlights=False)
    log.info("DOCX rendered → %s", docx_path)

    # Generate interview defense notes (per-bullet honest backing)
    from scripts.match_report import write_defense_md, write_prep_md
    defense_path = write_defense_md(out_dir, plan, resume, job_dict)
    log.info("Defense notes → %s", defense_path)

    # Generate interview prep guide (JD-grounded — replaces study_guide)
    prep_path = write_prep_md(out_dir, plan, job_dict)
    log.info("Prep guide → %s", prep_path)

    # Build prep summary for the Telegram message code block
    prep_lines: list[str] = []
    if jdi.get("priority_frame"):
        prep_lines.append(f"What they want: {jdi['priority_frame']}")
    req = jdi.get("required_skills") or []
    if req:
        prep_lines.append("Must-know: " + ", ".join(req[:6]))
    pref = jdi.get("preferred_skills") or []
    if pref:
        prep_lines.append("Brush-up: " + ", ".join(pref[:6]))
    cb = plan.get("interview_callback_estimate")
    score_parts: list[str] = []
    if cb is not None:
        score_parts.append(f"callback est. {cb}/100")
    if score_parts:
        prep_lines.append("Scores: " + " · ".join(score_parts))

    return {
        "resume_path": str(docx_path),
        "defense_path": str(defense_path),
        "prep_path": str(prep_path),
        "prep_summary": "\n".join(prep_lines),
    }
