"""
Scraper service — registers Temporal activities and starts the worker.

Activities registered here:
  - load_active_companies: reads company YAML files from /app/companies/
  - fetch_company_jobs:    calls the right ATS connector for one company
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import yaml
from temporalio.client import Client
from temporalio.worker import Worker

from connectors import get_connector
from shared.config import settings
from shared.models import ATSType, CompanyConfig, CustomScraperConfig, JobPost, WorkdayConfig
from temporalio import activity

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("scraper")

COMPANIES_DIR = Path("/app/companies")


def _load_company_yaml(path: Path) -> CompanyConfig:
    raw = yaml.safe_load(path.read_text())
    wd = raw.pop("workday", None)
    custom = raw.pop("custom", None)
    config = CompanyConfig(
        **raw,
        workday=WorkdayConfig(**wd) if wd else None,
        custom=CustomScraperConfig(**custom) if custom else None,
    )
    return config


# ─────────────────── Temporal Activities ───────────────────

@activity.defn
async def load_active_companies() -> list[dict]:
    """Load all active company configs from YAML files. Returns list of dicts for serialisation."""
    companies = []
    for yaml_path in sorted(COMPANIES_DIR.rglob("*.yaml")):
        try:
            config = _load_company_yaml(yaml_path)
            if config.active:
                companies.append(config.model_dump())
        except Exception as e:
            log.warning("Failed to load %s: %s", yaml_path, e)
    log.info("Loaded %d active companies", len(companies))
    return companies


@activity.defn
async def fetch_company_jobs(company_dict: dict) -> list[dict]:
    """Fetch jobs for a single company. Returns list of serialised JobPost dicts."""
    company = CompanyConfig(**company_dict)
    connector = get_connector(company)
    try:
        jobs = await connector.fetch_jobs()
        log.info("[%s] Fetched %d jobs", company.name, len(jobs))
        return [j.model_dump(mode="json") for j in jobs]
    except Exception as e:
        log.error("[%s] Fetch failed: %s", company.name, e)
        return []


# ─────────────────── Worker bootstrap ───────────────────

async def main() -> None:
    log.info("Connecting to Temporal at %s", settings.temporal_host)
    client = await Client.connect(settings.temporal_host, namespace=settings.temporal_namespace)

    worker = Worker(
        client,
        task_queue=settings.temporal_task_queue,
        activities=[load_active_companies, fetch_company_jobs],
    )
    log.info("Scraper worker started on queue '%s'", settings.temporal_task_queue)
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
