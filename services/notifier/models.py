"""Pydantic models for the scored-job payload that flows through NATS.

The scoring service publishes to `jobs.scored`; the dedup component republishes
new (never-before-seen) jobs to `jobs.new`. Both subjects carry the same shape.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class ScoredJobInner(BaseModel):
    """Subset of JobPost fields the scoring service includes in its payload."""

    id: str = ""
    source_id: str
    company: str
    title: str
    url: str
    location: str = ""
    department: Optional[str] = None
    remote: bool = False
    scraped_at: str = ""


class ScoredJob(BaseModel):
    """Envelope published on `jobs.scored` and `jobs.new`."""

    job: ScoredJobInner
    score: float
    embedding_score: float = 0.0
    matched_skills: list[str] = Field(default_factory=list)
    missing_skills: list[str] = Field(default_factory=list)
    reason: str = ""
    # Block G — populated by scorer's assess_legitimacy().
    legitimacy_tier: str = ""
    legitimacy_signals: list[str] = Field(default_factory=list)
    rule_adjustments: dict[str, float] = Field(default_factory=dict)

    @property
    def dedup_key_inputs(self) -> tuple[str, str]:
        """Stable identity is (lowercased company, source_id) — NOT job.id."""
        return self.job.company.lower(), self.job.source_id
