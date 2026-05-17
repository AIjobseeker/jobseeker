"""Artifact storage abstraction — write tailored docs once, retrieve by dedup_id.

Tier 1 (default): local filesystem under ~/.jobseeker/docs/<dedup_id>/
Tier 2 (when MINIO_ENDPOINT is set + bucket reachable): MinIO uploads with
        presigned 7-day URLs.
Tier 3 (future): S3 / R2 / B2.

The `path` field stored in seen.db tells you which tier produced it:
    "local:<dedup_id>/resume.docx"        -> ~/.jobseeker/docs/...
    "minio:<bucket>/<key>"                -> MinIO object
    "https://..."                         -> direct URL (R2 / S3)

This way the mobile app / Telegram bot reads `resume_path` and decides:
    - if it starts with "local:", read from local FS (only useful on the host)
    - if "minio:" or http, present the URL directly
"""
from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Optional

log = logging.getLogger("notifier.artifact_store")

DEFAULT_ROOT = Path("~/.jobseeker/docs").expanduser()


class LocalArtifactStore:
    """Writes everything under <root>/<dedup_id>/<filename>.

    Returns a `local:<dedup_id>/<filename>` path-string that's stable across
    services. The notifier and (later) the API/mobile-app can resolve it via
    `resolve_local()`.
    """

    def __init__(self, root: Path | str = DEFAULT_ROOT) -> None:
        self.root = Path(root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)

    def dir_for(self, dedup_id: str) -> Path:
        d = self.root / dedup_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def put_file(self, dedup_id: str, src: Path, name: Optional[str] = None) -> str:
        """Copy a file in. Returns the canonical path-string."""
        target_name = name or src.name
        dst = self.dir_for(dedup_id) / target_name
        shutil.copy2(str(src), str(dst))
        return f"local:{dedup_id}/{target_name}"

    def put_bytes(self, dedup_id: str, name: str, data: bytes) -> str:
        dst = self.dir_for(dedup_id) / name
        dst.write_bytes(data)
        return f"local:{dedup_id}/{name}"

    def put_text(self, dedup_id: str, name: str, text: str) -> str:
        dst = self.dir_for(dedup_id) / name
        dst.write_text(text, encoding="utf-8")
        return f"local:{dedup_id}/{name}"

    def resolve_local(self, path_str: str) -> Optional[Path]:
        """If `path_str` is local-tier, return the absolute Path. Else None."""
        if not path_str or not path_str.startswith("local:"):
            return None
        rel = path_str[len("local:"):]
        p = (self.root / rel).resolve()
        # Defense: don't escape the root.
        try:
            p.relative_to(self.root.resolve())
        except ValueError:
            return None
        if not p.exists():
            return None
        return p


def get_default_store() -> LocalArtifactStore:
    """Construct from env. JOBSEEKER_ARTIFACT_ROOT overrides the location."""
    root = os.environ.get("JOBSEEKER_ARTIFACT_ROOT", "").strip()
    return LocalArtifactStore(root=root) if root else LocalArtifactStore()
