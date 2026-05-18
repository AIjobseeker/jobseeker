from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Optional

import numpy as np
import yaml

from services.scorer.models import ScoredJob

log = logging.getLogger("scorer")

DEFAULT_MODEL = "all-MiniLM-L6-v2"
DESCRIPTION_TRUNCATE = 1500
SKILL_OVERLAP_BONUS_PER = 0.02
SKILL_OVERLAP_BONUS_CAP = 0.10

NO_SPONSORSHIP_PHRASES = [
    "no visa sponsorship",
    "not able to sponsor",
    "cannot sponsor",
    "us citizenship required",
    "must be authorized to work",
]


# ── Non-tech disambiguation ──────────────────────────────────────────────
# Word substrings that almost certainly mean a non-software role even when
# the title also contains "infrastructure" or "engineer" or "architect".
# Hits HARD-CAP the score.
NON_TECH_TITLE_TOKENS = [
    # Civil/construction — match the standalone word too, since the role
    # might be "Civil Infrastructure Engineer" with words between them.
    "civil ", "civil engineer", "civil engineering",
    "structural engineer", "structural eng",
    "mechanical engineer", "mechanical eng",
    "electrical engineer ",   # space-suffix to avoid "electrical engineering software"
    "chemical engineer", "chemical eng",
    "industrial engineer",
    "biomedical engineer",
    "petroleum engineer",
    "environmental engineer",
    "manufacturing engineer", "manufacturing operations",
    "highway", "roadway", "bridge engineer",
    "construction", "construction manager",
    "facilities", "facility engineer", "facility manager",
    "real estate", "property manager",
    "landscape architect", "interior architect",
    "building architect", "architectural designer",
    "logistics manager", "supply chain manager",
]

# Title words that are genuinely ambiguous between tech and non-tech. We
# require the title ALSO contain at least one tech-context token, OR the
# job description must be visibly tech-flavored (kubernetes, cloud, etc.),
# else we apply a moderate penalty.
AMBIGUOUS_TITLE_TOKENS = ["architect", "infrastructure"]
TECH_CONTEXT_TOKENS = [
    "software", "cloud", "kubernetes", "k8s", "devops", "platform",
    "site reliability", "sre", "data", "ml ", "ai ", "ai/", "machine learning",
    "developer", "engineering manager",
    "security", "network", "systems", "production", "backend",
    "iac", "terraform", "linux", "observability",
    "sde", "saas",
]


def _has_token(text: str, tokens: list[str]) -> Optional[str]:
    """Return the first token found in `text` (already lowercased)."""
    for tok in tokens:
        if tok in text:
            return tok
    return None


def _count_tokens(text: str, tokens: list[str]) -> int:
    """How many tokens (case-insensitive substring match) appear in text."""
    return sum(1 for tok in tokens if tok in text)


# ── SRE/DevOps/Cloud niche density ───────────────────────────────────────
# Vocabulary that distinguishes "this is genuinely an operational SRE/DevOps/
# Cloud role" from "this is a software engineering role that happens to use
# Kubernetes". The second sees k8s as a tool; the first lives there.
#
# Profiled for: 8yr SRE/Platform/Cloud engineer, IC track, infrastructure
# focus. Adjust the lists if you want a different niche.
SRE_NICHE_TOKENS = [
    # operational / reliability vocabulary — strongly diagnostic
    "on-call", "on call rotation", "pager", "incident response",
    "runbook", "postmortem", "rca",
    "slo", "sli", "sla", "error budget", "reliability engineering",
    "site reliability", "chaos engineering",
    # IaC / config mgmt — heavily SRE-adjacent
    "terraform", "pulumi", "ansible", "puppet", "chef ", "salt",
    "infrastructure as code", "iac",
    "helm chart", "argocd", "flux ",
    # platform / k8s ops (not just "kubernetes" once)
    "kubernetes operator", "kubernetes platform", "k8s platform",
    "platform engineering", "internal developer platform", "idp",
    "cluster operations", "cluster management", "multi-cluster",
    "control plane",
    # observability / SRE tooling
    "prometheus", "grafana", "datadog", "new relic", "splunk",
    "opentelemetry", "tracing", "metrics pipeline", "logging pipeline",
    "observability", "monitoring",
    # cloud platform ops
    "aws organizations", "aws control tower", "landing zone",
    "vpc peering", "transit gateway", "service mesh", "istio", "linkerd",
    # ci/cd ops
    "ci/cd pipeline", "github actions", "jenkins", "spinnaker",
    "blue/green deploy", "canary deploy", "progressive delivery",
]

# Signals that the role is primarily SOFTWARE/PRODUCT engineering (where
# coding is the job) rather than operational infrastructure. NOT a hard
# block — penalty is moderate so genuinely SRE-titled roles still pass.
CODING_HEAVY_TOKENS = [
    # ML / data science / research
    "training pipeline", "model training", "fine-tun",
    "transformer architecture", "neural network",
    "deep learning research", "ml research", "ai research",
    "research engineer", "research scientist",
    "computer vision model", "nlp model",
    # frontend / mobile
    "react.js", "vue.js", "angular", "next.js",
    "ios app", "android app", "react native", "swift ", "kotlin ",
    "frontend framework",
    # algorithm-heavy SWE
    "competitive programming", "leetcode", "algorithmic problem",
    "data structures and algorithms",
    # generic full-stack with no infra angle
    "full-stack developer", "full stack developer",
]

# Title patterns that almost certainly mean "operational SRE/DevOps/Cloud
# role" — these always get a small bonus to push them to the top.
NICHE_TITLE_TOKENS = [
    "site reliability", "sre", "devops", "devsecops",
    "platform engineer", "platform eng",
    "infrastructure engineer", "cloud engineer", "cloud architect",
    "production engineer", "systems engineer", "reliability engineer",
    "developer platform", "developer experience",
    "kubernetes engineer", "k8s engineer",
]


# ── Block G — Posting legitimacy / ghost-job detection ──────────────────
# We assess whether a posting is likely a real, active opening. The output
# is a tier (HIGH_CONFIDENCE / PROCEED_WITH_CAUTION / SUSPICIOUS) and a list
# of signals — NOT a score adjustment. We don't want to silently downrank
# a legitimate posting that happens to look stale; the user makes the call.
GHOST_HIGH = "HIGH_CONFIDENCE"
GHOST_CAUTION = "PROCEED_WITH_CAUTION"
GHOST_SUSPICIOUS = "SUSPICIOUS"


def _parse_iso_date(s: str) -> Optional[float]:
    """Parse an ISO-like timestamp into days-ago. Returns None if unparseable."""
    if not s:
        return None
    from datetime import datetime, timezone

    s = s.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S.%f%z"):
        try:
            if fmt.endswith("Z"):
                dt = datetime.strptime(s.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S%z")
            else:
                dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0
        except ValueError:
            continue
    return None


def assess_legitimacy(job: dict, repost_count: int = 0) -> tuple[str, list[str]]:
    """Return (tier, list_of_signals). Pure function — easy to test.

    `repost_count` is how many times this same (company,title) appeared in
    seen.db in the past 90 days. Caller (notifier) supplies it; the scorer
    can pass 0 if it doesn't have access to that history (we still get the
    age + JD-specificity + salary signals).
    """
    signals: list[str] = []
    score = 0  # negative -> sus, positive -> healthy

    # Posting age
    posted_at = job.get("posted_at") or job.get("scraped_at") or ""
    age_days = _parse_iso_date(posted_at) if posted_at else None
    if age_days is not None:
        if age_days <= 30:
            score += 2
            signals.append(f"posted {age_days:.0f} days ago (fresh)")
        elif age_days <= 60:
            signals.append(f"posted {age_days:.0f} days ago (mixed)")
        elif age_days <= 120:
            score -= 1
            signals.append(f"posted {age_days:.0f} days ago (concerning)")
        else:
            score -= 2
            signals.append(f"posted {age_days:.0f} days ago (likely stale)")

    # Reposting pattern (seen.db tells us)
    if repost_count >= 4:
        score -= 2
        signals.append(f"reposted {repost_count}x in 90d (ghost pattern)")
    elif repost_count >= 2:
        score -= 1
        signals.append(f"reposted {repost_count}x in 90d")

    # Salary transparency — look for $ amounts or k/yr patterns
    desc = (job.get("description_text") or "").lower()
    has_salary = bool(re.search(r"\$\s*\d{2,3}[,kK]\s*-\s*\$?\s*\d{2,3}[,kK]", desc)) or \
                 bool(re.search(r"\$\s*\d{2,3},?\d{0,3}\s*(?:to|-|–)\s*\$\s*\d{2,3}", desc))
    if has_salary:
        score += 1
        signals.append("salary range disclosed")

    # JD specificity — the more concrete tech terms, the less generic
    tech_signals = sum(1 for tok in SRE_NICHE_TOKENS if tok in desc)
    if len(desc) > 200:
        if tech_signals >= 5:
            score += 1
            signals.append(f"specific JD ({tech_signals} concrete tech terms)")
        elif tech_signals == 0 and len(desc) > 800:
            score -= 1
            signals.append("vague JD (no specific tech mentioned)")

    # Verdict
    if score >= 2:
        tier = GHOST_HIGH
    elif score <= -2:
        tier = GHOST_SUSPICIOUS
    else:
        tier = GHOST_CAUTION
    return tier, signals


# ── Location filter ───────────────────────────────────────────────────────
# Substring tokens (lowercased) used to classify job postings by location.
# A job's `location` field is checked against these in order: US first,
# then India, then explicit "other" markers. Anything unknown defaults to US
# treatment (most jobs from US-headquartered Greenhouse/Lever/Ashby boards
# are US-based even when the location field is just "Remote").
US_LOCATION_TOKENS = [
    "united states", "usa", "u.s.a", " us ", "(us)", "(u.s.)",
    # major US cities
    "san francisco", "new york", "nyc", "los angeles", "seattle",
    "boston", "austin", "chicago", "atlanta", "denver", "portland",
    "miami", "dallas", "houston", "san jose", "san diego", "philadelphia",
    "washington dc", "washington, dc", "raleigh", "durham", "minneapolis",
    "phoenix", "salt lake city", "nashville", "charlotte", "pittsburgh",
    "detroit", "indianapolis", "columbus", "kansas city", "st. louis",
    # US states (full + abbrev)
    "california", "texas", "florida", "new york state", "pennsylvania",
    "illinois", "ohio", "georgia", "north carolina", "michigan",
    "virginia", "washington", "arizona", "massachusetts", "tennessee",
    "indiana", "missouri", "maryland", "wisconsin", "colorado", "minnesota",
    "south carolina", "alabama", "louisiana", "kentucky", "oregon", "oklahoma",
    "connecticut", "utah", "iowa", "nevada", "arkansas", "mississippi",
    "kansas", "new mexico", "nebraska", "idaho", "west virginia", "hawaii",
    "new hampshire", "maine", "montana", "rhode island", "delaware",
    "south dakota", "north dakota", "alaska", "vermont", "wyoming",
    # US time zones often appear in remote postings
    " est", " edt", " pst", " pdt", " cst", " cdt", " mst", " mdt",
    "eastern time", "pacific time", "central time", "mountain time",
]
INDIA_LOCATION_TOKENS = [
    "india", "bangalore", "bengaluru", "hyderabad", "mumbai", "delhi",
    "new delhi", "pune", "chennai", "kolkata", "gurgaon", "gurugram",
    "noida", "ahmedabad", "jaipur", "kochi", "thiruvananthapuram",
]
OTHER_NON_US_TOKENS = [
    # Use only HIGH-CONFIDENCE non-US markers. We accept some false negatives
    # (treat-as-US) over false positives (rejecting US jobs).
    "united kingdom", "london, uk", "london, england", "manchester, uk",
    "germany", "berlin", "munich", "frankfurt", "hamburg",
    "france", "paris,",
    "netherlands", "amsterdam",
    "ireland", "dublin",
    "spain", "madrid", "barcelona",
    "italy", "rome, italy", "milan",
    "sweden", "stockholm",
    "switzerland", "zurich, sw", "geneva,",
    "australia", "sydney", "melbourne",
    "canada", "toronto", "vancouver", "montreal",
    "brazil", "são paulo", "sao paulo",
    "mexico, mx", "mexico city",
    "japan", "tokyo,",
    "singapore",
    "south korea", "seoul",
    "hong kong",
    "philippines", "manila",
    "vietnam", "ho chi minh", "hanoi",
    "indonesia", "jakarta",
    "thailand", "bangkok",
    "uae", "dubai", "abu dhabi",
    "israel", "tel aviv",
    "south africa", "cape town", "johannesburg",
    "argentina", "buenos aires",
    "chile", "santiago, cl",
    "poland", "warsaw", "krakow",
    "ukraine", "kyiv",
    "romania", "bucharest",
    "czech", "prague",
]


def _classify_location(location: str) -> str:
    """Return 'us' | 'india' | 'other' | 'unknown'.

    Order matters: US tokens are checked first because some US locations
    contain country names (e.g. "Mexico, NY" should be US, not Mexico).
    """
    if not location:
        return "unknown"
    text = " " + location.lower() + " "
    for tok in US_LOCATION_TOKENS:
        if tok in text:
            return "us"
    for tok in INDIA_LOCATION_TOKENS:
        if tok in text:
            return "india"
    for tok in OTHER_NON_US_TOKENS:
        if tok in text:
            return "other"
    return "unknown"


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom == 0.0:
        return 0.0
    return float(np.dot(a, b) / denom)


def _word_token_set(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9+#.\-]+", text.lower()))


def _phrase_present(haystack_lower: str, needle: str) -> bool:
    needle = needle.strip().lower()
    if not needle:
        return False
    if " " in needle or any(c in needle for c in "+#.-"):
        return needle in haystack_lower
    return bool(re.search(rf"\b{re.escape(needle)}\b", haystack_lower))


class ProfileScorer:
    def __init__(
        self,
        profile_yaml_path: str | Path,
        model_name: Optional[str] = None,
        cache_dir: str | Path = ".cache",
        embedding_batch_size: int = 32,
    ) -> None:
        self.profile_path = Path(profile_yaml_path)
        self.model_name = model_name or os.getenv("EMBEDDING_MODEL", DEFAULT_MODEL)
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.embedding_batch_size = embedding_batch_size

        if not self.profile_path.exists():
            raise FileNotFoundError(f"profile yaml not found: {self.profile_path}")

        with self.profile_path.open() as f:
            self.profile: dict = yaml.safe_load(f) or {}

        self.core_skills: list[str] = [s.lower() for s in self.profile.get("core_skills", [])]
        self.adjacent_skills: list[str] = [s.lower() for s in self.profile.get("adjacent_skills", [])]
        self.target_titles: list[str] = [t.lower() for t in self.profile.get("target_titles", [])]
        self.red_flags: list[str] = [r.lower() for r in self.profile.get("red_flags", [])]
        self.preferred_signals: list[str] = [
            p.lower() for p in self.profile.get("preferred_signals", [])
        ]
        self.profile_summary: str = self.profile.get("profile_summary", "") or ""
        self.seniority_level: str = (
            (self.profile.get("seniority") or {}).get("level", "") or ""
        ).lower()
        self.skip_sre_niche_scoring: bool = bool(self.profile.get("skip_sre_niche_scoring", False))

        self._model = None  # lazy-loaded sentence-transformers model
        self.profile_embedding: np.ndarray = self._load_or_build_profile_embedding()

    def _load_model(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            log.info("loading embedding model %s", self.model_name)
            self._model = SentenceTransformer(self.model_name)
        return self._model

    def _profile_embed_text(self) -> str:
        skills_blob = ", ".join(self.core_skills)
        return f"{self.profile_summary}\n\nCore skills: {skills_blob}".strip()

    def _profile_cache_path(self) -> Path:
        # Cache key includes model name so swapping models invalidates the cache.
        safe = self.model_name.replace("/", "_")
        return self.cache_dir / f"profile_embedding_{safe}.npy"

    def _load_or_build_profile_embedding(self) -> np.ndarray:
        cache = self._profile_cache_path()
        profile_mtime = self.profile_path.stat().st_mtime
        if cache.exists() and cache.stat().st_mtime >= profile_mtime:
            try:
                return np.load(cache)
            except Exception as e:
                log.warning("failed to load cached profile embedding (%s); rebuilding", e)

        model = self._load_model()
        emb = model.encode([self._profile_embed_text()], normalize_embeddings=False)[0]
        emb = np.asarray(emb, dtype=np.float32)
        np.save(cache, emb)
        return emb

    def embed_jobs(self, texts: list[str]) -> np.ndarray:
        model = self._load_model()
        return np.asarray(
            model.encode(
                texts,
                batch_size=self.embedding_batch_size,
                normalize_embeddings=False,
                show_progress_bar=False,
            ),
            dtype=np.float32,
        )

    def _job_embed_text(self, job: dict) -> str:
        title = job.get("title", "") or ""
        description = (job.get("description_text", "") or "")[:DESCRIPTION_TRUNCATE]
        company = job.get("company", "") or ""
        return f"{title} at {company}\n\n{description}".strip()

    def _embedding_to_unit_score(self, cos: float) -> float:
        # MiniLM cosine for unrelated text sits ~0.0-0.2 and for strong matches ~0.5-0.8.
        # Map cosine in [0, 0.85] to [0, 1] linearly; clamp.
        return max(0.0, min(1.0, cos / 0.85))

    def _apply_rules(
        self, job: dict, embedding_score: float
    ) -> tuple[float, dict[str, float], list[str], list[str], list[str]]:
        title_lower = (job.get("title", "") or "").lower()
        desc_lower = (job.get("description_text", "") or "").lower()
        location = job.get("location", "") or ""

        adjustments: dict[str, float] = {}
        notes: list[str] = []

        for target in self.target_titles:
            if target and target in title_lower:
                adjustments["target_title_match"] = 0.10
                notes.append(f"title matches target '{target}'")
                break

        if self.seniority_level and self.seniority_level in title_lower:
            adjustments["seniority_match"] = 0.05
            notes.append(f"seniority '{self.seniority_level}' present in title")

        for flag in self.red_flags:
            if flag and flag in title_lower:
                adjustments["red_flag_penalty"] = -0.25
                notes.append(f"red flag '{flag}' in title")
                break

        # Also check description for experience-year red flags — "5+ years required"
        # in the JD body should penalise even if the title looks entry-level.
        if "red_flag_penalty" not in adjustments:
            for flag in self.red_flags:
                if flag and flag in desc_lower:
                    adjustments["red_flag_desc_penalty"] = -0.20
                    notes.append(f"red flag '{flag}' in description")
                    break

        # Check description-level red flags (separate from title flags so both can fire).
        # Used for entry-level profiles to catch "5+ years required" buried in JD body.
        desc_red_flags: list[str] = [f.lower() for f in self.profile.get("desc_red_flags", [])]
        for flag in desc_red_flags:
            if flag and flag in desc_lower:
                adjustments["desc_red_flag_penalty"] = -0.25
                notes.append(f"desc red flag '{flag}' in description")
                break

        no_sponsorship = False
        for phrase in NO_SPONSORSHIP_PHRASES:
            if phrase in desc_lower:
                adjustments["no_sponsorship_penalty"] = -0.30
                notes.append(f"description signals no sponsorship: '{phrase}'")
                no_sponsorship = True
                break

        # ── Non-tech / wrong-domain rejection ──────────────────────────────
        # "Civil Infrastructure Engineer" must NEVER match a software SRE
        # profile. Hard-cap the score for clearly non-software titles.
        non_tech_veto = False
        non_tech_hit = _has_token(title_lower, NON_TECH_TITLE_TOKENS)
        if non_tech_hit:
            adjustments["non_tech_penalty"] = -0.50
            notes.append(f"non-tech title token '{non_tech_hit}'")
            non_tech_veto = True

        # Ambiguous words ("architect", "infrastructure") need a tech
        # context token in the title OR a tech-flavored description, else
        # they're likely civil/building roles.
        ambig_hit = _has_token(title_lower, AMBIGUOUS_TITLE_TOKENS)
        if ambig_hit and not non_tech_veto:
            tech_in_title = _has_token(title_lower, TECH_CONTEXT_TOKENS)
            tech_in_desc = _has_token(desc_lower, TECH_CONTEXT_TOKENS)
            if not tech_in_title and not tech_in_desc:
                adjustments["ambiguous_no_tech_context"] = -0.25
                notes.append(f"ambiguous '{ambig_hit}' with no tech context")

        # ── SRE/DevOps/Cloud niche density ─────────────────────────────────
        # Skipped for profiles that set skip_sre_niche_scoring: true (e.g. ML/AI profiles)
        # because the coding-heavy and generic-SWE penalties would hurt exactly
        # the roles those profiles are targeting.
        if not self.skip_sre_niche_scoring:
            sre_in_title = _has_token(title_lower, NICHE_TITLE_TOKENS)
            sre_count = _count_tokens(desc_lower, SRE_NICHE_TOKENS)
            coding_count = (
                _count_tokens(title_lower, CODING_HEAVY_TOKENS)
                + _count_tokens(desc_lower, CODING_HEAVY_TOKENS)
            )

            if sre_in_title:
                adjustments["niche_title_match"] = 0.05
                notes.append(f"niche title token '{sre_in_title}'")

            if sre_count >= 2:
                density_bonus = round(min(0.10, 0.02 * (sre_count // 2)), 4)
                adjustments["sre_density_bonus"] = density_bonus
                notes.append(f"{sre_count} SRE-niche signals in JD")

            if coding_count >= 2 and coding_count > sre_count:
                penalty = round(min(0.25, 0.05 * coding_count), 4)
                adjustments["coding_heavy_penalty"] = -penalty
                notes.append(
                    f"coding-heavy role: {coding_count} coding vs {sre_count} SRE signals"
                )

            if (
                "software engineer" in title_lower
                and not sre_in_title
                and sre_count == 0
            ):
                adjustments["generic_swe_no_niche"] = -0.15
                notes.append("generic SWE title, no SRE-niche evidence")

        # ── Location preference ────────────────────────────────────────────
        # Goal (per profile.locations): US is target, India is acceptable but
        # de-prioritized, anywhere else is effectively a hard veto.
        location_class = _classify_location(location)
        location_veto = False
        if location_class == "india":
            adjustments["location_india"] = -0.20
            notes.append(f"location is India ({location})")
        elif location_class == "other":
            adjustments["location_non_us"] = -0.40
            notes.append(f"location is non-US/non-India ({location})")
            location_veto = True
        # 'us' and 'unknown' get no adjustment.

        matched_skills: list[str] = []
        for skill in self.core_skills:
            if _phrase_present(desc_lower, skill):
                matched_skills.append(skill)
        if matched_skills:
            bonus = min(SKILL_OVERLAP_BONUS_CAP, SKILL_OVERLAP_BONUS_PER * len(matched_skills))
            adjustments["skill_overlap_bonus"] = round(bonus, 4)

        missing_skills = [s for s in self.core_skills if s not in matched_skills][:5]

        final = embedding_score + sum(adjustments.values())
        # No-sponsorship is a hard veto for visa-dependent users: even a perfect
        # technical match is unactionable, so cap the score below the typical
        # match threshold regardless of bonuses applied.
        if no_sponsorship:
            final = min(final, 0.20)
        # Non-US/non-India is a hard veto for "US-focused" mode.
        if location_veto:
            final = min(final, 0.20)
        # Civil / non-software roles MUST NOT pass through, even if some
        # other rule (e.g. "engineer" in title) earned a partial match.
        if non_tech_veto:
            final = min(final, 0.15)
        final = max(0.0, min(1.0, final))
        return final, adjustments, matched_skills, missing_skills, notes

    def _build_reason(
        self,
        embedding_score: float,
        adjustments: dict[str, float],
        notes: list[str],
        matched_skills: list[str],
    ) -> str:
        parts = [f"semantic match {embedding_score:.2f}"]
        if matched_skills:
            parts.append(f"matched {len(matched_skills)} core skills ({', '.join(matched_skills[:4])})")
        for note in notes:
            parts.append(note)
        if not adjustments:
            parts.append("no rule adjustments applied")
        return "; ".join(parts)

    def score_job(self, job: dict) -> ScoredJob:
        embedding = self.embed_jobs([self._job_embed_text(job)])[0]
        cos = _cosine(self.profile_embedding, embedding)
        embedding_score = self._embedding_to_unit_score(cos)

        final, adjustments, matched, missing, notes = self._apply_rules(job, embedding_score)
        reason = self._build_reason(embedding_score, adjustments, notes, matched)
        tier, sigs = assess_legitimacy(job, repost_count=0)

        return ScoredJob(
            job=job,
            score=round(final, 4),
            embedding_score=round(embedding_score, 4),
            rule_adjustments={k: round(v, 4) for k, v in adjustments.items()},
            matched_skills=matched,
            missing_skills=missing,
            reason=reason,
            legitimacy_tier=tier,
            legitimacy_signals=sigs,
        )

    def score_batch(self, jobs: list[dict]) -> list[ScoredJob]:
        if not jobs:
            return []
        texts = [self._job_embed_text(j) for j in jobs]
        embeddings = self.embed_jobs(texts)
        results: list[ScoredJob] = []
        for job, embedding in zip(jobs, embeddings):
            cos = _cosine(self.profile_embedding, embedding)
            embedding_score = self._embedding_to_unit_score(cos)
            final, adjustments, matched, missing, notes = self._apply_rules(job, embedding_score)
            reason = self._build_reason(embedding_score, adjustments, notes, matched)
            tier, sigs = assess_legitimacy(job, repost_count=0)
            results.append(
                ScoredJob(
                    job=job,
                    score=round(final, 4),
                    embedding_score=round(embedding_score, 4),
                    rule_adjustments={k: round(v, 4) for k, v in adjustments.items()},
                    matched_skills=matched,
                    missing_skills=missing,
                    reason=reason,
                    legitimacy_tier=tier,
                    legitimacy_signals=sigs,
                )
            )
        return results
