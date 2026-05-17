"""Two-tier HTML fetcher.

Tier 1: httpx — fast, no browser. Always tried first.
Tier 2: Playwright (chromium headless) — used when:
  * the target's html_targets.yaml row sets js_required: true, OR
  * httpx returns a body that strongly suggests JS bootstrap (heuristic: page
    contains <noscript> with a "please enable JavaScript" message, or the
    body has fewer than 8 <a> tags AND a <script> with a known SPA bootstrap
    pattern such as window.__NEXT_DATA__ / data-reactroot).
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

import httpx

log = logging.getLogger("html_scraper.fetcher")

# A modern desktop UA. We deliberately do not rotate — these career sites are
# quite tolerant and a stable UA makes any rate-limit / WAF response easier
# to debug.
DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
HTTP_TIMEOUT = 25.0
PLAYWRIGHT_TIMEOUT_MS = 30_000

_NOSCRIPT_HINT = re.compile(
    r"<noscript[^>]*>[^<]*(?:enable\s+javascript|requires\s+javascript)",
    re.IGNORECASE,
)
_SPA_BOOTSTRAP = re.compile(
    r"(window\.__NEXT_DATA__|data-reactroot|id=\"__NUXT__\"|ng-version=|window\.__INITIAL_STATE__)",
    re.IGNORECASE,
)
_A_TAG = re.compile(r"<a[\s>]", re.IGNORECASE)


@dataclass
class FetchResult:
    url: str
    html: str
    tier: str            # "httpx" | "playwright"
    status: int = 200
    a_count: int = 0


def _looks_js_required(html: str) -> bool:
    if _NOSCRIPT_HINT.search(html):
        return True
    a_count = len(_A_TAG.findall(html))
    if a_count < 8 and _SPA_BOOTSTRAP.search(html):
        return True
    return False


class Fetcher:
    """Per-run fetcher that lazily spins up Playwright on first need."""

    def __init__(
        self,
        per_domain_delay: float = 1.5,
        user_agent: str = DEFAULT_UA,
    ) -> None:
        self._delay = per_domain_delay
        self._ua = user_agent
        self._last_hit: dict[str, float] = {}
        self._client: Optional[httpx.AsyncClient] = None
        self._pw = None
        self._browser = None
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> "Fetcher":
        self._client = httpx.AsyncClient(
            timeout=HTTP_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": self._ua, "Accept": "text/html,*/*;q=0.8"},
        )
        return self

    async def __aexit__(self, *_exc) -> None:
        if self._client is not None:
            await self._client.aclose()
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:
                pass
        if self._pw is not None:
            try:
                await self._pw.stop()
            except Exception:
                pass

    async def _polite_wait(self, url: str) -> None:
        host = urlparse(url).netloc
        now = asyncio.get_event_loop().time()
        async with self._lock:
            last = self._last_hit.get(host, 0.0)
            wait = self._delay - (now - last)
            self._last_hit[host] = now + max(wait, 0.0)
        if wait > 0:
            await asyncio.sleep(wait)

    async def fetch(
        self,
        url: str,
        force_js: bool = False,
        extra_headers: Optional[dict[str, str]] = None,
    ) -> Optional[FetchResult]:
        """Try httpx first, escalate to Playwright when needed."""
        if not force_js:
            await self._polite_wait(url)
            try:
                assert self._client is not None
                resp = await self._client.get(url, headers=extra_headers or {})
                html = resp.text or ""
                if resp.status_code >= 400:
                    log.warning("httpx %d for %s", resp.status_code, url)
                if not _looks_js_required(html) and resp.status_code < 400:
                    return FetchResult(
                        url=str(resp.url),
                        html=html,
                        tier="httpx",
                        status=resp.status_code,
                        a_count=len(_A_TAG.findall(html)),
                    )
                log.info("escalating to playwright: %s", url)
            except (httpx.HTTPError, httpx.TimeoutException) as e:
                log.warning("httpx failed for %s: %s — escalating to playwright", url, e)

        return await self._fetch_playwright(url, extra_headers=extra_headers)

    async def _ensure_browser(self) -> None:
        if self._browser is not None:
            return
        try:
            from playwright.async_api import async_playwright  # local import keeps
            # the module importable in test envs where playwright isn't present.
        except ImportError as e:
            log.error("playwright not installed: %s", e)
            return
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=True)

    async def _fetch_playwright(
        self,
        url: str,
        extra_headers: Optional[dict[str, str]] = None,
    ) -> Optional[FetchResult]:
        await self._polite_wait(url)
        await self._ensure_browser()
        if self._browser is None:
            return None
        context = await self._browser.new_context(
            user_agent=self._ua,
            extra_http_headers=extra_headers or {},
        )
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="networkidle", timeout=PLAYWRIGHT_TIMEOUT_MS)
            html = await page.content()
            return FetchResult(
                url=page.url,
                html=html,
                tier="playwright",
                status=200,
                a_count=len(_A_TAG.findall(html)),
            )
        except Exception as e:
            log.warning("playwright failed for %s: %s", url, e)
            return None
        finally:
            await context.close()
