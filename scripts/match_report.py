"""Match scoring + reporting for the tailored resume pipeline.

Two scores produced per match:

  ats_score         (0-100, deterministic):
     What fraction of the JD's high-value technical terms appear in the
     candidate's tailored resume? Pure keyword overlap, no LLM. Recruiters
     and ATS systems weight this heavily.

  recruiter_score   (0-100, judged by Claude):
     Reads the rendered resume + JD and rates "would I forward this to
     the hiring manager?" — a small, structured rubric. Skipped if no
     Claude backend is available.

Plus three artifacts saved next to the resume:

  match_report.json   structured numbers (above two scores, missing/present
                      keywords, archetype, jd_requirements, raw match score
                      from the upstream scorer if available)
  missing_skills.txt  human-readable list of JD terms NOT in the resume
                      (so the candidate can decide whether to learn / mention
                      them in the cover letter)
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Optional

log = logging.getLogger("scripts.match_report")


# ── Vocabulary used to extract technical keywords from a JD ────────────────
# A practical, opinionated list — not exhaustive. Picks the words a hiring
# manager would scan for. We DO want false-positives more than misses
# (better to flag too many JD terms than miss "Helm" entirely).
TECH_VOCAB = {
    # Languages
    "python", "go", "golang", "java", "javascript", "typescript", "rust", "c++",
    "ruby", "scala", "kotlin", "bash", "shell", "sql",
    # Cloud
    "aws", "gcp", "azure", "ec2", "s3", "eks", "rds", "iam", "vpc", "lambda",
    "fargate", "ecs", "cloudfront", "route53", "cloudwatch", "fsxn",
    # Containers / Orchestration
    "kubernetes", "k8s", "docker", "helm", "rancher", "openshift", "containerd",
    "kubelet", "kustomize", "argocd", "fluxcd", "kyverno", "keda",
    # CI/CD
    "jenkins", "harness", "github actions", "gitlab", "ci/cd", "ci-cd",
    "buildkite", "circleci", "travis", "gitops", "spinnaker", "argo",
    # IaC
    "terraform", "ansible", "pulumi", "cloudformation", "chef", "puppet",
    # Observability
    "prometheus", "grafana", "datadog", "dynatrace", "splunk", "loki", "jaeger",
    "opentelemetry", "otel", "elk", "efk", "newrelic", "sentry", "honeycomb",
    # Reliability concepts
    "slo", "sli", "sla", "rca", "mttr", "incident response", "on-call", "oncall",
    "high availability", "disaster recovery", "chaos engineering",
    "release engineering", "release management", "site reliability",
    "platform engineering", "blue/green", "canary", "feature flags",
    # Workflow / Messaging
    "temporal", "nats", "jetstream", "rabbitmq", "kafka", "redis", "celery",
    # Storage / VM
    "netapp", "vmware", "kvm", "cloudstack", "fsx", "trident", "nfs",
    # Auth / Sec
    "oidc", "oauth", "saml", "jwt", "vault", "consul", "kms", "tls",
    "rbac", "iam policy", "least privilege",
    # ML / AI (for Pooja)
    "tensorflow", "pytorch", "keras", "scikit-learn", "scikit", "huggingface",
    "transformers", "llm", "rag", "fine-tuning", "fine tuning", "embedding",
    "vector database", "pinecone", "milvus", "weaviate", "openai", "anthropic",
    "nlp", "computer vision", "rnn", "cnn", "lstm", "attention", "diffusion",
    "deep learning", "reinforcement learning", "alignment", "rlhf",
    "feature engineering", "model evaluation", "etl", "power bi", "tableau",
    "pandas", "numpy", "matplotlib", "librosa", "spectrogram",
    # Web
    "react", "node", "node.js", "nodejs", "rest api", "graphql", "fastapi",
    "django", "flask", "spring",
    # Databases
    "postgres", "postgresql", "mysql", "mongodb", "elasticsearch", "dynamodb",
    "cassandra", "redshift", "snowflake", "bigquery",
    # Tools / OS
    "linux", "rhel", "ubuntu", "macos", "git", "github", "bitbucket",
    "nginx", "apache",
}

# Multi-word terms must be checked before single tokens. We sort once.
_MULTIWORD = sorted([t for t in TECH_VOCAB if " " in t], key=len, reverse=True)
_SINGLEWORD = {t for t in TECH_VOCAB if " " not in t}


def extract_jd_keywords(jd_text: str) -> list[str]:
    """Extract canonical tech keywords from a JD. Returns lowercased terms."""
    if not jd_text:
        return []
    text = jd_text.lower()
    found: set[str] = set()
    # Multi-word first (so "github actions" doesn't get split into "github")
    for term in _MULTIWORD:
        if term in text:
            found.add(term)
            text = text.replace(term, " " * len(term))
    # Single-word: word-boundary regex so "go" doesn't match "ago"
    for term in _SINGLEWORD:
        pat = re.escape(term)
        if re.search(rf"\b{pat}\b", text):
            found.add(term)
    return sorted(found)


def _resume_text_blob(resume: dict, plan: dict) -> str:
    """Build a rendered-resume text blob — matches what the DOCX/HTML show.

    Two consumers:
      1. ATS scorer: needs every term that appears anywhere in the resume,
         so it's lenient — includes dropped bullets and full skills.
      2. Recruiter judge: needs to see EXACTLY what the recruiter would see,
         so it should NOT include dropped bullets (else the recruiter scores
         a wall of text it never actually rendered).

    Use `for_ats=True` for ATS scoring, `for_ats=False` for recruiter prompts.
    Default keeps the legacy ATS-friendly behavior.
    """
    return _build_blob(resume, plan, for_ats=True)


def _build_blob(resume: dict, plan: dict, *, for_ats: bool) -> str:
    parts: list[str] = []

    person = resume.get("person") or {}
    if not for_ats:
        # Recruiter sees the header
        parts.append(person.get("name", ""))
        contact_bits = [str(person.get(k, "")) for k in
                        ("email", "phone", "location", "linkedin") if person.get(k)]
        if contact_bits:
            parts.append(" | ".join(contact_bits))

    parts.append((plan.get("summary") or ""))

    if not for_ats:
        parts.append("\nSELECTED HIGHLIGHTS")
    for h in (resume.get("key_highlights") or [])[:6]:
        parts.append(f"• {h}" if not for_ats else str(h))

    if not for_ats:
        parts.append("\nEXPERIENCE")

    # Experience: rewrites (if rewrite mode) or selected originals (if select mode).
    # CRITICAL: never include both — that's what made the recruiter call it
    # "repetitive wall of text".
    roles = resume.get("experience") or []
    picks_by_idx = {}
    for entry in plan.get("experience") or []:
        ridx = entry.get("role_index")
        # tolerant int parse — same behavior as tailor_v2._coerce_int
        if isinstance(ridx, str) and ridx.strip().isdigit():
            ridx = int(ridx.strip())
        if isinstance(ridx, int):
            picks_by_idx[ridx] = entry

    for ridx, role in enumerate(roles, start=1):
        sel = picks_by_idx.get(ridx)
        bullets_visible: list[str] = []
        bullets = role.get("bullets") or []
        if sel and sel.get("rewrites"):
            for r in sel["rewrites"]:
                t = (r.get("rewritten") or "").strip()
                if t:
                    bullets_visible.append(t)
        elif sel:
            keep = sel.get("keep_bullet_indices") or []
            keep = [int(i) for i in keep
                    if (isinstance(i, int)) or (isinstance(i, str) and i.strip().isdigit())]
            keep = [i for i in keep if 1 <= i <= len(bullets)]
            bullets_visible = [bullets[i - 1] for i in keep]
        else:
            # No plan entry for this role — for_ats includes its bullets,
            # for recruiter we skip it (the role wouldn't render).
            if for_ats:
                bullets_visible = list(bullets)

        if not bullets_visible:
            continue

        if not for_ats:
            head = f"{role.get('company', '')} — {role.get('title', '')}"
            dates = f"{role.get('start', '')} - {role.get('end', '')}".strip(" -")
            parts.append(f"\n{head}  ({dates})")
        for b in bullets_visible:
            parts.append(f"• {b}" if not for_ats else str(b))

    # Skills
    if not for_ats:
        parts.append("\nSKILLS")
    skills = resume.get("skills")
    if isinstance(skills, dict):
        for group, items in skills.items():
            if not items:
                continue
            line = ", ".join(str(s) for s in items)
            parts.append(f"{group}: {line}" if not for_ats else line)
    elif isinstance(skills, list):
        parts.append(", ".join(str(s) for s in skills))

    # Certifications + Education only matter for ATS keyword extraction.
    # Recruiters skim them but don't penalize layout.
    if not for_ats:
        certs = resume.get("certifications") or []
        if certs:
            parts.append("\nCERTIFICATIONS")
            for c in certs:
                if isinstance(c, dict):
                    name = c.get("name", "")
                    yr = c.get("year") or ""
                    parts.append(f"{name} {yr}".strip())
                else:
                    parts.append(str(c))
        edu = resume.get("education") or []
        if edu:
            parts.append("\nEDUCATION")
            for e in edu:
                line = f"{e.get('degree','')}, {e.get('field','')} — {e.get('school','')} ({e.get('end','')})"
                parts.append(line.strip())
    else:
        # ATS path: just dump cert names + edu fields for keyword recall
        for c in resume.get("certifications") or []:
            parts.append(c.get("name", "") if isinstance(c, dict) else str(c))

    return "\n".join(p for p in parts if p)


def compute_ats_score(jd_text: str, resume: dict, plan: dict) -> dict:
    """Pure keyword-overlap score.

    Returns:
      {
        "ats_score": int 0-100,
        "jd_keywords": [...],           # canonical terms in the JD
        "matched_keywords": [...],      # JD terms also in resume
        "missing_keywords": [...],      # JD terms NOT in resume (gap list)
      }
    """
    jd_terms = extract_jd_keywords(jd_text)
    if not jd_terms:
        return {
            "ats_score": 0,
            "jd_keywords": [],
            "matched_keywords": [],
            "missing_keywords": [],
        }
    blob = _build_blob(resume, plan, for_ats=True).lower()
    matched = []
    missing = []
    for term in jd_terms:
        # word boundary again — protect "go" / "k8s" against false hits
        if " " in term:
            hit = term in blob
        else:
            hit = re.search(rf"\b{re.escape(term)}\b", blob) is not None
        (matched if hit else missing).append(term)
    score = round(100 * len(matched) / max(1, len(jd_terms)))
    return {
        "ats_score": score,
        "jd_keywords": jd_terms,
        "matched_keywords": matched,
        "missing_keywords": missing,
    }


# ── Recruiter score (Claude only) ─────────────────────────────────────────

RECRUITER_PROMPT = """You are a senior technical recruiter at a top-tier company.
You see 200 resumes a day. Be decisive — most resumes do NOT make your shortlist.

JD (truncated):
{jd}

CANDIDATE RESUME (rendered, post-tailoring):
---
{resume_text}
---

Rate this resume on 5 axes (each 0-20):
  - jd_match: Does the resume's content directly match what the JD asks for?
  - achievement_density: Are bullets quantified outcomes vs vague responsibilities?
  - technical_specificity: Are exact technologies named (vs hand-wavy)?
  - clarity: Are sections scannable in <30s? Strong action verbs? No fluff?
  - shortlist_signal: Would YOU pass this resume to the hiring manager?

Output ONLY this JSON:
{{
  "jd_match": <int 0-20>,
  "achievement_density": <int 0-20>,
  "technical_specificity": <int 0-20>,
  "clarity": <int 0-20>,
  "shortlist_signal": <int 0-20>,
  "verdict": "<one sentence: forward / hold / pass + why>"
}}"""


def compute_recruiter_score(
    jd_text: str,
    rendered_resume_text: str,
    *,
    use_claude: bool,
) -> Optional[dict]:
    """Ask Claude to play recruiter. Returns None if Claude unavailable."""
    if not use_claude:
        return None
    try:
        from shared.llm_client import claude_chat, available_backend
    except Exception:
        return None
    if available_backend() == "none":
        return None
    prompt = RECRUITER_PROMPT.format(
        jd=jd_text[:3500], resume_text=rendered_resume_text[:6000],
    )
    try:
        raw = claude_chat(prompt, max_tokens=400)
    except Exception as e:
        log.warning("recruiter score failed: %s", e)
        return None
    # Loose JSON parse — some models wrap in fences
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*\n?|\n?```$", "", raw, flags=re.MULTILINE).strip()
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return None
    total = sum(
        int(obj.get(k, 0))
        for k in ("jd_match", "achievement_density", "technical_specificity",
                  "clarity", "shortlist_signal")
    )
    obj["recruiter_score"] = max(0, min(100, total))
    return obj


# ── Build & write artifacts ───────────────────────────────────────────────

def build_match_report(
    *,
    job: dict,
    resume: dict,
    plan: dict,
    ats: dict,
    recruiter: Optional[dict],
    archetype: str,
) -> dict:
    """Assemble the single source-of-truth match report dict."""
    jdi = plan.get("jd_intelligence") or {}
    return {
        "company": job.get("company"),
        "title": job.get("title"),
        "url": job.get("url"),
        "location": job.get("location"),
        "remote": job.get("remote"),
        "scraped_at": job.get("scraped_at"),
        "tailored_at": job.get("_tailored_at"),
        "person": job.get("_person"),
        "raw_match_score": job.get("_score"),
        "ats_score": ats["ats_score"],
        "ats_keywords_matched": ats["matched_keywords"],
        "ats_keywords_missing": ats["missing_keywords"],
        "ats_keywords_total": len(ats["jd_keywords"]),
        "recruiter_score": (recruiter or {}).get("recruiter_score"),
        "recruiter_breakdown": recruiter,
        "archetype": archetype,
        "company_type": jdi.get("company_type"),
        "priority_frame": jdi.get("priority_frame"),
        "jd_required_skills": jdi.get("required_skills") or [],
        "jd_preferred_skills": jdi.get("preferred_skills") or [],
        "jd_core_responsibilities": jdi.get("core_responsibilities") or [],
        "jd_hidden_keywords": jdi.get("hidden_keywords") or [],
        # Backwards compat: legacy field name still consumed by sheet_sync
        "jd_requirements": (
            jdi.get("required_skills") or plan.get("jd_requirements") or []
        ),
        "skills_emphasized": (
            (plan.get("skills_reordered") or {}).get("lead_with")
            or plan.get("skills_to_emphasize") or []
        ),
        "missing_skill_risks": plan.get("missing_skill_risks") or [],
        "interview_callback_estimate": plan.get("interview_callback_estimate"),
        "confidence_breakdown": _confidence_breakdown(plan),
    }


def _confidence_breakdown(plan: dict) -> dict:
    """Count rewrites by confidence tier."""
    counts = {"HIGH": 0, "MEDIUM": 0, "LOW": 0, "UNKNOWN": 0}
    for entry in plan.get("experience") or []:
        for r in entry.get("rewrites") or []:
            c = (r.get("confidence") or "").upper()
            counts[c if c in counts else "UNKNOWN"] += 1
    return counts


def write_match_artifacts(out_dir: Path, report: dict) -> tuple[Path, Path]:
    """Write match_report.json + missing_skills.txt to `out_dir`. Returns paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / "match_report.json"
    report_path.write_text(json.dumps(report, indent=2, default=str))

    missing_path = out_dir / "missing_skills.txt"
    missing = report.get("ats_keywords_missing") or []
    if missing:
        body = (
            "JD keywords NOT present in your tailored resume.\n"
            "Add them only if true to your experience — never invent.\n"
            f"({len(missing)} terms)\n\n" + "\n".join(f"  - {t}" for t in missing)
        )
    else:
        body = "All JD keywords are present in your tailored resume."
    missing_path.write_text(body)
    return report_path, missing_path


def write_defense_md(out_dir: Path, plan: dict, resume: dict, job: dict) -> Path:
    """Write interview_defense.md — per-bullet honest backing.

    Used by the candidate before interviews: each rewritten bullet, paired
    with the LLM's confidence tag and a one-line "honest version" of how to
    talk about it. Not visible to recruiters.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "interview_defense.md"
    lines: list[str] = []
    lines.append(f"# Interview defense — {job.get('company','?')} · {job.get('title','?')}")
    lines.append("")
    lines.append("Per-bullet honest backing for the tailored resume. **Not** sent to recruiters —")
    lines.append("this is your prep doc. If the interviewer drills into a bullet, the **defense**")
    lines.append("line is your honest answer. **LOW**-confidence bullets are the ones most worth")
    lines.append("rehearsing.")
    lines.append("")
    roles = resume.get("experience") or []
    for entry in plan.get("experience") or []:
        ridx = entry.get("role_index")
        if not isinstance(ridx, int) or not (1 <= ridx <= len(roles)):
            continue
        role = roles[ridx - 1]
        lines.append(f"## {role.get('company','?')} — {role.get('title','?')}")
        lines.append(f"_({role.get('start','?')} – {role.get('end','?')})_")
        lines.append("")
        for r in entry.get("rewrites") or []:
            conf = (r.get("confidence") or "").upper() or "—"
            badge = {"HIGH": "🟢", "MEDIUM": "🟡", "LOW": "🔴"}.get(conf, "⚪")
            lines.append(f"- {badge} **[{conf}]** {r.get('rewritten','').strip()}")
            if r.get("defense_note"):
                lines.append(f"    - *defense:* {r['defense_note'].strip()}")
            if r.get("jd_alignment"):
                lines.append(f"    - *jd alignment:* {r['jd_alignment'].strip()}")
            orig_idx = r.get("original_index")
            if isinstance(orig_idx, int) and 1 <= orig_idx <= len(role.get("bullets") or []):
                orig = role["bullets"][orig_idx - 1]
                lines.append(f"    - *original (for reference):* {orig}")
            lines.append("")
    path.write_text("\n".join(lines))
    return path


def write_risks_md(out_dir: Path, plan: dict, ats: dict, job: dict) -> Path:
    """Write missing_skill_risks.md — JD requirements not in candidate's
    profile, with severity + mitigation suggestions."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "missing_skill_risks.md"
    lines: list[str] = []
    lines.append(f"# Missing skill risks — {job.get('company','?')} · {job.get('title','?')}")
    lines.append("")
    plan_risks = plan.get("missing_skill_risks") or []
    if plan_risks:
        lines.append("## LLM-identified risks")
        lines.append("")
        for risk in plan_risks:
            sev = (risk.get("severity") or "?").upper()
            badge = {"BLOCKER": "🔴", "MODERATE": "🟡", "MINOR": "🟢"}.get(sev, "⚪")
            lines.append(f"- {badge} **[{sev}]** `{risk.get('skill','?')}`")
            if risk.get("mitigation"):
                lines.append(f"    - mitigation: {risk['mitigation']}")
        lines.append("")
    missing = ats.get("missing_keywords") or []
    if missing:
        lines.append("## ATS keyword gaps")
        lines.append("")
        lines.append("JD keywords not present in your rendered resume:")
        for t in missing:
            lines.append(f"- `{t}`")
    if not plan_risks and not missing:
        lines.append("No major skill gaps identified for this JD.")
    path.write_text("\n".join(lines))
    return path


def write_prep_md(out_dir: Path, plan: dict, job: dict) -> Path:
    """Write interview_prep.md — JD-grounded study guide.

    Uses jd_intelligence to pull out the company's priorities, required
    skills, hidden keywords. Surfaces likely interview topics and a short
    company-research checklist. Replaces the old generic study_guide.md
    (which used a fixed Ollama prompt with no JD knowledge).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "interview_prep.md"
    jdi = plan.get("jd_intelligence") or {}
    lines: list[str] = []
    lines.append(f"# Interview prep — {job.get('company','?')} · {job.get('title','?')}")
    lines.append("")
    if jdi.get("priority_frame"):
        lines.append(f"**What this team really wants:** {jdi['priority_frame']}")
        lines.append("")
    if jdi.get("company_type"):
        lines.append(f"**Company type:** `{jdi['company_type']}`")
        lines.append("")

    if jdi.get("required_skills"):
        lines.append("## Must know cold (required by JD)")
        for s in jdi["required_skills"][:10]:
            lines.append(f"- {s}")
        lines.append("")

    if jdi.get("preferred_skills"):
        lines.append("## Brush up (JD preferred)")
        for s in jdi["preferred_skills"][:10]:
            lines.append(f"- {s}")
        lines.append("")

    if jdi.get("core_responsibilities"):
        lines.append("## Likely interview topics (mapped from responsibilities)")
        for r in jdi["core_responsibilities"][:8]:
            lines.append(f"- {r}")
        lines.append("")

    if jdi.get("hidden_keywords"):
        lines.append("## Hidden ATS keywords to weave into stories")
        lines.append(", ".join(jdi["hidden_keywords"][:15]))
        lines.append("")

    lines.append("## Company-specific research checklist")
    lines.append(f"- Read 2 recent {job.get('company','?')} engineering blog posts")
    lines.append(f"- Skim the team's open-source repos (if any)")
    lines.append(f"- Find 1-2 recent press releases / earnings highlights")
    lines.append(f"- Identify the team's tech stack & deployment cadence")
    lines.append("")
    path.write_text("\n".join(lines))
    return path
