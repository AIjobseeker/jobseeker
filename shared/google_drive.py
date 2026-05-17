"""Google Drive sync — per-match folders + file uploads.

Why a separate module from sheet_sync? Different scope, different lifecycle:
  - Sheets need only `spreadsheets` scope and append-style writes.
  - Drive needs `drive.file` scope and folder + file CRUD.

Two auth modes are supported:

  1. Service Account (default; same SA the sheet sync uses)
     — Reads GOOGLE_SERVICE_ACCOUNT_JSON.
     — IMPORTANT: a personal Google Drive folder shared with a service account
       lets the SA *create* folders inside it but NOT *upload files* — service
       accounts have 0 GB of personal storage. So uploads via SA only work
       inside a Google Workspace **Shared Drive** (not a regular folder).
     — If you don't have Workspace, use mode #2 instead.

  2. OAuth user credentials  (set JOBSEEKER_GDRIVE_AUTH=oauth)
     — Reads ~/.jobseeker/google_oauth.json (a token JSON with refresh_token).
     — Run `python3 scripts/google_oauth_init.py` once to create that file.
     — Files end up owned by your personal Google account, no quota issues.

Per-match folder layout (created by `tailor_v2.py`):

    <parent>/
      Stripe_Staff-SRE_2026-05-17/
        resume.tailored.html
        resume.tailored.pdf       (if weasyprint succeeded)
        resume.tailored.docx
        cover_letter.txt
        study_guide.md
        job.json
        match_report.json
        missing_skills.txt
"""
from __future__ import annotations

import json
import logging
import mimetypes
import os
from pathlib import Path
from typing import Optional

log = logging.getLogger("shared.google_drive")

# drive.file scope = SA can only see/touch files IT created or that were
# explicitly shared with it. This is the right scope: prevents accidental
# enumeration of the user's whole Drive.
DRIVE_SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
]

DEFAULT_OAUTH_TOKEN_PATH = Path("~/.jobseeker/google_oauth.json").expanduser()


class DriveSyncer:
    """Lightweight wrapper over Google Drive API v3.

    Auth modes:
      - service_account_json + parent_folder_id  (default; SA mode)
      - oauth_token_path     + parent_folder_id  (user-credential mode)

    Synchronous on purpose — Drive uploads are infrequent (one per match,
    a few per minute at most) so the simplicity is worth more than async.
    Callers wrap calls in `asyncio.to_thread` if they need to stay async.
    """

    def __init__(
        self,
        parent_folder_id: str,
        *,
        service_account_json: Optional[str] = None,
        oauth_token_path: Optional[Path] = None,
    ) -> None:
        self.parent_folder_id = parent_folder_id
        self._sa_json = service_account_json
        self._oauth_token_path = oauth_token_path
        self._svc = None  # lazy

    @classmethod
    def from_env(cls) -> Optional["DriveSyncer"]:
        parent = os.environ.get("GOOGLE_DRIVE_PARENT_FOLDER_ID", "").strip()
        if not parent:
            log.info("Drive sync disabled (no GOOGLE_DRIVE_PARENT_FOLDER_ID)")
            return None

        # OAuth user-credential mode wins if requested OR if a token file
        # exists at the canonical path.
        force_oauth = os.environ.get("JOBSEEKER_GDRIVE_AUTH", "").strip().lower() == "oauth"
        if force_oauth or DEFAULT_OAUTH_TOKEN_PATH.exists():
            if not DEFAULT_OAUTH_TOKEN_PATH.exists():
                log.warning(
                    "JOBSEEKER_GDRIVE_AUTH=oauth but %s missing. "
                    "Run scripts/google_oauth_init.py first.",
                    DEFAULT_OAUTH_TOKEN_PATH,
                )
                return None
            return cls(parent_folder_id=parent,
                       oauth_token_path=DEFAULT_OAUTH_TOKEN_PATH)

        sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
        if not sa_json:
            log.info(
                "Drive sync disabled (need GOOGLE_SERVICE_ACCOUNT_JSON OR "
                "an OAuth token at %s)", DEFAULT_OAUTH_TOKEN_PATH,
            )
            return None
        return cls(parent_folder_id=parent, service_account_json=sa_json)

    def _service(self):
        if self._svc is not None:
            return self._svc
        from googleapiclient.discovery import build

        if self._oauth_token_path:
            creds = self._load_oauth_credentials()
        else:
            from google.oauth2.service_account import Credentials
            info = json.loads(self._sa_json or "{}")
            creds = Credentials.from_service_account_info(info, scopes=DRIVE_SCOPES)
        # cache_discovery=False silences a noisy "no module named cachecontrol"
        # warning on Python 3.9 environments without optional deps.
        self._svc = build("drive", "v3", credentials=creds, cache_discovery=False)
        return self._svc

    def _load_oauth_credentials(self):
        """Load + refresh OAuth user credentials from ~/.jobseeker/google_oauth.json."""
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request

        data = json.loads(self._oauth_token_path.read_text())
        creds = Credentials(
            token=data.get("token"),
            refresh_token=data.get("refresh_token"),
            token_uri=data.get("token_uri", "https://oauth2.googleapis.com/token"),
            client_id=data["client_id"],
            client_secret=data["client_secret"],
            scopes=data.get("scopes", DRIVE_SCOPES),
        )
        if not creds.valid and creds.refresh_token:
            creds.refresh(Request())
            # Persist refreshed access token so we don't have to refresh on
            # every script run.
            data["token"] = creds.token
            self._oauth_token_path.write_text(json.dumps(data, indent=2))
        return creds

    # ── Folder ops ────────────────────────────────────────────────────────

    def create_folder(self, name: str, parent_id: Optional[str] = None) -> dict:
        """Create a folder under `parent_id` (defaults to configured parent).

        Returns {id, name, webViewLink}. Idempotent ONLY in the sense that
        Drive happily creates two folders with the same name — caller should
        use `find_folder` first if they want dedup behavior.
        """
        svc = self._service()
        meta = {
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id or self.parent_folder_id],
        }
        f = svc.files().create(
            body=meta,
            fields="id,name,webViewLink",
            supportsAllDrives=True,
        ).execute()
        return f

    def find_folder(self, name: str, parent_id: Optional[str] = None) -> Optional[dict]:
        """Look up a folder by exact name under `parent_id`. Returns None if missing."""
        svc = self._service()
        parent = parent_id or self.parent_folder_id
        # Drive query API — escape single quotes in name.
        safe_name = name.replace("'", "\\'")
        q = (
            f"name = '{safe_name}' and "
            f"mimeType = 'application/vnd.google-apps.folder' and "
            f"'{parent}' in parents and trashed = false"
        )
        resp = svc.files().list(
            q=q,
            fields="files(id,name,webViewLink)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            pageSize=5,
        ).execute()
        files = resp.get("files", []) or []
        return files[0] if files else None

    def get_or_create_folder(self, name: str, parent_id: Optional[str] = None) -> dict:
        existing = self.find_folder(name, parent_id=parent_id)
        if existing:
            return existing
        return self.create_folder(name, parent_id=parent_id)

    # ── File ops ──────────────────────────────────────────────────────────

    def upload_file(
        self,
        local_path: Path,
        parent_id: str,
        name: Optional[str] = None,
    ) -> dict:
        """Upload `local_path` into `parent_id`. Returns {id, name, webViewLink}."""
        from googleapiclient.http import MediaFileUpload

        svc = self._service()
        local_path = Path(local_path)
        if not local_path.exists():
            raise FileNotFoundError(str(local_path))
        target_name = name or local_path.name
        mime, _ = mimetypes.guess_type(str(local_path))
        media = MediaFileUpload(str(local_path), mimetype=mime, resumable=False)
        meta = {"name": target_name, "parents": [parent_id]}
        f = svc.files().create(
            body=meta,
            media_body=media,
            fields="id,name,webViewLink,webContentLink",
            supportsAllDrives=True,
        ).execute()
        return f

    def upload_text(
        self,
        content: str,
        parent_id: str,
        name: str,
        mime_type: str = "text/plain",
    ) -> dict:
        """Upload text content directly (no temp file)."""
        from googleapiclient.http import MediaInMemoryUpload

        svc = self._service()
        media = MediaInMemoryUpload(content.encode("utf-8"), mimetype=mime_type)
        meta = {"name": name, "parents": [parent_id]}
        f = svc.files().create(
            body=meta,
            media_body=media,
            fields="id,name,webViewLink,webContentLink",
            supportsAllDrives=True,
        ).execute()
        return f

    def make_anyone_viewable(self, file_id: str) -> bool:
        """Add 'anyone with the link can view' permission. Returns True on success.

        Required if you want the Drive link in Telegram to open without
        login on a phone that's not signed into the SA's Workspace domain.
        """
        svc = self._service()
        try:
            svc.permissions().create(
                fileId=file_id,
                body={"role": "reader", "type": "anyone"},
                supportsAllDrives=True,
            ).execute()
            return True
        except Exception as e:
            log.warning("make_anyone_viewable(%s) failed: %s", file_id, e)
            return False
