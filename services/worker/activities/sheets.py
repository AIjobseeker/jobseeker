"""Google Sheets tracker — append every match to a per-user Google Sheet.

Why a sheet (not just Postgres):
  - Cross-device read access without exposing a database
  - Easy filtering/sorting/charts for the user
  - Survives if the local stack goes down
  - Shareable with a recruiter/coach if needed

The sheet is upsert-safe: we use a `dedup_id` column (sha256 of company+source_id)
as the primary key. If the same job comes through twice, the second call updates
the existing row instead of appending a duplicate.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from typing import Optional

from temporalio import activity

from shared.config import settings
from shared.models import GeneratedDocuments, JobPost, MatchResult, UserProfile

log = logging.getLogger("worker.sheets")

# Column order — change with care, sheet headers must match.
HEADERS = [
    "dedup_id",     # hidden-ish primary key (col A)
    "Date Added",
    "Company",
    "Title",
    "Score",
    "Visa OK",
    "Location",
    "Remote",
    "Apply URL",
    "Resume URL",
    "Cover Letter URL",
    "Status",
    "Match Reasoning",
]
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def _sheet_id_for(person_id: str) -> Optional[str]:
    if person_id == "sai":
        return settings.google_sheets_id_sai or None
    if person_id == "gf":
        return settings.google_sheets_id_gf or None
    return None


def _open_sheet(sheet_id: str):
    """Authorize and return the first worksheet of the given spreadsheet."""
    import gspread
    from google.oauth2.service_account import Credentials

    raw = settings.google_service_account_json
    if not raw:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON is not set")

    creds_info = json.loads(raw)
    creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(sheet_id)
    return spreadsheet.sheet1


def _ensure_headers(sheet) -> None:
    """If the sheet is empty, write the header row. No-op otherwise."""
    first_row = sheet.row_values(1)
    if first_row != HEADERS:
        if not first_row:
            sheet.append_row(HEADERS, value_input_option="RAW")
        # If headers exist but differ, leave them alone. Manual fix only.


def _dedup_id(company: str, source_id: str) -> str:
    blob = f"{company.lower()}|{source_id}".encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def _row_for(
    job: JobPost,
    match: MatchResult,
    docs: GeneratedDocuments,
) -> list:
    return [
        _dedup_id(job.company, job.source_id),
        datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
        job.company,
        job.title,
        f"{match.score:.0%}",
        "Yes" if match.visa_ok else "No",
        job.location,
        "Yes" if job.remote else "No",
        job.url,
        docs.resume_url,
        docs.cover_letter_url,
        "PENDING_REVIEW",
        (match.reasoning or "")[:300],
    ]


def _upsert_row(sheet, row: list) -> str:
    """Append if dedup_id is new, else update the existing row in place.
    Returns 'inserted' or 'updated' for logging.
    """
    dedup_id = row[0]
    # Cheap scan of column A. For a personal tool with O(1k) rows this is fine.
    # If we outgrow it, switch to a Sheets API filter view.
    col_a = sheet.col_values(1)
    if dedup_id in col_a[1:]:  # skip header
        idx = col_a.index(dedup_id) + 1  # 1-based row number
        sheet.update(f"A{idx}:M{idx}", [row], value_input_option="USER_ENTERED")
        return "updated"
    sheet.append_row(row, value_input_option="USER_ENTERED")
    return "inserted"


@activity.defn
async def sync_to_sheet(
    job_dict: dict,
    profile_dict: dict,
    match_dict: dict,
    docs_dict: dict,
) -> str:
    """Append-or-update one tracking row for this match. Returns status.

    Non-fatal: returns 'skipped' on misconfiguration so the workflow can
    continue. Real network failures DO raise — Temporal retries those.
    """
    profile = UserProfile(**profile_dict)
    sheet_id = _sheet_id_for(profile.id)
    if not sheet_id:
        log.info("sheets sync skipped (%s has no sheet configured)", profile.id)
        return "skipped"
    if not settings.google_service_account_json:
        log.warning("GOOGLE_SERVICE_ACCOUNT_JSON not set; sheets sync skipped")
        return "skipped"

    job = JobPost(**job_dict)
    match = MatchResult(**match_dict)
    docs = GeneratedDocuments(**docs_dict)

    sheet = _open_sheet(sheet_id)
    _ensure_headers(sheet)
    status = _upsert_row(sheet, _row_for(job, match, docs))

    log.info(
        "sheets %s: %s @ %s (score=%.2f)", status, job.title, job.company, match.score
    )
    return status
