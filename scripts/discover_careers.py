"""
Discover the canonical scraping method for each company in companies/seed_500.yaml
by inspecting their official /careers page HTML.

Outputs:
  companies/catalog.yaml             — discovery results for every company
  companies/catalog_mismatches.yaml  — entries where seed disagrees with discovery

Run:
  cd /Users/saikrishnanarvaneni/jobseeker && python3 scripts/discover_careers.py
"""
from __future__ import annotations

import asyncio
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
SEED_PATH = ROOT / "companies" / "seed_500.yaml"
CATALOG_PATH = ROOT / "companies" / "catalog.yaml"
MISMATCH_PATH = ROOT / "companies" / "catalog_mismatches.yaml"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
TIMEOUT = httpx.Timeout(15.0, connect=10.0)
MAX_CONCURRENCY = 5
PER_DOMAIN_DELAY_S = 0.5

CANDIDATE_PATHS = [
    "https://{domain}/careers",
    "https://{domain}/jobs",
    "https://{domain}/about/careers",
    "https://{domain}/about/jobs",
    "https://{domain}/company/careers",
    "https://{domain}/work-with-us",
    "https://{domain}/join-us",
    "https://careers.{domain}",
    "https://jobs.{domain}",
    "https://corporate.{domain}/careers",
    "https://corp.{domain}/careers",
    "https://www.{domain}/careers",
]


def normalize_domain(domain: str) -> tuple[str, str]:
    """Return (root_domain_without_www, original_domain). Strips a single leading www."""
    d = domain.strip().lower()
    if d.startswith("www."):
        d = d[4:]
    return d, domain.strip().lower()

GREENHOUSE_RX = [
    re.compile(r"boards(?:-api)?\.greenhouse\.io/(?:embed/job_board\?for=)?([a-zA-Z0-9_\-]+)"),
    re.compile(r"job-boards\.greenhouse\.io/([a-zA-Z0-9_\-]+)"),
]
LEVER_RX = re.compile(r"jobs\.(?:eu\.)?lever\.co/([a-zA-Z0-9_\-]+)")
ASHBY_RX = [
    re.compile(r"jobs\.ashbyhq\.com/([a-zA-Z0-9_\-\.]+)"),
    re.compile(r"api\.ashbyhq\.com/posting-api/job-board/([a-zA-Z0-9_\-\.]+)"),
]
WORKDAY_RX = re.compile(
    r"https?://([a-zA-Z0-9\-]+)\.(wd[1-9])?\.?myworkdayjobs\.com/(?:wday/cxs/[^/]+/)?([a-zA-Z0-9_\-]+)"
)
SMARTRECRUITERS_RX = [
    re.compile(r"jobs\.smartrecruiters\.com/([a-zA-Z0-9_\-]+)"),
    re.compile(r"api\.smartrecruiters\.com/v1/companies/([a-zA-Z0-9_\-]+)/postings"),
    re.compile(r"careers\.smartrecruiters\.com/([a-zA-Z0-9_\-]+)"),
]
TALEO_RX = re.compile(r"([a-zA-Z0-9\-]+)\.taleo\.net")
ICIMS_RX = re.compile(r"([a-zA-Z0-9\-]+)\.icims\.com")
BAMBOO_RX = re.compile(r"([a-zA-Z0-9\-]+)\.bamboohr\.com/(?:jobs|careers)")
JAZZHR_RX = re.compile(r"([a-zA-Z0-9\-]+)\.applytojob\.com")
RECRUITEE_RX = re.compile(r"([a-zA-Z0-9\-]+)\.recruitee\.com")
JOBVITE_RX = re.compile(r"jobs\.jobvite\.com/([a-zA-Z0-9_\-]+)")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_seed() -> list[dict[str, Any]]:
    with SEED_PATH.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return data.get("companies", []) or []


def candidate_urls(domain: str) -> list[str]:
    root, original = normalize_domain(domain)
    urls: list[str] = []
    if original.startswith("careers.") or original.startswith("jobs.") or original.endswith(".jobs"):
        urls.append(f"https://{original}/")
        urls.append(f"https://{original}/careers")
        urls.append(f"https://{original}/jobs")
        return urls
    seen: set[str] = set()
    for tmpl in CANDIDATE_PATHS:
        u = tmpl.format(domain=root)
        if u not in seen:
            urls.append(u)
            seen.add(u)
    return urls


def detect_ats(html: str, final_url: str) -> dict[str, Any] | None:
    haystack = html + " " + final_url

    for rx in GREENHOUSE_RX:
        m = rx.search(haystack)
        if m:
            slug = m.group(1)
            if slug.lower() in {"embed", "job_boards", "boards", "static"}:
                continue
            return {
                "detected_ats": "greenhouse",
                "detected_board_id": slug,
                "api_endpoint": f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
            }

    m = LEVER_RX.search(haystack)
    if m:
        slug = m.group(1)
        return {
            "detected_ats": "lever",
            "detected_board_id": slug,
            "api_endpoint": f"https://api.lever.co/v0/postings/{slug}?mode=json",
        }

    for rx in ASHBY_RX:
        m = rx.search(haystack)
        if m:
            slug = m.group(1)
            return {
                "detected_ats": "ashby",
                "detected_board_id": slug,
                "api_endpoint": f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
            }

    m = WORKDAY_RX.search(haystack)
    if m:
        tenant = m.group(1)
        board = m.group(3)
        if board.lower() in {"wday", "cxs"}:
            board = "External"
        return {
            "detected_ats": "workday",
            "detected_board_id": tenant,
            "detected_workday_tenant": tenant,
            "detected_workday_board": board,
            "api_endpoint": f"https://{tenant}.wd1.myworkdayjobs.com/wday/cxs/{tenant}/{board}/jobs",
        }

    for rx in SMARTRECRUITERS_RX:
        m = rx.search(haystack)
        if m:
            slug = m.group(1)
            return {
                "detected_ats": "smartrecruiters",
                "detected_board_id": slug,
                "api_endpoint": f"https://api.smartrecruiters.com/v1/companies/{slug}/postings",
            }

    m = JOBVITE_RX.search(haystack)
    if m:
        slug = m.group(1)
        return {
            "detected_ats": "jobvite",
            "detected_board_id": slug,
            "api_endpoint": f"https://jobs.jobvite.com/{slug}",
        }

    m = ICIMS_RX.search(haystack)
    if m:
        return {
            "detected_ats": "icims",
            "detected_board_id": m.group(1),
            "api_endpoint": f"https://{m.group(1)}.icims.com/jobs",
        }

    m = TALEO_RX.search(haystack)
    if m:
        return {
            "detected_ats": "taleo",
            "detected_board_id": m.group(1),
            "api_endpoint": f"https://{m.group(1)}.taleo.net",
        }

    m = BAMBOO_RX.search(haystack)
    if m:
        return {
            "detected_ats": "bamboohr",
            "detected_board_id": m.group(1),
            "api_endpoint": f"https://{m.group(1)}.bamboohr.com/jobs",
        }

    m = JAZZHR_RX.search(haystack)
    if m:
        return {
            "detected_ats": "jazzhr",
            "detected_board_id": m.group(1),
            "api_endpoint": f"https://{m.group(1)}.applytojob.com",
        }

    m = RECRUITEE_RX.search(haystack)
    if m:
        return {
            "detected_ats": "recruitee",
            "detected_board_id": m.group(1),
            "api_endpoint": f"https://{m.group(1)}.recruitee.com/api/offers/",
        }

    return None


def confidence_for(html: str, detected: dict[str, Any]) -> str:
    ats = detected["detected_ats"]
    soup = BeautifulSoup(html, "html.parser")
    iframe_hit = False
    script_hit = False

    markers = {
        "greenhouse": "greenhouse",
        "lever": "lever.co",
        "ashby": "ashby",
        "workday": "myworkdayjobs",
        "smartrecruiters": "smartrecruiters",
        "icims": "icims",
        "taleo": "taleo",
        "bamboohr": "bamboohr",
        "jazzhr": "applytojob",
        "recruitee": "recruitee",
        "jobvite": "jobvite",
    }
    needle = markers.get(ats, ats)

    for iframe in soup.find_all("iframe"):
        src = (iframe.get("src") or "").lower()
        if needle in src:
            iframe_hit = True
            break

    for script in soup.find_all("script"):
        src = (script.get("src") or "").lower()
        if needle in src:
            script_hit = True
            break
        text = (script.string or "").lower() if script.string else ""
        if needle in text:
            script_hit = True
            break

    if iframe_hit and script_hit:
        return "high"
    if iframe_hit or script_hit:
        return "high"
    return "medium"


async def fetch_first_ok(
    client: httpx.AsyncClient, urls: list[str]
) -> tuple[str | None, str | None, str | None]:
    last_err: str | None = None
    for url in urls:
        try:
            r = await client.get(url, headers=HEADERS, follow_redirects=True)
            if r.status_code == 200 and r.text:
                return url, str(r.url), r.text
            last_err = f"{url} -> HTTP {r.status_code}"
        except httpx.HTTPError as e:
            last_err = f"{url} -> {type(e).__name__}: {e}"
        except Exception as e:
            last_err = f"{url} -> {type(e).__name__}: {e}"
        await asyncio.sleep(PER_DOMAIN_DELAY_S)
    return None, None, last_err


async def discover_one(
    sem: asyncio.Semaphore, client: httpx.AsyncClient, company: dict[str, Any]
) -> dict[str, Any]:
    name = company.get("name", "?")
    domain = company.get("domain", "")
    result: dict[str, Any] = {
        "name": name,
        "domain": domain,
        "seed_ats": company.get("ats"),
        "seed_board_id": company.get("board_id"),
    }
    if not domain:
        result["error"] = "missing domain"
        result["detected_ats"] = "undiscovered"
        return result

    async with sem:
        # Honor a seed-provided careers URL when it's a real override (not the
        # auto-generated https://{domain}/careers default). Many big companies
        # don't follow that pattern (Apple -> jobs.apple.com, Walmart ->
        # corporate.walmart.com/careers, Amazon -> amazon.jobs, etc).
        seed_url = company.get("career_url", "").strip()
        default_url = f"https://{domain.lstrip('www.')}/careers"
        urls: list[str]
        if seed_url and seed_url != default_url and seed_url != f"https://{domain}/careers":
            urls = [seed_url] + candidate_urls(domain)
        else:
            urls = candidate_urls(domain)
        tried_url, final_url, html_or_err = await fetch_first_ok(client, urls)
        if tried_url is None or final_url is None:
            result["error"] = html_or_err or "no candidate returned 200"
            result["detected_ats"] = "undiscovered"
            return result

        html = html_or_err or ""
        result["careers_url"] = tried_url
        result["final_url"] = final_url

        detected = detect_ats(html, final_url)
        if detected is None:
            result["detected_ats"] = "custom_html"
            result["confidence"] = "medium"
            result["last_verified"] = now_iso()
            return result

        conf = confidence_for(html, detected)
        result.update(detected)
        result["confidence"] = conf
        result["last_verified"] = now_iso()
        return result


async def run_all(companies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sem = asyncio.Semaphore(MAX_CONCURRENCY)
    limits = httpx.Limits(max_connections=MAX_CONCURRENCY * 2, max_keepalive_connections=MAX_CONCURRENCY)
    async with httpx.AsyncClient(timeout=TIMEOUT, limits=limits, http2=False) as client:
        tasks = [discover_one(sem, client, c) for c in companies]
        results: list[dict[str, Any]] = []
        done = 0
        total = len(tasks)
        for coro in asyncio.as_completed(tasks):
            r = await coro
            done += 1
            tag = r.get("detected_ats", "?")
            extra = ""
            if r.get("detected_board_id"):
                extra = f" board={r['detected_board_id']}"
            print(f"[{done:3d}/{total}] {r.get('name','?'):<30} -> {tag}{extra}", flush=True)
            results.append(r)
    results.sort(key=lambda x: x.get("name", "").lower())
    return results


def build_catalog_entry(r: dict[str, Any]) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "name": r.get("name"),
        "domain": r.get("domain"),
    }
    for k in (
        "careers_url",
        "final_url",
        "detected_ats",
        "detected_board_id",
        "detected_workday_tenant",
        "detected_workday_board",
        "api_endpoint",
        "confidence",
        "last_verified",
    ):
        if r.get(k) is not None:
            entry[k] = r[k]
    return entry


def find_mismatches(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    mismatches: list[dict[str, Any]] = []
    for r in results:
        seed_ats = r.get("seed_ats")
        seed_board = r.get("seed_board_id")
        det_ats = r.get("detected_ats")
        det_board = r.get("detected_board_id")
        if not seed_ats or det_ats in (None, "undiscovered", "custom_html"):
            continue
        ats_diff = seed_ats != det_ats
        board_diff = bool(det_board) and bool(seed_board) and seed_board.lower() != det_board.lower()
        if ats_diff or board_diff:
            mismatches.append(
                {
                    "name": r.get("name"),
                    "domain": r.get("domain"),
                    "seed_ats": seed_ats,
                    "seed_board_id": seed_board,
                    "detected_ats": det_ats,
                    "detected_board_id": det_board,
                    "detected_workday_tenant": r.get("detected_workday_tenant"),
                    "detected_workday_board": r.get("detected_workday_board"),
                    "confidence": r.get("confidence"),
                    "careers_url": r.get("careers_url"),
                    "final_url": r.get("final_url"),
                    "api_endpoint": r.get("api_endpoint"),
                    "kind": "ats_change" if ats_diff else "board_id_change",
                }
            )
    return mismatches


def write_yaml(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(payload, fh, sort_keys=False, default_flow_style=False, width=120)


def main() -> int:
    started = time.time()
    companies = load_seed()
    total = len(companies)
    print(f"Loaded {total} companies from {SEED_PATH}", flush=True)

    results = asyncio.run(run_all(companies))

    by_ats: dict[str, int] = defaultdict(int)
    errors: list[dict[str, Any]] = []
    catalog_entries: list[dict[str, Any]] = []
    discovered_count = 0
    for r in results:
        ats = r.get("detected_ats") or "undiscovered"
        by_ats[ats] += 1
        if ats not in ("undiscovered",):
            discovered_count += 1
        if r.get("error"):
            errors.append(
                {
                    "name": r.get("name"),
                    "domain": r.get("domain"),
                    "error": r.get("error"),
                }
            )
        catalog_entries.append(build_catalog_entry(r))

    catalog = {
        "generated_at": now_iso(),
        "total": total,
        "discovered": discovered_count,
        "summary": dict(sorted(by_ats.items(), key=lambda kv: -kv[1])),
        "companies": catalog_entries,
        "errors": errors,
    }
    write_yaml(CATALOG_PATH, catalog)

    mismatches = find_mismatches(results)
    seed_counts: dict[str, int] = defaultdict(int)
    for c in companies:
        seed_counts[c.get("ats", "?")] += 1

    mismatch_payload = {
        "generated_at": now_iso(),
        "total_mismatches": len(mismatches),
        "seed_counts": dict(sorted(seed_counts.items(), key=lambda kv: -kv[1])),
        "discovered_counts": dict(sorted(by_ats.items(), key=lambda kv: -kv[1])),
        "mismatches": mismatches,
    }
    write_yaml(MISMATCH_PATH, mismatch_payload)

    elapsed = time.time() - started
    print()
    print(f"Discovery complete: {total} companies in {elapsed:.1f}s")
    for ats, count in sorted(by_ats.items(), key=lambda kv: -kv[1]):
        seed_n = seed_counts.get(ats)
        suffix = ""
        if seed_n is not None and ats not in ("custom_html", "undiscovered"):
            mismatch_n = sum(1 for m in mismatches if m["detected_ats"] == ats)
            suffix = f" (current seed has {seed_n} -> {mismatch_n} mismatches)"
        print(f"  - {ats:<18} {count}{suffix}")
    print()
    print(f"Mismatches written to {MISMATCH_PATH}")
    print(f"Catalog written to {CATALOG_PATH}")
    print(f"Errors: {len(errors)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
