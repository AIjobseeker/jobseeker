"""
Worker service — runs ALL activities (match, resume, cover letter, study guide,
storage, notify, track) and registers the Temporal workflows.
Also bootstraps the cron schedule on first startup.
"""
from __future__ import annotations

import asyncio
import logging
import sys

from temporalio.client import Client, Schedule, ScheduleActionStartWorkflow, ScheduleSpec
from temporalio.common import RetryPolicy
from temporalio.worker import Worker

from shared.config import settings
from worker.activities import (
    is_new_job,
    match_job,
    tailor_resume,
    generate_cover_letter,
    generate_study_guide,
    upload_document,
    notify_telegram,
    track_application,
    update_application_status,
    sync_to_sheet,
)
from services.workflows.process_workflow import (
    MatchAndProcessWorkflow,
)
from services.workflows.scrape_workflow import (
    ScrapeAllCompaniesWorkflow,
    ScrapeCompanyWorkflow,
)
from worker.activities.profile import load_profile_activity

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("worker")

SCHEDULE_ID = "scrape-all-companies"
CRON = "*/15 * * * *"  # every 15 minutes


async def _register_schedule(client: Client) -> None:
    """Create the cron schedule if it doesn't already exist."""
    try:
        handle = client.get_schedule_handle(SCHEDULE_ID)
        await handle.describe()
        log.info("Cron schedule '%s' already exists", SCHEDULE_ID)
    except Exception:
        await client.create_schedule(
            SCHEDULE_ID,
            Schedule(
                action=ScheduleActionStartWorkflow(
                    ScrapeAllCompaniesWorkflow.run,
                    id="scrape-all-companies-run",
                    task_queue=settings.temporal_task_queue,
                ),
                spec=ScheduleSpec(cron_expressions=[CRON]),
            ),
        )
        log.info("Created cron schedule '%s' (%s)", SCHEDULE_ID, CRON)


async def main() -> None:
    log.info("Connecting to Temporal at %s", settings.temporal_host)
    client = await Client.connect(
        settings.temporal_host, namespace=settings.temporal_namespace
    )

    await _register_schedule(client)

    worker = Worker(
        client,
        task_queue=settings.temporal_task_queue,
        workflows=[
            ScrapeAllCompaniesWorkflow,
            ScrapeCompanyWorkflow,
            MatchAndProcessWorkflow,
        ],
        activities=[
            is_new_job,
            match_job,
            tailor_resume,
            generate_cover_letter,
            generate_study_guide,
            upload_document,
            notify_telegram,
            track_application,
            update_application_status,
            sync_to_sheet,
            load_profile_activity,
        ],
    )
    log.info("Worker started — listening on queue '%s'", settings.temporal_task_queue)
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
