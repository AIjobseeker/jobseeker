"""FastAPI gateway — manual triggers, application status updates, dashboard."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

import asyncpg
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from temporalio.client import Client

from shared.config import settings
from shared.models import ApplicationStatus
from services.workflows.process_workflow import MatchAndProcessWorkflow

app = FastAPI(title="JobSeeker API", version="1.0.0")
log = logging.getLogger("api")


# ─────────────────── Health ───────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "ts": datetime.utcnow().isoformat()}


# ─────────────────── Manual triggers ───────────────────

class ProcessJobRequest(BaseModel):
    job_url: str
    company: str
    title: str
    description: str
    location: str = ""
    person_id: str = "sai"  # "sai" or "gf"


@app.post("/jobs/process")
async def process_job_manually(req: ProcessJobRequest):
    """Manually trigger the full pipeline for a specific job URL."""
    from shared.models import JobPost
    import uuid

    job = JobPost(
        source_id=str(uuid.uuid4()),
        source="manual",
        company=req.company,
        title=req.title,
        description_text=req.description,
        url=req.job_url,
        location=req.location,
    )

    client = await Client.connect(settings.temporal_host)
    handle = await client.start_workflow(
        MatchAndProcessWorkflow.run,
        args=[job.model_dump(mode="json")],
        id=f"manual-{job.id}",
        task_queue=settings.temporal_task_queue,
    )
    return {"workflow_id": handle.id, "job_id": job.id}


# ─────────────────── Application tracker ───────────────────

@app.get("/applications")
async def list_applications(person_id: Optional[str] = None, status: Optional[str] = None):
    conn = await asyncpg.connect(settings.postgres_url)
    try:
        query = "SELECT * FROM applications WHERE 1=1"
        params = []
        if person_id:
            params.append(person_id)
            query += f" AND person_id = ${len(params)}"
        if status:
            params.append(status)
            query += f" AND status = ${len(params)}"
        query += " ORDER BY created_at DESC LIMIT 100"
        rows = await conn.fetch(query, *params)
        return [dict(r) for r in rows]
    finally:
        await conn.close()


class StatusUpdate(BaseModel):
    status: str
    notes: str = ""


@app.patch("/applications/{record_id}")
async def update_status(record_id: str, body: StatusUpdate):
    conn = await asyncpg.connect(settings.postgres_url)
    try:
        result = await conn.execute(
            "UPDATE applications SET status=$1, notes=$2 WHERE id=$3",
            body.status, body.notes, record_id,
        )
        if result == "UPDATE 0":
            raise HTTPException(status_code=404, detail="Application not found")
        return {"updated": True}
    finally:
        await conn.close()


@app.get("/applications/stats")
async def stats():
    conn = await asyncpg.connect(settings.postgres_url)
    try:
        rows = await conn.fetch(
            """
            SELECT person_id, status, COUNT(*) as count
            FROM applications
            GROUP BY person_id, status
            ORDER BY person_id, status
            """
        )
        return [dict(r) for r in rows]
    finally:
        await conn.close()
