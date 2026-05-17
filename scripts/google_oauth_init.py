#!/usr/bin/env python3
"""One-time OAuth flow for Google Drive uploads under your personal account.

Why this exists: Google Service Accounts have 0 GB of personal Drive quota,
so uploads to a regular My Drive folder shared with the SA fail with
`storageQuotaExceeded` even though folder creation works. Switching to OAuth
user credentials makes uploaded files owned by YOUR account, using YOUR
quota — and the regular folder you already created at
GOOGLE_DRIVE_PARENT_FOLDER_ID continues to work.

Usage (one-time):

    pip3 install --user google-auth-oauthlib
    python3 scripts/google_oauth_init.py

The first run opens a browser, asks you to sign in to your personal Google
account, and asks for permission to create/modify files in YOUR Drive.
On success it writes ~/.jobseeker/google_oauth.json (with a long-lived
refresh token); future runs of tailor_v2 / the notifier will pick that up
automatically as long as JOBSEEKER_GDRIVE_AUTH=oauth is set OR the file
exists at the canonical path.

Need a client_id/secret? Either:
  (a) Create one at https://console.cloud.google.com/apis/credentials
      under your jobseeker-496610 project, type "Desktop app". Drop the
      JSON into ~/.jobseeker/google_client_secret.json.
  (b) Re-use the desktop client that ships with this repo (none exists by
      default — option (a) is the way).

We DON'T commit a client secret to the repo — it's per-account.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

JOBSEEKER_DIR = Path("~/.jobseeker").expanduser()
DEFAULT_TOKEN_PATH = JOBSEEKER_DIR / "google_oauth.json"
DEFAULT_CLIENT_PATH = JOBSEEKER_DIR / "google_client_secret.json"

DRIVE_SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/spreadsheets",
]


def main() -> int:
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print(
            "Missing google-auth-oauthlib. Install with:\n"
            "  pip3 install --user google-auth-oauthlib"
        )
        return 1

    if not DEFAULT_CLIENT_PATH.exists():
        print(
            f"\nNo client-secret JSON at {DEFAULT_CLIENT_PATH}.\n\n"
            f"Get one from Google Cloud Console:\n"
            f"  1. https://console.cloud.google.com/apis/credentials"
            f"?project=jobseeker-496610\n"
            f"  2. + Create Credentials -> OAuth client ID -> Desktop app\n"
            f"  3. Name it 'jobseeker-cli', Create, Download JSON.\n"
            f"  4. Save it as {DEFAULT_CLIENT_PATH}\n"
        )
        return 2

    JOBSEEKER_DIR.mkdir(parents=True, exist_ok=True)
    flow = InstalledAppFlow.from_client_secrets_file(
        str(DEFAULT_CLIENT_PATH), DRIVE_SCOPES,
    )
    # run_local_server opens a browser tab; works on macOS without extra setup.
    creds = flow.run_local_server(port=0, prompt="consent")

    # Persist a self-contained token blob (so DriveSyncer doesn't need the
    # client_secret file at runtime — only this token).
    client_data = json.loads(DEFAULT_CLIENT_PATH.read_text())
    client_block = client_data.get("installed") or client_data.get("web") or {}
    out = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": client_block.get("client_id", ""),
        "client_secret": client_block.get("client_secret", ""),
        "scopes": list(creds.scopes),
    }
    DEFAULT_TOKEN_PATH.write_text(json.dumps(out, indent=2))
    DEFAULT_TOKEN_PATH.chmod(0o600)

    print(
        f"\nSaved OAuth token to {DEFAULT_TOKEN_PATH}\n"
        f"refresh_token: {'present' if creds.refresh_token else 'MISSING'}\n\n"
        f"Next: re-run\n"
        f"  python3 scripts/tailor_v2.py --person sai --use-claude --mode rewrite "
        f"--sample --send-telegram\n"
        f"and the Drive uploads should work under your personal account."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
