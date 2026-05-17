"""Lever public postings API — no auth required."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from connectors.base import BaseConnector, make_http_client, strip_html
from shared.models import CompanyConfig, JobPost


class LeverConnector(BaseConnector):
    BASE_URL = "https://api.lever.co/v0/postings/{board}"

    async def fetch_jobs(self) -> list[JobPost]:
        url = self.BASE_URL.format(board=self.company.board_id)
        async with make_http_client() as client:
            resp = await client.get(url, params={"mode": "json"})
            resp.raise_for_status()
            data = resp.json()

        jobs: list[JobPost] = []
        for raw in data:
            title = raw.get("text", "")
            desc_html = raw.get("description", "") + raw.get("additional", "")
            desc_text = raw.get("descriptionPlain", "") or strip_html(desc_html)

            if not self._passes_keyword_filter(title, desc_text):
                continue

            cats = raw.get("categories", {})
            location = cats.get("location", "") or cats.get("allLocations", [""])[0]
            department = cats.get("team", None)

            # Lever timestamps are milliseconds since epoch
            posted_at: Optional[datetime] = None
            created_ms = raw.get("createdAt")
            if created_ms:
                posted_at = datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc)

            jobs.append(JobPost(
                source_id=raw["id"],
                source="lever",
                company=self.company.name,
                company_config=self.company,
                title=title,
                description_html=desc_html,
                description_text=desc_text,
                url=raw.get("hostedUrl", ""),
                location=location,
                department=department,
                posted_at=posted_at,
            ))

        return jobs
