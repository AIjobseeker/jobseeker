#!/usr/bin/env python3
"""
view-jobs.py — Terminal viewer for scraped jobs NDJSON output.

Usage:
    # Run scraper and pipe to viewer:
    /tmp/go-scraper --once --mode stdout --seed ../../companies/seed_500.yaml 2>/tmp/scrape.err | python3 scripts/view-jobs.py

    # View from saved file:
    python3 scripts/view-jobs.py /tmp/jobs.jsonl

    # Filter for specific role:
    python3 scripts/view-jobs.py /tmp/jobs.jsonl --filter sre

    # Filter by company:
    python3 scripts/view-jobs.py /tmp/jobs.jsonl --company stripe
"""

import sys
import json
import argparse
from collections import Counter
from datetime import datetime

# ANSI colors
BOLD  = "\033[1m"
GREEN = "\033[32m"
CYAN  = "\033[36m"
YELLOW= "\033[33m"
RED   = "\033[31m"
RESET = "\033[0m"
DIM   = "\033[2m"

SRE_KEYWORDS = [
    "sre", "site reliability", "devops", "platform engineer",
    "infrastructure", "kubernetes", "k8s", "cloud engineer",
    "devsecops", "reliability engineer", "mlops", "data engineer",
    "systems engineer", "production engineer"
]

def load_jobs(path):
    jobs = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                j = json.loads(line)
                if "title" in j and "company" in j:
                    jobs.append(j)
            except json.JSONDecodeError:
                pass
    return jobs

def matches_filter(job, keyword):
    text = (job.get("title","") + " " + job.get("description_text","") + " " + job.get("department","")).lower()
    return keyword.lower() in text

def is_sre_role(job):
    text = (job.get("title","") + " " + job.get("department","")).lower()
    return any(kw in text for kw in SRE_KEYWORDS)

def print_summary(jobs, title="Job Summary"):
    print(f"\n{BOLD}{'═'*70}{RESET}")
    print(f"{BOLD}  {title}{RESET}")
    print(f"{BOLD}{'═'*70}{RESET}\n")

    by_company = Counter(j["company"] for j in jobs)
    by_ats = Counter(j.get("source","?") for j in jobs)
    sre_count = sum(1 for j in jobs if is_sre_role(j))

    print(f"  {BOLD}Total jobs:{RESET}     {GREEN}{len(jobs)}{RESET}")
    print(f"  {BOLD}SRE/Platform:{RESET}   {GREEN}{sre_count}{RESET}  ({sre_count*100//len(jobs) if jobs else 0}% of total)")
    print(f"  {BOLD}Companies:{RESET}      {len(by_company)}")
    print()

    print(f"  {BOLD}By ATS:{RESET}")
    for ats, count in sorted(by_ats.items(), key=lambda x: -x[1]):
        bar = "█" * (count * 30 // max(by_ats.values()))
        print(f"    {ats:20s} {CYAN}{bar}{RESET} {count}")
    print()

    print(f"  {BOLD}Top companies:{RESET}")
    for company, count in by_company.most_common(25):
        bar = "█" * (count * 30 // max(by_company.values()))
        print(f"    {company:30s} {YELLOW}{bar}{RESET} {count}")

def print_jobs(jobs, limit=50):
    print(f"\n{BOLD}{'─'*70}{RESET}")
    print(f"{BOLD}  Job Listings (showing {min(limit, len(jobs))} of {len(jobs)}){RESET}")
    print(f"{BOLD}{'─'*70}{RESET}\n")

    for i, j in enumerate(jobs[:limit]):
        title   = j.get("title","?")
        company = j.get("company","?")
        loc     = j.get("location","Remote/Unknown") or "Remote/Unknown"
        url     = j.get("url","")
        ats     = j.get("source","?")
        remote  = f"  {GREEN}[REMOTE]{RESET}" if j.get("remote") else ""

        print(f"  {BOLD}{i+1:3d}. {GREEN}{title}{RESET}")
        print(f"       {CYAN}{company}{RESET}  {DIM}[{ats}]{RESET}{remote}")
        print(f"       {DIM}{loc[:60]}{RESET}")
        if url:
            print(f"       {DIM}{url[:80]}{RESET}")
        print()

def main():
    parser = argparse.ArgumentParser(description="View scraped jobs")
    parser.add_argument("file", nargs="?", help="NDJSON jobs file (default: read stdin)")
    parser.add_argument("--filter", "-f", help="Filter by keyword in title")
    parser.add_argument("--company", "-c", help="Filter by company name")
    parser.add_argument("--sre", action="store_true", help="Show only SRE/DevOps/Platform jobs")
    parser.add_argument("--limit", "-n", type=int, default=50, help="Max jobs to show (default: 50)")
    parser.add_argument("--summary-only", "-s", action="store_true", help="Show summary only, no job list")
    args = parser.parse_args()

    # Load from file or stdin
    if args.file:
        jobs = load_jobs(args.file)
    else:
        # Read from stdin (piped output from scraper)
        import tempfile, os
        tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False)
        for line in sys.stdin:
            if line.strip().startswith("{"):
                tmp.write(line)
        tmp.close()
        jobs = load_jobs(tmp.name)
        os.unlink(tmp.name)

    if not jobs:
        print(f"{RED}No jobs found. Run the scraper first:{RESET}")
        print("  cd services/go-scraper")
        print("  go build -o /tmp/go-scraper .")
        print("  /tmp/go-scraper --once --mode file --output /tmp/jobs.jsonl --seed ../../companies/seed_500.yaml")
        print("  python3 scripts/view-jobs.py /tmp/jobs.jsonl --sre")
        sys.exit(1)

    # Apply filters
    filtered = jobs
    if args.sre:
        filtered = [j for j in filtered if is_sre_role(j)]
    if args.filter:
        filtered = [j for j in filtered if matches_filter(j, args.filter)]
    if args.company:
        filtered = [j for j in filtered if args.company.lower() in j.get("company","").lower()]

    title = "Scraped Jobs"
    if args.sre:
        title += " — SRE/DevOps/Platform"
    if args.filter:
        title += f" — '{args.filter}'"
    if args.company:
        title += f" @ {args.company}"

    print_summary(filtered, title)

    if not args.summary_only:
        print_jobs(filtered, args.limit)

    print(f"\n{DIM}Scraped at: {datetime.now().strftime('%Y-%m-%d %H:%M')}{RESET}\n")

if __name__ == "__main__":
    main()
