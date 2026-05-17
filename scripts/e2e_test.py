#!/usr/bin/env python3
"""End-to-end QA orchestrator.

Runs all 7 layers of the JobSeeker pipeline with verbose ✓/✗ output.
Each layer is independently invocable so a regression shows up in exactly
ONE box. Use this before any production cycle.

Usage:
    ./scripts/e2e_test.py --all                      # run every layer
    ./scripts/e2e_test.py --layer L0,L2              # just env + tailoring
    ./scripts/e2e_test.py --all --skip-live          # local-only (no API calls)
    ./scripts/e2e_test.py --all --person sai         # one profile only

Exit codes:
  0  all selected layers passed
  1  one or more tests failed
  2  setup error (missing module / .env)

Design:
  - Each test is a small async fn returning (name, ok, detail).
  - Layers are decoupled: L3 doesn't need L2 to have run; we use synthetic
    fixtures so a single failing layer doesn't block the rest.
  - Live tests (Drive/Sheet/Telegram/LLM) are gated by `--skip-live` so
    you can run the same suite offline on a plane.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

try:
    from dotenv import load_dotenv
    load_dotenv(REPO / ".env", override=False)
except ImportError:
    pass


# ── ANSI helpers ──────────────────────────────────────────────────────────
def _g(s): return f"\033[32m{s}\033[0m"
def _r(s): return f"\033[31m{s}\033[0m"
def _y(s): return f"\033[33m{s}\033[0m"
def _b(s): return f"\033[1m{s}\033[0m"
def _d(s): return f"\033[2m{s}\033[0m"


# Result tuple: (name, ok, detail)
Result = tuple[str, bool, str]


# ──────────────────────────────────────────────────────────────────────────
# L0 — environment
# ──────────────────────────────────────────────────────────────────────────

REQUIRED_ENV = [
    "TELEGRAM_BOT_TOKEN_SAI", "TELEGRAM_CHAT_ID_SAI",
    "TELEGRAM_BOT_TOKEN_GF",  "TELEGRAM_CHAT_ID_GF",
    "GOOGLE_SHEETS_ID_SAI", "GOOGLE_DRIVE_PARENT_FOLDER_ID",
    "GOOGLE_SERVICE_ACCOUNT_JSON",
    # Anthropic key may be empty if an internal proxy or SDK is in use.
]

OPTIONAL_ENV_AT_LEAST_ONE = ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"]


async def L0_env() -> list[Result]:
    out: list[Result] = []

    # L0.1 — .env parse already happened on import; check by reading expected key
    out.append(("L0.1 .env loaded",
                bool(os.environ.get("GOOGLE_SHEETS_ID_SAI") or os.environ.get("ANTHROPIC_API_KEY")),
                "load_dotenv ran on script start"))

    # L0.2 — required env keys
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k, "").strip()]
    out.append(("L0.2 required env vars present",
                not missing,
                f"missing: {missing}" if missing else "all 7 set"))

    # L0.3 — Pooja chat id
    gf_chat = os.environ.get("TELEGRAM_CHAT_ID_GF", "").strip()
    out.append(("L0.3 Pooja chat_id is real (not placeholder)",
                gf_chat not in ("987654321", "0", ""),
                f"current: {gf_chat!r}"))

    # L0.4 — OAuth token (only required if user picked OAuth Drive auth)
    oauth_path = Path("~/.jobseeker/google_oauth.json").expanduser()
    if oauth_path.exists():
        try:
            tok = json.loads(oauth_path.read_text())
            ok = bool(tok.get("refresh_token"))
            out.append(("L0.4 google_oauth.json has refresh_token", ok,
                        "found" if ok else "MISSING refresh_token"))
        except Exception as e:
            out.append(("L0.4 google_oauth.json parses", False, str(e)))
    else:
        out.append(("L0.4 google_oauth.json (optional, OAuth path)",
                    True, "absent — using SA path"))

    # L0.5 — profiles parse
    try:
        import yaml
        for who in ("sai", "gf"):
            r = REPO / "profiles" / who / "resume.yaml"
            p = REPO / "profiles" / who / "profile.parsed.yaml"
            assert r.exists() and p.exists(), f"{who}: missing {r} or {p}"
            yaml.safe_load(r.read_text())
            yaml.safe_load(p.read_text())
        out.append(("L0.5 both profiles parse", True,
                    "sai + gf: resume.yaml + profile.parsed.yaml all loaded"))
    except Exception as e:
        out.append(("L0.5 both profiles parse", False, str(e)))

    # L0.6 — LLM backend
    try:
        from shared.llm_client import available_backend
        backend = available_backend()
        out.append(("L0.6 LLM backend available", backend != "none",
                    f"backend={backend}"))
    except Exception as e:
        out.append(("L0.6 LLM backend available", False, str(e)))

    # L0.7 — required python deps
    deps_ok = True
    deps_detail = []
    for pkg in ("docx", "googleapiclient", "anthropic", "yaml", "httpx"):
        try:
            __import__(pkg)
            deps_detail.append(pkg)
        except ImportError:
            deps_ok = False
            deps_detail.append(f"MISSING:{pkg}")
    out.append(("L0.7 python deps installed", deps_ok, ", ".join(deps_detail)))

    return out


# ──────────────────────────────────────────────────────────────────────────
# L1 — scoring (synthetic JDs through the scorer)
# ──────────────────────────────────────────────────────────────────────────

async def L1_scoring(person: str = "sai") -> list[Result]:
    out: list[Result] = []
    try:
        from services.scorer.scorer import score_job_for_profile
        import yaml
    except Exception as e:
        return [("L1.0 scorer import", False, str(e))]

    profile = yaml.safe_load((REPO / "profiles" / person / "profile.parsed.yaml").read_text())
    test_cases = [
        {
            "id": "L1.1 civil eng → low",
            "expect_below": 0.25,
            "job": {
                "title": "Senior Civil Infrastructure Engineer",
                "description_text": "Design highway and bridge infrastructure. Civil engineering degree required.",
                "company": "AECOM", "location": "Austin, TX", "remote": False,
            },
        },
        {
            "id": "L1.3 SRE staff → high (sai only)",
            "expect_above": 0.55 if person == "sai" else 0.0,
            "expect_below": 1.01,
            "job": {
                "title": "Staff Site Reliability Engineer, Platform",
                "description_text": "Lead Kubernetes platform operations. SLO/error-budget discipline. Terraform, Prometheus, Grafana. 8+ years. We sponsor H1B transfers.",
                "company": "Stripe", "location": "San Francisco, CA", "remote": True,
            },
        },
        {
            "id": "L1.4 ML new grad → high (gf only)",
            "expect_above": 0.50 if person == "gf" else 0.0,
            "expect_below": 1.01,
            "job": {
                "title": "Machine Learning Engineer (NLP, New Grad)",
                "description_text": "Entry-level ML role. PyTorch, transformers, Hugging Face. Deep learning fundamentals. F1 OPT welcome.",
                "company": "Hugging Face", "location": "New York, NY", "remote": True,
            },
        },
    ]
    for tc in test_cases:
        try:
            score, reason = score_job_for_profile(tc["job"], profile)
            ok = True
            if "expect_below" in tc and score >= tc["expect_below"]:
                ok = False
            if "expect_above" in tc and score < tc["expect_above"]:
                ok = False
            out.append((tc["id"], ok, f"score={score:.2f} reason={reason[:60]}"))
        except Exception as e:
            out.append((tc["id"], False, str(e)))
    return out


# ──────────────────────────────────────────────────────────────────────────
# L2 — tailoring (deterministic checks; calls Claude unless --skip-live)
# ──────────────────────────────────────────────────────────────────────────

async def L2_tailoring(person: str, skip_live: bool) -> list[Result]:
    out: list[Result] = []

    # L2.3 — validator behavior
    try:
        from scripts.tailor_v2 import validate_rewrites
        bad_plan = {
            "experience": [
                {"role_index": 1, "rewrites": [
                    {"original_index": 1,
                     "rewritten": "Built Kubernetes platforms at Google scale.",
                     "confidence": "HIGH"},
                ]},
            ],
        }
        bad_resume = {"experience": [{"company": "Apple", "title": "SRE",
                                       "bullets": ["orig"]}]}
        warns = validate_rewrites(bad_plan, bad_resume)
        # Should flag the "at Google" mention as suspicious employer
        flagged = any("Google" in w for w in warns)
        out.append(("L2.3 validator flags fake employer 'at Google'", flagged,
                    f"{len(warns)} warns: {warns[:1]}"))

        # And shouldn't flag a legit transferable skill claim
        legit_plan = {
            "experience": [
                {"role_index": 1, "rewrites": [
                    {"original_index": 1,
                     "rewritten": "Used Helm + Terraform for production rollouts",
                     "confidence": "MEDIUM"},
                ]},
            ],
        }
        legit_warns = validate_rewrites(legit_plan, bad_resume)
        out.append(("L2.3b validator allows transferable Helm/Terraform claim",
                    not legit_warns,
                    f"{len(legit_warns)} warns" if legit_warns else "no warns"))
    except Exception as e:
        out.append(("L2.3 validator", False, str(e)))

    # L2.4 — scrubber
    try:
        from scripts.tailor_v2 import _scrub_visa
        cases = [
            ("Currently on F1 OPT in Chicago. Built ML pipelines.",
             lambda x: "F1" not in x and "OPT" not in x and "Built ML" in x),
            ("Hands-on with PyTorch. I am currently on F1 OPT and would transition via H1B.",
             lambda x: "PyTorch" in x and "F1" not in x and "H1B" not in x),
            ("Authorized to work in the US under H-1B.",
             lambda x: "H-1B" not in x and "Authorized" not in x),
            ("Built Kubernetes platforms at scale.",  # benign
             lambda x: "Kubernetes" in x),
        ]
        all_ok = True
        details = []
        for inp, check in cases:
            res = _scrub_visa(inp)
            ok = check(res)
            all_ok = all_ok and ok
            details.append(f"{'✓' if ok else '✗'}")
        out.append(("L2.4 visa scrubber across 4 cases", all_ok, " ".join(details)))
    except Exception as e:
        out.append(("L2.4 scrubber", False, str(e)))

    # L2.5 — coerce_int dotted parsing
    try:
        from scripts.tailor_v2 import _coerce_int
        cases = [(1, 1), ("3", 3), (1.2, 2), ("1.2", 2), ("2.13", 13),
                 (None, None), ("bad", None)]
        wrong = [(inp, _coerce_int(inp), exp) for inp, exp in cases
                 if _coerce_int(inp) != exp]
        out.append(("L2.5 _coerce_int handles all formats", not wrong,
                    f"failed: {wrong}" if wrong else "7/7 cases pass"))
    except Exception as e:
        out.append(("L2.5 _coerce_int", False, str(e)))

    # L2.7 — ATS score deterministic
    try:
        from scripts.match_report import compute_ats_score
        import yaml
        resume = yaml.safe_load((REPO / "profiles" / person / "resume.yaml").read_text())
        plan = {"summary": "ML Engineer with PyTorch transformers experience",
                "experience": []}
        jd = "We need PyTorch, transformers, Hugging Face, NLP, Python."
        a = compute_ats_score(jd, resume, plan)
        b = compute_ats_score(jd, resume, plan)
        out.append(("L2.7 ATS score deterministic across runs",
                    a == b, f"run1={a['ats_score']} run2={b['ats_score']}"))
    except Exception as e:
        out.append(("L2.7 ATS score", False, str(e)))

    # L2.8 — confidence breakdown
    try:
        from scripts.match_report import _confidence_breakdown
        plan = {"experience": [
            {"rewrites": [{"confidence": "HIGH"}, {"confidence": "MEDIUM"}]},
            {"rewrites": [{"confidence": "LOW"}, {"confidence": "high"}]},  # case-insensitive
        ]}
        cb = _confidence_breakdown(plan)
        ok = cb["HIGH"] == 2 and cb["MEDIUM"] == 1 and cb["LOW"] == 1
        out.append(("L2.8 confidence breakdown counts", ok,
                    f"got={cb}"))
    except Exception as e:
        out.append(("L2.8 confidence breakdown", False, str(e)))

    # L2.10 — both profiles have NO highlights by default in rendered HTML
    # We do this by running tailor in select mode (no Claude) and inspecting
    # the output. Skip if --skip-live is on AND the previous run isn't on disk.
    if not skip_live:
        try:
            import subprocess
            tmp_out = Path("/tmp/jobseeker_qa")
            tmp_out.mkdir(parents=True, exist_ok=True)
            cmd = [sys.executable, str(REPO / "scripts" / "tailor_v2.py"),
                   "--person", person, "--sample", "--mode", "select",
                   "--no-persist", "--no-drive", "--no-sheet",
                   "--output", str(tmp_out)]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            out.append((f"L2.1 tailor_v2 runs end-to-end ({person}, select)",
                        res.returncode == 0,
                        f"rc={res.returncode}; stderr={res.stderr[-200:]}"))
            html = (tmp_out / person / "resume.tailored.html").read_text()
            no_hl = "Selected Highlights" not in html and "Selected Engineering" not in html
            out.append((f"L2.10 {person} HTML has no Selected Highlights by default",
                        no_hl, "absent" if no_hl else "STILL PRESENT"))
        except Exception as e:
            out.append((f"L2.1 tailor_v2 ({person})", False, str(e)))
    else:
        out.append((f"L2.1 tailor_v2 ({person}) — skipped (--skip-live)", True, ""))
    return out


# ──────────────────────────────────────────────────────────────────────────
# L3 — Drive (live)
# ──────────────────────────────────────────────────────────────────────────

async def L3_drive(skip_live: bool) -> list[Result]:
    out: list[Result] = []
    if skip_live:
        return [("L3 — skipped (--skip-live)", True, "")]
    try:
        from shared.google_drive import DriveSyncer
        ds = DriveSyncer.from_env()
    except Exception as e:
        return [("L3.1 DriveSyncer.from_env", False, str(e))]
    if ds is None:
        return [("L3.1 DriveSyncer.from_env returns object", False,
                 "got None — env not configured")]
    out.append(("L3.1 DriveSyncer.from_env returns object", True, ""))

    # L3.3 — create folder
    folder_name = f"E2ETEST_{int(time.time())}"
    try:
        folder = ds.create_folder(folder_name)
        out.append(("L3.3 create_folder",
                    bool(folder.get("id")),
                    f"id={folder.get('id')[:12]}... url={folder.get('webViewLink','')[:60]}"))
    except Exception as e:
        return out + [("L3.3 create_folder", False, str(e))]

    # L3.4 — upload a small file
    try:
        tmp = REPO / "tests" / "qa" / f"_test_{folder_name}.txt"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text("e2e test artifact")
        f = ds.upload_file(tmp, folder["id"])
        ok = bool(f.get("id"))
        out.append(("L3.4 upload_file", ok,
                    f"id={f.get('id','?')[:12]}..."))
        tmp.unlink(missing_ok=True)
    except Exception as e:
        out.append(("L3.4 upload_file", False, str(e)))

    # L3.5 — share publicly
    try:
        ok = ds.make_anyone_viewable(folder["id"])
        out.append(("L3.5 make_anyone_viewable", ok, ""))
    except Exception as e:
        out.append(("L3.5 make_anyone_viewable", False, str(e)))

    # L3.6 — find existing folder (idempotency)
    try:
        again = ds.find_folder(folder_name)
        ok = bool(again and again.get("id") == folder["id"])
        out.append(("L3.6 find_folder finds the same id", ok,
                    f"matched={ok}"))
    except Exception as e:
        out.append(("L3.6 find_folder", False, str(e)))

    return out


# ──────────────────────────────────────────────────────────────────────────
# L4 — Sheet (live)
# ──────────────────────────────────────────────────────────────────────────

async def L4_sheet(skip_live: bool) -> list[Result]:
    out: list[Result] = []
    if skip_live:
        return [("L4 — skipped (--skip-live)", True, "")]
    try:
        from services.notifier.sheet_sync import SheetSyncer, HEADERS
        ss = SheetSyncer.from_env(person="sai")
    except Exception as e:
        return [("L4.1 SheetSyncer.from_env", False, str(e))]
    if ss is None:
        return [("L4.1 SheetSyncer.from_env returns object", False,
                 "got None — env not configured")]
    out.append(("L4.1 SheetSyncer.from_env returns object", True, ""))
    out.append(("L4.2 HEADERS has 22 columns", len(HEADERS) == 22,
                f"len={len(HEADERS)}"))

    # L4.3 — insert a synthetic test row
    test_dedup = f"e2etest{int(time.time())%10000:04d}xxxxxx"[:16]
    row_data = {
        "dedup_id": test_dedup, "person": "sai", "company": "E2E_TEST",
        "title": "QA Bot", "department": "QA", "location": "Remote",
        "remote": True, "source": "e2e", "match_score": 0.99,
        "ats_score": 100, "recruiter_score": 95,
        "archetype": "QA", "visa_ok": True,
        "url": "https://example.com/e2e", "drive_folder_link": "",
        "resume_url": "", "cover_letter_url": "",
        "required_skills": "qa", "missing_skills": "",
        "status": "NEW", "notes": "e2e test row — safe to delete",
    }
    try:
        status = await ss.upsert_dict(row_data)
        out.append(("L4.3 insert synthetic row",
                    status in ("inserted", "updated"),
                    f"status={status}"))
    except Exception as e:
        out.append(("L4.3 insert synthetic row", False, str(e)))

    # L4.4 — re-insert with same dedup_id should UPDATE not duplicate
    try:
        row_data["notes"] = "e2e test row — updated"
        status = await ss.upsert_dict(row_data)
        out.append(("L4.4 re-insert is UPDATE not duplicate",
                    status == "updated", f"status={status}"))
    except Exception as e:
        out.append(("L4.4 re-insert is UPDATE", False, str(e)))

    # L4.5 — status update by dedup_id
    try:
        ok = await ss.update_status_by_dedup_id(test_dedup, "APPLIED")
        out.append(("L4.5 update_status_by_dedup_id", ok,
                    "row found and updated" if ok else "row not found"))
    except Exception as e:
        out.append(("L4.5 update_status_by_dedup_id", False, str(e)))

    return out


# ──────────────────────────────────────────────────────────────────────────
# L5 — Telegram (live)
# ──────────────────────────────────────────────────────────────────────────

async def L5_telegram(person: str, skip_live: bool) -> list[Result]:
    out: list[Result] = []
    if skip_live:
        return [("L5 — skipped (--skip-live)", True, "")]
    try:
        import httpx
    except ImportError:
        return [("L5 imports", False, "httpx missing")]

    token_env = "TELEGRAM_BOT_TOKEN_GF" if person == "gf" else "TELEGRAM_BOT_TOKEN_SAI"
    chat_env = "TELEGRAM_CHAT_ID_GF" if person == "gf" else "TELEGRAM_CHAT_ID_SAI"
    token = os.environ.get(token_env, "").strip()
    chat = os.environ.get(chat_env, "").strip()
    if not token or chat in ("987654321", "0", ""):
        return [(f"L5 ({person}) — chat/token unset", False,
                 f"token_set={bool(token)} chat={chat!r}")]

    # L5.1 — getMe
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"https://api.telegram.org/bot{token}/getMe")
        ok = r.status_code == 200 and r.json().get("ok")
        out.append((f"L5.1 ({person}) getMe", ok,
                    f"@{r.json().get('result',{}).get('username','?')}"))
    except Exception as e:
        out.append((f"L5.1 ({person}) getMe", False, str(e)))

    # L5.3 — send message with inline keyboard
    try:
        kb = {"inline_keyboard": [
            [{"text": "Apply (test)", "url": "https://example.com"},
             {"text": "Mark Applied", "callback_data": "applied:e2etest"}],
            [{"text": "Skip", "callback_data": "skip:e2etest"},
             {"text": "Save for Later", "callback_data": "saved:e2etest"}],
        ]}
        body = {"chat_id": chat, "text": f"*E2E test — {person}*\nIf you see this, L5 passed.",
                "parse_mode": "Markdown", "reply_markup": kb}
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(f"https://api.telegram.org/bot{token}/sendMessage", json=body)
        ok = r.status_code == 200 and r.json().get("ok")
        out.append((f"L5.3 ({person}) sendMessage with 4-button keyboard", ok,
                    f"msg_id={r.json().get('result',{}).get('message_id','?')}"))
    except Exception as e:
        out.append((f"L5.3 ({person}) sendMessage", False, str(e)))
    return out


# ──────────────────────────────────────────────────────────────────────────
# L6 — bot listener (manual confirmation)
# ──────────────────────────────────────────────────────────────────────────

async def L6_callback(person: str, skip_live: bool, interactive: bool) -> list[Result]:
    if skip_live or not interactive:
        return [("L6 — skipped (non-interactive)", True, "")]
    print(_y(f"\n[L6] manual test: tap a button on the most recent {person} alert"))
    print(_y("    Will wait up to 90s for a callback to land via getUpdates."))
    try:
        import httpx, time as _t
        token_env = "TELEGRAM_BOT_TOKEN_GF" if person == "gf" else "TELEGRAM_BOT_TOKEN_SAI"
        token = os.environ.get(token_env, "").strip()
        deadline = _t.time() + 90
        async with httpx.AsyncClient(timeout=10) as c:
            offset = None
            while _t.time() < deadline:
                params = {"timeout": 10}
                if offset is not None:
                    params["offset"] = offset
                r = await c.get(f"https://api.telegram.org/bot{token}/getUpdates", params=params)
                for u in r.json().get("result", []):
                    offset = u["update_id"] + 1
                    if "callback_query" in u:
                        cb = u["callback_query"]
                        return [("L6 manual callback received", True,
                                 f"data={cb.get('data')!r} from {cb.get('from',{}).get('first_name','?')}")]
                await asyncio.sleep(0.5)
        return [("L6 manual callback received", False, "timeout — no tap detected")]
    except Exception as e:
        return [("L6 callback poll", False, str(e))]


# ──────────────────────────────────────────────────────────────────────────
# L7 — full integration (uses tailor_v2 with ALL flags on)
# ──────────────────────────────────────────────────────────────────────────

async def L7_e2e(person: str, skip_live: bool) -> list[Result]:
    if skip_live:
        return [("L7 — skipped (--skip-live)", True, "")]
    out: list[Result] = []
    try:
        import subprocess
        cmd = [sys.executable, str(REPO / "scripts" / "tailor_v2.py"),
               "--person", person, "--use-claude", "--mode", "rewrite",
               "--sample", "--send-telegram",
               "--output", "/tmp/jobseeker_e2e"]
        t0 = time.time()
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        dt = time.time() - t0
        ok = res.returncode == 0
        out.append((f"L7.1 tailor_v2 full pipeline ({person}, claude, send_telegram)",
                    ok, f"rc={res.returncode}, took {dt:.1f}s"))
        if not ok:
            out.append((f"L7.1 stderr tail", False, res.stderr[-500:]))
            return out
        # Check the artifact set
        out_dir = Path("/tmp/jobseeker_e2e") / person
        expected = ["resume.tailored.html", "resume.tailored.docx",
                    "resume.preview.html", "cover_letter.txt",
                    "match_report.json", "missing_skills.txt",
                    "interview_defense.md", "missing_skill_risks.md",
                    "interview_prep.md", "job.json"]
        missing = [f for f in expected if not (out_dir / f).exists()]
        out.append((f"L7.2 ({person}) all 10 artifacts written",
                    not missing,
                    f"missing: {missing}" if missing else "all 10 present"))
        # Check no F1/OPT/H1B leak
        leak_files = ["resume.tailored.html", "resume.preview.html", "cover_letter.txt"]
        for f in leak_files:
            p = out_dir / f
            if p.exists():
                txt = p.read_text()
                hits = len(re.findall(r"\bF1\b|\bOPT\b|\bH-?1B\b|\bsponsor", txt, re.IGNORECASE))
                out.append((f"L7.3 ({person}) {f} no visa/sponsor leak",
                            hits == 0, f"{hits} matches" if hits else "clean"))
    except subprocess.TimeoutExpired:
        out.append((f"L7.1 ({person}) timeout (>5min)", False, ""))
    except Exception as e:
        out.append((f"L7.1 ({person})", False, str(e)))
    return out


# ──────────────────────────────────────────────────────────────────────────
# Runner
# ──────────────────────────────────────────────────────────────────────────

LAYERS: dict[str, Callable[..., Awaitable[list[Result]]]] = {
    "L0": lambda **kw: L0_env(),
    "L1": lambda person, **kw: L1_scoring(person=person),
    "L2": lambda person, skip_live, **kw: L2_tailoring(person=person, skip_live=skip_live),
    "L3": lambda skip_live, **kw: L3_drive(skip_live=skip_live),
    "L4": lambda skip_live, **kw: L4_sheet(skip_live=skip_live),
    "L5": lambda person, skip_live, **kw: L5_telegram(person=person, skip_live=skip_live),
    "L6": lambda person, skip_live, interactive, **kw: L6_callback(person=person, skip_live=skip_live, interactive=interactive),
    "L7": lambda person, skip_live, **kw: L7_e2e(person=person, skip_live=skip_live),
}


async def run(args) -> int:
    layer_keys = list(LAYERS.keys()) if args.all else args.layer.split(",")
    persons = ["sai", "gf"] if args.person == "both" else [args.person]
    print(_b("\n=== JobSeeker E2E QA ==="))
    print(f"  layers:    {','.join(layer_keys)}")
    print(f"  persons:   {','.join(persons)}")
    print(f"  skip_live: {args.skip_live}")
    print(f"  interactive: {args.interactive}\n")

    all_results: list[tuple[str, str, list[Result]]] = []
    for layer in layer_keys:
        layer_runner = LAYERS.get(layer)
        if layer_runner is None:
            print(_r(f"unknown layer: {layer}"))
            continue
        for who in persons:
            # L0/L3/L4 don't depend on person
            if layer in ("L0", "L3", "L4") and who != persons[0]:
                continue
            label = layer if layer in ("L0", "L3", "L4") else f"{layer}/{who}"
            print(_b(f"\n── {label} ──"))
            try:
                results = await layer_runner(
                    person=who, skip_live=args.skip_live,
                    interactive=args.interactive,
                )
            except Exception as e:
                results = [(f"{layer} crashed", False, str(e))]
            all_results.append((layer, who, results))
            for name, ok, detail in results:
                mark = _g("✓") if ok else _r("✗")
                line = f"  {mark} {name}"
                if detail:
                    line += _d(f"  ({detail[:200]})")
                print(line)

    # Summary
    total = sum(len(r[2]) for r in all_results)
    passed = sum(1 for _, _, rs in all_results for _, ok, _ in rs if ok)
    failed = total - passed
    print(_b("\n" + "=" * 60))
    print(_b(f"  summary: {passed}/{total} passed, {failed} failed"))
    if failed == 0:
        print(_g("  all green — pipeline ready for production"))
    else:
        print(_r(f"  {failed} test(s) failed — see ✗ lines above"))
    print(_b("=" * 60))
    return 0 if failed == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--all", action="store_true", help="run every layer")
    ap.add_argument("--layer", default="L0",
                    help="comma-sep list of layers to run (e.g. L0,L2,L3)")
    ap.add_argument("--person", choices=["sai", "gf", "both"], default="both")
    ap.add_argument("--skip-live", action="store_true",
                    help="skip Drive/Sheet/Telegram/LLM live calls — local only")
    ap.add_argument("--interactive", action="store_true",
                    help="enable manual-confirmation tests like L6 (tap button on phone)")
    args = ap.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
