"""Ashby public job board API."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from connectors.base import BaseConnector, make_http_client, strip_html
from shared.models import CompanyConfig, JobPost


class AshbyConnector(BaseConnector):
    BASE_URL = "https://api.ashbyhq.com/posting-api/job-board/{board}/jobs"

    async def fetch_jobs(self) -> list[JobPost]:
        url = self.BASE_URL.format(board=self.company.board_id)
        async with make_http_client() as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()

        jobs: list[JobPost] = []
        for raw in data.get("jobs", []):
            title = raw.get("title", "")
            desc_html = raw.get("descriptionHtml", "")
            desc_text = strip_html(desc_html)

            if not self._passes_keyword_filter(title, desc_text):
                continue

            posted_at: Optional[datetime] = None
            published = raw.get("publishedAt")
            if published:
                try:
                    posted_at = datetime.fromisoformat(published.replace("Z", "+00:00"))
                except ValueError:
                    pass

            location = raw.get("location", "")
            if raw.get("isRemote"):
                location = f"Remote ({location})" if location else "Remote"

            jobs.append(JobPost(
                source_id=raw["id"],
                source="ashby",
                company=self.company.name,
                company_config=self.company,
                title=title,
                description_html=desc_html,
                description_text=desc_text,
                url=raw.get("jobUrl", ""),
                location=location,
                department=raw.get("department"),
                remote=raw.get("isRemote", False),
                posted_at=posted_at,
            ))

        return jobs
