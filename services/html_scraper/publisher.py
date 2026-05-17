"""NATS publisher and ExtractedJob -> JobPost normaliser.

Publishes via JetStream on subject jobs.raw, identical to what the Go scraper
does in publisher/nats.go. Stable source_id = sha1(canonical_url)[:16] so the
notifier's dedup logic keys correctly across runs.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from typing import Iterable, Optional
from urllib.parse import urljoin, urlparse, urlunparse

import nats
from nats.aio.client import Client as NATSClient

from services.html_scraper.models import ExtractedJob, JobPostOut

log = logging.getLogger("html_scraper.publisher")

NATS_URL = os.getenv("NATS_URL", "nats://localhost:4222")
SUBJECT_RAW = os.getenv("HTML_SCRAPER_SUBJECT_RAW", "jobs.raw")
STREAM_NAME = "JOBS"

_SPONSOR_PHRASE = re.compile(
    r"(visa\s+sponsorship|sponsor(?:s|ed)?\s+h-?1b|h-?1b\s+sponsorship\s+available)",
    re.IGNORECASE,
)
_NO_SPONSOR_PHRASE = re.compile(
    r"(no\s+visa\s+sponsorship|will\s+not\s+sponsor|do\s+not\s+sponsor|"
    r"unable\s+to\s+(?:offer|provide)\s+sponsorship)",
    re.IGNORECASE,
)


def canonicalize_url(raw_url: str, base: str) -> str:
    """Resolve relative URLs and strip fragments / common tracking params."""
    if not raw_url:
        return ""
    absolute = urljoin(base, raw_url.strip())
    parsed = urlparse(absolute)
    # Drop fragment and any utm_* / gh_jid (carry-over from share buttons).
    query_pairs = [
        kv for kv in parsed.query.split("&")
        if kv and not kv.lower().startswith(("utm_", "gh_jid=", "gclid=", "fbclid="))
    ]
    cleaned = parsed._replace(query="&".join(query_pairs), fragment="")
    return urlunparse(cleaned)


def stable_source_id(canonical_url: str) -> str:
    """sha1(canonical_url)[:16] — the dedup primary key downstream."""
    digest = hashlib.sha1(canonical_url.encode("utf-8")).hexdigest()
    return digest[:16]


def _detect_remote(location: str, description: str) -> bool:
    blob = f"{location} {description}".lower()
    return any(token in blob for token in ("remote", "anywhere", "work from home"))


def normalize(
    extracted: ExtractedJob,
    company: str,
    base_url: str,
) -> Optional[JobPostOut]:
    """Convert one extractor row into a publishable JobPostOut.

    Returns None when the row is unusable (no title or no URL).
    """
    url = canonicalize_url(extracted.url or "", base_url)
    title = (extracted.title or "").strip()
    if not title or not url:
        return None
    description = (extracted.description_text or "").strip()
    location = (extracted.location or "").strip()
    return JobPostOut(
        source_id=stable_source_id(url),
        source="html",
        company=company,
        title=title,
        description_html="",
        description_text=description,
        url=url,
        location=location,
        department=(extracted.department or None),
        remote=bool(extracted.remote) or _detect_remote(location, description),
        posted_at=extracted.posted_at or None,
        mentions_sponsorship=bool(_SPONSOR_PHRASE.search(description)),
        no_sponsorship_phrase=bool(_NO_SPONSOR_PHRASE.search(description)),
    )


class NATSPublisher:
    def __init__(self, nc: NATSClient, subject: str = SUBJECT_RAW) -> None:
        self.nc = nc
        self.subject = subject
        self._js = None

    @classmethod
    async def connect(cls, url: str = NATS_URL, subject: str = SUBJECT_RAW) -> "NATSPublisher":
        nc = await nats.connect(url, name="jobseeker-html-scraper", max_reconnect_attempts=-1)
        log.info("nats connected url=%s", url)
        pub = cls(nc, subject=subject)
        try:
            pub._js = nc.jetstream()
            try:
                await pub._js.stream_info(STREAM_NAME)
            except Exception:
                # Stream creation is owned by the Go scraper. If it isn't there
                # yet we silently fall back to core NATS publish.
                log.info("JetStream stream %s not present; using core publish", STREAM_NAME)
                pub._js = None
        except Exception as e:
            log.info("JetStream unavailable (%s); using core publish", e)
            pub._js = None
        return pub

    async def publish(self, jobs: Iterable[JobPostOut]) -> int:
        count = 0
        for j in jobs:
            payload = j.model_dump_json().encode("utf-8")
            try:
                if self._js is not None:
                    await self._js.publish(self.subject, payload)
                else:
                    await self.nc.publish(self.subject, payload)
                count += 1
            except Exception as e:
                log.exception("publish failed for %s: %s", j.url, e)
        if count:
            try:
                await self.nc.flush(timeout=5)
            except Exception:
                pass
        return count

    async def close(self) -> None:
        try:
            await self.nc.drain()
        except Exception:
            pass
