"""Entrypoint for the html_scraper service.

Run loop:
1. Load companies from seed_500.yaml + html_targets.yaml.
2. Fan out across max-concurrency workers (default 3). Each worker:
   a. Fetches the careers page (httpx -> Playwright fallback).
   b. Walks pagination up to max_pages.
   c. Sends each page's HTML to Ollama for extraction.
   d. Normalises and publishes each job to NATS jobs.raw.
3. After every cycle, sleeps SCRAPE_INTERVAL_SECONDS (default 1800s).

A single company crashing must NEVER halt the run — each worker wraps its
own task in a try/except and only logs.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from typing import Optional
from urllib.parse import urlencode, urlparse, urlunparse

from bs4 import BeautifulSoup

from services.html_scraper.config import load_tasks, passes_keyword_filter
from services.html_scraper.extractor import extract_jobs
from services.html_scraper.fetcher import Fetcher
from services.html_scraper.models import CompanyTask, ExtractedJob, JobPostOut, RunStats
from services.html_scraper.publisher import NATSPublisher, normalize

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("html_scraper.main")

MAX_CONCURRENCY = int(os.getenv("HTML_SCRAPER_CONCURRENCY", "3"))
SCRAPE_INTERVAL = int(os.getenv("HTML_SCRAPER_INTERVAL", "1800"))
RUN_ONCE = os.getenv("HTML_SCRAPER_RUN_ONCE", "0") == "1"
NATS_URL = os.getenv("NATS_URL", "nats://localhost:4222")


def _next_url(current_url: str, current_html: str, target) -> Optional[str]:
    """Compute the next page URL according to the per-company strategy."""
    strategy = target.pagination
    if strategy == "none" or strategy == "infinite-scroll":
        return None
    if strategy == "page-param":
        # Increment ?page=N (or whatever target.page_param is) by one.
        parsed = urlparse(current_url)
        query = dict(
            kv.split("=", 1) if "=" in kv else (kv, "")
            for kv in parsed.query.split("&")
            if kv
        )
        try:
            current_n = int(query.get(target.page_param, str(target.page_start)))
        except ValueError:
            current_n = target.page_start
        query[target.page_param] = str(current_n + 1)
        new_query = urlencode({k: v for k, v in query.items() if v != ""})
        return urlunparse(parsed._replace(query=new_query))
    if strategy == "next-link":
        if not target.next_selector:
            return None
        soup = BeautifulSoup(current_html, "html.parser")
        link = soup.select_one(target.next_selector)
        href = link.get("href") if link else None
        if not href:
            return None
        # Resolve relative -> absolute.
        from urllib.parse import urljoin
        return urljoin(current_url, href)
    return None


async def _scrape_company(
    task: CompanyTask,
    fetcher: Fetcher,
    publisher: Optional[NATSPublisher],
    stats: RunStats,
) -> None:
    log.info("[%s] start url=%s pages<=%d js=%s",
             task.name, task.target.url, task.target.max_pages, task.target.js_required)
    pages_done = 0
    page_url: Optional[str] = task.target.url
    visited: set[str] = set()
    company_jobs_published = 0

    while page_url and pages_done < task.target.max_pages:
        if page_url in visited:
            log.info("[%s] pagination loop detected, stopping", task.name)
            break
        visited.add(page_url)

        result = await fetcher.fetch(
            page_url,
            force_js=task.target.js_required,
            extra_headers=task.target.extra_headers or None,
        )
        if result is None:
            log.warning("[%s] fetch failed at %s", task.name, page_url)
            break
        if result.tier == "playwright":
            stats.fetch_playwright += 1
        else:
            stats.fetch_httpx += 1

        try:
            extracted = await extract_jobs(
                html=result.html,
                company=task.name,
                base_url=result.url,
            )
        except Exception as e:
            log.exception("[%s] extractor failed: %s", task.name, e)
            extracted = []

        log.info("[%s] page %d (%s) -> %d extracted", task.name, pages_done + 1, result.tier, len(extracted))

        normed: list[JobPostOut] = []
        for ex in extracted:
            if not passes_keyword_filter(
                ex.title, ex.description_text, task.keywords_include, task.keywords_exclude
            ):
                continue
            n = normalize(ex, company=task.name, base_url=result.url)
            if n is not None:
                normed.append(n)

        if normed and publisher is not None:
            published = await publisher.publish(normed)
            company_jobs_published += published
            stats.jobs_published += published
        elif normed:
            company_jobs_published += len(normed)
            stats.jobs_published += len(normed)

        pages_done += 1
        nxt = _next_url(result.url, result.html, task.target)
        if not nxt or nxt == page_url:
            break
        page_url = nxt

    log.info("[%s] done pages=%d published=%d", task.name, pages_done, company_jobs_published)


async def _run_cycle(publisher: Optional[NATSPublisher]) -> RunStats:
    tasks = load_tasks()
    stats = RunStats(companies_total=len(tasks))
    if not tasks:
        log.warning("no companies to scrape")
        return stats

    sem = asyncio.Semaphore(MAX_CONCURRENCY)

    async with Fetcher(per_domain_delay=1.5) as fetcher:
        async def _bounded(t: CompanyTask) -> None:
            async with sem:
                try:
                    await _scrape_company(t, fetcher, publisher, stats)
                    stats.companies_ok += 1
                except Exception as e:
                    stats.companies_failed += 1
                    log.exception("[%s] crashed: %s", t.name, e)

        await asyncio.gather(*[_bounded(t) for t in tasks])

    log.info(
        "cycle stats: total=%d ok=%d fail=%d jobs=%d httpx=%d playwright=%d",
        stats.companies_total,
        stats.companies_ok,
        stats.companies_failed,
        stats.jobs_published,
        stats.fetch_httpx,
        stats.fetch_playwright,
    )
    return stats


async def run() -> None:
    publisher: Optional[NATSPublisher] = None
    try:
        publisher = await NATSPublisher.connect(NATS_URL)
    except Exception as e:
        log.warning("NATS connect failed (%s); running in dry mode", e)
        publisher = None

    stop_event = asyncio.Event()

    def _sig() -> None:
        log.info("signal received; finishing current cycle then exiting")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for s in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(s, _sig)
        except NotImplementedError:
            pass

    while not stop_event.is_set():
        try:
            await _run_cycle(publisher)
        except Exception as e:
            log.exception("cycle failed: %s", e)
        if RUN_ONCE:
            break
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=SCRAPE_INTERVAL)
        except asyncio.TimeoutError:
            pass

    if publisher is not None:
        await publisher.close()
    log.info("html_scraper stopped cleanly")


if __name__ == "__main__":
    asyncio.run(run())
