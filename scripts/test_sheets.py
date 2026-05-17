#!/usr/bin/env python3
"""Smoke test: write one row to the configured Google Sheet.

  python3 scripts/test_sheets.py [--person sai] [--dry-run]

Validates:
  - GOOGLE_SERVICE_ACCOUNT_JSON parses
  - Sheet ID resolves
  - Service account has access (the sheet must be shared with the
    client_email from the JSON, with at least Editor permission)
  - Header row is written
  - A test row appends successfully
  - Upsert (idempotency): writing the same dedup_id twice updates, not appends.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# Allow .env to win over Docker-style defaults when run locally.
try:
    from dotenv import load_dotenv  # type: ignore[import-not-found]

    load_dotenv(REPO / ".env", override=False)
except ImportError:
    pass

# Reset the cached settings since we may have just loaded .env above.
import shared.config as cfg  # noqa: E402

cfg.get_settings.cache_clear()
cfg.settings = cfg.get_settings()

from services.worker.activities.sheets import (  # noqa: E402
    HEADERS,
    _dedup_id,
    _ensure_headers,
    _open_sheet,
    _row_for,
    _sheet_id_for,
    _upsert_row,
)
from shared.config import settings  # noqa: E402


def color(s: str, c: str) -> str:
    return f"\033[{c}m{s}\033[0m"


def fail(msg: str, hint: str = "") -> None:
    print(color(f"FAIL: {msg}", "31"))
    if hint:
        print(color(f"  hint: {hint}", "33"))
    sys.exit(1)


def ok(msg: str) -> None:
    print(color(f"OK   {msg}", "32"))


def main() -> int:
    ap = argparse.ArgumentParser(description="Smoke-test Google Sheets sync")
    ap.add_argument("--person", default="sai", choices=["sai", "gf"])
    ap.add_argument("--dry-run", action="store_true",
                    help="Skip the write — just validate config + auth")
    args = ap.parse_args()

    print(color(f"\n=== Sheets smoke test for person={args.person} ===\n", "1"))

    # 1. Config
    raw = settings.google_service_account_json
    if not raw:
        fail("GOOGLE_SERVICE_ACCOUNT_JSON is empty",
             "Set it in .env, then `set -a; source .env; set +a`")
    try:
        info = json.loads(raw)
    except json.JSONDecodeError as e:
        fail(f"GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON: {e}",
             "Make sure the value in .env is wrapped in single quotes "
             "with the multi-line private_key encoded as \\n escapes.")
    ok(f"service account loaded: {info.get('client_email')}")

    sheet_id = _sheet_id_for(args.person)
    if not sheet_id:
        fail(f"GOOGLE_SHEETS_ID_{args.person.upper()} is empty",
             f"Set it in .env from your Google Sheet URL: "
             f"https://docs.google.com/spreadsheets/d/<THIS_PART>/edit")
    ok(f"target sheet: https://docs.google.com/spreadsheets/d/{sheet_id}/edit")

    # 2. Open
    try:
        sheet = _open_sheet(sheet_id)
    except Exception as e:
        msg = str(e)
        if "PermissionDenied" in msg or "permission" in msg.lower() or "403" in msg:
            fail(
                f"Sheet open failed: {msg[:200]}",
                f"Share the sheet with {info['client_email']} "
                f"as Editor (Share button -> paste email -> Editor -> Send)",
            )
        fail(f"Sheet open failed: {e}")
    ok(f"opened worksheet: '{sheet.title}'")

    # 3. Headers
    _ensure_headers(sheet)
    first = sheet.row_values(1)
    if first != HEADERS:
        print(color(f"WARN headers don't match expected schema:", "33"))
        print(f"  expected: {HEADERS}")
        print(f"  found:    {first}")
    else:
        ok("header row matches expected schema")

    if args.dry_run:
        print(color("\nDry-run mode — skipping the test write.\n", "33"))
        return 0

    # 4. Test write — uses a deterministic dedup_id so we can verify upsert
    fake_row = [
        _dedup_id("Test Inc", "smoke-test-001"),
        datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
        "Test Inc",
        "Smoke Test SRE",
        "75%",
        "Yes",
        "Remote",
        "Yes",
        "https://example.com/jobs/smoke-001",
        "https://example.com/resume.docx",
        "https://example.com/cover.txt",
        "PENDING_REVIEW",
        "Smoke test from scripts/test_sheets.py",
    ]
    status1 = _upsert_row(sheet, fake_row)
    ok(f"first write: {status1}")

    # Update the same row — should be 'updated', not 'inserted'
    fake_row[3] = "Smoke Test SRE (updated)"
    fake_row[12] = "second write — should overwrite the first"
    status2 = _upsert_row(sheet, fake_row)
    if status2 != "updated":
        fail(f"upsert broken: second write returned '{status2}', expected 'updated'")
    ok(f"second write: {status2} (upsert works)")

    print(color(
        f"\n--- Open the sheet: "
        f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit ---\n"
        f"You should see ONE row for Test Inc, not two.\n",
        "1",
    ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
