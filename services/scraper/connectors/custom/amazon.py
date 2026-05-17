"""Amazon Jobs — uses their own JSON search API."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from connectors.base import BaseConnector, make_http_client, strip_html
from shared.models import CompanyConfig, JobPost

AMAZON_SEARCH_URL = "https://www.amazon.jobs/en/search.json"

AMAZON_CATEGORIES = [
    "software-development",
    "operations-it-support-engineering",
    "systems-quality-and-security-engineering",
]


class AmazonConnector(BaseConnector):

    async def fetch_jobs(self) -> list[JobPost]:
        params = self.company.custom.params if self.company.custom else {}
        categories = params.get("categories", AMAZON_CATEGORIES)

        all_jobs: list[JobPost] = []

        async with make_http_client() as client:
            offset = 0
            page_size = 10

            while True:
                query_params = {
                    "normalized_location[]": "United States",
                    "result_limit": page_size,
                    "offset": offset,
                    "job_type[]": "Full-Time",
                }
                for cat in categories:
                    query_params.setdefault("category[]", [])
                    if isinstance(query_params["category[]"], list):
                        query_params["category[]"].append(cat)

                resp = await client.get(AMAZON_SEARCH_URL, params=query_params)
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

                    job_id = str(raw.get("id_icims", raw.get("job_id", "")))
                    url = f"https://www.amazon.jobs{raw.get('job_path', '')}"

                    posted_at: Optional[datetime] = None
                    posted_date = raw.get("posted_date")
                    if posted_date:
                        try:
                            posted_at = datetime.strptime(posted_date, "%B %d, %Y")
                        except ValueError:
                            pass

                    all_jobs.append(JobPost(
                        source_id=job_id,
                        source="amazon",
                        company="Amazon",
                        company_config=self.company,
                        title=title,
                        description_html=desc_html,
                        description_text=desc_text,
                        url=url,
                        location=raw.get("normalized_location", ""),
                        department=raw.get("team", None),
                        posted_at=posted_at,
                    ))

                total = data.get("hits", 0)
                offset += page_size
                if offset >= min(total, 200):  # cap at 200 per run
                    break

        return all_jobs
