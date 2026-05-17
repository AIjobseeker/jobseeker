#!/usr/bin/env python3
"""
Run on your Mac:  python3 scripts/preflight.py

Tests every external dependency the platform needs, in <30 seconds.
Prints a green/yellow/red status for each. Exits 0 only if everything green.
"""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

# ANSI colors (no emojis; user dislikes them)
G = "\033[32m"
Y = "\033[33m"
R = "\033[31m"
B = "\033[1m"
X = "\033[0m"

REPO = Path(__file__).resolve().parent.parent


def head(s: str) -> None:
    print(f"\n{B}{s}{X}")


def ok(s: str) -> None:
    print(f"  {G}OK   {X} {s}")


def warn(s: str) -> None:
    print(f"  {Y}WARN {X} {s}")


def fail(s: str) -> None:
    print(f"  {R}FAIL {X} {s}")


def tcp(host: str, port: int, timeout: float = 2.0) -> Optional[str]:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return None
    except Exception as e:
        return f"{type(e).__name__}: {e}"


def http_json(url: str, timeout: float = 5.0) -> tuple[int, Optional[dict], Optional[str]]:
    import urllib.request
    import urllib.error

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "jobseeker-preflight/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            try:
                return resp.status, json.loads(resp.read()), None
            except json.JSONDecodeError:
                return resp.status, None, "non-json response"
    except urllib.error.HTTPError as e:
        return e.code, None, f"HTTP {e.code}"
    except Exception as e:
        return 0, None, f"{type(e).__name__}: {e}"


# ─── Tests ────────────────────────────────────────────────────────────────

passed = 0
failed = 0


def record_ok(msg: str) -> None:
    global passed
    passed += 1
    ok(msg)


def record_fail(msg: str) -> None:
    global failed
    failed += 1
    fail(msg)


# 1. Local infra (Docker Compose services)
head("Local infrastructure (Docker Compose)")
infra = [
    ("Postgres", "localhost", 5432),
    ("Redis", "localhost", 6379),
    ("MinIO", "localhost", 9000),
    ("MinIO console", "localhost", 9001),
    ("NATS", "localhost", 4222),
    ("Temporal", "localhost", 7233),
]
for name, host, port in infra:
    err = tcp(host, port, 1.5)
    if err is None:
        record_ok(f"{name:18s} {host}:{port}")
    else:
        record_fail(f"{name:18s} {host}:{port}  ({err})")
        warn(f"  -> Run: cd ~/jobseeker && docker compose up -d")
        break

# 2. Ollama (Tailscale)
head("Ollama (local LLM)")
ollama_url = os.environ.get("OLLAMA_HOST", "http://100.115.111.9:11434")
host, _, port_str = ollama_url.replace("http://", "").replace("https://", "").partition(":")
port = int(port_str.partition("/")[0]) if port_str else 11434

err = tcp(host, port, 3.0)
if err:
    record_fail(f"Ollama TCP {host}:{port}  ({err})")
    warn(f"  -> On Windows: ensure OLLAMA_HOST=0.0.0.0:11434 + restart Ollama")
    warn(f"  -> Add Windows Firewall rule for inbound TCP {port}")
else:
    record_ok(f"Ollama TCP reachable at {host}:{port}")
    code, data, err = http_json(f"{ollama_url}/api/tags", 5.0)
    if data and "models" in data:
        names = [m.get("name", "?") for m in data["models"]]
        record_ok(f"Ollama API responding — {len(names)} models pulled")
        for n in names:
            print(f"          - {n}")
        # Check the models the code expects
        wanted = {
            "match (qwen3)":      ["qwen3:latest", "qwen3"],
            "resume (qwen3 reasoning)": ["qwen3:latest", "qwen3", "deepseek-r1:14b"],
            "cover (qwen3 prose)": ["qwen3:latest", "qwen3"],
            "study (qwen2.5:7b)": ["qwen2.5:7b", "qwen2.5"],
            "embeddings":         ["nomic-embed-text", "all-MiniLM-L6-v2"],
        }
        have = set(names)
        for purpose, options in wanted.items():
            if any(o in have or any(h.startswith(o) for h in have) for o in options):
                record_ok(f"  Model for {purpose}: present")
            else:
                warn(f"  Model for {purpose}: MISSING — try `ollama pull {options[0]}`")
    else:
        record_fail(f"Ollama API HTTP {code}: {err}")

# 3. Sample of public ATS endpoints (no auth needed)
head("Public ATS endpoints (no credentials needed — these are anonymous)")
samples = [
    ("Greenhouse", "https://api.greenhouse.io/v1/boards/airbnb/jobs?content=false"),
    ("Lever",      "https://api.lever.co/v0/postings/netflix?mode=json&limit=1"),
    ("Ashby",      "https://api.ashbyhq.com/posting-api/job-board/anthropic/jobs"),
]
for name, url in samples:
    code, data, err = http_json(url, 8.0)
    if 200 <= code < 300 and data:
        n = len(data.get("jobs", data) if isinstance(data, dict) else data)
        record_ok(f"{name:14s} HTTP {code}, {n} jobs returned")
    elif code == 0:
        record_fail(f"{name:14s} {err}")
        warn(f"  -> If on Apple corporate VPN, this may be blocked. Try a non-VPN test.")
    else:
        record_fail(f"{name:14s} HTTP {code}: {err}")

# 4. Profile + seed file presence
head("Local files")
seed = REPO / "companies" / "seed_500.yaml"
profile_op = REPO / "profiles" / "sai" / "profile.yaml"
profile_ai = REPO / "profiles" / "sai" / "profile.parsed.yaml"

for label, p in [("Seed companies", seed), ("Profile (operational)", profile_op), ("Profile (AI-parsed)", profile_ai)]:
    if p.exists():
        size = p.stat().st_size
        record_ok(f"{label:25s} {p.relative_to(REPO)}  ({size:,} bytes)")
    else:
        if label == "Profile (AI-parsed)":
            warn(f"{label:25s} {p.relative_to(REPO)} not found — run `python3 scripts/parse_resume.py`")
        else:
            record_fail(f"{label:25s} {p.relative_to(REPO)} not found")

# 5. Dedup DB (created on first notifier run)
head("Dedup database")
db = Path("~/.jobseeker/seen.db").expanduser()
if db.exists():
    try:
        conn = sqlite3.connect(str(db))
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM seen_jobs")
        total = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM seen_jobs WHERE notified=1")
        notified = cur.fetchone()[0]
        record_ok(f"Dedup DB has {total:,} jobs, {notified:,} notified")
        conn.close()
    except Exception as e:
        warn(f"Dedup DB exists but query failed: {e}")
else:
    warn(f"Dedup DB not yet created at {db} — first notifier run will create it")

# 6. Telegram (just credential presence; sending real msg requires CHAT_ID match)
head("Telegram credentials")
tok = os.environ.get("TELEGRAM_BOT_TOKEN", "")
chat = os.environ.get("TELEGRAM_CHAT_ID_SAI") or os.environ.get("TELEGRAM_CHAT_ID", "")
if tok and len(tok) > 30 and ":" in tok:
    record_ok(f"TELEGRAM_BOT_TOKEN set (len={len(tok)})")
elif tok:
    warn(f"TELEGRAM_BOT_TOKEN looks malformed (len={len(tok)})")
else:
    warn("TELEGRAM_BOT_TOKEN not set — Telegram alerts will not send")
if chat:
    record_ok(f"TELEGRAM_CHAT_ID present (you'll receive at chat={chat})")
else:
    warn("TELEGRAM_CHAT_ID not set")

# 7. Anthropic / Claude (optional fallback)
head("Anthropic / Claude (optional)")
ak = os.environ.get("ANTHROPIC_API_KEY", "")
base = os.environ.get("ANTHROPIC_BASE_URL", "")
if ak and (len(ak) >= 40 or base):
    if base:
        record_ok(f"Claude via proxy: {base}")
    else:
        record_ok(f"Claude direct: api.anthropic.com (key prefix {ak[:7]}...)")
else:
    warn("ANTHROPIC_API_KEY not set — Claude fallback unavailable")
    warn("  Internal proxy: ANTHROPIC_BASE_URL=https://your-internal-proxy/api/anthropic")
    warn("                  ANTHROPIC_AUTH_TOKEN=<bearer JWT from your token CLI>")
    warn("                  ANTHROPIC_API_KEY=dummy")

# ─── Summary ──────────────────────────────────────────────────────────────
head(f"Summary")
total = passed + failed
print(f"  {G}{passed} passed{X}, {R}{failed} failed{X}, of {total} checks\n")
sys.exit(0 if failed == 0 else 1)
