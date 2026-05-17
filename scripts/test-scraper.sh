#!/usr/bin/env bash
# Quick smoke test: scrape all companies once, output to jobs.jsonl
set -euo pipefail

cd "$(dirname "$0")/../services/go-scraper"

echo "==> Building go-scraper..."
go build -o /tmp/go-scraper .

echo "==> Scraping 149 companies (once, stdout mode)..."
/tmp/go-scraper \
  --once \
  --mode file \
  --output /tmp/jobs.jsonl \
  --seed ../../companies/seed_500.yaml

echo ""
echo "==> Results:"
echo "    Total jobs: $(wc -l < /tmp/jobs.jsonl)"
echo "    Sample (first 3 jobs):"
head -3 /tmp/jobs.jsonl | python3 -m json.tool --no-ensure-ascii | grep -E '"(company|title|url)"' | head -20
echo ""
echo "==> Output file: /tmp/jobs.jsonl"
