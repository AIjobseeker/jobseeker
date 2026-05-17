"""PostgreSQL application tracker — records every match and application."""
from __future__ import annotations

import asyncio
import logging

import asyncpg
from temporalio import activity

from shared.config import settings
from shared.models import ApplicationRecord, ApplicationStatus, GeneratedDocuments, JobPost, MatchResult, UserProfile

log = logging.getLogger("worker.tracker")

# Module-level pool and lock for lazy initialisation.
_pool: asyncpg.Pool | None = None
_pool_lock = asyncio.Lock()


async def get_pool() -> asyncpg.Pool:
    """Return the shared connection pool, creating it on first call."""
    global _pool
    if _pool is not None:
        return _pool
    async with _pool_lock:
        # Double-checked locking: another coroutine may have created it while
        # we were waiting for the lock.
        if _pool is None:
            _pool = await asyncpg.create_pool(
                settings.postgres_url,
                min_size=2,
                max_size=10,
            )
    return _pool


@activity.defn
async def track_application(
    job_dict: dict,
    profile_dict: dict,
    match_dict: dict,
    docs_dict: dict,
) -> str:
    """Insert an application record. Returns the new record ID."""
    job = JobPost(**job_dict)
    profile = UserProfile(**profile_dict)
    match = MatchResult(**match_dict)
    docs = GeneratedDocuments(**docs_dict)

    record = ApplicationRecord(
        person_id=profile.id,
        job_id=job.id,
        source_id=job.source_id,
        company=job.company,
        role=job.title,
        job_url=job.url,
        location=job.location,
        resume_variant=match.recommended_variant,
        resume_url=docs.resume_url,
        cover_letter_url=docs.cover_letter_url,
        match_score=match.score,
        status=ApplicationStatus.PENDING_REVIEW,
    )

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO applications (
                id, person_id, job_id, source_id, company, role, job_url,
                location, resume_variant, resume_url, cover_letter_url,
                match_score, status, created_at
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7,
                $8, $9, $10, $11,
                $12, $13, NOW()
            )
            ON CONFLICT (source_id, person_id) DO NOTHING
            """,
            record.id, record.person_id, record.job_id, record.source_id,
            record.company, record.role, record.job_url,
            record.location, record.resume_variant, record.resume_url, record.cover_letter_url,
            record.match_score, record.status.value,
        )
    log.info("Tracked application: %s @ %s for %s", job.title, job.company, profile.id)
    return record.id


@activity.defn
async def update_application_status(record_id: str, status: str, notes: str = "") -> None:
    """Update application status (called manually or via API)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE applications SET status=$1, notes=$2 WHERE id=$3",
            status, notes, record_id,
        )
