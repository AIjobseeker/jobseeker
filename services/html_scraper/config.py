"""Configuration loader for html_scraper.

Combines two YAML sources:

* companies/seed_500.yaml — filter to entries where ats == "custom" and the
  custom_module is NOT one of the modules implemented by the Go scraper. Those
  Go-side modules cover Apple, Google, Amazon, Meta. Everything else is fair
  game for HTML scraping.
* companies/html_targets.yaml — per-company override for URL, JS flag, and
  pagination. If a company appears in seed_500 but not in html_targets, we
  build a minimal HTMLTarget from career_url with safe defaults.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Iterable

import yaml

from services.html_scraper.models import CompanyTask, HTMLTarget

log = logging.getLogger("html_scraper.config")

# Custom modules already implemented in the Go scraper — skip them here so we
# don't double-publish.
GO_KNOWN_CUSTOM_MODULES: frozenset[str] = frozenset({"amazon", "apple", "google", "meta"})

DEFAULT_SEED_PATH = Path(os.getenv("SEED_PATH", "companies/seed_500.yaml"))
DEFAULT_TARGETS_PATH = Path(os.getenv("HTML_TARGETS_PATH", "companies/html_targets.yaml"))


def _load_yaml(path: Path) -> dict | list:
    if not path.exists():
        log.warning("yaml not found: %s", path)
        return {}
    with path.open() as fh:
        return yaml.safe_load(fh) or {}


def _seed_companies(seed_doc: dict | list) -> list[dict]:
    if isinstance(seed_doc, dict):
        return list(seed_doc.get("companies", []) or [])
    if isinstance(seed_doc, list):
        return list(seed_doc)
    return []


def _targets_index(targets_doc: dict | list) -> dict[str, dict]:
    if isinstance(targets_doc, dict):
        items = targets_doc.get("targets", []) or []
    elif isinstance(targets_doc, list):
        items = targets_doc
    else:
        items = []
    return {row.get("name"): row for row in items if isinstance(row, dict) and row.get("name")}


def _build_target(name: str, fallback_url: str, override: dict | None) -> HTMLTarget:
    base = {
        "name": name,
        "url": fallback_url,
        "js_required": False,
        "pagination": "none",
        "max_pages": 3,
    }
    if override:
        # Override wins, but fall back to the seed URL when the override omits one.
        merged = {**base, **{k: v for k, v in override.items() if v is not None}}
        if not merged.get("url"):
            merged["url"] = fallback_url
        return HTMLTarget(**merged)
    return HTMLTarget(**base)


def load_tasks(
    seed_path: Path = DEFAULT_SEED_PATH,
    targets_path: Path = DEFAULT_TARGETS_PATH,
    only: Iterable[str] | None = None,
) -> list[CompanyTask]:
    """Load and merge seed + html_targets into CompanyTask records."""
    seed_doc = _load_yaml(seed_path)
    targets_doc = _load_yaml(targets_path)
    targets = _targets_index(targets_doc)
    only_set = {s.lower() for s in only} if only else None

    tasks: list[CompanyTask] = []
    for raw in _seed_companies(seed_doc):
        if raw.get("ats") != "custom":
            continue
        module = (raw.get("custom_module") or "").strip().lower()
        if module and module in GO_KNOWN_CUSTOM_MODULES:
            continue
        name = raw.get("name") or raw.get("board_id")
        if not name:
            continue
        if only_set and name.lower() not in only_set:
            continue
        career_url = raw.get("career_url") or ""
        if not career_url:
            log.debug("skipping %s: no career_url", name)
            continue
        target = _build_target(name, career_url, targets.get(name))
        tasks.append(
            CompanyTask(
                name=name,
                domain=raw.get("domain", ""),
                career_url=career_url,
                target=target,
                keywords_include=list(raw.get("keywords_include") or []),
                keywords_exclude=list(raw.get("keywords_exclude") or []),
            )
        )

    log.info("loaded tasks: %d (seed=%s, targets=%s)", len(tasks), seed_path, targets_path)
    return tasks


def passes_keyword_filter(
    title: str,
    description: str,
    include: list[str],
    exclude: list[str],
) -> bool:
    """Cheap include/exclude title+description filter.

    Mirrors the Go scraper's passesKeywordFilter so this service produces a
    comparable set of postings.
    """
    haystack = f"{title}\n{description}".lower()
    if exclude:
        for term in exclude:
            term = term.strip().lower()
            if term and term in haystack:
                return False
    if include:
        for term in include:
            term = term.strip().lower()
            if term and term in haystack:
                return True
        return False
    return True
