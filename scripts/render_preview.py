"""Company-themed preview HTML — for sharing via Drive link, NOT ATS upload.

The ATS-uploadable file is `resume.tailored.{html,docx}` — plain, single-column,
no themes. THIS file (`resume.preview.html`) is what you share when you DM a
recruiter, post in a referral channel, or attach to a cold email. It carries
a subtle company-flavored accent (Apple = charcoal/silver, Netflix = red/black,
Google = Material blue, etc.) without breaking single-column layout.

Why two files: cold-application uploads go through ATS parsers that mangle
fancy CSS; warm intros are read by humans on a laptop and a polished look
genuinely lifts your odds. This module produces the second.

The accent is applied to:
  - top header band (subtle gradient or solid)
  - name + section headings (accent color)
  - role-title hover/border accents
  - link underlines

Body text stays plain and parseable so even if a recruiter forwards this
file to their ATS, the content survives.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path


# ── Per-company themes ────────────────────────────────────────────────────
# Each theme has:
#   accent     - section headings, name, dividers
#   accent_2   - secondary accent (links, highlights)
#   bg_band    - top header band (subtle, never overwhelming)
#   text       - body color
#   muted      - dates / contact subtle text
#   font       - font stack (web-safe + nice fallback)
#   tag        - short word printed in the meta line — internal only, no AI tell
#                ("" means no tag rendered)
THEMES: dict[str, dict[str, str]] = {
    # Apple — minimal, lots of whitespace, charcoal + silver.
    "apple": {
        "accent": "#1d1d1f", "accent_2": "#0066cc",
        "bg_band": "linear-gradient(180deg, #f5f5f7 0%, #ffffff 100%)",
        "text": "#1d1d1f", "muted": "#6e6e73",
        "font": '"SF Pro Display", "SF Pro Text", -apple-system, BlinkMacSystemFont, "Helvetica Neue", sans-serif',
    },
    # Google — Material blue, Roboto.
    "google": {
        "accent": "#1a73e8", "accent_2": "#ea4335",
        "bg_band": "linear-gradient(90deg, #4285f4 0%, #34a853 25%, #fbbc04 50%, #ea4335 75%, #4285f4 100%)",
        "text": "#202124", "muted": "#5f6368",
        "font": '"Google Sans", "Roboto", "Helvetica Neue", sans-serif',
    },
    "alphabet": {
        "accent": "#1a73e8", "accent_2": "#ea4335",
        "bg_band": "linear-gradient(90deg, #4285f4 0%, #34a853 25%, #fbbc04 50%, #ea4335 75%, #4285f4 100%)",
        "text": "#202124", "muted": "#5f6368",
        "font": '"Google Sans", "Roboto", "Helvetica Neue", sans-serif',
    },
    # Netflix — dark mode card, red accent.
    "netflix": {
        "accent": "#e50914", "accent_2": "#ffffff",
        "bg_band": "linear-gradient(180deg, #141414 0%, #221f1f 100%)",
        "text": "#221f1f", "muted": "#737373",
        "font": '"Netflix Sans", "Helvetica Neue", "Arial", sans-serif',
        "dark_header": "1",
    },
    # Meta / Facebook — blue.
    "meta": {
        "accent": "#1877f2", "accent_2": "#42b72a",
        "bg_band": "linear-gradient(90deg, #0866ff 0%, #1877f2 100%)",
        "text": "#1c1e21", "muted": "#65676b",
        "font": '"SF Pro Text", "Helvetica Neue", "Segoe UI", sans-serif',
    },
    "facebook": {
        "accent": "#1877f2", "accent_2": "#42b72a",
        "bg_band": "linear-gradient(90deg, #0866ff 0%, #1877f2 100%)",
        "text": "#1c1e21", "muted": "#65676b",
        "font": '"SF Pro Text", "Helvetica Neue", "Segoe UI", sans-serif',
    },
    # Amazon — orange + dark blue.
    "amazon": {
        "accent": "#232f3e", "accent_2": "#ff9900",
        "bg_band": "linear-gradient(180deg, #232f3e 0%, #131a22 100%)",
        "text": "#0f1111", "muted": "#565959",
        "font": '"Amazon Ember", "Helvetica Neue", "Arial", sans-serif',
        "dark_header": "1",
    },
    # Microsoft — segmented header with the four-square accent.
    "microsoft": {
        "accent": "#0078d4", "accent_2": "#107c10",
        "bg_band": "linear-gradient(90deg, #f25022 0%, #f25022 25%, #7fba00 25%, #7fba00 50%, #00a4ef 50%, #00a4ef 75%, #ffb900 75%, #ffb900 100%)",
        "text": "#252525", "muted": "#605e5c",
        "font": '"Segoe UI", "Helvetica Neue", "Arial", sans-serif',
    },
    # Stripe — purple, modern.
    "stripe": {
        "accent": "#635bff", "accent_2": "#0a2540",
        "bg_band": "linear-gradient(135deg, #635bff 0%, #0a2540 100%)",
        "text": "#0a2540", "muted": "#425466",
        "font": '"Sohne", "Inter", "Helvetica Neue", sans-serif',
        "dark_header": "1",
    },
    # OpenAI — clean dark + soft green.
    "openai": {
        "accent": "#10a37f", "accent_2": "#202123",
        "bg_band": "linear-gradient(180deg, #202123 0%, #353740 100%)",
        "text": "#202123", "muted": "#6e6e80",
        "font": '"Söhne", "Inter", "Helvetica Neue", sans-serif',
        "dark_header": "1",
    },
    # Anthropic — warm cream, subtle orange/red accent.
    "anthropic": {
        "accent": "#d97757", "accent_2": "#1f1f1d",
        "bg_band": "linear-gradient(180deg, #f4f0ec 0%, #ffffff 100%)",
        "text": "#1f1f1d", "muted": "#6b6b6b",
        "font": '"Tiempos", "Inter", "Helvetica Neue", serif',
    },
    # Hugging Face — yellow, friendly.
    "hugging face": {
        "accent": "#ff9d00", "accent_2": "#1c1c1c",
        "bg_band": "linear-gradient(90deg, #ffd21e 0%, #ff9d00 100%)",
        "text": "#1c1c1c", "muted": "#6b6b6b",
        "font": '"IBM Plex Sans", "Inter", "Helvetica Neue", sans-serif',
    },
    "huggingface": {
        "accent": "#ff9d00", "accent_2": "#1c1c1c",
        "bg_band": "linear-gradient(90deg, #ffd21e 0%, #ff9d00 100%)",
        "text": "#1c1c1c", "muted": "#6b6b6b",
        "font": '"IBM Plex Sans", "Inter", "Helvetica Neue", sans-serif',
    },
    # NVIDIA — green/black.
    "nvidia": {
        "accent": "#76b900", "accent_2": "#000000",
        "bg_band": "linear-gradient(180deg, #000000 0%, #1a1a1a 100%)",
        "text": "#1a1a1a", "muted": "#666666",
        "font": '"NVIDIA Sans", "Inter", "Helvetica Neue", sans-serif',
        "dark_header": "1",
    },
    # Airbnb — coral.
    "airbnb": {
        "accent": "#ff5a5f", "accent_2": "#484848",
        "bg_band": "linear-gradient(135deg, #ff5a5f 0%, #fc642d 100%)",
        "text": "#484848", "muted": "#767676",
        "font": '"Cereal", "Circular", "Helvetica Neue", sans-serif',
    },
    # Uber — black + white.
    "uber": {
        "accent": "#000000", "accent_2": "#06c167",
        "bg_band": "linear-gradient(180deg, #000000 0%, #1a1a1a 100%)",
        "text": "#000000", "muted": "#545454",
        "font": '"UberMove", "Helvetica Neue", "Arial", sans-serif',
        "dark_header": "1",
    },
    # LinkedIn — corporate blue.
    "linkedin": {
        "accent": "#0a66c2", "accent_2": "#057642",
        "bg_band": "linear-gradient(90deg, #0a66c2 0%, #004182 100%)",
        "text": "#0c0d0e", "muted": "#56687a",
        "font": '"Source Sans 3", "Helvetica Neue", "Arial", sans-serif',
        "dark_header": "1",
    },
    # Snowflake — blue.
    "snowflake": {
        "accent": "#29b5e8", "accent_2": "#11567f",
        "bg_band": "linear-gradient(135deg, #11567f 0%, #29b5e8 100%)",
        "text": "#11567f", "muted": "#5c6b73",
        "font": '"Inter", "Helvetica Neue", sans-serif',
        "dark_header": "1",
    },
    # Spotify — green.
    "spotify": {
        "accent": "#1db954", "accent_2": "#191414",
        "bg_band": "linear-gradient(180deg, #191414 0%, #121212 100%)",
        "text": "#191414", "muted": "#6a6a6a",
        "font": '"Circular", "Helvetica Neue", sans-serif',
        "dark_header": "1",
    },
    # Generic fallback — Inter + navy. Same as the polished base template.
    "default": {
        "accent": "#0a2540", "accent_2": "#0066cc",
        "bg_band": "linear-gradient(180deg, #f5f7fa 0%, #ffffff 100%)",
        "text": "#1f2328", "muted": "#5f6368",
        "font": '"Inter", "Helvetica Neue", "Arial", sans-serif',
    },
}


def pick_theme(company: str) -> tuple[str, dict[str, str]]:
    """Return (theme_name, theme_dict) for the given company name. Falls
    back to 'default' if no match.
    """
    key = (company or "").lower().strip()
    # Direct hit first
    if key in THEMES:
        return key, THEMES[key]
    # Substring scan — lets "Apple Inc.", "Apple, Inc.", "Apple Music" all
    # match the apple theme.
    for k in THEMES:
        if k == "default":
            continue
        if k in key:
            return k, THEMES[k]
    return "default", THEMES["default"]


# ── Render ────────────────────────────────────────────────────────────────

PREVIEW_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{name} - Resume</title>
<style>
  @page {{ size: Letter; margin: 0.45in 0.55in; }}
  * {{ box-sizing: border-box; }}
  body {{
    font-family: {font};
    font-size: 10.5pt;
    color: {text};
    line-height: 1.45;
    margin: 0; padding: 0;
    background: #ffffff;
  }}
  .header {{
    background: {bg_band};
    padding: 22pt 28pt 18pt 28pt;
    margin: 0 0 10pt 0;
    {dark_header_style}
  }}
  .name {{
    font-size: 24pt; font-weight: 700; letter-spacing: -0.5px;
    color: {header_text}; margin: 0 0 4pt 0;
  }}
  .contact {{
    font-size: 9.5pt; color: {header_muted};
    letter-spacing: 0.2px;
  }}
  .body-wrap {{ padding: 0 28pt 24pt 28pt; }}
  h2 {{
    font-size: 10pt; font-weight: 700; text-transform: uppercase;
    letter-spacing: 1.6px; color: {accent};
    border-bottom: 1.5pt solid {accent};
    padding-bottom: 3pt; margin: 14pt 0 6pt 0;
  }}
  .summary {{ margin: 0 0 4pt 0; line-height: 1.5; }}
  .highlights {{ margin: 0; padding-left: 18pt; }}
  .highlights li {{ margin-bottom: 3pt; }}
  .role {{ margin-bottom: 10pt; }}
  .role-head {{
    display: flex; justify-content: space-between; align-items: baseline;
    margin-bottom: 2pt;
  }}
  .role-head .who {{
    font-weight: 700; color: {accent}; font-size: 10.5pt;
  }}
  .role-head .when {{
    font-size: 9.5pt; color: {muted};
    font-variant-numeric: tabular-nums;
  }}
  .role-sub {{
    font-style: italic; color: {muted}; font-size: 9.5pt;
    margin-bottom: 3pt;
  }}
  ul {{ margin: 0; padding-left: 18pt; }}
  li {{ margin-bottom: 2pt; }}
  .skills {{ font-size: 10pt; }}
  .skills .group {{ margin-bottom: 4pt; }}
  .skills .group-name {{
    font-weight: 700; color: {accent};
    min-width: 9em; display: inline-block;
  }}
  a {{ color: {accent_2}; text-decoration: none;
       border-bottom: 0.5pt solid {accent_2}; }}
  strong {{ color: {accent}; }}
</style>
</head>
<body>
<div class="header">
  <div class="name">{name}</div>
  <div class="contact">{contact}</div>
</div>
<div class="body-wrap">
<h2>Summary</h2>
<div class="summary">{summary}</div>

{highlights_section}

<h2>Experience</h2>
{experience_html}

{skills_section}

{certifications_section}

{education_section}
</div>
</body>
</html>
"""


def render_preview_html(
    *,
    resume: dict,
    plan: dict,
    out_path: Path,
    company: str,
    show_highlights: bool = False,
) -> tuple[Path, str]:
    """Render `resume.preview.html` themed for `company`.

    Returns (path_written, theme_name). Re-uses the rendering helpers from
    tailor_v2 so the content is identical to the ATS file.
    """
    # Local import to avoid a cycle at module-load time.
    from scripts.tailor_v2 import (
        _esc, _build_contact, _render_highlights, _render_experience,
        _render_skills, _render_certifications, _render_education,
    )

    theme_name, theme = pick_theme(company)
    person = resume.get("person", {}) or {}

    # Whether the header band is dark — flips name + contact text colors.
    dark = theme.get("dark_header") == "1"
    header_text = "#ffffff" if dark else theme["accent"]
    header_muted = "#e5e5e5" if dark else theme["muted"]
    dark_header_style = "color: #ffffff;" if dark else ""

    summary = (plan.get("summary") or "").strip()
    emphasize = plan.get("skills_to_emphasize") or []
    highlights_html = (
        _render_highlights(resume, plan.get("archetype") or "")
        if show_highlights else ""
    )

    html = PREVIEW_TEMPLATE.format(
        name=_esc(person.get("name", "")),
        contact=_build_contact(person),
        summary=_esc(summary),
        highlights_section=highlights_html,
        experience_html=_render_experience(resume, plan.get("experience") or []),
        skills_section=_render_skills(resume, emphasize),
        certifications_section=_render_certifications(resume),
        education_section=_render_education(resume),
        font=theme["font"],
        text=theme["text"],
        muted=theme["muted"],
        accent=theme["accent"],
        accent_2=theme["accent_2"],
        bg_band=theme["bg_band"],
        header_text=header_text,
        header_muted=header_muted,
        dark_header_style=dark_header_style,
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    return out_path, theme_name
