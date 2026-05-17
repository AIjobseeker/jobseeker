#!/usr/bin/env python3
"""Parse a DOCX resume into a structured YAML profile.

Run once after editing your resume. Output is profiles/<person>/resume.yaml
which is the source-of-truth for tailored renders. Edit the YAML by hand if
the auto-extraction got something wrong — that's a feature, not a bug.

  python3 scripts/resume_to_yaml.py --input ~/Downloads/MyResume.docx \
                                    --output profiles/sai/resume.yaml

  # or use Claude (internal proxy / direct Anthropic) for higher-fidelity extraction:
  python3 scripts/resume_to_yaml.py --use-claude
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

try:
    from dotenv import load_dotenv

    load_dotenv(REPO / ".env", override=False)
except ImportError:
    pass


SYSTEM_PROMPT = """You are an expert at parsing resumes into structured data. \
Extract EXACTLY what the resume says — do NOT invent, expand, summarise, or \
reword anything. The structured output will be rendered back into a resume so \
fidelity matters more than polish. Output ONLY valid YAML inside <resume_yaml> \
tags. No commentary."""


USER_TEMPLATE = """Parse the resume below into this exact YAML schema. Use the
candidate's literal words; preserve all numbers, dates, employer names,
acronyms exactly as written.

```yaml
person:
  name: ...
  email: ...
  phone: ...
  location: ...                    # city, state — or null
  linkedin: ...                    # URL or null
  github: ...                      # URL or null

summary: |                          # one paragraph; the candidate's existing summary
  ...

experience:
  - company: ...
    title: ...
    location: ...                  # or null
    start: "MMM YYYY"              # or "YYYY"
    end: "MMM YYYY" | "present"
    bullets:                       # EXACT TEXT of each bullet, no rewording
      - "..."
      - "..."

skills:                            # exact technical skill list, comma/period split
  - kubernetes
  - terraform

education:
  - school: ...
    degree: ...
    field: ...                     # or null
    start: ...                     # or null
    end: ...

certifications:                    # or empty list
  - ...

publications: []                   # or list
awards: []
```

CRITICAL:
- Every bullet under `experience[].bullets` MUST appear verbatim in the resume — character for character.
- Do NOT add bullets that aren't in the original.
- Do NOT split, merge, or paraphrase bullets. If a bullet has a typo, keep the typo.
- If a section (e.g. publications) is missing, output an empty list, not made-up content.
- Wrap your YAML in <resume_yaml>...</resume_yaml>.

RESUME TEXT:
---
{{RESUME_TEXT}}
---
"""


def die(msg: str, code: int = 1) -> None:
    print(msg, file=sys.stderr)
    sys.exit(code)


def extract_pdf_text(path: Path) -> str:
    try:
        import pdfplumber
    except ImportError:
        die("pdfplumber not installed — run: pip install pdfplumber")
    with pdfplumber.open(str(path)) as pdf:
        return "\n\n".join((p.extract_text() or "") for p in pdf.pages).strip()


def extract_docx_text(path: Path) -> str:
    from docx import Document

    doc = Document(str(path))
    out: list[str] = []
    for para in doc.paragraphs:
        text = para.text.strip()
        if text:
            out.append(text)
    return "\n".join(out)


def extract_text(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        return extract_pdf_text(path)
    if path.suffix.lower() in (".docx", ".doc"):
        return extract_docx_text(path)
    die(f"unsupported resume format: {path.suffix}")


def claude_extract(resume_text: str) -> str:
    """Use Claude (internal proxy / direct Anthropic) for high-fidelity parsing."""
    from shared.llm_client import claude_chat, available_backend

    backend = available_backend()
    if backend == "none":
        die(
            "ERROR: No Claude backend available.\n\n"
            "Pick ONE:\n\n"
            "  A. Internal SDK (set JOBSEEKER_LLM_INTERNAL_PKG to an importable\n"
            "     package providing `<pkg>.ai.ask(prompt, model, max_tokens)`)\n\n"
            "  B. Internal OAuth proxy (corporate / VPN-gated Claude):\n"
            "     export ANTHROPIC_API_KEY='dummy'\n"
            "     export ANTHROPIC_BASE_URL='https://your-internal-proxy/api/anthropic'\n"
            "     export ANTHROPIC_AUTH_TOKEN='<bearer JWT from your token CLI>'\n\n"
            "  C. Direct Anthropic (paid; needs credit):\n"
            "     https://console.anthropic.com/settings/billing -> add $5\n"
            "     export ANTHROPIC_API_KEY='sk-ant-api03-<real-key>'\n\n"
            "  D. Skip Claude entirely — drop --use-claude to use local Ollama:\n"
            "     python3 scripts/resume_to_yaml.py --input <pdf> --output <yaml>\n"
        )

    user = USER_TEMPLATE.replace("{{RESUME_TEXT}}", resume_text)
    return claude_chat(
        prompt=user,
        model=os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6"),
        max_tokens=8000,
        system=SYSTEM_PROMPT,
    )


def ollama_extract(resume_text: str, model: str) -> str:
    """Fallback: use a local Ollama model. qwen3:14b recommended."""
    import asyncio

    from ollama import AsyncClient

    host = os.environ.get("OLLAMA_HOST", "http://100.115.111.9:11434")
    client = AsyncClient(host=host)

    async def _run() -> str:
        prompt = SYSTEM_PROMPT + "\n\n" + USER_TEMPLATE.replace("{{RESUME_TEXT}}", resume_text)
        resp = await client.chat(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            think=True,
            options={"num_predict": 8000},
        )
        return (resp.message.content or "").strip()

    return asyncio.run(_run())


def extract_yaml_block(reply: str) -> str:
    m = re.search(r"<resume_yaml>(.*?)</resume_yaml>", reply, re.DOTALL)
    if not m:
        die("LLM did not wrap output in <resume_yaml> tags. Raw reply:\n" + reply[:2000])
    body = m.group(1).strip()
    body = re.sub(r"^```ya?ml\s*\n?|\n?```$", "", body, flags=re.MULTILINE).strip()
    return body


def validate_no_hallucination(parsed: dict, resume_text: str) -> list[str]:
    """Verify every bullet appears verbatim in the source. Return warnings."""
    warns: list[str] = []
    text_lower = resume_text.lower()
    for role in parsed.get("experience", []) or []:
        company = role.get("company", "?")
        title = role.get("title", "?")
        for b in role.get("bullets", []) or []:
            # Be lenient: trim leading dashes/bullets, normalize whitespace
            needle = re.sub(r"^[-•*\s]+", "", b).strip().lower()
            needle = re.sub(r"\s+", " ", needle)
            haystack = re.sub(r"\s+", " ", text_lower)
            if needle and needle[:60] not in haystack:
                warns.append(f"bullet may be hallucinated under {company}/{title}: {b[:80]!r}")
    for skill in parsed.get("skills", []) or []:
        if str(skill).lower() not in text_lower:
            warns.append(f"skill not found in source: {skill!r}")
    return warns


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", type=Path,
                    default=Path.home() / "Downloads" / "Narvaneni Saikrishna94 resume.docx")
    ap.add_argument("--output", type=Path,
                    default=REPO / "profiles" / "sai" / "resume.yaml")
    ap.add_argument("--use-claude", action="store_true",
                    help="Use Claude instead of Ollama (higher fidelity)")
    ap.add_argument("--ollama-model", default="qwen3:14b")
    args = ap.parse_args()

    if not args.input.exists():
        die(f"resume not found: {args.input}")

    print(f"Reading {args.input}")
    text = extract_text(args.input)
    if not text:
        die("extracted no text from resume")
    print(f"  {len(text):,} chars extracted")

    print(f"Parsing via {'Claude' if args.use_claude else 'Ollama qwen3:14b'} ...")
    if args.use_claude:
        reply = claude_extract(text)
    else:
        reply = ollama_extract(text, args.ollama_model)
    yaml_body = extract_yaml_block(reply)

    import yaml as yamllib

    try:
        parsed = yamllib.safe_load(yaml_body)
    except yamllib.YAMLError as e:
        die(f"LLM produced invalid YAML: {e}\n\n{yaml_body[:2000]}")
    if not isinstance(parsed, dict):
        die("parsed YAML is not a mapping")

    warns = validate_no_hallucination(parsed, text)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(yaml_body + ("\n" if not yaml_body.endswith("\n") else ""))

    print(f"\nWrote {args.output}")
    p = parsed.get("person", {}) or {}
    print(f"  name:   {p.get('name')}")
    print(f"  email:  {p.get('email')}")
    print(f"  roles:  {len(parsed.get('experience', []) or [])}")
    print(f"  skills: {len(parsed.get('skills', []) or [])}")
    if warns:
        print(f"\n{len(warns)} hallucination warnings (review the YAML):")
        for w in warns[:10]:
            print(f"  - {w}")
        if len(warns) > 10:
            print(f"  ... +{len(warns) - 10} more")
    else:
        print("\nNo hallucination warnings — every bullet maps back to the source.")
    print(f"\nNext: edit {args.output} by hand if anything is wrong.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
