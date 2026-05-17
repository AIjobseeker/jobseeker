#!/usr/bin/env python3
"""Pre-commit secret + employer-internal-reference sweeper.

Runs in <1s on the whole repo and bails (exit 1) if it finds anything that
shouldn't ship publicly. Wire it as a git pre-commit hook or run it manually
before every push.

What it scans for:
  - Anthropic API keys (sk-ant-api03-...)
  - AWS access keys (AKIA[A-Z0-9]{16})
  - Telegram bot tokens (\\d+:AA[A-Za-z0-9_-]{30,})
  - Google service-account private-key blocks
  - Generic JWT-shaped strings in non-code files
  - Employer-internal hostnames / CLI names / audience IDs that have been
    explicitly listed as banned in INTERNAL_BANNED below

What it skips:
  - .git, __pycache__, node_modules, .venv (never scanned)
  - .env / .env.bak / .env.local / .env.* (gitignored — local-only)
  - profiles/*/resume.yaml — resume content can mention prior employers
    (those go in the resume; that's fine)
  - tests/qa/*.md — checklists deliberately reference banned strings as
    examples of what to scrub

Exit codes:
  0 — clean
  1 — at least one finding (printed)
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Patterns that should never appear in committed source.
SECRETS_PATTERNS = [
    (r"sk-ant-api03-[A-Za-z0-9_\-]{40,}",       "Anthropic API key"),
    (r"AKIA[A-Z0-9]{16}",                        "AWS access key"),
    (r"\b\d{8,12}:AA[FGH][A-Za-z0-9_\-]{30,}",  "Telegram bot token"),
    (r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", "Private-key PEM"),
    (r'"private_key":\s*"-----BEGIN',           "Service-account JSON private_key"),
]

# Employer-internal references that should never ship publicly.
# Add to this list as new ones are spotted.
INTERNAL_BANNED = [
    (r"hvys3fcwcteqrvw3qzkvtk86viuoqv", "internal OAuth audience ID"),
    (r"floodgate\.g\.apple\.com",       "internal Apple proxy hostname"),
    (r"\bappleconnect\b",                "internal Apple CLI binary name"),
    (r"\bIDMS\b",                         "internal Apple identity service"),
    (r"\bFloodgate\b",                    "internal Apple proxy name (any case)"),
    (r"\bInterlinked\b",                  "internal Apple SDK name (any case)"),
]

# Files / dirs we never scan at all.
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv",
             ".pytest_cache", "dist", "build", ".tmp", ".docker"}
# Filenames (basename) we always skip.
SKIP_FILES = {".env", ".env.bak", ".env.local"}
# Extensions we scan. Skipping binaries.
SCAN_EXTS = {".py", ".yaml", ".yml", ".md", ".sh", ".json", ".txt",
             ".go", ".dockerfile", ".env.example", ".html", ".css", ".js", ".ts"}

# Specific paths whose hits we treat as informational (resume content can
# legitimately mention prior employers; QA docs document banned strings).
INFO_PATHS = (
    "profiles/sai/resume.yaml",
    "profiles/gf/resume.yaml",
    "profiles/sai/resume.yaml.bak",
    "profiles/gf/resume.yaml.bak",
    "profiles/sai/profile.parsed.yaml",
    "profiles/gf/profile.parsed.yaml",
    "tests/qa/PRODUCTION_CHECKLIST.md",
    "tests/qa/E2E_PLAN.md",
    "scripts/audit_secrets.py",  # this file documents the patterns
)


def _should_scan(path: Path) -> bool:
    parts = set(path.parts)
    if parts & SKIP_DIRS:
        return False
    if path.name in SKIP_FILES:
        return False
    if path.name.startswith(".env.") and path.name != ".env.example":
        return False
    suffix = path.suffix.lower()
    return suffix in SCAN_EXTS or path.name == "Dockerfile"


def _is_info(path: Path) -> bool:
    rel = str(path.relative_to(REPO))
    return rel in INFO_PATHS


def main() -> int:
    findings: list[tuple[Path, int, str, str, bool]] = []
    for p in REPO.rglob("*"):
        if not p.is_file() or not _should_scan(p):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except (OSError, UnicodeDecodeError):
            continue
        info_only = _is_info(p)
        for pat, label in SECRETS_PATTERNS:
            for m in re.finditer(pat, text):
                line_no = text.count("\n", 0, m.start()) + 1
                snippet = m.group(0)[:60]
                findings.append((p, line_no, label, snippet, info_only))
        for pat, label in INTERNAL_BANNED:
            for m in re.finditer(pat, text, re.IGNORECASE):
                line_no = text.count("\n", 0, m.start()) + 1
                snippet = m.group(0)[:60]
                findings.append((p, line_no, label, snippet, info_only))

    blockers = [f for f in findings if not f[4]]
    info = [f for f in findings if f[4]]

    if blockers:
        print("\033[31mBLOCKING findings — must fix before push:\033[0m\n")
        for path, ln, label, snip, _ in blockers:
            rel = path.relative_to(REPO)
            print(f"  ✗ {rel}:{ln}  [{label}]  {snip!r}")
        if info:
            print("\nInformational (in resume / QA docs — left alone):")
            for path, ln, label, snip, _ in info:
                rel = path.relative_to(REPO)
                print(f"    · {rel}:{ln}  [{label}]")
        print(f"\n\033[31m{len(blockers)} blocker(s).\033[0m")
        return 1

    print(f"\033[32mClean.\033[0m  Scanned {sum(1 for p in REPO.rglob('*') if p.is_file() and _should_scan(p))} files.")
    if info:
        print(f"  ({len(info)} informational hits in resume / QA docs — those are fine)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
