# JobSeeker E2E QA plan

Version: 2026-05-17
Owner: Sai (you)
Last green run: _fill in after first green pass_

End-to-end means: scrape -> score -> dedup -> notify (Telegram) -> tailor
(resume + cover + reports) -> Drive sync -> Sheet sync -> button-callback
status update. We treat each layer as independently testable so a regression
shows up in exactly ONE box.

```
[scrape]  ->  [score]  ->  [dedup]  ->  [notify]
                                          |    \
                                          |     \-> [sheet upsert (streaming)]
                                          v
                                     [tailor cli]
                                       |   |   |
                                       v   v   v
                                    [Drive][Sheet][Telegram doc replies]
                                            |
                                            v
                                  [bot listener handles buttons]
```

## Pass criteria — what 99.99 means here

- Zero crashes across 50 sample JDs in rewrite mode for both profiles
- Every artifact generated (resume html/docx, cover, match_report,
  missing_skills, defense, risks, prep, preview) — file-exists and
  >0 bytes
- ATS score deterministic and reproducible (same JD + same plan → same %)
- Scrubber removes 100% of visa-status / sponsorship language
- Validator never blocks a legitimate transferable-skill rewrite,
  always blocks invented employer names
- Drive folder + 8 files visible at the URL printed in logs
- Sheet row appears in the right person's tab with all 22 columns
- Telegram alert delivered, all 4 inline buttons render, callbacks
  update both seen.db and the sheet within 2s
- Re-running the same JD a second time UPDATES the row (not duplicate)


## Layer matrix

### L0 — environment

| ID | Test | Pass criteria |
|----|------|---------------|
| L0.1 | `.env` parses | `python3 -c "from dotenv import load_dotenv; load_dotenv('.env')"` exits 0 |
| L0.2 | All required env vars present | TELEGRAM_BOT_TOKEN_SAI/_GF, TELEGRAM_CHAT_ID_SAI/_GF, GOOGLE_SHEETS_ID_SAI, GOOGLE_DRIVE_PARENT_FOLDER_ID, GOOGLE_SERVICE_ACCOUNT_JSON, ANTHROPIC_API_KEY |
| L0.3 | Pooja chat_id is real | not equal to placeholder `987654321` |
| L0.4 | OAuth token present (if user picked OAuth Drive auth) | `~/.jobseeker/google_oauth.json` exists, `refresh_token` populated |
| L0.5 | Profiles parse | `profiles/{sai,gf}/resume.yaml` and `profile.parsed.yaml` load via `yaml.safe_load` |
| L0.6 | LLM backend available | `shared.llm_client.available_backend()` != "none" |
| L0.7 | python-docx + google-api-python-client installed | imports succeed |

### L1 — scoring (already covered by `services/scorer/test_scorer.py`)

| ID | Test | Pass criteria |
|----|------|---------------|
| L1.1 | Civil engineering JD scores < 0.20 (Sai profile) | hard-cap by NON_TECH_TITLE_TOKENS |
| L1.2 | NVIDIA Cloud Data Engineer scores < 0.55 (coding-heavy) | density penalty applied |
| L1.3 | Stripe Staff SRE scores >= 0.65 for Sai | passes match_threshold |
| L1.4 | Hugging Face ML new-grad scores >= 0.60 for Pooja | passes match_threshold |
| L1.5 | India-only JD scores < 0.25 for Sai | location filter triggers |
| L1.6 | "no sponsorship" JD scores < 0.20 for Sai | hard-cap by sponsorship rule |
| L1.7 | Block-G ghost-job scoring | tier ∈ {HIGH_CONFIDENCE, PROCEED_WITH_CAUTION, SUSPICIOUS} |

### L2 — tailoring

| ID | Test | Pass criteria |
|----|------|---------------|
| L2.1 | Sample JD (Stripe SRE) for Sai, rewrite mode | exits 0; HTML/DOCX/preview/cover/match_report/defense/risks/prep all written, all >100 bytes |
| L2.2 | Sample JD (Hugging Face ML) for Pooja, rewrite mode | same set of artifacts |
| L2.3 | Validator only blocks invented employer names | `validate_rewrites` returns warns for fake "at Google" entries; no warns for legitimate transferable skill mentions |
| L2.4 | Scrubber removes visa text | `_scrub_visa("Currently on F1 OPT")` returns empty; preserves benign sentences |
| L2.5 | Indices in dotted form parse | `_coerce_int(1.2) == 2`, `_coerce_int("2.13") == 13` |
| L2.6 | Re-run same JD twice → identical artifacts | hash of HTML matches across two runs (with deterministic LLM, otherwise content equivalence check) |
| L2.7 | ATS score is deterministic on fixed plan | `compute_ats_score(jd, resume, plan)` returns same int every run |
| L2.8 | Confidence breakdown counts correctly | HIGH+MEDIUM+LOW = total_rewrites |
| L2.9 | Both profiles never leak F1/OPT/H1B/sponsor in HTML, DOCX, cover | `grep -ic` returns 0 |
| L2.10 | Both profiles have NO "Selected Highlights" by default | `grep -c` returns 0 in HTML, DOCX, preview |
| L2.11 | `--highlights` flag forces section ON | grep returns ≥ 1 when flag is set |

### L3 — Drive sync

| ID | Test | Pass criteria |
|----|------|---------------|
| L3.1 | DriveSyncer.from_env() returns object | not None |
| L3.2 | Auth refreshes (OAuth path) | `_load_oauth_credentials` succeeds, refreshed token persists |
| L3.3 | Folder created under parent with name `<safe_company>_<safe_title>_<YYYY-MM-DD>` | folder.id returned, webViewLink valid |
| L3.4 | All artifacts upload | resume.docx, html, preview.html, cover, report, missing, defense, risks, prep, job → 10 file IDs |
| L3.5 | Folder is publicly viewable | anyone-with-link permission applied |
| L3.6 | Re-run upserts (folder reused, files updated) | `find_folder` matches existing; no duplicate folders created |

### L4 — Sheet sync

| ID | Test | Pass criteria |
|----|------|---------------|
| L4.1 | SheetSyncer.from_env(person="sai") returns object | not None |
| L4.2 | Header row has 22 columns matching HEADERS list | row 1 = HEADERS literal |
| L4.3 | First insert populates A..V correctly | dedup_id, person, company, title, ats_score etc. all match |
| L4.4 | Second insert with same dedup_id UPDATES (no duplicate) | `_upsert_blocking` returns "updated" |
| L4.5 | Status update works | `update_status_by_dedup_id(id, "APPLIED")` sets col U |
| L4.6 | Pooja explicitly skipped from sheet (per PERSON_DEFAULTS.gf.use_sheet=False) | no row written |

### L5 — Telegram

| ID | Test | Pass criteria |
|----|------|---------------|
| L5.1 | `getMe` succeeds for both bots | 200 OK, bot username returned |
| L5.2 | Pooja's chat_id is reachable | `sendChatAction` to her chat returns 200 |
| L5.3 | Alert message renders | inline_keyboard has 4 buttons (Apply / Mark Applied / Skip / Save for Later) |
| L5.4 | Markdown parses | bold, links, code block all render in app |
| L5.5 | Threaded attachments | resume DOCX, cover.txt, defense.md, prep.md all replied to alert msg_id |
| L5.6 | Caption shows correct company | each attachment caption = "Tailored resume - Company" etc. |
| L5.7 | Per-person isolation | Sai's run sends to Sai's bot+chat; Pooja's run sends to her bot+chat; no cross-talk |

### L6 — Bot listener (callback buttons)

| ID | Test | Pass criteria |
|----|------|---------------|
| L6.1 | Bot listener running on long-poll | `bot_listener.run_listener` returns task; getUpdates polled |
| L6.2 | Tap "Mark Applied" → seen.db status=APPLIED | dedup row.status updates to APPLIED, status_updated_at set |
| L6.3 | Same tap → sheet col U updates to APPLIED | sheet.update fires, value="APPLIED" |
| L6.4 | Tap "Skip" → status=SKIPPED in both | same flow as above |
| L6.5 | Tap "Save for Later" → status=SAVED | new code path; verify sheet update |
| L6.6 | Original message edited to show new status | edit_message_reply_markup fires, button highlights or strikethrough |
| L6.7 | Listener idempotent on dup callbacks | same callback received twice → only first one updates |

### L7 — End-to-end (production)

| ID | Test | Pass criteria |
|----|------|---------------|
| L7.1 | Run scrapers against `companies/seed_500.yaml` for 1 minute | NATS publishes ≥10 messages on `jobs.raw` |
| L7.2 | Scorer publishes ≥1 to `jobs.scored.sai` per run | scorer logs show emit |
| L7.3 | Notifier dedup → Telegram alert ≤ 60s after scoring | timestamp delta in logs |
| L7.4 | Tailor CLI on the top scored JD produces all 10 artifacts | files exist + sizes |
| L7.5 | Drive folder visible | open URL in browser, see 10 files |
| L7.6 | Sheet row visible with all 22 cols populated | open sheet, find row |
| L7.7 | Tap Mark Applied on phone | within 2s: seen.db status=APPLIED, sheet U=APPLIED, alert message edited |
| L7.8 | Re-run tailor on same JD | folder reused, sheet updated (not duplicate) |
| L7.9 | Trigger Pooja's path (after her chat_id is real) | Telegram alert + 4 attachments delivered to her, no Drive/Sheet |


## Production runbook

### One-time setup
```
# 1. Verify env
./scripts/e2e_test.py --layer L0

# 2. Confirm OAuth (if not done already)
ls ~/.jobseeker/google_oauth.json || python3 scripts/google_oauth_init.py

# 3. Kick the OS scheduler (only if not already up)
make compose-up      # default profile: nats, scrapers, scorers, notifiers
```

### Daily smoke test
```
./scripts/e2e_test.py --all
```

### Live production test (one full match → phone)
```
# 1. Force a single sample JD through tailor + Drive + Sheet + Telegram
python3 scripts/tailor_v2.py --person sai --use-claude --mode rewrite --sample --send-telegram

# 2. On phone: tap "Mark Applied"
# 3. Verify within 2s:
sqlite3 ~/.jobseeker/seen.db "select status from seen_jobs where substr(key,1,16)='b4adf57d81b351f5'"
#    -> APPLIED
# 4. Open the sheet, refresh — col U for that row should show APPLIED
```

### Per-layer drilldown when something fails
```
./scripts/e2e_test.py --layer L2     # tailor only
./scripts/e2e_test.py --layer L3     # drive only
./scripts/e2e_test.py --layer L5     # telegram only
```


## Known limits

- Recruiter score is non-deterministic (Claude judgment). Score-stability
  test uses ±10 tolerance.
- Telegram callback test (L6.x) requires manual phone tap — orchestrator
  pauses with `press Enter when you've tapped Mark Applied`.
- Live scraper test (L7.1) requires NATS up and depends on company portals
  not rate-limiting us.
