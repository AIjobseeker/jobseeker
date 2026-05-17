"""LLM-driven job extraction from raw HTML.

We do NOT try to write per-site CSS selectors. Instead we hand a trimmed copy
of the page to a small local model (qwen3:8b by default) with a strict prompt
that demands a JSON object of the form {"jobs": [...]}. The prompt explicitly
instructs the model to return an empty list when no jobs are present.

Two layers of defence against hallucinations:
1. The prompt itself ("If you cannot extract any jobs from this HTML, return
   {\"jobs\": []}. Do not invent jobs.").
2. A sanity cap: if Ollama returns more rows than max(3 * <a>-tag count, 200)
   we drop the entire result and log a warning. Real career pages have many
   <a> tags per job (apply link, share, etc.), so this is a generous bound.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Optional

from services.html_scraper.models import ExtractedJob

log = logging.getLogger("html_scraper.extractor")

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://100.115.111.9:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL_HTML", "qwen3:8b")
MAX_HTML_CHARS = int(os.getenv("HTML_EXTRACT_MAX_CHARS", "60000"))
MAX_JOBS_HARD_CAP = int(os.getenv("HTML_EXTRACT_HARD_CAP", "200"))

PROMPT_TEMPLATE = """\
You are a precise data extractor. The HTML below is from a company careers
page for {company}. Extract every job posting that is clearly listed on this
page.

Rules — follow them strictly:
- Output ONLY a JSON object of the form: {{"jobs": [...]}}
- Each job object MUST have these keys: title, url, location, department,
  posted_at, remote, description_text. Use empty string "" or false when a
  field is missing — never invent values.
- url MUST be an absolute URL. If the link in the HTML is relative (starts
  with /), prepend "{base_url}".
- remote MUST be true if the location text contains "remote" / "anywhere",
  else false.
- If you cannot extract any jobs from this HTML, return {{"jobs": []}}.
- Do NOT include navigation links, blog posts, or "Apply now" buttons that
  are not tied to a specific role.
- Do NOT invent jobs. If unsure, leave the list empty.
- No markdown, no commentary, no <think> tags — JSON only.

HTML (truncated):
{html}
"""

_JSON_FENCE = re.compile(r"```(?:json)?\s*|\s*```", re.IGNORECASE)
_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_A_TAG = re.compile(r"<a[\s>]", re.IGNORECASE)


def _clean_response(raw: str) -> str:
    raw = _THINK_BLOCK.sub("", raw).strip()
    raw = _JSON_FENCE.sub("", raw).strip()
    # Tolerate the model returning a JSON array directly.
    if raw.startswith("["):
        return '{"jobs": ' + raw + "}"
    return raw


def _trim_html(html: str, limit: int = MAX_HTML_CHARS) -> str:
    """Strip <script>/<style> blobs and clamp to `limit` chars.

    Career pages frequently embed huge JSON-LD or React state blobs that bury
    the actual posting markup. Removing them keeps Ollama's context budget
    focused on the job cards.
    """
    cleaned = re.sub(r"<script\b[^>]*>.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"<style\b[^>]*>.*?</style>", " ", cleaned, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"<!--.*?-->", " ", cleaned, flags=re.DOTALL)
    cleaned = re.sub(r"\s+", " ", cleaned)
    if len(cleaned) > limit:
        cleaned = cleaned[:limit]
    return cleaned


async def _call_ollama(prompt: str, model: str = OLLAMA_MODEL) -> str:
    """Thin async wrapper around the ollama AsyncClient."""
    from ollama import AsyncClient

    client = AsyncClient(host=OLLAMA_HOST)
    resp = await client.chat(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        options={"num_predict": 4096, "temperature": 0.0},
        think=False,
    )
    return resp.message.content or ""


def parse_extractor_payload(raw: str) -> list[ExtractedJob]:
    """Parse the model's output. Always returns a list (possibly empty)."""
    cleaned = _clean_response(raw)
    if not cleaned:
        return []
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        # Some models wrap the JSON in extra text — try to recover the first
        # {...} object greedily.
        match = re.search(r"\{[\s\S]*\}", cleaned)
        if not match:
            log.warning("extractor: no JSON object in response; got %r", cleaned[:200])
            return []
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError as e:
            log.warning("extractor: invalid JSON: %s", e)
            return []

    rows = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return []

    out: list[ExtractedJob] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        # Defaults that match each field's expected type — pydantic refuses to
        # coerce "" into a bool, so we hand it real defaults.
        kwargs = {
            "title": row.get("title", "") or "",
            "url": row.get("url", "") or "",
            "location": row.get("location", "") or "",
            "department": row.get("department") or None,
            "posted_at": row.get("posted_at") or None,
            "remote": bool(row.get("remote", False)) if row.get("remote") not in ("", None) else False,
            "description_text": row.get("description_text", "") or "",
        }
        try:
            out.append(ExtractedJob(**kwargs))
        except Exception as e:  # pragma: no cover - pydantic v2 is permissive
            log.debug("dropping bad row %s: %s", row, e)
    return out


def sanity_cap(jobs: list[ExtractedJob], html: str) -> tuple[list[ExtractedJob], bool]:
    """Reject obviously-hallucinated payloads.

    Returns (jobs, dropped). When dropped is True the caller should treat the
    extraction as a failure and not publish anything.
    """
    a_count = len(_A_TAG.findall(html))
    cap = max(3 * a_count, MAX_JOBS_HARD_CAP)
    if len(jobs) > cap:
        log.warning(
            "sanity cap: dropping %d jobs (cap=%d, a_tags=%d)",
            len(jobs),
            cap,
            a_count,
        )
        return [], True
    return jobs, False


async def extract_jobs(
    html: str,
    company: str,
    base_url: str,
    model: Optional[str] = None,
) -> list[ExtractedJob]:
    """Run the extractor end-to-end. Empty list on any failure."""
    if not html or not html.strip():
        return []
    trimmed = _trim_html(html)
    prompt = PROMPT_TEMPLATE.format(company=company, base_url=base_url, html=trimmed)
    try:
        raw = await _call_ollama(prompt, model=model or OLLAMA_MODEL)
    except Exception as e:
        log.warning("ollama call failed for %s: %s", company, e)
        return []
    jobs = parse_extractor_payload(raw)
    jobs, dropped = sanity_cap(jobs, html)
    if dropped:
        return []
    return jobs
