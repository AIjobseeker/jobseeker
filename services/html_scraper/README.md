# html_scraper

LLM-driven HTML scraper for the ~55 custom-ATS companies in
`companies/seed_500.yaml` whose careers pages are static HTML or JS-rendered
SPAs without a public JSON endpoint (Walmart, Disney, Costco, Wells Fargo,
Best Buy, AT&T, Verizon, NBCU, Paramount, USAA, Sony, Marriott-tier custom).

## Architecture

1. `config.py` filters seed_500 to `ats == custom` AND custom_module not in the
   Go scraper's hardcoded list (`amazon`, `apple`, `google`, `meta`).
2. `fetcher.py` — `httpx` first, falls back to Playwright (chromium headless)
   when JS is required (config flag, `<noscript>` hint, or SPA bootstrap).
3. `extractor.py` — strict prompt to Ollama (`OLLAMA_MODEL_HTML`, default
   `qwen3:8b`) returning `{"jobs": [...]}`. Drops the result when row count
   exceeds `max(3 * <a>-tag count, 200)`.
4. `publisher.py` — normalises into the JobPost shape (matching Go's
   `models.Job`), publishes to NATS subject `jobs.raw` via JetStream when
   available. `source_id = sha1(canonical_url)[:16]` so the notifier dedups
   correctly across runs.
5. `main.py` — fan-out (max 3 concurrent companies), 1.5 s polite delay per
   domain, per-cycle loop on `HTML_SCRAPER_INTERVAL` (default 1800 s).

## Environment

| var                       | default                          |
|---------------------------|----------------------------------|
| `NATS_URL`                | `nats://localhost:4222`          |
| `OLLAMA_HOST`             | `http://100.115.111.9:11434`     |
| `OLLAMA_MODEL_HTML`       | `qwen3:8b`                       |
| `HTML_SCRAPER_CONCURRENCY`| `3`                              |
| `HTML_SCRAPER_INTERVAL`   | `1800`                           |
| `HTML_SCRAPER_RUN_ONCE`   | `0`                              |
| `SEED_PATH`               | `companies/seed_500.yaml`        |
| `HTML_TARGETS_PATH`       | `companies/html_targets.yaml`    |

## Running

```bash
make up                           # starts as part of the streaming pipeline
make html-scraper-logs            # tail logs
docker compose run --rm html-scraper python -m services.html_scraper.main \
    -e HTML_SCRAPER_RUN_ONCE=1    # single cycle (debug)
```

## Tests

```bash
python3 -m pytest services/html_scraper/ -v
```

Tests cover: empty HTML, no-jobs page, fixture extraction (parse-only),
source_id stability, sanity-cap rejection, httpx_mock fetcher behaviour.
