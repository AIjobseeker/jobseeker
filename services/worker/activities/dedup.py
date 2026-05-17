"""Redis-based job deduplication using SETNX with 30-day TTL."""
from __future__ import annotations

import redis.asyncio as aioredis
from temporalio import activity

from shared.config import settings
from shared.models import JobPost

TTL_SECONDS = 30 * 24 * 60 * 60  # 30 days

# Connection pool created once at module level — reused across all activity calls.
_pool: aioredis.ConnectionPool = aioredis.ConnectionPool.from_url(
    settings.redis_url,
    decode_responses=True,
    max_connections=10,
)


@activity.defn
async def is_new_job(job_dict: dict) -> bool:
    """Return True if this job has NOT been seen before (and mark it as seen)."""
    job = JobPost(**job_dict)
    client = aioredis.Redis(connection_pool=_pool)
    # SETNX: sets only if key does not exist
    is_new = await client.set(job.dedup_key, "1", ex=TTL_SECONDS, nx=True)
    return bool(is_new)
