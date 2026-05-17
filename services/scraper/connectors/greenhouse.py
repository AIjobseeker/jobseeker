"""Greenhouse public jobs API — no auth required."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from connectors.base import BaseConnector, make_http_client, strip_html
from shared.models import CompanyConfig, JobPost


class GreenhouseConnector(BaseConnector):
    BASE_URL = "https://api.greenhouse.io/v1/boards/{board}/jobs"

    async def fetch_jobs(self) -> list[JobPost]:
        url = self.BASE_URL.format(board=self.company.board_id)
        async with make_http_client() as client:
            resp = await client.get(url, params={"content": "true"})
            resp.raise_for_status()
            data = resp.json()

        jobs: list[JobPost] = []
        for raw in data.get("jobs", []):
            title = raw.get("title", "")
            desc_html = raw.get("content", "")
            desc_text = strip_html(desc_html)

            if not self._passes_keyword_filter(title, desc_text):
                continue

            location = ""
            loc = raw.get("location")
            if isinstance(loc, dict):
                location = loc.get("name", "")

            posted_at: Optional[datetime] = None
            updated = raw.get("updated_at")
            if updated:
                try:
                    posted_at = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                except ValueError:
                    pass

            department = None
            depts = raw.get("departments", [])
            if depts:
                department = depts[0].get("name")

            jobs.append(JobPost(
                source_id=str(raw["id"]),
                source="greenhouse",
                company=self.company.name,
                company_config=self.company,
                title=title,
                description_html=desc_html,
                description_text=desc_text,
                url=raw.get("absolute_url", ""),
                location=location,
                department=department,
                posted_at=posted_at,
            ))

        return jobs
