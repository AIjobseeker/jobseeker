from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Optional

import httpx
from bs4 import BeautifulSoup

from shared.config import settings
from shared.models import CompanyConfig, JobPost


def strip_html(html: str) -> str:
    """Strip HTML tags and normalise whitespace."""
    if not html:
        return ""
    text = BeautifulSoup(html, "html.parser").get_text(separator="\n")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def make_http_client() -> httpx.AsyncClient:
    proxies = {}
    if settings.http_proxy:
        proxies["http://"] = settings.http_proxy
    if settings.https_proxy:
        proxies["https://"] = settings.https_proxy
    return httpx.AsyncClient(
        headers={"User-Agent": settings.user_agent},
        timeout=settings.http_timeout,
        follow_redirects=True,
        proxies=proxies or None,
    )


class BaseConnector(ABC):
    """All ATS connectors implement this interface."""

    def __init__(self, company: CompanyConfig) -> None:
        self.company = company

    @abstractmethod
    async def fetch_jobs(self) -> list[JobPost]:
        """Fetch all current open jobs for this company."""

    def _passes_keyword_filter(self, title: str, description: str) -> bool:
        """Return True if the job passes include/exclude keyword filters."""
        text = f"{title} {description}".lower()
        if self.company.keywords_exclude:
            for kw in self.company.keywords_exclude:
                if kw.lower() in text:
                    return False
        # If no include keywords configured, accept everything
        if not self.company.keywords_include:
            return True
        return any(kw.lower() in text for kw in self.company.keywords_include)
