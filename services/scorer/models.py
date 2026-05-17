from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

try:
    from shared.models import JobPost
except ImportError:
    JobPost = None  # type: ignore[assignment,misc]


class RawJob(BaseModel):
    """Lightweight job model for messages off `jobs.raw` when shared.models is unavailable."""

    id: str
    source_id: str = ""
    source: str = "custom"
    company: str = ""
    title: str = ""
    description_text: str = ""
    description_html: str = ""
    url: str = ""
    location: str = ""
    department: Optional[str] = None
    remote: bool = False
    posted_at: Optional[str] = None
    scraped_at: Optional[str] = None


JobLike = JobPost if JobPost is not None else RawJob


class ScoredJob(BaseModel):
    job: dict
    score: float = Field(ge=0.0, le=1.0)
    embedding_score: float
    rule_adjustments: dict[str, float] = Field(default_factory=dict)
    matched_skills: list[str] = Field(default_factory=list)
    missing_skills: list[str] = Field(default_factory=list)
    reason: str = ""
    # Block G — posting legitimacy. Optional so existing producers don't break.
    legitimacy_tier: str = ""           # HIGH_CONFIDENCE | PROCEED_WITH_CAUTION | SUSPICIOUS
    legitimacy_signals: list[str] = Field(default_factory=list)
