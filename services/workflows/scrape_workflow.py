"""
Temporal workflows for the JobSeeker platform.

Workflow tree:
  ScrapeAllCompaniesWorkflow (cron every 15 min)
    └── ScrapeCompanyWorkflow   (child, one per company, parallel)
          └── MatchAndProcessWorkflow (child, one per new job)
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from scraper.main import load_active_companies, fetch_company_jobs
    from worker.activities import is_new_job

log = logging.getLogger("workflow.scrape")

_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=2),
    maximum_attempts=3,
)


@workflow.defn
class ScrapeCompanyWorkflow:
    """Scrapes one company, deduplicates results, returns list of new job dicts."""

    @workflow.run
    async def run(self, company_dict: dict) -> list[dict]:
        company_name = company_dict.get("name", "unknown")

        # Fetch all current jobs from this company
        raw_jobs: list[dict] = await workflow.execute_activity(
            fetch_company_jobs,
            args=[company_dict],
            schedule_to_close_timeout=timedelta(minutes=3),
            retry_policy=_RETRY,
        )

        if not raw_jobs:
            return []

        # Deduplicate — check each job against Redis SETNX
        new_jobs: list[dict] = []
        for job_dict in raw_jobs:
            is_new: bool = await workflow.execute_activity(
                is_new_job,
                args=[job_dict],
                schedule_to_close_timeout=timedelta(seconds=10),
                retry_policy=_RETRY,
            )
            if is_new:
                new_jobs.append(job_dict)

        if new_jobs:
            workflow.logger.info(
                "[%s] %d new jobs (of %d total)", company_name, len(new_jobs), len(raw_jobs)
            )
        return new_jobs


@workflow.defn
class ScrapeAllCompaniesWorkflow:
    """
    Main cron workflow — scrapes all active companies in parallel,
    then triggers MatchAndProcessWorkflow for each new job found.

    Register this with a cron_schedule of '*/15 * * * *'.
    """

    @workflow.run
    async def run(self) -> dict:
        # Load all active company configs
        companies: list[dict] = await workflow.execute_activity(
            load_active_companies,
            schedule_to_close_timeout=timedelta(seconds=30),
            retry_policy=_RETRY,
        )

        workflow.logger.info("Starting scrape run for %d companies", len(companies))

        # Fan out — process companies in batches of 20 to limit concurrency.
        # asyncio.Semaphore is non-deterministic inside Temporal workflows and
        # causes replay failures, so we chunk instead.
        BATCH_SIZE = 20
        all_new_jobs: list[dict] = []

        for i in range(0, len(companies), BATCH_SIZE):
            batch = companies[i : i + BATCH_SIZE]
            tasks = []
            for company in batch:
                ts = workflow.now().strftime("%Y%m%d-%H%M")
                tasks.append(
                    workflow.execute_child_workflow(
                        ScrapeCompanyWorkflow,
                        args=[company],
                        id=f"scrape-{company['name'].lower().replace(' ', '-')}-{ts}",
                        execution_timeout=timedelta(minutes=5),
                    )
                )
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in results:
                if isinstance(r, list):
                    all_new_jobs.extend(r)

        workflow.logger.info("Scrape complete: %d new jobs total", len(all_new_jobs))

        # Spawn a processing workflow for each new job
        from services.workflows.process_workflow import MatchAndProcessWorkflow
        for job in all_new_jobs:
            job_id = job.get("id", "unknown")
            await workflow.execute_child_workflow(
                MatchAndProcessWorkflow,
                args=[job],
                id=f"process-{job_id}",
                execution_timeout=timedelta(minutes=10),
            )

        return {"companies": len(companies), "new_jobs": len(all_new_jobs)}
