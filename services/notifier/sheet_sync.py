"""Append-or-update one row in the user's tracking Google Sheet for each
match that gets a Telegram alert.

Why duplicate the worker-activity logic? Two reasons:
  1. The streaming pipeline (scraper -> scorer -> notifier -> Telegram) MUST
     write to the sheet at notify-time, even if Temporal isn't running. The
     user wants visibility the second an alert fires.
  2. Documents (resume / cover letter URLs) aren't available yet in the
     streaming pipeline — they're generated later by the Temporal workflow.
     Both writers use the SAME dedup_id, so the Temporal write upserts the
     same row with URLs filled in.

If GOOGLE_SHEETS_ID_SAI or GOOGLE_SERVICE_ACCOUNT_JSON is missing, this is a
no-op (logged once). It must NEVER fail the Telegram pipeline — alerts come
first, sheet is best-effort.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from services.notifier.models import ScoredJob

log = logging.getLogger("notifier.sheet_sync")

HEADERS = [
    "dedup_id",          # A — stable id used to upsert
    "Date Added",        # B
    "Person",            # C — sai | gf
    "Company",           # D
    "Title",             # E
    "Department",        # F
    "Location",          # G
    "Remote",            # H
    "Source",            # I — greenhouse, lever, workday, html, ...
    "Match Score",       # J — raw scorer output (0-100%)
    "ATS Score",         # K — keyword overlap %
    "Recruiter Score",   # L — Claude impression /100 (blank if not run)
    "Archetype",         # M — which framing was picked
    "Visa OK",           # N — Yes/No based on sponsorship language
    "Apply URL",         # O
    "Drive Folder Link", # P
    "Resume URL",        # Q — public Drive link or canonical local path
    "Cover Letter URL",  # R — same
    "Required Skills",   # S — top JD requirements
    "Missing Skills",    # T — JD keywords not in tailored resume
    "Status",            # U — NEW | APPLIED | SKIPPED | INTERVIEW | OFFER | REJECTED
    "Notes",             # V — recruiter verdict / freeform
]
LAST_COL = "V"           # 22 columns -> A..V

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def _dedup_id(company: str, source_id: str) -> str:
    blob = f"{company.lower()}|{source_id}".encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


class SheetSyncer:
    """Thread-safe-by-asyncio-Lock wrapper around a gspread worksheet.

    All gspread calls are blocking, so we run them via asyncio.to_thread so
    the notifier's event loop stays responsive while the API roundtrip
    happens (Sheets writes take 200-800ms over the network).
    """

    def __init__(
        self,
        sheet_id: str,
        service_account_json: str,
    ) -> None:
        self.sheet_id = sheet_id
        self._sa_json = service_account_json
        self._lock = asyncio.Lock()
        self._sheet = None  # lazy-opened on first use
        self.appended = 0
        self.updated = 0
        self.failed = 0

    @classmethod
    def from_env(cls, person: str = "sai") -> Optional["SheetSyncer"]:
        """Build from env vars, or None if disabled.

        Env names match the existing .env shape:
          GOOGLE_SHEETS_ID_SAI / GOOGLE_SHEETS_ID_GF
          GOOGLE_SERVICE_ACCOUNT_JSON
        """
        sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
        sheet_id = os.environ.get(f"GOOGLE_SHEETS_ID_{person.upper()}", "").strip()
        if not sheet_id or not sa_json:
            log.info("sheet sync disabled (missing GOOGLE_SHEETS_ID_%s or "
                     "GOOGLE_SERVICE_ACCOUNT_JSON)", person.upper())
            return None
        return cls(sheet_id=sheet_id, service_account_json=sa_json)

    def _open(self):
        if self._sheet is not None:
            return self._sheet
        # Imported here so unrelated tests don't pay the import cost.
        import gspread
        from google.oauth2.service_account import Credentials

        info = json.loads(self._sa_json)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
        client = gspread.authorize(creds)
        sheet = client.open_by_key(self.sheet_id).sheet1
        # Ensure header row exists.
        try:
            first_row = sheet.row_values(1)
        except Exception:
            first_row = []
        if not first_row:
            sheet.append_row(HEADERS, value_input_option="RAW")
        self._sheet = sheet
        return sheet

    def _row(self, payload: ScoredJob, person: str = "sai") -> list[str]:
        """Build a row from a streaming ScoredJob (no scoring artifacts yet).

        Used by the streaming notifier — Drive/match-report fields are
        filled in later by `upsert_row` when tailor_v2 runs.
        """
        job = payload.job
        # Heuristic: visa concern when score is hard-capped at 0.20 by the
        # no-sponsorship rule. That information has already been baked into
        # `score` and `reason`, so we read it back from there.
        reason = (payload.reason or "")
        visa_concern = "no sponsorship" in reason.lower()
        return [
            _dedup_id(job.company, job.source_id),
            datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
            person,
            job.company,
            job.title,
            getattr(job, "department", "") or "",
            job.location,
            "Yes" if job.remote else "No",
            getattr(job, "source", "") or "",
            f"{payload.score:.0%}",
            "",  # ATS Score (filled by tailor)
            "",  # Recruiter Score (filled by tailor)
            "",  # Archetype (filled by tailor)
            "No" if visa_concern else "Yes",
            job.url,
            "",  # Drive Folder Link (filled by tailor)
            "",  # Resume URL (filled by tailor)
            "",  # Cover Letter URL (filled by tailor)
            "",  # Required Skills (filled by tailor)
            "",  # Missing Skills (filled by tailor)
            "NEW",
            reason[:300],
        ]

    def _row_from_dict(self, data: dict) -> list[str]:
        """Build a row from a flat dict — used by tailor_v2.

        Required keys: dedup_id, company, title, url.
        Optional keys (everything else uses ""):
          person, department, location, remote, source, match_score (0-1 float),
          ats_score (0-100), recruiter_score (0-100), archetype,
          visa_ok (bool), drive_folder_link, resume_url, cover_letter_url,
          required_skills, missing_skills, status, notes.
        """
        def s(k, default=""):
            v = data.get(k)
            return "" if v is None else str(v)

        match_score = data.get("match_score")
        if match_score is None:
            match_str = ""
        elif isinstance(match_score, (int, float)) and match_score <= 1.0:
            match_str = f"{float(match_score):.0%}"
        else:
            match_str = f"{match_score}"

        ats = data.get("ats_score")
        ats_str = f"{ats}" if ats not in (None, "") else ""
        rec = data.get("recruiter_score")
        rec_str = f"{rec}" if rec not in (None, "") else ""

        return [
            data["dedup_id"],
            datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
            s("person", "sai"),
            s("company"),
            s("title"),
            s("department"),
            s("location"),
            "Yes" if data.get("remote") else "No",
            s("source"),
            match_str,
            ats_str,
            rec_str,
            s("archetype"),
            "Yes" if data.get("visa_ok", True) else "No",
            s("url"),
            s("drive_folder_link"),
            s("resume_url"),
            s("cover_letter_url"),
            s("required_skills"),
            s("missing_skills"),
            s("status", "NEW"),
            s("notes")[:500],
        ]

    def _upsert_blocking(self, row: list[str]) -> str:
        sheet = self._open()
        col_a = sheet.col_values(1)
        dedup_id = row[0]
        if dedup_id in col_a[1:]:
            idx = col_a.index(dedup_id) + 1
            sheet.update(f"A{idx}:{LAST_COL}{idx}", [row], value_input_option="USER_ENTERED")
            return "updated"
        sheet.append_row(row, value_input_option="USER_ENTERED")
        return "inserted"

    def _update_status_blocking(self, dedup_id: str, status: str) -> bool:
        """Update the Status column (col U = index 21) for the row whose
        column-A value is `dedup_id`. Returns True if a row was updated.
        """
        sheet = self._open()
        col_a = sheet.col_values(1)
        if dedup_id not in col_a[1:]:
            return False
        idx = col_a.index(dedup_id) + 1
        sheet.update(f"U{idx}", [[status]], value_input_option="USER_ENTERED")
        return True

    async def update_status_by_dedup_id(self, dedup_id: str, status: str) -> bool:
        async with self._lock:
            try:
                return await asyncio.to_thread(self._update_status_blocking, dedup_id, status)
            except Exception as e:
                log.warning("sheet status update failed for %s -> %s: %s",
                            dedup_id, status, e)
                return False

    async def upsert(self, payload: ScoredJob, person: str = "sai") -> str:
        """Append-or-update one row. Returns 'inserted'|'updated'|'failed'.

        Never raises — sheet sync is best-effort behind Telegram.
        """
        async with self._lock:
            try:
                row = self._row(payload, person=person)
                status = await asyncio.to_thread(self._upsert_blocking, row)
            except Exception as e:
                self.failed += 1
                log.warning(
                    "sheet sync failed for %s @ %s: %s",
                    payload.job.title, payload.job.company, e,
                )
                return "failed"
        if status == "inserted":
            self.appended += 1
        else:
            self.updated += 1
        log.info(
            "sheet %s: %s @ %s (score=%.2f)",
            status, payload.job.title, payload.job.company, payload.score,
        )
        return status

    async def upsert_dict(self, data: dict) -> str:
        """Same as upsert(), but takes a flat dict — used by tailor_v2 which
        has computed ATS / recruiter / Drive fields not present in the
        streaming ScoredJob shape.

        Required key: dedup_id. Other keys: see _row_from_dict docstring.
        """
        async with self._lock:
            try:
                row = self._row_from_dict(data)
                status = await asyncio.to_thread(self._upsert_blocking, row)
            except Exception as e:
                self.failed += 1
                log.warning("sheet upsert_dict failed (%s): %s",
                            data.get("dedup_id"), e)
                return "failed"
        if status == "inserted":
            self.appended += 1
        else:
            self.updated += 1
        log.info("sheet %s: %s @ %s",
                 status, data.get("title"), data.get("company"))
        return status
