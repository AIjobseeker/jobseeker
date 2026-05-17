"""
Workday undocumented JSON API.

Almost every major company on Workday has a REST endpoint:
  POST https://{tenant}.wd5.myworkdayjobs.com/wday/cxs/{tenant}/{board}/jobs

Step 1: We call the locations endpoint to discover USA location IDs.
Step 2: We POST with keyword + location filters and paginate.

Tenant info is in the company YAML under workday.tenant / workday.board.
location_ids should be pre-seeded in the YAML (they rarely change).
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from connectors.base import BaseConnector, make_http_client, strip_html
from shared.models import CompanyConfig, JobPost

PAGE_SIZE = 20


class WorkdayConnector(BaseConnector):

    def _base_url(self) -> str:
        wc = self.company.workday
        return (
            f"https://{wc.tenant}.wd5.myworkdayjobs.com"
            f"/wday/cxs/{wc.tenant}/{wc.board}"
        )

    async def fetch_jobs(self) -> list[JobPost]:
        if not self.company.workday:
            raise ValueError(f"{self.company.name}: missing workday config")

        wc = self.company.workday
        base = self._base_url()
        jobs_url = f"{base}/jobs"

        # Build location filter from pre-configured IDs
        location_filter = [{"id": loc_id} for loc_id in wc.location_ids]

        all_jobs: list[JobPost] = []
        offset = 0

        async with make_http_client() as client:
            while True:
                payload = {
                    "limit": PAGE_SIZE,
                    "offset": offset,
                    "searchText": "",
                    "locations": location_filter,
                }
                resp = await client.post(jobs_url, json=payload)
                if resp.status_code == 404:
                    break
                resp.raise_for_status()
                data = resp.json()

                job_postings = data.get("jobPostings", [])
                if not job_postings:
                    break

                for raw in job_postings:
                    title = raw.get("title", "")
                    # Workday returns brief listing — full description requires detail call
                    desc_text = raw.get("jobDescription", {}).get("text", "")
                    if not desc_text:
                        desc_text = title  # fallback — detailed fetch can enrich later

                    if not self._passes_keyword_filter(title, desc_text):
                        continue

                    # Build canonical URL
                    ext_id = raw.get("externalPath", "")
                    job_url = (
                        f"https://{wc.tenant}.wd5.myworkdayjobs.com"
                        f"/en-US/{wc.board}{ext_id}"
                    )

                    posted_at: Optional[datetime] = None
                    posted_str = raw.get("postedOn")
                    if posted_str:
                        try:
                            posted_at = datetime.fromisoformat(
                                posted_str.replace("Z", "+00:00")
                            )
                        except ValueError:
                            pass

                    location = raw.get("locationsText", "")
                    all_jobs.append(JobPost(
                        source_id=raw.get("bulletFields", [ext_id])[0] or ext_id,
                        source="workday",
                        company=self.company.name,
                        company_config=self.company,
                        title=title,
                        description_text=desc_text,
                        url=job_url,
                        location=location,
                        posted_at=posted_at,
                    ))

                total = data.get("total", 0)
                offset += PAGE_SIZE
                if offset >= total:
                    break

        return all_jobs
