"""
Test a specific ATS connector from the command line.

Usage:
  python scripts/test_connector.py --ats greenhouse --board stripe
  python scripts/test_connector.py --ats lever --board netflix
  python scripts/test_connector.py --ats custom --module connectors.custom.apple
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, "/app")

from shared.models import ATSType, CompanyConfig, CustomScraperConfig


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ats", required=True, choices=["greenhouse", "lever", "ashby", "workday", "custom"])
    parser.add_argument("--board", required=True)
    parser.add_argument("--module", default="")
    args = parser.parse_args()

    config = CompanyConfig(
        name=args.board.title(),
        ats=ATSType(args.ats),
        board_id=args.board,
        active=True,
        keywords_include=[],
        keywords_exclude=["intern"],
        custom=CustomScraperConfig(scraper_module=args.module) if args.module else None,
    )

    from connectors import get_connector
    connector = get_connector(config)

    print(f"Testing {args.ats} connector for board: {args.board}")
    jobs = await connector.fetch_jobs()
    print(f"\nFound {len(jobs)} jobs")

    if jobs:
        print("\nFirst 3 jobs:")
        for j in jobs[:3]:
            print(f"  [{j.source}] {j.title} @ {j.company} — {j.location}")
            print(f"    URL: {j.url}")
            print(f"    Posted: {j.posted_at}")
            print()


if __name__ == "__main__":
    asyncio.run(main())
