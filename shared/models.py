from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional
from uuid import uuid4

from pydantic import BaseModel, Field


class ATSType(str, Enum):
    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    ASHBY = "ashby"
    WORKDAY = "workday"
    CUSTOM = "custom"


class VisaStatus(str, Enum):
    H1B = "h1b"
    OPT = "opt"
    CPT = "cpt"
    GREEN_CARD = "green_card"
    CITIZEN = "citizen"


class ApplicationStatus(str, Enum):
    PENDING_REVIEW = "pending_review"
    APPLIED = "applied"
    SCREENING = "screening"
    INTERVIEW = "interview"
    OFFER = "offer"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"


# ──────────────────────────────────────────────
# Company / ATS configuration
# ──────────────────────────────────────────────

class WorkdayConfig(BaseModel):
    tenant: str
    board: str
    location_ids: list[str] = []     # Workday USA location IDs
    categories: list[str] = []


class CustomScraperConfig(BaseModel):
    scraper_module: str              # e.g. "connectors.custom.apple"
    params: dict = {}


class CompanyConfig(BaseModel):
    name: str
    ats: ATSType
    board_id: str                    # company slug / board token
    active: bool = True
    visa_transfers_h1b: bool = True
    sponsors_new_h1b: bool = False
    keywords_include: list[str] = []
    keywords_exclude: list[str] = []
    workday: Optional[WorkdayConfig] = None
    custom: Optional[CustomScraperConfig] = None

    @property
    def display_name(self) -> str:
        return self.name


# ──────────────────────────────────────────────
# Job posting (normalised across all ATS)
# ──────────────────────────────────────────────

class JobPost(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    source_id: str                   # native ID from ATS
    source: str                      # ats type string
    company: str
    company_config: Optional[CompanyConfig] = None
    title: str
    description_html: str = ""
    description_text: str            # HTML stripped
    url: str
    location: str = ""
    department: Optional[str] = None
    remote: bool = False
    posted_at: Optional[datetime] = None
    scraped_at: datetime = Field(default_factory=datetime.utcnow)

    @property
    def dedup_key(self) -> str:
        """Stable Redis key for deduplication."""
        return f"job:{self.source}:{self.source_id}"


# ──────────────────────────────────────────────
# User profile
# ──────────────────────────────────────────────

class ResumeVariant(BaseModel):
    id: str                          # e.g. "sai_infra", "sai_cicd"
    label: str                       # human readable
    focus: str                       # infra | cicd | reliability | leadership | fresher
    minio_path: str                  # bucket/path/to/file.docx


class NotificationConfig(BaseModel):
    telegram_chat_id: str
    telegram_bot_token: str = ""   # per-person bot token; falls back to TELEGRAM_BOT_TOKEN
    email: Optional[str] = None


class UserProfile(BaseModel):
    id: str                          # "sai" or "gf"
    name: str
    email: str
    phone: str
    visa_status: VisaStatus
    needs_sponsorship: bool          # true if needs company to file H1B / OPT→H1B
    h1b_transfer_ok: bool = False    # true if already has H1B and just needs transfer
    experience_years: int
    target_roles: list[str]          # ["devops", "sre", "platform_engineer"]
    skills: list[str]
    education: Optional[str] = None
    resume_variants: list[ResumeVariant] = []
    notification: NotificationConfig
    match_threshold: float = 0.65


# ──────────────────────────────────────────────
# AI outputs
# ──────────────────────────────────────────────

class MatchResult(BaseModel):
    job_id: str
    person_id: str
    score: float                     # 0.0 – 1.0
    matched: bool
    visa_ok: bool
    matched_skills: list[str] = []
    missing_skills: list[str] = []
    reasoning: str = ""
    recommended_variant: str = ""    # which resume variant fits best
    seniority_match: bool = True


class GeneratedDocuments(BaseModel):
    resume_minio_path: str
    resume_url: str                  # presigned URL (48h)
    cover_letter_minio_path: str
    cover_letter_url: str
    study_guide: str = ""            # plain text / markdown (legacy; prep.md replaces)
    defense_minio_path: str = ""     # interview_defense.md in MinIO
    prep_minio_path: str = ""        # interview_prep.md in MinIO


# ──────────────────────────────────────────────
# Application tracking
# ──────────────────────────────────────────────

class ApplicationRecord(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    person_id: str
    job_id: str
    source_id: str
    company: str
    role: str
    job_url: str
    location: str = ""
    resume_variant: str
    resume_url: str
    cover_letter_url: str
    match_score: float
    status: ApplicationStatus = ApplicationStatus.PENDING_REVIEW
    applied_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    notes: str = ""
