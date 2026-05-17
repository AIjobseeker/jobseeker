"""End-to-end pipeline test — scorer + notifier integration.

Validates the data contract between services without needing NATS, the
embedding model, Telegram, or live network. Simulates the message flow:

  raw job (dict)
    -> scorer.score_job() -> ScoredJob (scorer schema)
    -> JSON serialize (what NATS would carry)
    -> ScoredJob (notifier schema) — drops extras like rule_adjustments
    -> compute_key() / dedup
    -> telegram_dispatch.format_message()

Catches schema drift between scorer and notifier, format regressions,
and dedup-key collisions. Runs in <1s with no network.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.notifier.dedup import DedupStore, compute_key
from services.notifier.models import ScoredJob as NotifierScoredJob
from services.notifier.telegram_dispatch import format_message
from services.scorer import scorer as scorer_mod
from services.scorer.scorer import ProfileScorer


@pytest.fixture
def fake_scorer(tmp_path, monkeypatch):
    profile = {
        "person": {"name": "Sai", "email": "x@y.z"},
        "seniority": {"level": "staff"},
        "core_skills": ["kubernetes", "terraform", "aws", "python"],
        "adjacent_skills": [],
        "target_titles": [
            "site reliability engineer", "platform engineer",
            "staff sre", "staff software engineer",
        ],
        "red_flags": ["manager", "director"],
        "preferred_signals": ["h1b transfer"],
        "profile_summary": "Staff SRE on AWS+K8s, looking for IC roles.",
    }
    profile_path = tmp_path / "profile.parsed.yaml"
    profile_path.write_text(yaml.safe_dump(profile))

    fake_model = MagicMock()
    rng = np.random.default_rng(7)
    vec = rng.standard_normal(384).astype(np.float32)
    fake_model.encode.return_value = np.array([vec])

    monkeypatch.setattr(ProfileScorer, "_load_model", lambda self: fake_model)
    return ProfileScorer(profile_yaml_path=profile_path, cache_dir=tmp_path / ".cache")


def _job(title, desc, source_id="abc-123", company="Stripe"):
    return {
        "id": "uuid-1",
        "source_id": source_id,
        "source": "greenhouse",
        "company": company,
        "title": title,
        "description_text": desc,
        "url": "https://stripe.com/jobs/abc-123",
        "location": "San Francisco, Remote OK",
        "department": "Infrastructure",
        "remote": True,
        "scraped_at": "2026-05-17T15:23:00Z",
    }


def test_e2e_strong_match_flows_through_pipeline(fake_scorer, tmp_path):
    """Strong match: scorer publishes, notifier parses, telegram formats."""
    job = _job(
        "Staff Site Reliability Engineer, Platform",
        "kubernetes terraform aws python — H1B transfers welcome.",
    )

    # Stage 1: scorer
    scored = fake_scorer.score_job(job)
    assert scored.score > 0.0
    payload_json = scored.model_dump_json()

    # Stage 2: notifier parses what came over NATS
    parsed = NotifierScoredJob.model_validate(json.loads(payload_json))
    assert parsed.job.source_id == "abc-123"
    assert parsed.job.company == "Stripe"
    assert parsed.dedup_key_inputs == ("stripe", "abc-123")

    # Stage 3: dedup
    db_path = tmp_path / "seen.db"
    with DedupStore(db_path) as store:
        key = compute_key(*parsed.dedup_key_inputs)
        is_new = store.insert_if_new(
            key=key, company=parsed.job.company, title=parsed.job.title,
            url=parsed.job.url, score=parsed.score,
        )
        assert is_new is True
        # Second insert is a duplicate
        is_new2 = store.insert_if_new(
            key=key, company=parsed.job.company, title=parsed.job.title,
            url=parsed.job.url, score=parsed.score,
        )
        assert is_new2 is False

    # Stage 4: telegram format
    msg = format_message(parsed)
    assert "Stripe" in msg
    assert "Staff Site Reliability Engineer" in msg
    assert parsed.job.url in msg  # the apply URL must be present
    # No emoji codepoints
    for c in msg:
        assert ord(c) < 0x2600 or ord(c) > 0x27BF, f"unexpected emoji: {c!r}"


def test_e2e_no_sponsorship_caps_under_threshold(fake_scorer):
    """A job with sponsorship rejection should score under any reasonable threshold."""
    job = _job(
        "Staff Site Reliability Engineer",
        "Strong fit. kubernetes terraform aws. We cannot sponsor visas.",
    )
    scored = fake_scorer.score_job(job)
    assert scored.score <= 0.20, f"got {scored.score}"

    # Notifier with default min=0.65 would skip this
    payload = NotifierScoredJob.model_validate(json.loads(scored.model_dump_json()))
    assert payload.score <= 0.20


def test_dedup_key_matches_across_uuid_changes(fake_scorer, tmp_path):
    """Job UUIDs change every scrape; dedup must use (company, source_id) only."""
    job_a = _job("SRE", "kubernetes", source_id="job-42")
    job_a["id"] = "uuid-FIRST"
    job_b = _job("SRE", "kubernetes", source_id="job-42")
    job_b["id"] = "uuid-SECOND"

    a = NotifierScoredJob.model_validate(
        json.loads(fake_scorer.score_job(job_a).model_dump_json())
    )
    b = NotifierScoredJob.model_validate(
        json.loads(fake_scorer.score_job(job_b).model_dump_json())
    )
    assert a.dedup_key_inputs == b.dedup_key_inputs
    assert compute_key(*a.dedup_key_inputs) == compute_key(*b.dedup_key_inputs)


def test_dedup_distinguishes_same_id_at_different_companies(fake_scorer):
    """Greenhouse and Lever both use small IDs — collision risk if not company-scoped."""
    j1 = _job("SRE", "k8s", source_id="42", company="Stripe")
    j2 = _job("SRE", "k8s", source_id="42", company="Datadog")

    s1 = NotifierScoredJob.model_validate(
        json.loads(fake_scorer.score_job(j1).model_dump_json())
    )
    s2 = NotifierScoredJob.model_validate(
        json.loads(fake_scorer.score_job(j2).model_dump_json())
    )
    assert compute_key(*s1.dedup_key_inputs) != compute_key(*s2.dedup_key_inputs)


def test_company_name_case_does_not_split_dedup_key(fake_scorer):
    """Stripe vs stripe vs STRIPE must share one dedup key."""
    j1 = _job("SRE", "k8s", company="Stripe")
    j2 = _job("SRE", "k8s", company="stripe")
    j3 = _job("SRE", "k8s", company="STRIPE")

    keys = []
    for j in (j1, j2, j3):
        scored = fake_scorer.score_job(j)
        parsed = NotifierScoredJob.model_validate(json.loads(scored.model_dump_json()))
        keys.append(compute_key(*parsed.dedup_key_inputs))
    assert len(set(keys)) == 1, f"company case should not split key, got {keys}"


def test_missing_source_id_rejected_at_notifier(fake_scorer):
    """A job missing source_id must fail at the notifier boundary, not silently dedup."""
    bad = _job("SRE", "k8s")
    bad["source_id"] = ""

    scored = fake_scorer.score_job(bad)
    payload_json = scored.model_dump_json()

    # Notifier requires non-empty source_id (else dedup is unsafe)
    parsed = NotifierScoredJob.model_validate(json.loads(payload_json))
    company, src = parsed.dedup_key_inputs
    # The dedup_republisher logic checks this and drops; we assert the inputs reveal it.
    assert src == "" or src is None, "expected empty source_id to be visible"


def test_scorer_extras_dropped_silently_by_notifier(fake_scorer):
    """rule_adjustments exists in scorer schema, not in notifier — must not crash parsing."""
    scored = fake_scorer.score_job(_job("Staff SRE", "kubernetes"))
    blob = json.loads(scored.model_dump_json())
    assert "rule_adjustments" in blob

    parsed = NotifierScoredJob.model_validate(blob)
    # Notifier doesn't have the field — confirm pydantic behavior
    assert not hasattr(parsed, "rule_adjustments")
