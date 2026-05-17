"""Activity for loading a user profile from disk."""
from __future__ import annotations

from pathlib import Path

import yaml
from temporalio import activity

PROFILES_DIR = Path("/app/profiles")


def _load_profile(person_id: str) -> dict:
    profile_path = PROFILES_DIR / person_id / "profile.yaml"
    skills_path = PROFILES_DIR / person_id / "skills.yaml"
    raw = yaml.safe_load(profile_path.read_text())
    skills_raw = yaml.safe_load(skills_path.read_text()) if skills_path.exists() else {}
    raw["skills"] = skills_raw.get("skills", raw.get("skills", []))
    return raw


@activity.defn
async def load_profile_activity(person_id: str) -> dict:
    """Wraps file I/O so it runs in an activity (not in workflow code)."""
    return _load_profile(person_id)
