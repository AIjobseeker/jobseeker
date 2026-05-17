#!/usr/bin/env python3
"""Show what changed between your base resume and the tailored one.

When the LLM does its job, the differences should be visible:
  - dropped bullets (struck-through in red)
  - reordered bullets (called out)
  - kept bullets (gray)
  - new summary (replaces old summary entirely)

If `diff_resume.py` shows nothing changed, the tailorer either failed or
qwen3 was too conservative. Switch to Claude (internal proxy / direct
Anthropic) or run with stricter prompts.

  python3 scripts/diff_resume.py
  python3 scripts/diff_resume.py --base profiles/sai/resume.yaml \
                                 --tailored /tmp/jobseeker_demo/resume.tailored.html
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


R = "\033[31m"      # red — dropped
G = "\033[32m"      # green — new
Y = "\033[33m"      # yellow — reordered
D = "\033[2m"       # dim — kept
B = "\033[1m"       # bold
X = "\033[0m"       # reset


def _bullets_from_html(html_path: Path) -> dict[str, list[str]]:
    """Pull each role's bullets out of the rendered HTML.

    Returns {role_header: [bullet1, bullet2, ...]}. Order matters.
    Cheap regex parse — DOM is small and we control the template.
    """
    text = html_path.read_text()
    out: dict[str, list[str]] = {}
    for role_block in re.finditer(
        r'<div class="role">(.*?)</div>\s*</div>', text, re.DOTALL
    ):
        block = role_block.group(1)
        head = re.search(r'<div class="role-head"><span>(.*?)</span>', block)
        if not head:
            continue
        role_label = re.sub(r"&middot;", "-", head.group(1)).strip()
        bullets = re.findall(r"<li>(.*?)</li>", block, re.DOTALL)
        out[role_label] = [
            re.sub(r"<[^>]+>", "", b).strip() for b in bullets
        ]
    return out


def _summary_from_html(html_path: Path) -> str:
    text = html_path.read_text()
    m = re.search(r'<div class="summary">(.*?)</div>', text, re.DOTALL)
    return m.group(1).strip() if m else ""


def _bullets_from_yaml(yaml_path: Path) -> dict[str, list[str]]:
    import yaml

    data = yaml.safe_load(yaml_path.read_text()) or {}
    out: dict[str, list[str]] = {}
    for role in data.get("experience", []) or []:
        label = f"{role.get('company', '?')} - {role.get('title', '?')}"
        out[label] = list(role.get("bullets", []) or [])
    return out


def _norm(b: str) -> str:
    return re.sub(r"\s+", " ", b.strip().lower())


def diff_role(role_label: str, base: list[str], tailored: list[str]) -> int:
    """Print a side-by-side diff for one role. Returns the count of changes."""
    print(f"\n{B}{role_label}{X}")
    base_norms = [_norm(b) for b in base]
    tailored_norms = [_norm(b) for b in tailored]
    base_set = set(base_norms)
    tailored_set = set(tailored_norms)

    dropped = [b for b in base if _norm(b) not in tailored_set]
    kept_in_order = [b for b in tailored if _norm(b) in base_set]
    new_bullets = [b for b in tailored if _norm(b) not in base_set]

    # Reorder check: did kept bullets shift position?
    reordered = False
    if base and kept_in_order:
        base_order_kept = [b for b in base if _norm(b) in {_norm(k) for k in kept_in_order}]
        if [_norm(b) for b in base_order_kept] != [_norm(b) for b in kept_in_order]:
            reordered = True

    print(f"  {D}base: {len(base)} bullets  ->  tailored: {len(tailored)} bullets"
          f"  (dropped {len(dropped)}, "
          f"{'reordered' if reordered else 'same order'})  {X}")

    for b in tailored:
        prefix = "  +" if _norm(b) in {_norm(n) for n in new_bullets} else "  ."
        color = G if prefix == "  +" else D
        print(f"{color}{prefix} {b[:140]}{X}")
    for b in dropped:
        print(f"{R}  - {b[:140]}{X}")
    if new_bullets:
        # New bullets shouldn't exist with select-only tailoring; flag them.
        print(f"{R}  WARNING: {len(new_bullets)} bullets in tailored output do not "
              f"match any base bullet (possible hallucination){X}")
    return len(dropped) + (1 if reordered else 0) + len(new_bullets)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", type=Path,
                    default=REPO / "profiles" / "sai" / "resume.yaml")
    ap.add_argument("--tailored", type=Path,
                    default=Path("/tmp/jobseeker_demo/resume.tailored.html"))
    args = ap.parse_args()

    if not args.base.exists():
        print(f"base resume not found: {args.base}", file=sys.stderr)
        return 1
    if not args.tailored.exists():
        print(f"tailored output not found: {args.tailored}", file=sys.stderr)
        print(f"  run scripts/tailor_v2.py first", file=sys.stderr)
        return 1

    print(f"{B}Resume diff{X}")
    print(f"  base:     {args.base}")
    print(f"  tailored: {args.tailored}")

    # Summary
    base_summary = ""
    try:
        import yaml
        base_data = yaml.safe_load(args.base.read_text()) or {}
        base_summary = (base_data.get("summary") or "").strip()
    except Exception:
        pass
    tailored_summary = _summary_from_html(args.tailored)
    print(f"\n{B}Summary{X}")
    print(f"{R}- {base_summary[:200] or '(none in base)'}{X}")
    print(f"{G}+ {tailored_summary[:200] or '(none in tailored)'}{X}")

    # Bullets per role
    base_bullets = _bullets_from_yaml(args.base)
    tailored_bullets = _bullets_from_html(args.tailored)
    total_changes = 0
    for label, base in base_bullets.items():
        tailored = tailored_bullets.get(label) or tailored_bullets.get(label.replace(" - ", " - "), [])
        # Try fuzzy match: any tailored role label that contains base company name
        if not tailored:
            for tlabel, tbullets in tailored_bullets.items():
                if label.split(" - ")[0].lower() in tlabel.lower():
                    tailored = tbullets
                    break
        total_changes += diff_role(label, base, tailored)

    print(f"\n{B}Total changes:{X} {total_changes}")
    if total_changes == 0:
        print(f"{Y}WARNING: tailorer produced no visible changes. Likely causes:{X}")
        print(f"  - LLM was too conservative (kept all bullets in original order)")
        print(f"  - JSON parsing failed silently — check tailor_v2.py output")
        print(f"  - Try: --use-claude (better reasoning) or stricter prompts")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
