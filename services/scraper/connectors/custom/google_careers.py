"""Google Careers — undocumented JSON API."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from connectors.base import BaseConnector, make_http_client, strip_html
from shared.models import CompanyConfig, JobPost

GOOGLE_SEARCH_URL = "https://careers.google.com/api/v3/search/"


class GoogleConnector(BaseConnector):

    async def fetch_jobs(self) -> list[JobPost]:
        params = self.company.custom.params if self.company.custom else {}
        location = params.get("location", "United States")
        query = params.get("query", "")

        all_jobs: list[JobPost] = []

        async with make_http_client() as client:
            page = 1
            while True:
                resp = await client.get(
                    GOOGLE_SEARCH_URL,
                    params={
                        "q": query,
                        "location": location,
                        "page": page,
                        "num": 20,
                    },
                )
                if resp.status_code != 200:
                    break
                data = resp.json()

                jobs_list = data.get("jobs", [])
                if not jobs_list:
                    break

                for raw in jobs_list:
                    title = raw.get("title", "")
                    desc_html = raw.get("description", "")
                    desc_text = strip_html(desc_html)

                    if not self._passes_keyword_filter(title, desc_text):
                        continue

                    job_id = raw.get("id", "")
                    apply_url = raw.get("apply_url", "")
                    if not apply_url and job_id:
                        apply_url = f"https://careers.google.com/jobs/results/{job_id}"

                    locations = raw.get("locations", [])
                    location_str = ", ".join(locations)

                    posted_at: Optional[datetime] = None
                    date_str = raw.get("date")
                    if date_str:
                        try:
                            posted_at = datetime.strptime(date_str, "%Y-%m-%d")
                        except ValueError:
                            pass

                    all_jobs.append(JobPost(
                        source_id=str(job_id),
                        source="google",
                        company="Google",
                        company_config=self.company,
                        title=title,
                        description_html=desc_html,
                        description_text=desc_text,
                        url=apply_url,
                        location=location_str,
                        department=raw.get("department"),
                        posted_at=posted_at,
                    ))

                next_page = data.get("next_page")
                if not next_page:
                    break
                page += 1

        return all_jobs
