"""Apple careers — undocumented REST API (no Playwright needed)."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from connectors.base import BaseConnector, make_http_client, strip_html
from shared.models import CompanyConfig, JobPost

APPLE_SEARCH_URL = "https://jobs.apple.com/api/role/search"
APPLE_JOB_URL = "https://jobs.apple.com/en-us/details/{id}"

# Apple team IDs relevant to DevOps/SRE/Platform engineering
APPLE_TEAMS = [
    "team-software-and-services",
    "team-devops-re",
    "team-information-systems-and-technology",
    "team-site-reliability-engineering",
]


class AppleConnector(BaseConnector):

    async def fetch_jobs(self) -> list[JobPost]:
        params = self.company.custom.params if self.company.custom else {}
        teams = params.get("teams", APPLE_TEAMS)
        locations = params.get("locations", ["postLocation-USA"])

        all_jobs: list[JobPost] = []

        async with make_http_client() as client:
            page = 1
            while True:
                payload = {
                    "filters": {
                        "postingpostLocation": locations,
                        "team": teams,
                    },
                    "page": page,
                    "locale": "en-us",
                    "query": "",
                }
                resp = await client.post(APPLE_SEARCH_URL, json=payload)
                if resp.status_code != 200:
                    break
                data = resp.json()

                results = data.get("searchResults", [])
                if not results:
                    break

                for raw in results:
                    title = raw.get("postingTitle", "")
                    # Apple doesn't return full description in search — use title + team
                    team = raw.get("team", {}).get("teamName", "")
                    desc_text = f"{title}. Team: {team}. "
                    desc_text += raw.get("postingDescription", "")

                    if not self._passes_keyword_filter(title, desc_text):
                        continue

                    posting_id = raw.get("positionId", "")
                    slug = raw.get("transformedPostingTitle", "")
                    url = f"https://jobs.apple.com/en-us/details/{posting_id}/{slug}"

                    # Apple uses human-readable date strings
                    posted_at: Optional[datetime] = None
                    post_date = raw.get("postingDate")
                    if post_date:
                        try:
                            posted_at = datetime.strptime(post_date, "%b %d, %Y")
                        except ValueError:
                            pass

                    location = raw.get("homeOffice", "")
                    if not location:
                        locs = raw.get("locations", [])
                        location = ", ".join(loc.get("name", "") for loc in locs)

                    all_jobs.append(JobPost(
                        source_id=posting_id,
                        source="apple",
                        company="Apple",
                        company_config=self.company,
                        title=title,
                        description_text=desc_text,
                        url=url,
                        location=location,
                        department=team,
                        posted_at=posted_at,
                    ))

                total_pages = data.get("totalPages", 1)
                if page >= total_pages:
                    break
                page += 1

        return all_jobs
