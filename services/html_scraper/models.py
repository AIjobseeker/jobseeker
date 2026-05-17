"""Pydantic models for the html_scraper service.

Mirrors the JobPost shape from shared.models / Go scraper's models.Job so that
records published on NATS jobs.raw are indistinguishable for downstream
consumers (scorer, notifier).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel, Field


class HTMLTarget(BaseModel):
    """Per-company override loaded from companies/html_targets.yaml."""

    name: str
    url: str
    js_required: bool = False
    pagination: str = "none"      # next-link | page-param | infinite-scroll | none
    max_pages: int = 3
    page_param: str = "page"
    page_start: int = 1
    next_selector: Optional[str] = None
    job_link_selector: Optional[str] = None
    extra_headers: dict[str, str] = Field(default_factory=dict)
    notes: str = ""


class CompanyTask(BaseModel):
    """Resolved task — seed_500 entry merged with html_targets override."""

    name: str
    domain: str
    career_url: str
    target: HTMLTarget
    keywords_include: list[str] = Field(default_factory=list)
    keywords_exclude: list[str] = Field(default_factory=list)


class ExtractedJob(BaseModel):
    """One job as returned by the Ollama extractor before normalisation.

    All fields optional — the extractor may not see every column on every site.
    Normalisation in publisher.py fills defaults and drops rows that lack the
    minimum required fields (title + url).
    """

    title: str = ""
    url: str = ""
    location: str = ""
    department: Optional[str] = None
    posted_at: Optional[str] = None
    remote: bool = False
    description_text: str = ""


class JobPostOut(BaseModel):
    """Final shape published to jobs.raw — matches Go's models.Job JSON tags."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    source_id: str
    source: str = "html"
    company: str
    title: str
    description_html: str = ""
    description_text: str = ""
    url: str
    location: str = ""
    department: Optional[str] = None
    remote: bool = False
    posted_at: Optional[str] = None
    scraped_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")
    mentions_sponsorship: bool = False
    no_sponsorship_phrase: bool = False

    def to_payload(self) -> dict[str, Any]:
        """JSON-safe dict ready for NATS publish."""
        return self.model_dump(exclude_none=False)


class RunStats(BaseModel):
    companies_total: int = 0
    companies_ok: int = 0
    companies_failed: int = 0
    jobs_published: int = 0
    jobs_dropped_sanity: int = 0
    fetch_httpx: int = 0
    fetch_playwright: int = 0
