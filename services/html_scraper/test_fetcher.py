"""Tests for the two-tier fetcher.

Uses pytest-httpx to stub the httpx layer. Playwright is NEVER invoked from
these tests — we assert that the fetcher escalates by mocking the
`_fetch_playwright` private method.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.html_scraper.fetcher import Fetcher, FetchResult, _looks_js_required  # noqa: E402


SIMPLE_PAGE = """\
<html><body>
  <a href="/jobs/1">Job 1</a><a href="/jobs/2">Job 2</a><a href="/jobs/3">Job 3</a>
  <a href="/jobs/4">Job 4</a><a href="/jobs/5">Job 5</a><a href="/jobs/6">Job 6</a>
  <a href="/jobs/7">Job 7</a><a href="/jobs/8">Job 8</a><a href="/jobs/9">Job 9</a>
</body></html>
"""

SPA_PAGE = """\
<html><body>
  <noscript>Please enable JavaScript to view jobs.</noscript>
  <div id="root"></div>
  <script>window.__NEXT_DATA__ = {};</script>
</body></html>
"""


@pytest.mark.asyncio
async def test_httpx_path_returns_html(httpx_mock) -> None:
    """A static HTML page is fetched via httpx with no Playwright escalation."""
    httpx_mock.add_response(
        url="https://careers.example.com/jobs",
        text=SIMPLE_PAGE,
        status_code=200,
    )
    async with Fetcher(per_domain_delay=0.0) as f:
        result = await f.fetch("https://careers.example.com/jobs")
    assert result is not None
    assert result.tier == "httpx"
    assert "Job 1" in result.html
    assert result.a_count >= 9


@pytest.mark.asyncio
async def test_escalates_to_playwright_when_noscript_hint(httpx_mock, monkeypatch) -> None:
    """A page with the JS-required hint must trigger Playwright."""
    httpx_mock.add_response(
        url="https://spa.example.com/careers",
        text=SPA_PAGE,
        status_code=200,
    )
    pw_called = {"yes": False}

    async def fake_pw(self, url, extra_headers=None):
        pw_called["yes"] = True
        return FetchResult(url=url, html="<html>rendered</html>", tier="playwright", a_count=0)

    monkeypatch.setattr(Fetcher, "_fetch_playwright", fake_pw)

    async with Fetcher(per_domain_delay=0.0) as f:
        result = await f.fetch("https://spa.example.com/careers")

    assert pw_called["yes"] is True
    assert result is not None
    assert result.tier == "playwright"
    assert result.html == "<html>rendered</html>"


@pytest.mark.asyncio
async def test_force_js_skips_httpx(monkeypatch) -> None:
    """force_js=True must NOT hit httpx at all."""
    httpx_calls = {"n": 0}

    async def fake_pw(self, url, extra_headers=None):
        return FetchResult(url=url, html="<html>js-rendered</html>", tier="playwright", a_count=0)

    monkeypatch.setattr(Fetcher, "_fetch_playwright", fake_pw)

    async with Fetcher(per_domain_delay=0.0) as f:
        # patch the client.get so we'd see if it was called
        original_get = f._client.get  # type: ignore[union-attr]

        async def counting_get(*args, **kwargs):
            httpx_calls["n"] += 1
            return await original_get(*args, **kwargs)

        f._client.get = counting_get  # type: ignore[union-attr,assignment]
        result = await f.fetch("https://spa.example.com/careers", force_js=True)

    assert httpx_calls["n"] == 0
    assert result is not None
    assert result.tier == "playwright"


def test_looks_js_required_detection() -> None:
    """Heuristics for SPA detection."""
    assert _looks_js_required(SPA_PAGE) is True
    assert _looks_js_required(SIMPLE_PAGE) is False
    # Page with very few <a> tags AND React bootstrap → JS required.
    react_page = '<html><body><div data-reactroot></div><a href="/x">x</a></body></html>'
    assert _looks_js_required(react_page) is True
