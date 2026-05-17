#!/usr/bin/env python3
"""
demo.py — end-to-end pipeline simulation with fake data.

Runs the complete flow: 30 fake jobs through scoring, dedup, and Telegram
formatting. Prints a colored terminal dashboard. No network, no Ollama, no
Telegram needed. Use this to:

  - See the full pipeline in <2 seconds
  - Validate the data flow if you change schema
  - Show stakeholders what the system does

  python3 scripts/demo.py
  python3 scripts/demo.py --verbose       # show every job
  python3 scripts/demo.py --top 10        # show only top 10 matches
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


# ANSI colors
G = "\033[32m"
Y = "\033[33m"
R = "\033[31m"
C = "\033[36m"
M = "\033[35m"
B = "\033[1m"
D = "\033[2m"
X = "\033[0m"


# ─── Stub user profile ─────────────────────────────────────────────────────
PROFILE = {
    "person": {"name": "Saikrishna Narvaneni"},
    "seniority": {"level": "staff"},
    "core_skills": [
        "kubernetes", "terraform", "aws", "linux",
        "python", "go", "ci/cd", "prometheus", "observability",
    ],
    "adjacent_skills": ["snowflake", "kafka", "spark", "pulumi"],
    "target_titles": [
        "site reliability engineer", "platform engineer",
        "devops engineer", "infrastructure engineer",
        "staff sre", "staff software engineer",
        "cloud engineer", "cloud architect",
    ],
    "red_flags": ["manager", "director", "vp", "sales", "account executive"],
    "preferred_signals": ["h1b transfer", "visa sponsorship"],
    "profile_summary": (
        "Staff-level SRE/Platform engineer with 8 years building Kubernetes "
        "platforms, Terraform infrastructure, and observability stacks on AWS."
    ),
}

# ─── Realistic-shaped fake jobs spanning the whole match-quality spectrum ──
FAKE_JOBS = [
    # Strong matches (should score 0.7+) — all US-based
    ("Stripe", "Staff Site Reliability Engineer, Platform",
     "Build and operate the SRE platform serving Stripe's global infrastructure. "
     "Strong kubernetes, terraform, aws, observability experience required. "
     "We sponsor H1B transfers.", "San Francisco, CA"),
    ("Datadog", "Senior Platform Engineer, Kubernetes",
     "Lead the team building our internal kubernetes platform. terraform, ci/cd, "
     "prometheus stack. 8+ years infrastructure experience.", "New York, NY"),
    ("Cloudflare", "Staff SRE, Edge Network",
     "Operate one of the largest edge networks. linux deep expertise, observability, "
     "go programming, kubernetes.", "Austin, TX"),
    ("Anthropic", "Site Reliability Engineer",
     "Reliability for Claude. python, kubernetes, observability, prometheus. "
     "Visa sponsorship available.", "San Francisco, CA"),
    ("Snowflake", "Cloud Engineer, Infrastructure",
     "Design and operate Snowflake's cloud infrastructure. terraform, aws, "
     "kubernetes, ci/cd.", "Bellevue, WA"),

    # Medium matches (0.4-0.7) — adjacent or wrong seniority
    ("OpenAI", "Senior Software Engineer, Backend",
     "Build the API serving ChatGPT and our model APIs. python, go, kubernetes.",
     "San Francisco, CA"),
    ("Databricks", "Engineering Manager, Platform Infrastructure",
     "Lead a team of platform engineers. kubernetes, terraform, aws background.",
     "San Francisco, CA"),
    ("GitLab", "Software Engineer, Backend",
     "Work on GitLab's CI/CD core. ruby, ci/cd, kubernetes. Remote-first.",
     "Remote (US)"),
    ("Discord", "Junior Software Engineer",
     "Build user-facing features. javascript, react. Some backend python.",
     "San Francisco, CA"),
    ("Coinbase", "Staff Engineer, Trading Systems",
     "Low-latency systems for crypto trading. c++ deep expertise. python a plus.",
     "Remote, EST"),

    # Weak matches (should score < 0.4)
    ("Stripe", "Account Executive, Enterprise",
     "Sell Stripe to enterprise customers. 5+ years sales experience required.",
     "New York, NY"),
    ("Datadog", "Director of Engineering",
     "Lead the SRE org of 30+ engineers. People management, strategic planning.",
     "Boston, MA"),
    ("Snowflake", "Marketing Manager, Demand Gen",
     "Drive demand-gen campaigns for Snowflake. marketing automation expertise.",
     "Remote"),
    ("Figma", "Product Designer, Design Systems",
     "Design system at Figma. figma deep usage, typography.",
     "San Francisco, CA"),
    ("Tesla", "Manufacturing Operations Manager",
     "Run the factory floor at Fremont. lean manufacturing, six sigma.",
     "Fremont, CA"),

    # No-sponsorship trap (should be capped low even with great title)
    ("Acme Corp", "Staff Site Reliability Engineer, Platform",
     "Strong technical match: kubernetes, terraform, aws, observability. "
     "Note: we cannot sponsor visas at this time. US citizens only.",
     "Seattle, WA"),
    ("Beta Corp", "Senior Platform Engineer",
     "Looking for senior platform engineer. terraform, aws, kubernetes. "
     "Must be authorized to work in the US without future sponsorship.",
     "Boston, MA"),

    # Duplicate (test dedup)
    ("Stripe", "Staff Site Reliability Engineer, Platform",
     "[duplicate posting — test dedup] Build and operate the SRE platform.",
     "San Francisco, CA"),

    # Non-US: India (allowed, penalized)
    ("Stripe", "Site Reliability Engineer, Platform",
     "kubernetes terraform aws observability — Bangalore office.",
     "Bangalore, India"),
    ("Microsoft", "Senior SRE",
     "kubernetes terraform — Hyderabad team.",
     "Hyderabad"),

    # Non-US: rejected (UK, Canada, EU)
    ("Cloudflare", "Staff SRE",
     "kubernetes terraform aws — UK office.",
     "London, UK"),
    ("Shopify", "Senior Platform Engineer",
     "kubernetes terraform — Canadian team.",
     "Toronto, Canada"),
    ("SAP", "Site Reliability Engineer",
     "kubernetes — Walldorf HQ.",
     "Berlin, Germany"),

    # More US-based matches
    ("Roblox", "Site Reliability Engineer",
     "SRE at Roblox. kubernetes, observability.",
     "San Mateo, CA"),
    ("Pinterest", "Staff SRE",
     "Staff SRE position at Pinterest. terraform, aws.",
     "San Francisco, CA"),
    ("Lyft", "Software Engineer III, Distributed Systems",
     "Work on Lyft's distributed systems infrastructure. go, kubernetes, observability.",
     "Seattle, WA"),
    ("Spotify", "Backend Engineer, Platform",
     "Backend platform at Spotify. java, kubernetes, ci/cd.",
     "New York, NY"),
    ("Reddit", "Staff Software Engineer, Search Infrastructure",
     "Lead search infra. python, elasticsearch, kubernetes.",
     "Remote (US)"),
    ("Robinhood", "Senior Site Reliability Engineer",
     "SRE at Robinhood. kubernetes, terraform, ci/cd.",
     "Menlo Park, CA"),
    ("Vercel", "Site Reliability Engineer",
     "SRE at Vercel. kubernetes, observability, edge networking.",
     "Remote, PST"),
]


def _build_jobs() -> list[dict]:
    jobs = []
    for i, item in enumerate(FAKE_JOBS):
        company, title, desc = item[0], item[1], item[2]
        loc = item[3] if len(item) > 3 else "Remote / San Francisco, CA"
        jobs.append({
            "id": f"uuid-demo-{i:03d}",
            "source_id": f"src-{abs(hash((company, title))) % 100000}",
            "source": "greenhouse",
            "company": company,
            "title": title,
            "description_text": desc,
            "url": f"https://{company.lower().replace(' ', '')}.com/jobs/{i:03d}",
            "location": loc,
            "department": "Infrastructure",
            "remote": "remote" in loc.lower(),
            "scraped_at": "2026-05-17T15:30:00Z",
        })
    return jobs


def _make_scorer(workdir: Path):
    from services.scorer.scorer import ProfileScorer
    from services.scorer import scorer as scorer_mod

    profile_path = workdir / "profile.parsed.yaml"
    profile_path.write_text(yaml.safe_dump(PROFILE))

    # Deterministic but distinct embedding per job text — so semantic scores
    # actually distinguish between strong and weak matches in this demo.
    class FakeModel:
        def __init__(self):
            # one fixed profile vector
            rng = np.random.default_rng(0)
            self.profile_vec = rng.standard_normal(384).astype(np.float32)

        def encode(self, texts, **_):
            outs = []
            for t in texts:
                if not isinstance(t, str):
                    t = str(t)
                lower = t.lower()
                # baseline noise
                rng = np.random.default_rng(abs(hash(t)) % (2**32))
                vec = rng.standard_normal(384).astype(np.float32) * 0.4
                # nudge toward profile when the text looks SRE-ish
                strength = 0.0
                for kw in ["kubernetes", "k8s", "terraform", "aws", "platform",
                           "site reliability", "sre", "observability",
                           "infrastructure", "devops", "prometheus"]:
                    if kw in lower:
                        strength += 0.08
                strength = min(strength, 0.9)
                vec = vec + self.profile_vec * strength
                outs.append(vec)
            return np.array(outs)

    fake = FakeModel()
    # Store it before monkey-patching since the scorer's _load_or_build
    # call hits _load_model() during __init__ to build the profile embedding.
    monkey = MagicMock(side_effect=lambda *a, **k: fake)
    ProfileScorer._load_model = lambda self: fake  # type: ignore[method-assign]
    return ProfileScorer(profile_yaml_path=profile_path, cache_dir=workdir / ".cache")


def _print_pipeline_diagram() -> None:
    print(f"\n{B}Pipeline:{X}")
    print(f"  {C}fake jobs{X}  ->  {C}scorer{X}  ->  {C}dedup{X}  ->  {C}telegram format{X}")
    print()


def _color_score(s: float) -> str:
    if s >= 0.65:
        return f"{G}{s:.2f}{X}"
    if s >= 0.40:
        return f"{Y}{s:.2f}{X}"
    return f"{R}{s:.2f}{X}"


def main() -> int:
    ap = argparse.ArgumentParser(description="End-to-end pipeline demo with fake data")
    ap.add_argument("--top", type=int, default=10, help="Show top N matches in dashboard")
    ap.add_argument("--verbose", "-v", action="store_true", help="Show every scored job")
    ap.add_argument("--threshold", type=float, default=0.65, help="Match threshold (0-1)")
    args = ap.parse_args()

    print(f"\n{B}{'═'*72}{X}")
    print(f"{B}  JobSeeker pipeline demo — fake data, no network needed{X}")
    print(f"{B}{'═'*72}{X}")
    _print_pipeline_diagram()

    # ─── Stage 1: scorer ────────────────────────────────────────────────────
    with tempfile.TemporaryDirectory() as td:
        workdir = Path(td)
        scorer = _make_scorer(workdir)
        jobs = _build_jobs()

        print(f"  {B}Stage 1 — scoring{X}: {len(jobs)} jobs against your profile")
        print(f"    profile: {PROFILE['person']['name']}, "
              f"seniority={PROFILE['seniority']['level']}, "
              f"{len(PROFILE['core_skills'])} core skills")

        scored = scorer.score_batch(jobs)
        print(f"  {G}->{X} all jobs scored\n")

        # ─── Stage 2: dedup ────────────────────────────────────────────────
        from services.notifier.dedup import DedupStore, compute_key
        from services.notifier.models import ScoredJob as NotifierSJ

        db_path = workdir / "demo.db"
        store = DedupStore(db_path)

        new_jobs = []
        dropped_dup = 0
        rejected_no_src = 0
        for sj in scored:
            payload = json.loads(sj.model_dump_json())
            try:
                parsed = NotifierSJ.model_validate(payload)
            except Exception:
                rejected_no_src += 1
                continue
            company, src = parsed.dedup_key_inputs
            if not src:
                rejected_no_src += 1
                continue
            key = compute_key(company, src)
            is_new = store.insert_if_new(
                key=key, company=parsed.job.company, title=parsed.job.title,
                url=parsed.job.url, score=parsed.score,
            )
            if is_new:
                new_jobs.append((sj, parsed))
            else:
                dropped_dup += 1

        print(f"  {B}Stage 2 — dedup{X}: SQLite store at {db_path}")
        print(f"    new: {G}{len(new_jobs)}{X}, "
              f"duplicates dropped: {Y}{dropped_dup}{X}, "
              f"rejected (no source_id): {R}{rejected_no_src}{X}\n")

        # ─── Stage 3: filter by score threshold (Telegram min) ─────────────
        eligible = [(sj, p) for sj, p in new_jobs if sj.score >= args.threshold]
        print(f"  {B}Stage 3 — filter by threshold{X} (min score = {args.threshold}):")
        print(f"    above threshold: {G}{len(eligible)}{X}, "
              f"below: {D}{len(new_jobs) - len(eligible)}{X}\n")

        # ─── Stage 4: format for Telegram ──────────────────────────────────
        from services.notifier.telegram_dispatch import format_message

        print(f"  {B}Stage 4 — Telegram format{X}: {len(eligible)} alerts would be sent\n")

        # ─── Dashboard view ────────────────────────────────────────────────
        print(f"{B}{'─'*72}{X}")
        print(f"{B}  Top matches (would trigger Telegram alert){X}")
        print(f"{B}{'─'*72}{X}\n")

        eligible.sort(key=lambda x: -x[0].score)
        for sj, parsed in eligible[:args.top]:
            sk = ", ".join(sj.matched_skills[:5]) or "-"
            adj = " ".join(f"{Y}{k}{X}={D}{v:+.2f}{D}{X}" for k, v in sj.rule_adjustments.items())
            print(f"  {_color_score(sj.score)}  {C}{parsed.job.company:14s}{X}  {parsed.job.title}")
            print(f"         {D}skills: {sk}{X}")
            if adj:
                print(f"         {D}adjustments: {adj}{X}")

        # ─── Show a sample Telegram message ────────────────────────────────
        if eligible:
            print(f"\n{B}{'─'*72}{X}")
            print(f"{B}  Sample Telegram message (top match){X}")
            print(f"{B}{'─'*72}{X}\n")
            top_sj, top_parsed = eligible[0]
            msg = format_message(top_parsed)
            for line in msg.splitlines():
                print(f"  {line}")

        # ─── Verbose: list everything ─────────────────────────────────────
        if args.verbose:
            print(f"\n{B}{'─'*72}{X}")
            print(f"{B}  All scored jobs (sorted by score){X}")
            print(f"{B}{'─'*72}{X}\n")
            scored_sorted = sorted(scored, key=lambda s: -s.score)
            for sj in scored_sorted:
                job = sj.job
                print(f"  {_color_score(sj.score)}  {C}{job['company']:14s}{X}  {job['title']}")
                if sj.rule_adjustments:
                    adj = ", ".join(f"{k}={v:+.2f}" for k, v in sj.rule_adjustments.items())
                    print(f"         {D}{adj}{X}")

        # ─── DB state ──────────────────────────────────────────────────────
        print(f"\n{B}{'─'*72}{X}")
        print(f"{B}  SQLite dedup state (after this run){X}")
        print(f"{B}{'─'*72}{X}\n")
        conn = sqlite3.connect(str(db_path))
        for row in conn.execute(
            "SELECT first_seen_at, company, title, score FROM seen_jobs "
            "ORDER BY score DESC LIMIT 5"
        ):
            ts, company, title, score = row
            print(f"  [{D}{ts}{X}] {_color_score(score)}  {C}{company:14s}{X}  {title}")
        conn.close()

        store.close()

    # ─── Summary ───────────────────────────────────────────────────────────
    print(f"\n{B}{'═'*72}{X}")
    print(f"{B}  Summary{X}")
    print(f"{B}{'═'*72}{X}")
    print(f"  jobs in:                  {len(scored)}")
    print(f"  new (post-dedup):         {len(new_jobs)}")
    print(f"  above threshold:          {len(eligible)}")
    print(f"  alerts that would fire:   {G}{len(eligible)}{X}")
    print()
    print(f"  {D}Run with --verbose to see every job. Run with --top N for more matches.{X}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
