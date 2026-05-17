.PHONY: setup up down restart logs build test reset heavy heavy-down status sheets-test telegram-test demo

# ─────────────────── Setup ───────────────────
setup:
	@echo "Setting up JobSeeker..."
	@if [ ! -f .env ]; then cp .env.example .env && echo "Created .env - edit it with your credentials"; else echo ".env already exists"; fi
	@mkdir -p profiles/sai/resumes profiles/gf/resumes
	@echo ""
	@echo "Next steps:"
	@echo "  1. Edit .env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID_SAI, GOOGLE_SHEETS_ID_SAI, GOOGLE_SERVICE_ACCOUNT_JSON"
	@echo "  2. Make sure you have profiles/sai/profile.parsed.yaml (or run scripts/parse_resume.py)"
	@echo "  3. make up         # streaming pipeline (Telegram + Sheets, lightweight)"
	@echo "  4. make heavy      # add resume tailoring via Temporal + Ollama"

# ─────────────────── Streaming pipeline (default) ───────────────────
# Brings up only what's needed for Telegram alerts + Google Sheet sync:
#   nats, go-scraper, html-scraper, scorer, notifier (+ bot listener for inline buttons)
# This is the "be first applicant" loop — does NOT need postgres/redis/minio.
up:
	docker compose up -d --build
	@echo ""
	@echo "Streaming pipeline running:"
	@echo "  NATS UI:       http://localhost:8222"
	@echo "  Logs:          make logs"
	@echo "  Status:        make status"
	@echo "  Telegram:      check your phone — alerts arrive within 60s of new jobs"
	@echo "  Sheet:         https://docs.google.com/spreadsheets/d/$$(grep '^GOOGLE_SHEETS_ID_SAI=' .env | cut -d= -f2)/edit"

down:
	docker compose down

restart:
	docker compose restart $(SERVICE)

# ─────────────────── Heavy pipeline (Temporal + Ollama) ───────────────────
# Adds postgres, redis, minio, temporal, temporal-ui, worker.
# Use when you want resume tailoring + cover letter generation per match.
heavy:
	docker compose --profile heavy up -d --build
	@echo ""
	@echo "Full stack running. Additional UIs:"
	@echo "  Temporal UI:   http://localhost:8080"
	@echo "  MinIO Console: http://localhost:9001"

heavy-down:
	docker compose --profile heavy down

# ─────────────────── Build ───────────────────
build:
	docker compose build --no-cache

# ─────────────────── Logs / status ───────────────────
logs:
	docker compose logs -f --tail=200

scraper-logs:
	docker compose logs -f --tail=200 go-scraper

scorer-logs:
	docker compose logs -f --tail=200 scorer-sai scorer-gf

scorer-sai-logs:
	docker compose logs -f --tail=200 scorer-sai

scorer-gf-logs:
	docker compose logs -f --tail=200 scorer-gf

notifier-logs:
	docker compose logs -f --tail=200 notifier-sai notifier-gf

notifier-sai-logs:
	docker compose logs -f --tail=200 notifier-sai

notifier-gf-logs:
	docker compose logs -f --tail=200 notifier-gf

html-scraper-logs:
	docker compose logs -f --tail=200 html-scraper

worker-logs:
	docker compose logs -f --tail=200 worker

status:
	@docker compose ps

# ─────────────────── Tests / smoke ───────────────────
test:
	python3 -m pytest services/ -q

demo:
	python3 scripts/demo.py

sheets-test:
	python3 scripts/test_sheets.py --person sai --dry-run

telegram-test:
	@set -a; . ./.env; set +a; \
	curl -sS -X POST "https://api.telegram.org/bot$$TELEGRAM_BOT_TOKEN/sendMessage" \
	  -d "chat_id=$$TELEGRAM_CHAT_ID_SAI" \
	  --data-urlencode "text=jobseeker telegram smoke test"; echo

preflight:
	python3 scripts/preflight.py

# ─────────────────── Database ───────────────────
db-shell:
	docker compose exec postgres psql -U jobseeker -d jobseeker

# Inspect the local seen-jobs DBs (one per person; lives in the notifier-*-data volumes)
seen-recent:
	@echo "=== Sai ==="
	@docker compose exec notifier-sai sqlite3 /data/seen_sai.db \
	  "SELECT first_seen_at, score, status, company, title FROM seen_jobs ORDER BY first_seen_at DESC LIMIT 10;" || true
	@echo ""
	@echo "=== GF ==="
	@docker compose exec notifier-gf sqlite3 /data/seen_gf.db \
	  "SELECT first_seen_at, score, status, company, title FROM seen_jobs ORDER BY first_seen_at DESC LIMIT 10;" || true

seen-applied:
	@echo "=== Sai ==="
	@docker compose exec notifier-sai sqlite3 /data/seen_sai.db \
	  "SELECT first_seen_at, score, company, title FROM seen_jobs WHERE status='APPLIED' ORDER BY first_seen_at DESC;" || true
	@echo ""
	@echo "=== GF ==="
	@docker compose exec notifier-gf sqlite3 /data/seen_gf.db \
	  "SELECT first_seen_at, score, company, title FROM seen_jobs WHERE status='APPLIED' ORDER BY first_seen_at DESC;" || true

# ─────────────────── Reset (destructive) ───────────────────
reset:
	@echo "WARNING: deletes ALL data (Postgres, Redis, MinIO, dedup DB). 5 sec to cancel."
	@sleep 5
	docker compose --profile heavy --profile legacy down -v
	@echo "All volumes deleted."
