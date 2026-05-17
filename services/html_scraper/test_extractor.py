"""Tests for the Ollama-driven extractor.

These tests do NOT call the real Ollama backend. We patch
`services.html_scraper.extractor._call_ollama` so the test runs offline and
quickly. The integration with the real model is exercised by running the
service against a fixture page in dev.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.html_scraper import extractor  # noqa: E402
from services.html_scraper.extractor import (  # noqa: E402
    extract_jobs,
    parse_extractor_payload,
    sanity_cap,
)
from services.html_scraper.models import ExtractedJob  # noqa: E402
from services.html_scraper.publisher import (  # noqa: E402
    canonicalize_url,
    normalize,
    stable_source_id,
)

FIXTURE_HTML = """\
<html>
<body>
  <main>
    <ul class="jobs">
      <li class="job-card">
        <h3><a href="/careers/jobs/12345-platform-engineer">Senior Platform Engineer</a></h3>
        <span class="loc">Bentonville, AR</span>
        <span class="dept">Walmart Global Tech</span>
      </li>
      <li class="job-card">
        <h3><a href="/careers/jobs/67890-sre">Site Reliability Engineer II</a></h3>
        <span class="loc">Sunnyvale, CA (Remote)</span>
        <span class="dept">Walmart Global Tech</span>
      </li>
    </ul>
  </main>
</body>
</html>
"""

NO_JOBS_HTML = """\
<html><head><title>About us</title></head>
<body>
  <h1>About Acme</h1>
  <p>We are passionate about widgets. No jobs are open at this time.</p>
  <a href="/about">Learn more</a>
</body>
</html>
"""


@pytest.mark.asyncio
async def test_empty_html_returns_empty_list() -> None:
    """Extractor short-circuits on empty / whitespace input — never calls Ollama."""
    out = await extract_jobs("", "Walmart", "https://careers.walmart.com")
    assert out == []
    out = await extract_jobs("   \n\t ", "Walmart", "https://careers.walmart.com")
    assert out == []


@pytest.mark.asyncio
async def test_extractor_refuses_to_invent_jobs(monkeypatch) -> None:
    """When the page has no postings, the (well-prompted) model returns []."""
    async def fake(prompt: str, model: str = "") -> str:
        # Simulate a well-behaved model: empty list is the correct answer.
        return json.dumps({"jobs": []})

    monkeypatch.setattr(extractor, "_call_ollama", fake)
    out = await extract_jobs(NO_JOBS_HTML, "Acme", "https://acme.com")
    assert out == []


@pytest.mark.asyncio
async def test_extractor_parses_fixture(monkeypatch) -> None:
    """End-to-end: a real-looking page round-trips through the extractor."""
    async def fake(prompt: str, model: str = "") -> str:
        # The model would extract these from FIXTURE_HTML.
        return json.dumps({
            "jobs": [
                {
                    "title": "Senior Platform Engineer",
                    "url": "/careers/jobs/12345-platform-engineer",
                    "location": "Bentonville, AR",
                    "department": "Walmart Global Tech",
                    "posted_at": "",
                    "remote": False,
                    "description_text": "",
                },
                {
                    "title": "Site Reliability Engineer II",
                    "url": "/careers/jobs/67890-sre",
                    "location": "Sunnyvale, CA (Remote)",
                    "department": "Walmart Global Tech",
                    "posted_at": "",
                    "remote": True,
                    "description_text": "",
                },
            ]
        })

    monkeypatch.setattr(extractor, "_call_ollama", fake)
    out = await extract_jobs(FIXTURE_HTML, "Walmart", "https://careers.walmart.com")
    assert len(out) == 2
    titles = {j.title for j in out}
    assert "Senior Platform Engineer" in titles
    assert "Site Reliability Engineer II" in titles


def test_source_id_is_stable_across_runs() -> None:
    """Same canonical URL must hash to the same source_id every time."""
    url1 = "https://careers.walmart.com/us/jobs/WD12345-senior-platform-engineer"
    url2 = "https://careers.walmart.com/us/jobs/WD12345-senior-platform-engineer"
    assert stable_source_id(url1) == stable_source_id(url2)
    assert len(stable_source_id(url1)) == 16

    # Different URLs must produce different IDs.
    url3 = "https://careers.walmart.com/us/jobs/WD67890-sre-ii"
    assert stable_source_id(url1) != stable_source_id(url3)

    # Canonicalisation drops fragments and tracking params — so a tracked URL
    # and the canonical one share an ID.
    canonical = canonicalize_url(
        "/us/jobs/WD12345-senior-platform-engineer?utm_source=linkedin#apply",
        "https://careers.walmart.com",
    )
    assert stable_source_id(canonical) == stable_source_id(url1)


def test_sanity_cap_rejects_hallucinated_output() -> None:
    """A page with 5 <a> tags returning 999 jobs must be dropped."""
    html = "<html><body>" + ("<a href=\"x\">x</a>" * 5) + "</body></html>"
    fake_jobs = [
        ExtractedJob(title=f"Job {i}", url=f"/jobs/{i}") for i in range(999)
    ]
    kept, dropped = sanity_cap(fake_jobs, html)
    assert dropped is True
    assert kept == []

    # Reasonable output passes (5 <a> tags allow up to max(3*5, 200) = 200).
    sane_jobs = [ExtractedJob(title=f"Job {i}", url=f"/jobs/{i}") for i in range(10)]
    kept, dropped = sanity_cap(sane_jobs, html)
    assert dropped is False
    assert len(kept) == 10


def test_normalize_drops_rows_without_title_or_url() -> None:
    """A row missing required fields shouldn't be published."""
    bad_no_url = ExtractedJob(title="Engineer", url="", location="Remote")
    bad_no_title = ExtractedJob(title="", url="https://x.com/jobs/1")
    assert normalize(bad_no_url, "Acme", "https://acme.com") is None
    assert normalize(bad_no_title, "Acme", "https://acme.com") is None

    good = ExtractedJob(
        title="Platform Engineer",
        url="/jobs/42",
        location="Bentonville, AR",
        description_text="Kubernetes, Terraform.",
    )
    out = normalize(good, "Walmart", "https://careers.walmart.com")
    assert out is not None
    assert out.url == "https://careers.walmart.com/jobs/42"
    assert out.source == "html"
    assert out.company == "Walmart"
    assert out.source_id == stable_source_id(out.url)


def test_parse_extractor_payload_handles_fenced_json() -> None:
    """Some models wrap JSON in ```json fences — make sure we strip them."""
    raw = '```json\n{"jobs": [{"title": "Engineer", "url": "/x"}]}\n```'
    out = parse_extractor_payload(raw)
    assert len(out) == 1
    assert out[0].title == "Engineer"

    # And tolerates a bare array.
    raw2 = '[{"title": "Eng", "url": "/x"}]'
    out2 = parse_extractor_payload(raw2)
    assert len(out2) == 1
