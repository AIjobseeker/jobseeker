#!/usr/bin/env python3
"""
Parse a resume PDF into a structured profile.yaml using Claude.

Usage:
    python3 scripts/parse_resume.py [--input PATH] [--output PATH]
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

DEFAULT_INPUT = Path.home() / "Downloads" / "saikrishna-resume-uptodate.pdf"
# Output goes to profile.parsed.yaml — the existing profile.yaml holds
# operational config (email, telegram_chat_id, resume_variants paths) that
# downstream services read. The matcher consumes both.
DEFAULT_OUTPUT = Path("profiles/sai/profile.parsed.yaml")
MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 4096

SYSTEM_PROMPT = """You are an expert technical recruiter and resume parser. \
Read the candidate's resume and produce a structured YAML profile that downstream \
job-matching code can use to filter and rank job postings. Be precise, concise, and \
faithful to the resume — do not invent skills or experience. Infer seniority from \
years + titles + scope. Wrap your final YAML output in <profile_yaml>...</profile_yaml> \
tags and emit nothing else inside those tags besides valid YAML."""

USER_TEMPLATE = """Extract a structured profile from the resume below.

Return YAML matching exactly this schema (keys, order, types). Use null/[] when unknown.

```yaml
person:
  name: ...
  current_title: ...
  years_total_experience: <int>
  visa_status: ...
seniority:
  level: senior | staff | principal | architect
  ic_or_manager: ic | manager | mixed
  next_step_titles: [...]
core_skills:
  - {name: kubernetes, years: 5, level: expert, evidence: "managed prod clusters at X"}
adjacent_skills:
  - {name: ..., level: intermediate}
domains:
  - {name: fintech, years: 3, role: SRE}
career_arc: ic_track | manager_track | mixed
target_titles:
  - "Site Reliability Engineer"
  - "Staff SRE"
red_flags:
  - manager-only roles (no IC track)
  - frontend-heavy stacks
  - no-sponsorship phrases
preferred_signals:
  - kubernetes platform team
  - infrastructure as code (terraform)
profile_summary: |
  2-3 sentence free-text summary capturing the essence — used for embeddings.
```

Rules:
- `core_skills` = deep, hands-on skills with multi-year evidence. Aim for 8-15.
- `adjacent_skills` = familiar but shallower. Aim for 5-12.
- `target_titles` = exact title strings to match against job postings (10-20 entries).
- `red_flags` and `preferred_signals` should reflect this candidate specifically.
- Wrap the YAML in <profile_yaml>...</profile_yaml>.

RESUME TEXT:
---
{{RESUME_TEXT}}
---
"""


def die(msg: str, code: int = 1) -> None:
    print(msg, file=sys.stderr)
    sys.exit(code)


def check_api_key() -> tuple[str, str | None, dict[str, str]]:
    """Returns (api_key, base_url_or_None, extra_headers).

    Three modes are supported:
      1. Direct Anthropic — set ANTHROPIC_API_KEY only.
      2. Internal proxy   — set ANTHROPIC_BASE_URL + an auth header
         (ANTHROPIC_AUTH_TOKEN -> "Authorization: Bearer ...").
         API key may be the literal "dummy".
      3. Custom proxy     — set ANTHROPIC_BASE_URL with whatever auth
         the proxy expects via ANTHROPIC_EXTRA_HEADERS_JSON.
    """
    import json

    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "").strip() or None
    auth_token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "").strip()
    extra_headers_raw = os.environ.get("ANTHROPIC_EXTRA_HEADERS_JSON", "").strip()

    extra_headers: dict[str, str] = {}
    if extra_headers_raw:
        try:
            extra_headers = json.loads(extra_headers_raw)
        except json.JSONDecodeError as e:
            die(f"ERROR: ANTHROPIC_EXTRA_HEADERS_JSON is not valid JSON: {e}")
    if auth_token:
        extra_headers.setdefault("Authorization", f"Bearer {auth_token}")

    if base_url:
        # Proxy mode — key may be a placeholder, just must not be empty.
        return (key or "dummy", base_url, extra_headers)

    # Direct Anthropic — must be a real key.
    if not key or key.endswith("...") or len(key) < 40:
        die(
            "ERROR: no usable Claude credentials found.\n\n"
            "Pick one of:\n\n"
            "  Direct Anthropic:\n"
            "    export ANTHROPIC_API_KEY='sk-ant-api03-...real-key...'\n\n"
            "  Internal proxy (corporate / VPN-gated):\n"
            "    export ANTHROPIC_BASE_URL='https://your-internal-proxy/api/anthropic'\n"
            "    export ANTHROPIC_AUTH_TOKEN='<bearer JWT from your token CLI>'\n"
            "    export ANTHROPIC_API_KEY='dummy'\n\n"
            "  Custom proxy with extra headers:\n"
            "    export ANTHROPIC_BASE_URL='https://your.proxy/...'\n"
            "    export ANTHROPIC_EXTRA_HEADERS_JSON='{\"Authorization\":\"Bearer ...\"}'\n"
        )
    return (key, None, extra_headers)


def extract_pdf_text(path: Path) -> str:
    try:
        import pdfplumber  # type: ignore
    except ImportError:
        die("ERROR: pdfplumber not installed. Run: pip install pdfplumber anthropic pyyaml")
    if not path.exists():
        die(f"ERROR: resume PDF not found at {path}")
    chunks: list[str] = []
    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages:
            chunks.append(page.extract_text() or "")
    text = "\n\n".join(chunks).strip()
    if not text:
        die(f"ERROR: extracted no text from {path}")
    return text


def call_claude(api_key: str, base_url: str | None, headers: dict[str, str], resume_text: str) -> str:
    import anthropic
    kwargs: dict[str, object] = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
    if headers:
        kwargs["default_headers"] = headers
    client = anthropic.Anthropic(**kwargs)
    user_msg = USER_TEMPLATE.replace("{{RESUME_TEXT}}", resume_text)
    resp = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")


def extract_yaml(reply: str) -> str:
    m = re.search(r"<profile_yaml>(.*?)</profile_yaml>", reply, re.DOTALL)
    if not m:
        die("ERROR: Claude response did not contain <profile_yaml> tags. Raw reply:\n" + reply)
    body = m.group(1).strip()
    body = re.sub(r"^```ya?ml\s*\n?|\n?```$", "", body, flags=re.MULTILINE).strip()
    return body


def summarize(profile: dict) -> None:
    import yaml  # noqa: F401
    p = profile.get("person", {}) or {}
    s = profile.get("seniority", {}) or {}
    print("\n=== Extracted profile ===")
    print(f"  Name:        {p.get('name')}")
    print(f"  Title:       {p.get('current_title')}")
    print(f"  Experience:  {p.get('years_total_experience')} years")
    print(f"  Visa:        {p.get('visa_status')}")
    print(f"  Seniority:   {s.get('level')} ({s.get('ic_or_manager')})")
    print(f"  Career arc:  {profile.get('career_arc')}")
    print(f"  Core skills: {len(profile.get('core_skills') or [])}")
    print(f"  Adj skills:  {len(profile.get('adjacent_skills') or [])}")
    print(f"  Targets:     {len(profile.get('target_titles') or [])}")
    print(f"  Red flags:   {len(profile.get('red_flags') or [])}")
    print(f"  Pref signals:{len(profile.get('preferred_signals') or [])}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Parse resume PDF -> structured profile.yaml")
    ap.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="Path to resume PDF")
    ap.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Path to output YAML")
    args = ap.parse_args()

    api_key, base_url, headers = check_api_key()
    print(f"Reading resume: {args.input}")
    text = extract_pdf_text(args.input)
    via = base_url or "api.anthropic.com"
    print(f"Extracted {len(text):,} chars from PDF. Calling Claude ({MODEL}) via {via}...")

    reply = call_claude(api_key, base_url, headers, text)
    yaml_body = extract_yaml(reply)

    import yaml
    try:
        profile = yaml.safe_load(yaml_body)
    except yaml.YAMLError as e:
        die(f"ERROR: Claude returned invalid YAML: {e}\n\n{yaml_body}")
    if not isinstance(profile, dict):
        die("ERROR: parsed YAML is not a mapping at the top level.")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(yaml_body + ("\n" if not yaml_body.endswith("\n") else ""))
    print(f"Wrote {args.output}")
    summarize(profile)


if __name__ == "__main__":
    main()
