from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.scorer.scorer import ProfileScorer  # noqa: E402


STUB_PROFILE = {
    "person": {"name": "Test User", "email": "test@example.com"},
    "seniority": {"level": "staff", "min_years": 7},
    "core_skills": [
        "kubernetes",
        "terraform",
        "aws",
        "python",
        "linux",
        "ci/cd",
        "prometheus",
        "go",
    ],
    "adjacent_skills": ["snowflake", "kafka", "spark"],
    "target_titles": [
        "site reliability engineer",
        "platform engineer",
        "devops engineer",
        "infrastructure engineer",
        "software engineer",
    ],
    "red_flags": ["manager", "director", "sales", "account executive"],
    "preferred_signals": ["h1b transfer", "visa sponsorship"],
    "profile_summary": (
        "Senior platform/SRE engineer with 8 years building Kubernetes platforms, "
        "Terraform infrastructure, observability stacks (Prometheus, Grafana), and "
        "CI/CD pipelines on AWS. Strong Python and Go. Looking for senior or staff "
        "individual-contributor roles in platform, SRE, or infrastructure engineering."
    ),
}


def _ensure_model_available() -> bool:
    try:
        from sentence_transformers import SentenceTransformer  # noqa: F401
    except Exception as e:
        pytest.skip(f"sentence-transformers not installed: {e}")
        return False
    # The downloader hangs forever on a blocked network rather than raising.
    # Probe HF directly with a 3-second timeout BEFORE letting the SDK try.
    try:
        import socket

        socket.setdefaulttimeout(3.0)
        socket.create_connection(("huggingface.co", 443), timeout=3.0).close()
    except Exception as e:
        pytest.skip(f"HuggingFace unreachable (no model download possible): {e}")
        return False
    finally:
        socket.setdefaulttimeout(None)
    try:
        from sentence_transformers import SentenceTransformer

        SentenceTransformer("all-MiniLM-L6-v2")
        return True
    except Exception as e:
        pytest.skip(f"embedding model unavailable: {e}")
        return False


@pytest.fixture(scope="session")
def scorer(tmp_path_factory) -> ProfileScorer:
    _ensure_model_available()
    workdir = tmp_path_factory.mktemp("scorer")
    profile_path = workdir / "profile.parsed.yaml"
    profile_path.write_text(yaml.safe_dump(STUB_PROFILE))
    cache = workdir / ".cache"
    return ProfileScorer(profile_yaml_path=profile_path, cache_dir=cache)


def _job(title: str, description: str, company: str = "Acme") -> dict:
    return {
        "id": "test-id",
        "source_id": "src",
        "source": "greenhouse",
        "company": company,
        "title": title,
        "description_text": description,
        "url": "https://example.com/job",
        "location": "Remote",
        "remote": True,
    }


def test_strong_sre_match_scores_high(scorer: ProfileScorer) -> None:
    job = _job(
        title="Software Engineer III, Distributed Systems Infrastructure",
        company="Stripe",
        description=(
            "Stripe is hiring a Software Engineer to build distributed systems infrastructure. "
            "You will design and operate Kubernetes-based platforms, write Terraform, run "
            "services on AWS, and improve observability with Prometheus. Strong Python and Go "
            "skills required. We sponsor H1B transfers."
        ),
    )
    result = scorer.score_job(job)
    assert result.score > 0.7, f"expected >0.7, got {result.score} (reason={result.reason})"


def test_sales_role_scores_low(scorer: ProfileScorer) -> None:
    job = _job(
        title="Account Executive, Enterprise",
        description=(
            "We are hiring an Account Executive for our enterprise sales team. Quota carrying "
            "role; you will own the full sales cycle, build pipeline, close deals, and manage "
            "customer relationships. SaaS sales experience required."
        ),
    )
    result = scorer.score_job(job)
    assert result.score < 0.3, f"expected <0.3, got {result.score} (reason={result.reason})"


def test_engineering_manager_penalised(scorer: ProfileScorer) -> None:
    job = _job(
        title="Senior Manager, Engineering",
        description=(
            "Lead a team of 8 engineers building infrastructure platforms. People management, "
            "performance reviews, hiring, and roadmap ownership. Kubernetes and AWS background "
            "preferred but you will not be writing production code day to day."
        ),
    )
    result = scorer.score_job(job)
    assert result.score < 0.4, f"expected <0.4, got {result.score} (reason={result.reason})"
    assert "red_flag_penalty" in result.rule_adjustments


def test_no_sponsorship_zeroes_otherwise_strong_match(scorer: ProfileScorer) -> None:
    job = _job(
        title="Staff Site Reliability Engineer",
        description=(
            "Senior SRE role on our infrastructure team. Kubernetes, Terraform, AWS, "
            "Prometheus, Python. Note: no visa sponsorship available; you must be authorized "
            "to work in the US without sponsorship now or in the future."
        ),
    )
    result = scorer.score_job(job)
    assert result.score < 0.3, f"expected <0.3, got {result.score} (reason={result.reason})"
    assert "no_sponsorship_penalty" in result.rule_adjustments


def test_batch_scoring_matches_single(scorer: ProfileScorer) -> None:
    jobs = [
        _job("Site Reliability Engineer", "Kubernetes, Terraform, AWS, Python."),
        _job("Account Executive", "Sales role, quota carrying."),
    ]
    batch = scorer.score_batch(jobs)
    singles = [scorer.score_job(j) for j in jobs]
    for b, s in zip(batch, singles):
        assert abs(b.score - s.score) < 1e-6
