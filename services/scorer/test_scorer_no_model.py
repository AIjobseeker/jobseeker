"""Scorer tests that run WITHOUT downloading a model.

Mocks SentenceTransformer with a deterministic fake so we can validate the
rule engine and score arithmetic in any environment (sandbox, CI, offline).
The model-dependent tests live in test_scorer.py and skip in sandboxed envs.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


STUB_PROFILE = {
    "person": {"name": "Test User", "email": "test@example.com"},
    "seniority": {"level": "staff", "min_years": 7},
    "core_skills": [
        "kubernetes", "terraform", "aws", "python",
        "linux", "ci/cd", "prometheus", "go",
    ],
    "adjacent_skills": ["snowflake", "kafka", "spark"],
    "target_titles": [
        "site reliability engineer", "platform engineer",
        "devops engineer", "infrastructure engineer", "software engineer",
    ],
    "red_flags": ["manager", "director", "sales", "account executive"],
    "preferred_signals": ["h1b transfer", "visa sponsorship"],
    "profile_summary": (
        "Senior platform/SRE engineer with 8 years building Kubernetes platforms, "
        "Terraform infrastructure, and observability stacks on AWS."
    ),
}


def _job(title: str, description: str, company: str = "Acme") -> dict:
    return {
        "id": "test-id",
        "source_id": "src",
        "source": "greenhouse",
        "company": company,
        "title": title,
        "description_text": description,
        "url": "https://example.com/jobs/1",
        "location": "Remote",
    }


@pytest.fixture
def fake_scorer(tmp_path, monkeypatch):
    """A ProfileScorer whose embedding always returns a fixed cosine of 0.5.

    This isolates the rule engine — every job gets the same embedding score
    (~0.59 after the linear map) so deltas reflect ONLY rule adjustments.
    """
    profile_path = tmp_path / "profile.parsed.yaml"
    profile_path.write_text(yaml.safe_dump(STUB_PROFILE))

    fake_model = MagicMock()
    rng = np.random.default_rng(42)
    fixed_vec = rng.standard_normal(384).astype(np.float32)
    fake_model.encode.return_value = np.array([fixed_vec])

    from services.scorer import scorer as scorer_mod

    monkeypatch.setattr(
        scorer_mod.ProfileScorer, "_load_model", lambda self: fake_model
    )
    return scorer_mod.ProfileScorer(
        profile_yaml_path=profile_path,
        cache_dir=tmp_path / ".cache",
    )


def test_target_title_match_adds_bonus(fake_scorer):
    """An exact target title hit should add the +0.10 target bonus."""
    job = _job(
        title="Site Reliability Engineer",
        description="Looking for SRE with kubernetes and terraform experience.",
    )
    result = fake_scorer.score_job(job)
    assert "target_title_match" in result.rule_adjustments
    assert result.rule_adjustments["target_title_match"] == pytest.approx(0.10)


def test_red_flag_in_title_penalises(fake_scorer):
    job = _job(
        title="Engineering Manager, Platform",
        description="Lead a team of platform engineers building on kubernetes.",
    )
    result = fake_scorer.score_job(job)
    assert result.rule_adjustments.get("red_flag_penalty") == pytest.approx(-0.15)


def test_no_sponsorship_caps_score(fake_scorer):
    """No-sponsorship is a hard cap regardless of strong title match."""
    job = _job(
        title="Staff Site Reliability Engineer",
        description=(
            "Strong fit for kubernetes terraform aws platform engineer. "
            "Note: we cannot sponsor visas at this time."
        ),
    )
    result = fake_scorer.score_job(job)
    assert result.score <= 0.20, (
        f"expected score capped at 0.20, got {result.score}; "
        f"adjustments={result.rule_adjustments}"
    )
    assert "no_sponsorship_penalty" in result.rule_adjustments


def test_skill_overlap_bonus_caps_at_010(fake_scorer):
    job = _job(
        title="Backend Engineer",
        description=(
            "Stack: kubernetes terraform aws python linux ci/cd prometheus go. "
            "Build scalable systems."
        ),
    )
    result = fake_scorer.score_job(job)
    bonus = result.rule_adjustments.get("skill_overlap_bonus", 0)
    assert 0 < bonus <= 0.10
    assert len(result.matched_skills) >= 6


def test_seniority_match_when_title_contains_level(fake_scorer):
    job = _job(
        title="Staff Software Engineer, Infrastructure",
        description="Build platforms with kubernetes and terraform.",
    )
    result = fake_scorer.score_job(job)
    assert "seniority_match" in result.rule_adjustments
    assert result.rule_adjustments["seniority_match"] == pytest.approx(0.05)


def test_score_is_clamped_to_unit_interval(fake_scorer):
    job = _job(
        title="Site Reliability Engineer Staff Platform Engineer DevOps Engineer",
        description="kubernetes terraform aws python linux ci/cd prometheus go ".__mul__(20),
    )
    result = fake_scorer.score_job(job)
    assert 0.0 <= result.score <= 1.0


def test_missing_skills_excludes_matched(fake_scorer):
    job = _job(
        title="Backend Engineer",
        description="kubernetes terraform aws.",
    )
    result = fake_scorer.score_job(job)
    for s in result.matched_skills:
        assert s not in result.missing_skills


def test_score_batch_matches_score_job(fake_scorer):
    jobs = [
        _job("SRE Engineer", "kubernetes terraform aws."),
        _job("Account Executive", "sales role."),
    ]
    batch = fake_scorer.score_batch(jobs)
    individual = [fake_scorer.score_job(j) for j in jobs]
    for b, i in zip(batch, individual):
        assert b.score == i.score, (
            f"batch and single score diverge: {b.score} vs {i.score}"
        )
        assert b.matched_skills == i.matched_skills


def test_empty_description_does_not_crash(fake_scorer):
    job = _job(title="Staff SRE", description="")
    result = fake_scorer.score_job(job)
    assert result.score is not None
    assert result.matched_skills == []


def test_missing_optional_fields(fake_scorer):
    job = {
        "id": "x", "source_id": "y", "source": "greenhouse",
        "company": "Acme", "title": "SRE", "url": "u",
        # description_text deliberately omitted
    }
    result = fake_scorer.score_job(job)
    assert result.score is not None


# ── Location filter tests ────────────────────────────────────────────────

def _job_at(title, desc, location, **kw):
    return {
        "id": "x", "source_id": "y", "source": "greenhouse",
        "company": "Acme", "title": title, "description_text": desc,
        "url": "u", "location": location, **kw,
    }


def test_us_location_no_penalty(fake_scorer):
    job = _job_at(
        "Site Reliability Engineer",
        "kubernetes terraform",
        "San Francisco, CA",
    )
    r = fake_scorer.score_job(job)
    assert "location_india" not in r.rule_adjustments
    assert "location_non_us" not in r.rule_adjustments


def test_us_remote_unspecified_no_penalty(fake_scorer):
    """'Remote' alone defaults to US treatment (most US companies do this)."""
    job = _job_at("SRE", "kubernetes", "Remote")
    r = fake_scorer.score_job(job)
    assert "location_india" not in r.rule_adjustments
    assert "location_non_us" not in r.rule_adjustments


def test_india_location_penalises(fake_scorer):
    job = _job_at("SRE", "kubernetes", "Bangalore, India")
    r = fake_scorer.score_job(job)
    assert r.rule_adjustments.get("location_india") == pytest.approx(-0.20)


def test_india_city_penalises(fake_scorer):
    job = _job_at("SRE", "kubernetes", "Hyderabad")
    r = fake_scorer.score_job(job)
    assert r.rule_adjustments.get("location_india") == pytest.approx(-0.20)


def test_uk_location_caps_at_020(fake_scorer):
    job = _job_at(
        "Staff Site Reliability Engineer",
        "kubernetes terraform aws python linux observability prometheus",
        "London, UK",
    )
    r = fake_scorer.score_job(job)
    assert r.score <= 0.20, f"non-US should cap at 0.20, got {r.score}"
    assert "location_non_us" in r.rule_adjustments


def test_eu_location_caps_at_020(fake_scorer):
    job = _job_at("SRE", "kubernetes terraform", "Berlin, Germany")
    r = fake_scorer.score_job(job)
    assert r.score <= 0.20


def test_canada_location_caps_at_020(fake_scorer):
    """Canada is non-US for our visa situation — hard cap."""
    job = _job_at("SRE", "kubernetes", "Toronto, Canada")
    r = fake_scorer.score_job(job)
    assert r.score <= 0.20


def test_us_state_abbreviation_recognized(fake_scorer):
    """A common pattern: 'Austin, TX' — must be recognized as US."""
    job = _job_at("Staff SRE", "kubernetes terraform", "Austin, TX")
    r = fake_scorer.score_job(job)
    assert "location_india" not in r.rule_adjustments
    assert "location_non_us" not in r.rule_adjustments


def test_us_remote_explicit_recognized(fake_scorer):
    job = _job_at("SRE", "kubernetes", "Remote (US)")
    r = fake_scorer.score_job(job)
    assert "location_non_us" not in r.rule_adjustments


def test_us_timezone_in_remote_recognized(fake_scorer):
    """'Remote, EST' or 'Remote — Pacific Time' should count as US."""
    for loc in ["Remote, EST", "Remote — Pacific Time", "Remote (PST)"]:
        job = _job_at("SRE", "k8s", loc)
        r = fake_scorer.score_job(job)
        assert "location_non_us" not in r.rule_adjustments, (
            f"{loc!r} should be classified as US, but got "
            f"{r.rule_adjustments}"
        )


def test_india_overrides_no_sponsorship_doesnt_double_penalise(fake_scorer):
    """A job that is BOTH India AND no-sponsorship caps at 0.20 — once."""
    job = _job_at(
        "SRE",
        "kubernetes. We cannot sponsor visas.",
        "Bangalore, India",
    )
    r = fake_scorer.score_job(job)
    assert r.score <= 0.20


def test_us_strong_match_passes_threshold(fake_scorer):
    """A strong US-based match should NOT trigger any location penalty."""
    job = _job_at(
        "Staff Site Reliability Engineer",
        "kubernetes terraform aws python linux ci/cd prometheus observability",
        "San Francisco, CA",
    )
    r = fake_scorer.score_job(job)
    assert r.score >= 0.65, (
        f"strong US match should clear threshold, got {r.score} "
        f"with adjustments {r.rule_adjustments}"
    )


# ── Non-tech disambiguation tests (civil/architecture must NOT match) ────

def test_civil_infrastructure_engineer_capped(fake_scorer):
    """The original false-positive: 'Civil Infrastructure Engineer' is NOT
    a software role and must hard-cap below the match threshold."""
    job = _job_at(
        "Civil Infrastructure Engineer",
        "Bridges, highways, structural design. Civil PE license required.",
        "San Francisco, CA",
    )
    r = fake_scorer.score_job(job)
    assert r.score <= 0.15, f"expected hard-cap, got {r.score}"
    assert "non_tech_penalty" in r.rule_adjustments


def test_construction_manager_blocked(fake_scorer):
    job = _job_at("Construction Manager, Datacenter",
                  "kubernetes mentioned but role is on-site building work.",
                  "Austin, TX")
    r = fake_scorer.score_job(job)
    assert r.score <= 0.15


def test_landscape_architect_blocked(fake_scorer):
    job = _job_at("Landscape Architect",
                  "Outdoor design.", "Seattle, WA")
    r = fake_scorer.score_job(job)
    assert r.score <= 0.15


def test_software_architect_passes(fake_scorer):
    """'Software Architect' is the genuine tech meaning of architect — must pass."""
    job = _job_at(
        "Senior Software Architect",
        "Design distributed systems in kubernetes, terraform, aws.",
        "Remote (US)",
    )
    r = fake_scorer.score_job(job)
    assert "non_tech_penalty" not in r.rule_adjustments
    assert "ambiguous_no_tech_context" not in r.rule_adjustments


def test_cloud_architect_passes(fake_scorer):
    job = _job_at(
        "Cloud Architect, Platform",
        "Lead the AWS-based platform. terraform, kubernetes.",
        "New York, NY",
    )
    r = fake_scorer.score_job(job)
    assert "non_tech_penalty" not in r.rule_adjustments


def test_bare_architect_with_no_tech_context_penalised(fake_scorer):
    """'Architect' alone is ambiguous — without tech context in title or
    description, we apply a moderate penalty (won't clear threshold)."""
    job = _job_at("Architect",
                  "Lead design projects across our portfolio.",
                  "San Francisco, CA")
    r = fake_scorer.score_job(job)
    assert "ambiguous_no_tech_context" in r.rule_adjustments


def test_software_infrastructure_engineer_passes(fake_scorer):
    job = _job_at(
        "Software Infrastructure Engineer",
        "Build the platform. kubernetes terraform aws.",
        "San Francisco, CA",
    )
    r = fake_scorer.score_job(job)
    assert "non_tech_penalty" not in r.rule_adjustments
    assert "ambiguous_no_tech_context" not in r.rule_adjustments


def test_infrastructure_with_tech_in_description_passes(fake_scorer):
    """'Infrastructure Engineer' alone in title, but description is clearly
    software (kubernetes, devops) — should NOT be penalised."""
    job = _job_at(
        "Infrastructure Engineer III",
        "Run our kubernetes platform across regions. terraform, observability.",
        "Remote (US)",
    )
    r = fake_scorer.score_job(job)
    # Tech context in description rescues the ambiguous title.
    assert "ambiguous_no_tech_context" not in r.rule_adjustments


# ── SRE niche density tests ─────────────────────────────────────────────

def test_genuine_sre_role_gets_density_bonus(fake_scorer):
    """A genuine SRE JD has many operational signals."""
    job = _job_at(
        "Staff Site Reliability Engineer",
        "Lead our kubernetes platform. Write runbooks, drive postmortems, "
        "set SLOs and error budgets. Heavy terraform, prometheus, grafana. "
        "On-call rotation across the team. observability is core to the role.",
        "San Francisco, CA",
    )
    r = fake_scorer.score_job(job)
    assert "niche_title_match" in r.rule_adjustments
    assert "sre_density_bonus" in r.rule_adjustments
    assert r.rule_adjustments["sre_density_bonus"] >= 0.04
    assert "coding_heavy_penalty" not in r.rule_adjustments


def test_nvidia_cloud_data_engineer_penalised(fake_scorer):
    """The exact case the user flagged: cloud + kubernetes mentioned but
    role is heavy data-pipeline coding. Should NOT score high."""
    job = _job_at(
        "Cloud Data Engineer",
        "Build large-scale data pipelines at NVIDIA. Heavy Python coding. "
        "Training pipeline for our ML models. Transformer architecture, "
        "neural network training. Some kubernetes used for orchestration.",
        "Santa Clara, CA",
    )
    r = fake_scorer.score_job(job)
    # We expect coding-heavy penalty because training pipeline + neural network
    # + transformer architecture + ml model heavily outweigh the single 'kubernetes'.
    assert "coding_heavy_penalty" in r.rule_adjustments
    # And no niche-title bonus because "Cloud Data Engineer" isn't in our niche list.
    assert "niche_title_match" not in r.rule_adjustments


def test_generic_software_engineer_with_k8s_mention_penalised(fake_scorer):
    """'Senior Software Engineer' with one 'kubernetes' mention should get
    the generic-SWE penalty, not pass through as SRE."""
    job = _job_at(
        "Senior Software Engineer",
        "Build our payments API in Python. Some kubernetes for deployment.",
        "New York, NY",
    )
    r = fake_scorer.score_job(job)
    assert "generic_swe_no_niche" in r.rule_adjustments


def test_software_engineer_with_strong_sre_jd_passes(fake_scorer):
    """'Software Engineer III, Infrastructure' with rich SRE JD — should
    NOT get generic-SWE penalty because SRE niche tokens are present."""
    job = _job_at(
        "Software Engineer III, Infrastructure",
        "Operate our kubernetes platform. terraform, on-call rotation, "
        "prometheus and grafana, observability, runbooks, postmortems.",
        "Seattle, WA",
    )
    r = fake_scorer.score_job(job)
    assert "generic_swe_no_niche" not in r.rule_adjustments
    assert "sre_density_bonus" in r.rule_adjustments


def test_ml_research_engineer_penalised(fake_scorer):
    """'ML Research Engineer' with kubernetes mention — coding-heavy, NOT our niche."""
    job = _job_at(
        "Machine Learning Research Engineer",
        "Train transformer models at scale. Deep learning research. "
        "Some kubernetes for distributed training jobs.",
        "Mountain View, CA",
    )
    r = fake_scorer.score_job(job)
    assert "coding_heavy_penalty" in r.rule_adjustments


def test_frontend_engineer_penalised(fake_scorer):
    job = _job_at(
        "Senior Frontend Engineer",
        "Build user-facing React.js features. Next.js framework. "
        "Some Node.js backend work.",
        "Remote (US)",
    )
    r = fake_scorer.score_job(job)
    assert "coding_heavy_penalty" in r.rule_adjustments


def test_devops_title_gets_niche_bonus(fake_scorer):
    """A clear DevOps title even with sparse description gets niche bonus."""
    job = _job_at(
        "Senior DevOps Engineer",
        "Manage infrastructure for our SaaS product.",
        "Austin, TX",
    )
    r = fake_scorer.score_job(job)
    assert "niche_title_match" in r.rule_adjustments


def test_high_sre_density_outweighs_some_coding(fake_scorer):
    """SRE role at an ML company: lots of SRE signals + a couple of ML
    references in description. SRE wins because count is higher."""
    job = _job_at(
        "Staff Site Reliability Engineer, ML Platform",
        "Run our kubernetes platform that hosts ml models. terraform, "
        "prometheus, grafana, on-call rotation, runbooks, postmortems, "
        "slo and sli definitions. observability is critical. Help train "
        "neural network models means making the cluster reliable.",
        "San Francisco, CA",
    )
    r = fake_scorer.score_job(job)
    # Should NOT get coding-heavy penalty — SRE signals outnumber coding ones
    assert (
        "coding_heavy_penalty" not in r.rule_adjustments
        or r.rule_adjustments.get("coding_heavy_penalty", 0) >= -0.05
    )
    assert "sre_density_bonus" in r.rule_adjustments


# ── Block G: ghost-job legitimacy tests ──────────────────────────────────

def _ghost_job(title="SRE", desc="kubernetes terraform aws.", posted_days_ago=10,
               loc="San Francisco, CA"):
    """Build a job with a posted_at relative to now. days_ago=10 => 10 days old."""
    from datetime import datetime, timedelta, timezone
    posted = (datetime.now(timezone.utc) - timedelta(days=posted_days_ago)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    return _job_at(title, desc, loc) | {"posted_at": posted}


def test_legitimacy_high_for_fresh_specific_with_salary(fake_scorer):
    job = _ghost_job(
        title="Staff Site Reliability Engineer",
        desc=(
            "kubernetes terraform aws prometheus grafana on-call rotation "
            "runbook observability slo error budget. Salary range $200,000 - $260,000."
        ),
        posted_days_ago=5,
    )
    r = fake_scorer.score_job(job)
    assert r.legitimacy_tier == "HIGH_CONFIDENCE", (
        f"expected HIGH_CONFIDENCE, got {r.legitimacy_tier} "
        f"(signals: {r.legitimacy_signals})"
    )


def test_legitimacy_suspicious_when_old_and_vague(fake_scorer):
    job = _ghost_job(
        title="Senior Engineer",
        desc=(
            "We are looking for a passionate engineer to join our team. "
            "We value innovation, collaboration, and excellence. "
            "You will work on exciting projects. " * 5
        ),
        posted_days_ago=180,
    )
    r = fake_scorer.score_job(job)
    assert r.legitimacy_tier == "SUSPICIOUS", (
        f"expected SUSPICIOUS, got {r.legitimacy_tier} "
        f"(signals: {r.legitimacy_signals})"
    )


def test_legitimacy_caution_for_mixed(fake_scorer):
    job = _ghost_job(
        title="SRE",
        desc="kubernetes mentioned briefly.",
        posted_days_ago=45,
    )
    r = fake_scorer.score_job(job)
    assert r.legitimacy_tier == "PROCEED_WITH_CAUTION", r.legitimacy_tier


def test_legitimacy_does_not_affect_score(fake_scorer):
    """Ghost-job assessment is a separate signal — it MUST NOT silently
    downrank a posting that happens to be old."""
    fresh = _ghost_job("Staff SRE", "kubernetes terraform aws.", posted_days_ago=2)
    stale = _ghost_job("Staff SRE", "kubernetes terraform aws.", posted_days_ago=200)
    r_fresh = fake_scorer.score_job(fresh)
    r_stale = fake_scorer.score_job(stale)
    assert r_fresh.score == r_stale.score, (
        f"score must not depend on age: fresh={r_fresh.score} stale={r_stale.score}"
    )
