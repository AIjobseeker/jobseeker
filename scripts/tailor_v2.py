#!/usr/bin/env python3
"""Resume tailor v2 — clean HTML+PDF output.

Tightly constrained pipeline:
  1. Read resume.yaml (parsed once via scripts/resume_to_yaml.py)
  2. Read job dict from seen.db / NDJSON / CLI arg
  3. LLM picks (does NOT rewrite) the top N bullets per role for THIS jd
  4. LLM writes a fresh 3-line summary tailored to the JD
  5. Render to clean HTML then PDF (weasyprint) + plain MD
  6. Validate: every bullet in the output exists verbatim in resume.yaml

Why not rewrite bullets? Local LLMs hallucinate metrics, swap company names,
genericise specifics. We DON'T trust them with that. Selecting + reordering
is a much narrower task — and we validate the output against the source.

  python3 scripts/tailor_v2.py
  python3 scripts/tailor_v2.py --use-claude          # higher quality
  python3 scripts/tailor_v2.py --send-telegram       # ship the PDF
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

try:
    from dotenv import load_dotenv

    load_dotenv(REPO / ".env", override=False)
except ImportError:
    pass


def _short_dedup_id(company: str, source_id: str) -> str:
    raw = f"{company.lower()}|{source_id}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def _green(s):  return f"\033[32m{s}\033[0m"
def _yellow(s): return f"\033[33m{s}\033[0m"
def _bold(s):   return f"\033[1m{s}\033[0m"
def _dim(s):    return f"\033[2m{s}\033[0m"


# ── LLM-driven bullet selection ───────────────────────────────────────────

SELECT_PROMPT = """You are tailoring a resume for ONE specific job. Your task:
SELECT and REORDER bullets, write a fresh summary, choose ONE archetype.
You CANNOT invent or rewrite bullets — but you MUST drop bullets that don't
serve THIS JD.

JOB:
Company: {company}
Title: {title}
JD (first 4000 chars):
{jd}

CANDIDATE PROFILE SUMMARY (for context, not for output):
{profile_summary}

CANDIDATE ARCHETYPES (each is a different framing of the same career):
{archetypes_block}

CANDIDATE'S RESUME (numbered roles + numbered bullets):
{numbered_resume}

OUTPUT — exactly this JSON shape, nothing else:
```json
{{
  "archetype": "exact_archetype_name_from_list_above",
  "summary": "Three sentences. Start with the EXACT target title or closest \
equivalent. Mirror the JD's vocabulary verbatim. Lead with the candidate's \
single most relevant proof point for THIS role. End with what they bring \
that the JD specifically asks for. NEVER invent technologies, employers, \
or metrics. Must read differently from a generic summary — it should be \
unmistakably written for THIS company and role.",
  "experience": [
    {{
      "role_index": 1,
      "keep_bullet_indices": [3, 1, 5]
    }},
    {{
      "role_index": 2,
      "keep_bullet_indices": [2, 4, 1]
    }}
  ],
  "skills_to_emphasize": ["kubernetes", "terraform", "aws"]
}}
```

HARD RULES (failure to follow these = bad tailoring):
- For each role with MORE than 4 bullets in the source, you MUST drop at least
  one. A resume that keeps every bullet is NOT tailored.
- Pick AT MOST 4 bullets per role (3 is often better). Less is more.
- If a bullet doesn't directly support what the JD is asking for, DROP IT.
  Don't keep weak bullets just because they exist.
- ALWAYS reorder — most JD-relevant bullet first, less-relevant last.
  The order in `keep_bullet_indices` is the order they'll appear in the PDF.
- Pick ONE archetype that best fits THIS jd. Use that archetype's priority
  proof points to break ties when multiple bullets seem equal.
- The summary CANNOT be 'Experienced engineer with X years...' generic
  language. It must reference something specific from THIS JD."""


# ── Rewrite mode v3 — JD intelligence + aggressive reframing ──────────────
# Philosophy:
#   1. Read the WHOLE posting (responsibilities, required, preferred, "what
#      you will do", culture, hidden keywords). Don't just match titles.
#   2. Infer company type → infer priorities (startup=ownership/speed,
#      enterprise=process/scale, SRE=reliability/incident response, etc.).
#   3. Reposition the candidate's REAL experience using transferable
#      framings + industry-standard context. Allowed to reframe; not
#      allowed to invent employers/titles/degrees.
#   4. Tag every rewritten bullet with HIGH/MEDIUM/LOW confidence + a
#      one-line defense note explaining how the candidate can talk about
#      it honestly in an interview.
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
Use this priority frame when picking which bullets to lead with and what
language to use.

STEP 3 — Multi-perspective check (mental pass; don't emit)
  • ATS parser: does this resume contain the JD's required + preferred terms,
    naturally placed (not stuffed)?
  • Recruiter (10-second scan): does the top half of the resume make it
    obvious this person fits THIS role?
  • Hiring manager: do the bullets prove the candidate has solved similar
    problems at relevant scale?
  • Technical interviewer: can the candidate explain every claim in detail?
    If a bullet would crater under follow-up questions, soften it.
  • Culture: does the language match the company's stated values?

STEP 4 — Repositioning (the work)
For each role in the candidate's resume, pick which bullets to keep, drop,
or rewrite to match THIS role. Aggressive reframing is allowed:

  ALLOWED:
    • Reframe real work using JD vocabulary verbatim.
    • Expand a thin bullet with industry-standard context the candidate
      plausibly performed (e.g. "deployed Kubernetes apps" -> "deployed
      Kubernetes apps using Helm charts with HPA + readiness probes").
    • Use transferable-skill language (e.g. if candidate did kubectl +
      kustomize, claiming "Helm" is OK if they've at least used it
      adjacently — mark MEDIUM/LOW confidence).
    • Estimate impact in a defensible range when source has "improved" /
      "reduced" without a number (e.g. "reduced rollout time ~30% via
      canary deploys"). Keep estimates conservative and round.
    • Convert operational tasks into business-impact framing.
    • Reorder skills so JD-relevant ones appear first.

  NOT ALLOWED:
    • Inventing employers, titles, dates, degrees, or certifications.
    • Claiming senior ownership of tools the candidate has zero exposure to.
    • Numbers that imply a different scale than the candidate operated at
      (don't claim "10M req/s" if real work was at 1k req/s).
    • Stories the candidate cannot defend under follow-up questions.
    • Mentioning visa status, work authorization, F1, OPT, CPT, H1B, green
      card, sponsorship, or any immigration topic ANYWHERE in the summary,
      bullets, or skills. That information belongs on the application form,
      NEVER on the resume. If the source `profile_summary` mentions it,
      strip it from the rewritten summary. Recruiters who care will ask;
      surfacing it here invites bias filtering and reads as desperate.

STEP 5 — Per-bullet confidence tag
Tag each rewritten bullet with one of:
  • HIGH    — candidate directly performed this work; can defend deeply
  • MEDIUM  — adjacent / practical exposure; can talk about it credibly
  • LOW     — limited exposure; phrase as "familiarity / working knowledge /
              exposure" rather than "expert / led / architected"
For LOW-confidence claims, soften the verb (Used/Familiar with/Worked with)
and shorten the bullet. Do not lead a role with a LOW-confidence bullet.

STEP 6 — Bullet pattern
Every rewritten bullet should follow:
  Action verb + Technology/Tool + Problem solved + Business impact
Examples:
  Bad   : "Worked on Kubernetes deployments."
  Good  : "Automated Kubernetes deployment workflows with Helm + Terraform
           + Jenkins pipelines, cutting manual release effort by ~40% and
           standardizing rollouts across dev/stage/prod."

Banned first-words: Worked on, Helped with, Assisted, Was responsible for,
Participated in, Was involved in, Contributed to.

Strong verbs (use these): Architected, Designed, Built, Migrated, Operated,
Owned, Led, Drove, Reduced, Automated, Stabilized, Hardened, Implemented,
Deployed, Integrated, Optimized, Eliminated, Standardized, Engineered,
Orchestrated, Validated, Diagnosed, Scaled, Reframed, Delivered.

STEP 7 — Defense note (per bullet)
For every rewritten bullet, write a one-line defense note in plain English
explaining: "If a technical interviewer asks me to elaborate on this bullet,
here's the honest version of what I actually did and how I'd talk about it."

This is for the candidate's eyes only — never appears in the rendered
resume. It's the safety net that keeps aggressive positioning honest.

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
  "summary": "3-4 sentences. Lead with the EXACT target title (mirror JD). "
             "Reference 2-3 specific technologies the JD asks for. Include "
             "one differentiator (cert/scale/domain). Reads as written FOR "
             "THIS company and role.",
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
      "mitigation": "How candidate can address — short course, framing in interview, etc."
    }}
  ],
  "interview_callback_estimate": "0-100 — your honest read of how likely "
                                  "this resume gets an interview at THIS company"
}}

INDEX FORMAT — `role_index` and `original_index` MUST be plain integers.
The numbered resume above uses dotted labels like `(1.3)` for display only —
when you reference bullet 3 of role 1, output `"role_index": 1` and
`"original_index": 3`. Never output `"1.3"` or `1.3` as the original_index.

The order of `rewrites` IS the order bullets render in the PDF.
Output ONLY the JSON. No markdown fences, no commentary."""


# ── Hallucination validation for rewrite mode ─────────────────────────────


_NUMBER_RE = re.compile(r"\b\d[\d,\.]*\s*(?:[%kKmMbB]|qps|rps|nodes?|tps|TPS)?\b")


def _extract_numbers(text: str) -> set[str]:
    """Pull out concrete numeric facts (e.g. '500', '20%', '3M', '300 nodes')."""
    return {m.group().strip().lower() for m in _NUMBER_RE.finditer(text)}


def _extract_companies(resume: dict) -> set[str]:
    return {
        (r.get("company", "") or "").strip().lower()
        for r in resume.get("experience", []) or []
        if r.get("company")
    }


def validate_rewrites(plan: dict, resume: dict) -> list[str]:
    """Return a list of warnings if the rewrite invents employment history.

    With aggressive-reframing mode, we DON'T flag:
      - missing numbers (rewrites may use estimates or different framings)
      - new tech terms (transferable-skill claims allowed; defense_note backs)

    We DO flag:
      - role_index out of range
      - original_index out of range / unparseable
      - employer names introduced that don't appear in resume.experience

    Numbers and unfamiliar tech are now informational — they'll show up in
    the LOW-confidence bullets and the interview_defense.md as flags for
    the candidate to review, not blockers.
    """
    warns: list[str] = []
    roles = resume.get("experience", []) or []
    companies_lower = _extract_companies(resume)

    def _to_int(x):
        return _coerce_int(x)

    for entry in plan.get("experience", []) or []:
        ridx = _to_int(entry.get("role_index"))
        if ridx is None or ridx < 1 or ridx > len(roles):
            warns.append(f"role_index {entry.get('role_index')!r} out of range")
            continue
        role = roles[ridx - 1]
        original_bullets = role.get("bullets", []) or []
        for r in entry.get("rewrites", []) or []:
            orig_idx = _to_int(r.get("original_index"))
            new_text = r.get("rewritten", "") or ""
            if orig_idx is None or not (1 <= orig_idx <= len(original_bullets)):
                warns.append(
                    f"role {ridx}: invalid original_index "
                    f"{r.get('original_index')!r}"
                )
                continue
            # Only flag invented EMPLOYERS — common employer surnames the
            # candidate didn't actually work at. Tech terms are intentionally
            # allowed for transferable-skill positioning.
            for word in re.findall(r"\b[A-Z][a-zA-Z0-9]{3,}\b", new_text):
                wl = word.lower()
                # Skip if it's a real employer in the resume
                if wl in companies_lower:
                    continue
                # Heuristic: known company-sounding suffixes/patterns
                if wl in {"google", "meta", "facebook", "amazon", "microsoft",
                          "netflix", "uber", "airbnb", "linkedin", "tesla",
                          "salesforce", "oracle", "ibm", "twitter", "snap",
                          "stripe", "square", "robinhood", "coinbase"}:
                    # Only flag if it appears as employer-style
                    # (preceded by "at" or possessive)
                    if re.search(rf"\bat\s+{re.escape(word)}\b", new_text, re.IGNORECASE):
                        warns.append(
                            f"role {ridx}, bullet {orig_idx}: rewrite "
                            f"references employer '{word}' — verify candidate "
                            f"actually worked there"
                        )
    return warns


HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{name} - Resume</title>
<style>
  @page {{ size: Letter; margin: 0.5in 0.65in; }}
  body {{
    font-family: "Inter", "Helvetica Neue", "Arial", sans-serif;
    font-size: 10.5pt;
    color: #1f2328;
    line-height: 1.4;
    margin: 0;
  }}
  .name {{
    font-size: 22pt; font-weight: 700; letter-spacing: -0.4px;
    color: #0a2540; margin: 0 0 2px 0;
  }}
  .contact {{
    font-size: 9.5pt; color: #555; margin: 0 0 8px 0;
    letter-spacing: 0.1px;
  }}
  h2 {{
    font-size: 10.5pt; font-weight: 700; text-transform: uppercase;
    letter-spacing: 1.4px; color: #0a2540;
    border-bottom: 1.2pt solid #0a2540; padding-bottom: 2px;
    margin: 14px 0 6px 0;
  }}
  .summary {{ margin: 0 0 4px 0; line-height: 1.45; }}
  .highlights {{ margin: 0; padding-left: 18px; }}
  .highlights li {{ margin-bottom: 3px; }}
  .role {{ margin-bottom: 10px; }}
  .role-head {{
    display: flex; justify-content: space-between; align-items: baseline;
    margin-bottom: 1px;
  }}
  .role-head .who {{ font-weight: 700; color: #1f2328; }}
  .role-head .when {{
    font-size: 9.5pt; color: #555; font-variant-numeric: tabular-nums;
  }}
  .role-sub {{
    font-style: italic; color: #555; font-size: 9.5pt; margin-bottom: 3px;
  }}
  ul {{ margin: 0; padding-left: 18px; }}
  li {{ margin-bottom: 2px; }}
  .skills {{ font-size: 10pt; }}
  .skills .group {{ margin-bottom: 3px; }}
  .skills .group-name {{
    font-weight: 700; color: #0a2540; min-width: 9em;
    display: inline-block;
  }}
  a {{ color: #0a2540; text-decoration: none; border-bottom: 0.5pt dotted #0a2540; }}
  strong {{ color: #0a2540; }}
</style>
</head>
<body>
<div class="name">{name}</div>
<div class="contact">{contact}</div>

<h2>Summary</h2>
<div class="summary">{summary}</div>

{highlights_section}

<h2>Experience</h2>
{experience_html}

{skills_section}

{certifications_section}

{education_section}
</body>
</html>
"""


def _render_highlights(resume: dict, picks_archetype: str = "") -> str:
    """Render the 'Selected Engineering Highlights' section if present.

    `picks_archetype` is currently informational only; in future we could
    filter highlights by archetype-priority.
    """
    items = resume.get("key_highlights") or []
    if not items:
        return ""
    li = "".join(f"<li>{_esc(x)}</li>" for x in items[:6])
    return f'<h2>Selected Highlights</h2>\n<ul class="highlights">{li}</ul>'


def _esc(s: str) -> str:
    return (
        str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def _highlight_skills(text: str, emphasize: list[str]) -> str:
    """Bold the emphasized skills (case-insensitive). Used for the skills line."""
    out = _esc(text)
    for sk in emphasize:
        if not sk:
            continue
        out = re.sub(
            rf"(?i)\b({re.escape(sk)})\b",
            r"<strong>\1</strong>",
            out,
        )
    return out


def _number_resume(resume: dict) -> str:
    """Build the numbered resume the LLM sees. Stable indexing."""
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
    """Format the archetypes section for the LLM prompt.

    Looks for `archetypes:` in resume.yaml. If absent, returns a single
    'default' archetype derived from `summary` so the prompt stays valid.
    """
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


def _validate_picks(picks: list[dict], resume: dict) -> list[str]:
    """Every (role_index, bullet_index) must point to a real bullet."""
    warns: list[str] = []
    roles = resume.get("experience", []) or []
    for entry in picks:
        ridx = entry.get("role_index")
        if not isinstance(ridx, int) or ridx < 1 or ridx > len(roles):
            warns.append(f"role_index out of range: {ridx}")
            continue
        role = roles[ridx - 1]
        bullets = role.get("bullets", []) or []
        for bidx in entry.get("keep_bullet_indices", []) or []:
            if not isinstance(bidx, int) or bidx < 1 or bidx > len(bullets):
                warns.append(
                    f"bullet_index out of range for role {ridx}: {bidx}"
                )
    return warns


_VISA_SENTENCE_RE = re.compile(
    # Strip whole sentences that mention visa/work-auth topics. Anchored on
    # sentence boundaries so we don't mangle the rest of the prose.
    r"(?:^|(?<=[\.\!\?]))\s*[^\.\!\?]*\b("
    r"f-?1|opt|cpt|h-?1b|h1b|green\s*card|sponsorship|sponsor\s+visa|"
    r"work\s+authorization|authorized\s+to\s+work|visa\s+status"
    r")\b[^\.\!\?]*[\.\!\?]?",
    re.IGNORECASE,
)


def _scrub_visa(text: str) -> str:
    """Belt-and-suspenders: even if the prompt's NOT-ALLOWED rule is ignored,
    drop sentences mentioning visa / work-authorization topics. Resumes and
    cover letters should never have this content — the prompt forbids it,
    this scrubs it.
    """
    if not text:
        return text
    cleaned = _VISA_SENTENCE_RE.sub(" ", text)
    # Collapse double spaces produced by the substitution
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _coerce_int(x) -> Optional[int]:
    """Tolerant int coercion. LLMs sometimes return indices as:
      - plain ints: 3
      - strings:    "3"
      - dotted:     "1.2"  (role.bullet — we want the bullet portion)
    Returns the bullet-side int or None.
    """
    if isinstance(x, bool):
        return None
    if isinstance(x, int):
        return x
    if isinstance(x, float):
        # "1.2" YAML-loaded becomes float 1.2 — extract decimal part as int.
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
        # Dotted form "1.2" -> bullet 2 of role 1; we use the second part.
        if "." in s:
            tail = s.split(".", 1)[1].strip()
            if tail.isdigit():
                return int(tail)
    return None
    """Tolerant int coercion. LLMs sometimes return indices as:
      - plain ints: 3
      - strings:    "3"
      - dotted:     "1.2"  (role.bullet — we want the bullet portion)
    Returns the bullet-side int or None.
    """
    if isinstance(x, bool):
        return None
    if isinstance(x, int):
        return x
    if isinstance(x, float):
        # "1.2" YAML-loaded becomes float 1.2 — extract decimal part as int.
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
        # Dotted form "1.2" -> bullet 2 of role 1; we use the second part.
        if "." in s:
            tail = s.split(".", 1)[1].strip()
            if tail.isdigit():
                return int(tail)
    return None


def _render_experience(resume: dict, picks: list[dict]) -> str:
    """Render selected/rewritten bullets in the chosen order.

    Two input shapes are supported (auto-detected per role):
      A. select-only:  {role_index, keep_bullet_indices: [3,1,5]}
      B. rewrite:      {role_index, rewrites: [{original_index, rewritten, rationale}, ...]}
    """
    roles_meta = resume.get("experience", []) or []
    by_idx: dict[int, dict] = {}
    for p in picks:
        ridx = _coerce_int(p.get("role_index"))
        if ridx is not None:
            by_idx[ridx] = p
    chunks: list[str] = []
    for ridx, role in enumerate(roles_meta, start=1):
        sel = by_idx.get(ridx)
        bullets_to_show: list[str] = []
        bullets = role.get("bullets", []) or []

        if sel and sel.get("rewrites"):
            # Rewrite mode — use rewritten text in given order
            for r in sel["rewrites"]:
                t = (r.get("rewritten") or "").strip()
                if t:
                    bullets_to_show.append(t)
        elif sel:
            # Select mode — use original text by index. Coerce strings to ints.
            keep_raw = sel.get("keep_bullet_indices", []) or []
            keep_idxs = [_coerce_int(i) for i in keep_raw]
            keep_idxs = [i for i in keep_idxs if i is not None and 1 <= i <= len(bullets)]
            bullets_to_show = [bullets[i - 1] for i in keep_idxs]
        else:
            # Fallback: first 3 bullets
            bullets_to_show = bullets[:3]

        if not bullets_to_show:
            continue
        company = _esc(role.get("company", ""))
        title = _esc(role.get("title", ""))
        loc = role.get("location") or ""
        date_range = f"{role.get('start', '')} - {role.get('end', '')}".strip(" -")
        items = "".join(f"<li>{_esc(b)}</li>" for b in bullets_to_show)
        chunks.append(
            f'<div class="role">'
            f'<div class="role-head">'
            f'<span class="who">{company} &middot; {title}</span>'
            f'<span class="when">{_esc(date_range)}</span>'
            f'</div>'
            f'<div class="role-sub">{_esc(loc)}</div>'
            f'<ul>{items}</ul>'
            f'</div>'
        )
    return "\n".join(chunks)


def _render_skills(resume: dict, emphasize: list[str]) -> str:
    """Render the skills section. Supports two YAML shapes:

      skills: [list, of, strings]           -> single line, comma-separated

      skills:                               -> grouped by category (preferred)
        CI/CD & Release: [...]
        Containers: [...]
    """
    skills = resume.get("skills") or []
    if not skills:
        return ""

    if isinstance(skills, dict):
        groups = []
        for group_name, items in skills.items():
            if not items:
                continue
            line = ", ".join(str(s) for s in items)
            highlighted = _highlight_skills(line, emphasize)
            groups.append(
                f'<div class="group">'
                f'<span class="group-name">{_esc(str(group_name))}:</span> '
                f'{highlighted}'
                f'</div>'
            )
        if not groups:
            return ""
        return f'<h2>Skills</h2>\n<div class="skills">{"".join(groups)}</div>'

    # Flat list
    line = ", ".join(str(s) for s in skills)
    highlighted = _highlight_skills(line, emphasize)
    return f'<h2>Skills</h2>\n<div class="skills">{highlighted}</div>'


def _render_education(resume: dict) -> str:
    edu = resume.get("education") or []
    if not edu:
        return ""
    rows = []
    for e in edu:
        school = _esc(e.get("school", ""))
        deg = _esc(e.get("degree", ""))
        field = e.get("field") or ""
        end = e.get("end", "")
        suffix = f", {field}" if field else ""
        rows.append(
            f'<div class="role"><div class="role-head"><span>{school}</span>'
            f'<span>{_esc(end)}</span></div>'
            f'<div class="role-sub">{deg}{_esc(suffix)}</div></div>'
        )
    return "<h2>Education</h2>\n" + "\n".join(rows)


def _render_certifications(resume: dict) -> str:
    """Render the Certifications section. Each cert renders on its own line
    with year aligned right, matching the Education section style.
    """
    certs = resume.get("certifications") or []
    if not certs:
        return ""
    rows = []
    for c in certs:
        if isinstance(c, str):
            name, year, url = c, "", ""
        else:
            name = c.get("name", "")
            year = c.get("year", "")
            url = c.get("url", "") or ""
        name_html = _esc(name)
        year_html = _esc(str(year)) if year != "" else ""
        if url:
            name_html = f'<a href="{_esc(url)}">{name_html}</a>'
        rows.append(
            f'<div class="role"><div class="role-head">'
            f'<span>{name_html}</span>'
            f'<span>{year_html}</span></div></div>'
        )
    return "<h2>Certifications</h2>\n" + "\n".join(rows)


def _build_contact(person: dict) -> str:
    parts = []
    for k in ("email", "phone", "location", "linkedin", "github"):
        v = person.get(k)
        if v:
            parts.append(_esc(str(v)))
    return " &middot; ".join(parts)


# ── LLM dispatch ──────────────────────────────────────────────────────────

async def llm_select(provider: str, model: str, prompt: str, max_tokens: int = 3000) -> str:
    if provider == "claude":
        return await _claude(prompt, max_tokens=max_tokens)
    return await _ollama(model, prompt, max_tokens=max_tokens)


async def _ollama(model: str, prompt: str, max_tokens: int = 3000) -> str:
    from ollama import AsyncClient

    host = os.environ.get("OLLAMA_HOST", "http://100.115.111.9:11434")
    client = AsyncClient(host=host)
    resp = await client.chat(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        think=False,    # selection task is small; thinking adds latency, no quality
        format="json",  # ollama strict-JSON mode
        options={"num_predict": max_tokens},
    )
    return (resp.message.content or "").strip()


async def _claude(prompt: str, max_tokens: int = 3000) -> str:
    # Single dispatch: internal SDK > internal proxy > direct Anthropic > error.
    # Implementation lives in shared.llm_client so all scripts agree.
    import asyncio

    from shared.llm_client import claude_chat

    model = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
    return await asyncio.to_thread(
        claude_chat, prompt, model=model, max_tokens=max_tokens,
    )


def _parse_json_loose(text: str) -> dict:
    """LLMs sometimes wrap JSON in markdown fences. Strip and parse."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*\n?|\n?```$", "", text, flags=re.MULTILINE).strip()
    return json.loads(text)


# ── Cover letter (separate prompt — generation is fine for this) ──────────

COVER_PROMPT = """You are an expert career coach writing a tight, specific cover letter.

JOB:
Company: {company}
Title: {title}
JD:
{jd}

CANDIDATE — facts you can use:
Name: {name}
Years of experience: {years}
Top relevant achievements (verbatim from resume — pick 1-2 to reference):
{achievements}

RULES:
- 3-4 paragraphs, under 320 words.
- Open with a SPECIFIC hook about the company or role. Never "I am excited to apply".
- Paragraph 2: reference 1-2 of the achievements above. Do not paraphrase the metrics — quote them.
- Paragraph 3: why THIS company specifically.
- Confident IC tone. Avoid the words: passionate, excited to apply, dream company, results-driven, team player.
- NEVER mention visa status, work authorization, F1, OPT, CPT, H1B, green
  card, or sponsorship. That goes on the application form, NOT in the
  cover letter — including it invites bias and reads as desperate.
  If the JD itself says "we sponsor H1B", do NOT echo it back; just apply.
- Output ONLY the letter body (no date, no address, no subject).
- Never invent achievements or skills not listed above."""


# ── Main ──────────────────────────────────────────────────────────────────

# Sample job (used when seen.db is empty / --sample)
SAMPLE_JOB = {
    "id": "demo-uuid",
    "source_id": "demo-001",
    "source": "greenhouse",
    "company": "Stripe",
    "title": "Staff Site Reliability Engineer, Platform",
    "description_text": (
        "Stripe is hiring a Staff SRE on the Platform Reliability team. "
        "Lead our Kubernetes platform across multiple regions, drive SLO/error-budget "
        "discipline, and own decisions on infrastructure-as-code (Terraform), CI/CD, "
        "and observability (Prometheus, Grafana, distributed tracing). 8+ years of "
        "experience operating complex distributed systems in production. Strong fluency "
        "in Python or Go for tooling. Experience leading incident response. "
        "We sponsor H1B transfers."
    ),
    "url": "https://stripe.com/jobs/staff-sre-platform",
    "location": "San Francisco, CA / Remote (US)",
    "department": "Infrastructure",
    "remote": True,
    "scraped_at": "2026-05-17T18:00:00Z",
}


def pick_job(seen_db: Path, sample: bool, person: str = "sai") -> dict:
    sample_job = SAMPLE_JOB_GF if person == "gf" else SAMPLE_JOB
    if sample or not seen_db.exists():
        return dict(sample_job)
    import sqlite3

    try:
        conn = sqlite3.connect(str(seen_db))
        row = conn.execute(
            "SELECT key, company, title, url, score "
            "FROM seen_jobs WHERE notified=1 ORDER BY score DESC LIMIT 1"
        ).fetchone()
        conn.close()
        if not row:
            return dict(sample_job)
        _key, company, title, url, score = row
        # We don't store full JD in seen.db; reuse the sample's description
        # but with real header. For the live pipeline we'd pipe the JD through.
        return {
            **sample_job,
            "company": company, "title": title, "url": url,
            "_score": score,
            "description_text": sample_job["description_text"],
        }
    except Exception:
        return dict(sample_job)


def render_pdf(html_path: Path, pdf_path: Path) -> bool:
    """Try weasyprint first, then pure-python fallback. Returns True if PDF written."""
    try:
        from weasyprint import HTML

        HTML(filename=str(html_path)).write_pdf(str(pdf_path))
        return True
    except Exception as e:
        print(_yellow(f"weasyprint failed ({type(e).__name__}: {e}); skipping PDF"))
        print(_yellow(f"  install with: pip install weasyprint"))
        return False


PERSON_DEFAULTS = {
    "sai": {
        "resume_yaml": REPO / "profiles" / "sai" / "resume.yaml",
        "profile_yaml": REPO / "profiles" / "sai" / "profile.parsed.yaml",
        "telegram_token_env": "TELEGRAM_BOT_TOKEN_SAI",
        "telegram_chat_env": "TELEGRAM_CHAT_ID_SAI",
        # Sai gets the full pipeline: Drive folder + Sheet row + Telegram
        "use_drive": True,
        "use_sheet": True,
        # Default OFF: Sai's key_highlights duplicate his most-recent-role
        # bullets (245->333 req/s, 59,566 msgs, MAS token caching all appear
        # twice). Rendering both creates the "repetitive wall of text"
        # recruiters call out. The role bullets already do this work.
        # Override per-run with --highlights if a specific JD warrants it.
        "show_highlights": False,
    },
    "gf": {
        "resume_yaml": REPO / "profiles" / "gf" / "resume.yaml",
        "profile_yaml": REPO / "profiles" / "gf" / "profile.parsed.yaml",
        "telegram_token_env": "TELEGRAM_BOT_TOKEN_GF",
        "telegram_chat_env": "TELEGRAM_CHAT_ID_GF",
        # Pooja: Telegram alert + tailored docs only. Drive + Sheet come later.
        "use_drive": False,
        "use_sheet": False,
        # New-grad resumes traditionally don't have a highlights block — the
        # role bullets and projects do that work. Off by default for her.
        "show_highlights": False,
    },
}


# Pooja-flavored sample JD — used when --sample --person gf and seen.db has
# nothing for her yet.
SAMPLE_JOB_GF = {
    "id": "demo-uuid-gf",
    "source_id": "demo-gf-001",
    "source": "greenhouse",
    "company": "Hugging Face",
    "title": "Machine Learning Engineer (NLP, New Grad)",
    "description_text": (
        "Hugging Face is hiring an entry-level ML Engineer for our NLP team. "
        "You'll work with transformers, fine-tune large language models, and "
        "ship inference pipelines using PyTorch and our Transformers library. "
        "Required: MS in CS / ML / related; hands-on PyTorch or TensorFlow; "
        "deep learning fundamentals (RNN/CNN/attention); Python fluency. "
        "Bonus: published research, audio/speech experience, LLM alignment work. "
        "We sponsor F1 OPT and H1B transitions."
    ),
    "url": "https://huggingface.co/jobs/ml-engineer-nlp-new-grad",
    "location": "New York, NY / Remote (US)",
    "department": "AI Research",
    "remote": True,
    "scraped_at": "2026-05-17T18:00:00Z",
}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--person", choices=["sai", "gf"], default="sai",
                    help="Which profile to tailor for. sai=full pipeline "
                         "(Drive+Sheet+Telegram). gf=Telegram alert only.")
    ap.add_argument("--resume-yaml", type=Path, default=None,
                    help="Override resume.yaml path. Default: per-person.")
    ap.add_argument("--profile", type=Path, default=None,
                    help="Override profile.parsed.yaml path. Default: per-person.")
    ap.add_argument("--output", type=Path, default=Path("/tmp/jobseeker_demo"))
    ap.add_argument("--use-claude", action="store_true")
    ap.add_argument("--ollama-model", default="qwen3:14b")
    ap.add_argument("--sample", action="store_true",
                    help="Use the built-in sample job (skip seen.db)")
    ap.add_argument("--mode", choices=["select", "rewrite", "auto"], default="auto",
                    help="select=keep+reorder bullets only (safe). "
                         "rewrite=actual JD-aligned rewriting with fact validation. "
                         "auto=rewrite if --use-claude, select if Ollama (default).")
    ap.add_argument("--send-telegram", action="store_true")
    ap.add_argument("--no-persist", action="store_true")
    ap.add_argument("--no-drive", action="store_true",
                    help="Skip Drive upload even if person.use_drive is True.")
    ap.add_argument("--no-sheet", action="store_true",
                    help="Skip Sheet upsert even if person.use_sheet is True.")
    ap.add_argument("--no-recruiter-score", action="store_true",
                    help="Skip the Claude recruiter-impression score.")
    hl = ap.add_mutually_exclusive_group()
    hl.add_argument("--highlights", action="store_true", default=None,
                    help="Force the 'Selected Highlights' section ON.")
    hl.add_argument("--no-highlights", action="store_true", default=None,
                    help="Force the 'Selected Highlights' section OFF.")
    ap.add_argument("--no-preview", action="store_true",
                    help="Skip rendering the company-themed preview HTML.")
    args = ap.parse_args()

    person_cfg = PERSON_DEFAULTS[args.person]
    if args.resume_yaml is None:
        args.resume_yaml = person_cfg["resume_yaml"]
    if args.profile is None:
        args.profile = person_cfg["profile_yaml"]
    # Per-person artifact subfolder so Sai's and Pooja's runs don't clobber.
    args.output = args.output / args.person

    args.output.mkdir(parents=True, exist_ok=True)

    print(_bold(f"\n=== JobSeeker tailor v2 ==="))
    print(f"  resume_yaml:  {args.resume_yaml}")
    print(f"  output:       {args.output}")
    print(f"  llm:          {'Claude (Sonnet)' if args.use_claude else f'Ollama ({args.ollama_model})'}")

    if not args.resume_yaml.exists():
        print(_yellow(
            f"\nERROR: {args.resume_yaml} not found.\n"
            f"Run this first to create it:\n"
            f"  python3 scripts/resume_to_yaml.py --use-claude\n"
            f"  (or pass your DOCX path via --input ~/Downloads/yourresume.docx)"
        ))
        return 1

    import yaml

    resume = yaml.safe_load(args.resume_yaml.read_text())
    profile = (
        yaml.safe_load(args.profile.read_text())
        if args.profile.exists()
        else {"profile_summary": "", "core_skills": []}
    )

    seen_db = Path(os.environ.get("JOBSEEKER_DB_PATH", "~/.jobseeker/seen.db")).expanduser()
    job = pick_job(seen_db, args.sample, person=args.person)
    dedup_id = _short_dedup_id(job["company"], job["source_id"])

    print(f"\n{_bold('Job picked:')} {_green(job['company'])} - {job['title']}")
    print(f"  dedup_id:   {_yellow(dedup_id)}")

    n_roles = len(resume.get("experience", []) or [])
    n_bullets = sum(len(r.get("bullets", []) or []) for r in resume.get("experience", []) or [])
    print(f"  resume:     {n_roles} roles, {n_bullets} bullets")

    # ── LLM picks bullets + writes summary ───────────────────────────────
    # Mode resolution: 'auto' picks rewrite if Claude is being used, select
    # otherwise. Rewrite mode is OFF for local Ollama by default because
    # qwen3 isn't reliable enough at preserving facts during rewrite.
    mode = args.mode
    if mode == "auto":
        mode = "rewrite" if args.use_claude else "select"
    print(f"\n{_bold('Step 1')}  LLM tailors resume "
          f"({_yellow('rewrite' if mode == 'rewrite' else 'select')} mode)")

    template = REWRITE_PROMPT if mode == "rewrite" else SELECT_PROMPT
    # Rewrite mode reads the WHOLE posting (8k chars). Select mode is lighter.
    jd_chars = 8000 if mode == "rewrite" else 4000
    prompt = template.format(
        company=job["company"],
        title=job["title"],
        jd=job["description_text"][:jd_chars],
        profile_summary=profile.get("profile_summary", ""),
        archetypes_block=_archetypes_block(resume),
        numbered_resume=_number_resume(resume),
    )
    # Budget: rewrite v3 emits jd_intelligence + per-bullet confidence +
    # defense_note + missing_skill_risks. ~12k is comfortable for resumes
    # up to 10 roles; small resumes (Pooja: 2 roles) finish in ~4k.
    max_tokens = 12000 if mode == "rewrite" else 3500
    raw = await llm_select(
        "claude" if args.use_claude else "ollama",
        args.ollama_model,
        prompt,
        max_tokens=max_tokens,
    )
    try:
        plan = _parse_json_loose(raw)
    except json.JSONDecodeError as e:
        print(_yellow(f"  LLM returned invalid JSON: {e}"))
        print(_yellow(f"  Raw output (first 500 chars):\n{raw[:500]}"))
        return 1

    summary = (plan.get("summary") or "").strip()
    summary = _scrub_visa(summary)
    plan["summary"] = summary
    # Also scrub each rewritten bullet — defense net for the LLM ignoring
    # the NOT-ALLOWED rule.
    for entry in plan.get("experience") or []:
        for r in entry.get("rewrites") or []:
            if r.get("rewritten"):
                r["rewritten"] = _scrub_visa(r["rewritten"])
    picks = plan.get("experience") or []
    emphasize = plan.get("skills_to_emphasize") or []
    chosen_archetype = (plan.get("archetype") or "").strip() or "default"

    if mode == "rewrite":
        warns = validate_rewrites(plan, resume)
        rewrite_count = sum(len(p.get("rewrites", []) or []) for p in picks)
        print(f"  bullets rewritten: {rewrite_count}")
        # confidence-tier counts
        conf_counter = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
        for p in picks:
            for r in p.get("rewrites") or []:
                c = (r.get("confidence") or "").upper()
                if c in conf_counter:
                    conf_counter[c] += 1
        print(f"  confidence tiers: HIGH={conf_counter['HIGH']}  "
              f"MEDIUM={conf_counter['MEDIUM']}  LOW={conf_counter['LOW']}")
        jdi = plan.get("jd_intelligence") or {}
        if jdi:
            ctype = jdi.get("company_type") or "?"
            frame = jdi.get("priority_frame") or ""
            print(f"  company-type: {_yellow(ctype)} | {frame[:80]}")
            req = jdi.get("required_skills") or []
            if req:
                print(f"  JD required skills: {', '.join(req[:8])}")
        cb = plan.get("interview_callback_estimate")
        if cb is not None:
            print(f"  interview callback estimate: {_green(str(cb))}/100")
    else:
        warns = _validate_picks(picks, resume)
        kept = sum(len(p.get("keep_bullet_indices", []) or []) for p in picks)
        print(f"  bullets kept:    {kept}")

    if warns:
        print(_yellow(f"  validation warnings ({len(warns)}):"))
        for w in warns[:8]:
            print(_yellow(f"    - {w}"))
        if mode == "rewrite" and len(warns) >= 5:
            print(_yellow(
                "  WARN: many fact-preservation issues — review the rendered "
                "PDF carefully or rerun with --mode select for safety"
            ))

    print(f"  archetype:      {_green(chosen_archetype)}")
    print(f"  summary:        {summary[:120]}{'...' if len(summary) > 120 else ''}")

    # ── Render HTML ───────────────────────────────────────────────────────
    print(f"\n{_bold('Step 2')}  rendering HTML")
    person = resume.get("person", {}) or {}
    # Highlights toggle: CLI flag wins, otherwise per-person default.
    if args.highlights:
        show_highlights = True
    elif args.no_highlights:
        show_highlights = False
    else:
        show_highlights = bool(person_cfg.get("show_highlights", False))
    highlights_html = _render_highlights(resume, chosen_archetype) if show_highlights else ""
    html = HTML_TEMPLATE.format(
        name=_esc(person.get("name", "")),
        contact=_build_contact(person),
        summary=_esc(summary),
        highlights_section=highlights_html,
        experience_html=_render_experience(resume, picks),
        skills_section=_render_skills(resume, emphasize),
        certifications_section=_render_certifications(resume),
        education_section=_render_education(resume),
    )
    html_path = args.output / "resume.tailored.html"
    html_path.write_text(html, encoding="utf-8")
    print(f"  -> {html_path}")

    # ── Render PDF ────────────────────────────────────────────────────────
    pdf_path = args.output / "resume.tailored.pdf"
    pdf_ok = render_pdf(html_path, pdf_path)
    if pdf_ok:
        print(f"  -> {pdf_path}")

    # ── Render DOCX (ATS-friendly, used for uploads) ─────────────────────
    docx_path = args.output / "resume.tailored.docx"
    docx_ok = False
    try:
        from scripts.render_docx import render_docx
        render_docx(
            resume=resume, plan=plan, out_path=docx_path,
            show_highlights=show_highlights,
        )
        docx_ok = True
        print(f"  -> {docx_path}")
    except Exception as e:
        print(_yellow(f"  DOCX render failed: {type(e).__name__}: {e}"))

    # ── Render company-themed preview HTML (for warm-intro sharing) ─────
    preview_path: Optional[Path] = None
    preview_theme = ""
    if not args.no_preview:
        try:
            from scripts.render_preview import render_preview_html
            pp = args.output / "resume.preview.html"
            preview_path, preview_theme = render_preview_html(
                resume=resume, plan=plan, out_path=pp,
                company=job["company"], show_highlights=show_highlights,
            )
            print(f"  -> {preview_path}  (theme: {_yellow(preview_theme)})")
        except Exception as e:
            print(_yellow(f"  preview render failed: {type(e).__name__}: {e}"))

    # ── Cover letter ──────────────────────────────────────────────────────
    print(f"\n{_bold('Step 3')}  cover letter")
    achievements = []
    for role in resume.get("experience", []) or []:
        for b in (role.get("bullets") or [])[:2]:
            achievements.append(f"  - {b}")
    cover_prompt = COVER_PROMPT.format(
        company=job["company"],
        title=job["title"],
        jd=job["description_text"][:3000],
        name=person.get("name", "the candidate"),
        years=profile.get("seniority", {}).get("min_years") or 8,
        achievements="\n".join(achievements[:8]),
    )
    if args.use_claude:
        cover = await _claude(cover_prompt)
    else:
        from ollama import AsyncClient

        host = os.environ.get("OLLAMA_HOST", "http://100.115.111.9:11434")
        client = AsyncClient(host=host)
        resp = await client.chat(
            model="qwen3:8b",
            messages=[{"role": "user", "content": cover_prompt}],
            think=False,
            options={"num_predict": 900},
        )
        cover = (resp.message.content or "").strip()
    cover = _scrub_visa(cover)
    cover_path = args.output / "cover_letter.txt"
    cover_path.write_text(cover, encoding="utf-8")
    print(f"  -> {cover_path}")

    # ── Match report (ATS + recruiter scoring) ────────────────────────────
    print(f"\n{_bold('Step 4')}  match report")
    from scripts.match_report import (
        compute_ats_score, compute_recruiter_score,
        build_match_report, write_match_artifacts,
        write_defense_md, write_risks_md, write_prep_md,
    )

    ats = compute_ats_score(job["description_text"], resume, plan)
    print(f"  ATS keyword match: {_green(str(ats['ats_score']))}/100  "
          f"({len(ats['matched_keywords'])}/{len(ats['jd_keywords'])} terms)")

    recruiter = None
    if args.use_claude and not args.no_recruiter_score:
        # Build the resume blob the recruiter actually sees: only the kept /
        # rewritten bullets, structured like the rendered DOCX. Feeding ALL
        # original bullets (the for_ats=True path) made the recruiter score a
        # "repetitive wall of text" that doesn't reflect the rendered resume.
        from scripts.match_report import _build_blob
        rendered_text = _build_blob(resume, plan, for_ats=False)
        recruiter = compute_recruiter_score(
            job["description_text"], rendered_text, use_claude=True,
        )
        if recruiter:
            print(f"  Recruiter impression: {_green(str(recruiter['recruiter_score']))}/100")
            verdict = recruiter.get("verdict", "")
            if verdict:
                print(_dim(f"    verdict: {verdict[:120]}"))
        else:
            print(_yellow("  Recruiter score skipped (Claude returned no JSON)"))

    job["_tailored_at"] = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    job["_person"] = args.person
    report = build_match_report(
        job=job, resume=resume, plan=plan, ats=ats,
        recruiter=recruiter, archetype=chosen_archetype,
    )
    report_path, missing_path = write_match_artifacts(args.output, report)
    print(f"  -> {report_path}")
    print(f"  -> {missing_path}  ({len(report['ats_keywords_missing'])} terms)")

    # New artifacts (only meaningful in rewrite mode where we have plan.experience.rewrites)
    defense_path: Optional[Path] = None
    risks_path: Optional[Path] = None
    prep_path: Optional[Path] = None
    if mode == "rewrite":
        defense_path = write_defense_md(args.output, plan, resume, job)
        risks_path = write_risks_md(args.output, plan, ats, job)
        prep_path = write_prep_md(args.output, plan, job)
        print(f"  -> {defense_path}")
        print(f"  -> {risks_path}")
        print(f"  -> {prep_path}")

    # Stash the JD itself so the Drive folder is self-contained
    job_path = args.output / "job.json"
    job_path.write_text(json.dumps(job, indent=2, default=str))

    # ── Persist to seen.db (artifact paths) ───────────────────────────────
    seen_db_path = Path(
        os.environ.get("JOBSEEKER_DB_PATH", "~/.jobseeker/seen.db")
    ).expanduser()
    if not args.no_persist:
        from services.notifier.artifact_store import get_default_store
        from services.notifier.dedup import DedupStore, compute_key, TAILOR_DONE

        store = get_default_store()
        safe_company = re.sub(r"[^\w-]", "-", job["company"])[:40]
        safe_title = re.sub(r"[^\w-]", "-", job["title"])[:40]
        # Prefer DOCX in the canonical store (ATS-friendly), fall back to PDF/HTML
        if docx_ok:
            resume_dst, ext = docx_path, "docx"
        elif pdf_ok:
            resume_dst, ext = pdf_path, "pdf"
        else:
            resume_dst, ext = html_path, "html"
        resume_name = f"resume_{safe_company}_{safe_title}.{ext}"
        cover_name = f"cover_letter_{safe_company}_{safe_title}.txt"
        resume_canonical = store.put_file(dedup_id, resume_dst, name=resume_name)
        cover_canonical = store.put_file(dedup_id, cover_path, name=cover_name)
        with DedupStore(seen_db_path) as db:
            full_key = compute_key(job["company"], job["source_id"])
            db.insert_if_new(
                key=full_key, company=job["company"], title=job["title"],
                url=job["url"], score=job.get("_score") or 0.0,
            )
            db.record_artifacts(
                key=full_key,
                resume_path=resume_canonical,
                cover_letter_path=cover_canonical,
                tailor_status=TAILOR_DONE,
            )
        print(f"\n{_bold('Step 5')}  persisted to seen.db (dedup_id={dedup_id})")

    # ── Google Drive folder + uploads (Sai-only by default) ──────────────
    drive_folder_url: str = ""
    if person_cfg["use_drive"] and not args.no_drive:
        print(f"\n{_bold('Step 6')}  Google Drive sync")
        try:
            from shared.google_drive import DriveSyncer
            ds = DriveSyncer.from_env()
        except Exception as e:
            print(_yellow(f"  Drive disabled: {type(e).__name__}: {e}"))
            ds = None
        if ds is None:
            print(_yellow("  Drive disabled (set GOOGLE_DRIVE_PARENT_FOLDER_ID + "
                         "GOOGLE_SERVICE_ACCOUNT_JSON to enable)"))
        else:
            try:
                folder_name = (
                    f"{re.sub(r'[^A-Za-z0-9]+', '-', job['company'])[:30]}"
                    f"_{re.sub(r'[^A-Za-z0-9]+', '-', job['title'])[:40]}"
                    f"_{datetime.utcnow().strftime('%Y-%m-%d')}"
                )
                folder = ds.get_or_create_folder(folder_name)
                folder_id = folder["id"]
                drive_folder_url = folder.get("webViewLink", "")
                print(f"  folder: {_green(folder_name)}")
                print(f"  url:    {drive_folder_url}")

                # Upload everything that exists in args.output. Order: most
                # important first so a transient failure leaves the user with
                # at least the resume + cover letter.
                upload_order: list[Path] = []
                if docx_ok:
                    upload_order.append(docx_path)
                if pdf_ok:
                    upload_order.append(pdf_path)
                upload_order.append(html_path)
                # Themed preview HTML — meant for sharing via Drive link to
                # recruiters in DM/email; ATS file stays clean.
                if preview_path is not None and preview_path.exists():
                    upload_order.append(preview_path)
                upload_order.extend([
                    cover_path, report_path, missing_path, job_path,
                ])
                # New artifacts (only present in rewrite mode)
                for extra in (defense_path, risks_path, prep_path):
                    if extra is not None:
                        upload_order.append(extra)
                upload_failures = 0
                quota_error = False
                for p in upload_order:
                    if not p.exists():
                        continue
                    try:
                        f = ds.upload_file(p, folder_id, name=p.name)
                        print(_dim(f"    + {p.name}  ({f.get('id', '?')})"))
                    except Exception as e:
                        upload_failures += 1
                        msg = str(e)
                        if "storageQuotaExceeded" in msg:
                            quota_error = True
                            # Print the long error only once — the rest will
                            # all hit the same quota wall.
                            if upload_failures == 1:
                                print(_yellow(
                                    f"    upload {p.name} failed: "
                                    f"storageQuotaExceeded (Service Account "
                                    f"quota=0)"
                                ))
                        else:
                            print(_yellow(f"    upload {p.name} failed: {e}"))
                if quota_error:
                    print(_yellow(
                        "\n  ── Drive uploads failed (Service Account has no quota) ──\n"
                        "  This is a Google design limit, not a bug. Two fixes:\n"
                        "\n"
                        "  Option A (RECOMMENDED if you have Google Workspace):\n"
                        "    1. https://drive.google.com/drive/shared-drives -> + New\n"
                        "    2. Add jobseeker-sync@jobseeker-496610.iam.gserviceaccount.com\n"
                        "       as Manager of that Shared Drive.\n"
                        "    3. Open the Shared Drive and create a parent folder.\n"
                        "    4. Copy its folder ID (URL after /folders/) into:\n"
                        "       GOOGLE_DRIVE_PARENT_FOLDER_ID=<new id>\n"
                        "\n"
                        "  Option B (works on personal Google accounts):\n"
                        "    Switch to OAuth user credentials. Run:\n"
                        "      python3 scripts/google_oauth_init.py\n"
                        "    (creates ~/.jobseeker/google_oauth.json once;\n"
                        "     after that DriveSyncer uses YOUR account's quota.)\n"
                    ))

                # Make folder shareable so the Telegram link opens without
                # forcing a Google login on the user's phone.
                ds.make_anyone_viewable(folder_id)
            except Exception as e:
                print(_yellow(f"  Drive sync failed: {type(e).__name__}: {e}"))
                drive_folder_url = ""

    # ── Sheet upsert (Sai-only by default) ───────────────────────────────
    if person_cfg["use_sheet"] and not args.no_sheet and not args.no_persist:
        print(f"\n{_bold('Step 7')}  Google Sheet sync")
        try:
            from services.notifier.sheet_sync import SheetSyncer
            ss = SheetSyncer.from_env(person=args.person)
        except Exception as e:
            print(_yellow(f"  Sheet sync error: {type(e).__name__}: {e}"))
            ss = None
        if ss is None:
            print(_yellow("  Sheet sync disabled (set GOOGLE_SHEETS_ID_"
                         f"{args.person.upper()} + GOOGLE_SERVICE_ACCOUNT_JSON)"))
        else:
            try:
                row_data = {
                    "dedup_id": dedup_id,
                    "person": args.person,
                    "company": job["company"],
                    "title": job["title"],
                    "department": job.get("department") or "",
                    "location": job.get("location") or "",
                    "remote": bool(job.get("remote")),
                    "source": job.get("source") or "",
                    "match_score": job.get("_score") or 0.0,
                    "ats_score": ats["ats_score"],
                    "recruiter_score": (recruiter or {}).get("recruiter_score"),
                    "archetype": chosen_archetype,
                    "visa_ok": "no sponsorship" not in (job.get("description_text", "") or "").lower(),
                    "url": job["url"],
                    "drive_folder_link": drive_folder_url,
                    "resume_url": drive_folder_url,  # same folder for now
                    "cover_letter_url": drive_folder_url,
                    "required_skills": ", ".join(report["jd_requirements"][:8]),
                    "missing_skills": ", ".join(report["ats_keywords_missing"][:12]),
                    "status": "NEW",
                    "notes": (recruiter or {}).get("verdict", "") or f"archetype={chosen_archetype}",
                }
                status = await ss.upsert_dict(row_data)
                print(f"  sheet: {_green(status)}")
            except Exception as e:
                print(_yellow(f"  sheet upsert failed: {type(e).__name__}: {e}"))

    # ── Telegram (per-person bot + chat) ──────────────────────────────────
    if args.send_telegram:
        token = os.environ.get(person_cfg["telegram_token_env"], "").strip()
        chat = os.environ.get(person_cfg["telegram_chat_env"], "").strip()
        # Sanity check: Pooja's chat id was placeholder "987654321" early on.
        # Don't push to a placeholder; warn instead.
        if chat in ("987654321", "0", ""):
            print(_yellow(
                f"\nWARN: {person_cfg['telegram_chat_env']} looks unset/placeholder "
                f"(value={chat!r}). Skipping Telegram for {args.person}."
            ))
        elif not token:
            print(_yellow(
                f"\nWARN: {person_cfg['telegram_token_env']} not set. "
                f"Skipping Telegram for {args.person}."
            ))
        else:
            print(f"\n{_bold('Step 8')}  Telegram ({args.person} -> chat={chat})")
            from scripts.demo_tailor import (
                _build_alert_text, _alert_keyboard,
                telegram_send_message, telegram_send_document,
            )
            # JD-grounded prep summary — uses jd_intelligence so the
            # message tells the candidate exactly what to prep for THIS role.
            jdi = plan.get("jd_intelligence") or {}
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
            score_parts = [f"ATS {ats['ats_score']}%"]
            if recruiter:
                score_parts.append(f"recruiter {recruiter['recruiter_score']}/100")
            if cb is not None:
                score_parts.append(f"callback est. {cb}/100")
            prep_lines.append("Scores: " + " · ".join(score_parts))
            conf = report.get("confidence_breakdown") or {}
            if conf:
                prep_lines.append(
                    f"Bullet confidence: HIGH={conf.get('HIGH',0)} "
                    f"MED={conf.get('MEDIUM',0)} LOW={conf.get('LOW',0)} "
                    f"(see interview_defense.md before applying)"
                )
            if drive_folder_url:
                prep_lines.append(f"Drive: {drive_folder_url}")
            study_for_tg = "\n".join(prep_lines)
            text = _build_alert_text(
                {**job, "_score": job.get("_score") or 0.0},
                profile, study_for_tg,
            )
            kb = _alert_keyboard(dedup_id, job["url"])
            msg_id = await telegram_send_message(token, chat, text, kb)
            if msg_id is not None:
                # Prefer DOCX for the upload (most ATS-friendly).
                resume_to_send = (
                    docx_path if docx_ok
                    else (pdf_path if pdf_ok else html_path)
                )
                await telegram_send_document(
                    token, chat, resume_to_send,
                    caption=f"Tailored resume - *{job['company']}*",
                    reply_to=msg_id,
                )
                await telegram_send_document(
                    token, chat, cover_path,
                    caption=f"Cover letter - *{job['company']}*",
                    reply_to=msg_id,
                )
                # Defense + prep are critical — attach if present
                if defense_path is not None and defense_path.exists():
                    await telegram_send_document(
                        token, chat, defense_path,
                        caption=f"Interview defense notes - *{job['company']}*",
                        reply_to=msg_id,
                    )
                if prep_path is not None and prep_path.exists():
                    await telegram_send_document(
                        token, chat, prep_path,
                        caption=f"Interview prep - *{job['company']}*",
                        reply_to=msg_id,
                    )
                print(f"  alert msg_id={msg_id}, attachments sent")

    print(f"\n{_bold('=' * 70)}")
    print(f"All files in: {_green(str(args.output))}")
    print(f"  open {args.output}/resume.tailored.html       # browser preview")
    if pdf_ok:
        print(f"  open {args.output}/resume.tailored.pdf       # PDF")
    if docx_ok:
        print(f"  open {args.output}/resume.tailored.docx      # DOCX (for ATS)")
    print(f"  cat  {args.output}/cover_letter.txt")
    print(f"  cat  {args.output}/match_report.json")
    if drive_folder_url:
        print(f"\nDrive folder: {_green(drive_folder_url)}")
    print(_bold("=" * 70))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
