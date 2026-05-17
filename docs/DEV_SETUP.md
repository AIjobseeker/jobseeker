# Local Dev Environment

Single source of truth for getting jobseeker running on your Mac. No emojis, no hand-waving — copy/paste each block in order.

## 1. Prerequisites (one-time)

```bash
# Tooling
brew install go python3 docker docker-compose nats-cli sqlite

# Docker Desktop must be running before any `docker compose` command
open -a Docker

# Verify
go version          # 1.22+
python3 --version   # 3.12+
docker version
```

## 2. Secrets — fill in `.env`

```bash
cd ~/jobseeker
cp .env.example .env       # if you haven't already
nano .env                  # set the four required values below
```

**Required**:

| Var | Where to get it |
|-----|-----------------|
| `ANTHROPIC_API_KEY` | See "Claude access modes" below — pick one |
| `TELEGRAM_BOT_TOKEN` | Open Telegram → message `@BotFather` → `/newbot` → follow prompts. |
| `TELEGRAM_CHAT_ID_SAI` | After creating bot, message your bot once. Then visit `https://api.telegram.org/bot<TOKEN>/getUpdates` — copy the `chat.id` value. |
| `OLLAMA_HOST` (optional) | `http://<your-windows-ip>:11434` if running local LLM on Windows PC, else leave default. |

### Claude access modes

Pick the one that fits your situation:

**A. Internal corporate proxy (free, employer-provided):**
```
ANTHROPIC_API_KEY=dummy
ANTHROPIC_BASE_URL=https://your-internal-proxy/api/anthropic
ANTHROPIC_AUTH_TOKEN=<bearer JWT — see B for auto-fetch>
```
The `anthropic` Python SDK accepts a custom `base_url` and `default_headers`. Our code sets `Authorization: Bearer $ANTHROPIC_AUTH_TOKEN` automatically. If your proxy needs different headers, drop a JSON blob into `ANTHROPIC_EXTRA_HEADERS_JSON` instead.

**B. Internal proxy with auto-token-refresh via a CLI:**
If your employer provides a CLI that prints a fresh OAuth JWT, point us at it and the SDK refreshes tokens automatically:
```
ANTHROPIC_API_KEY=dummy
ANTHROPIC_BASE_URL=https://your-internal-proxy/api/anthropic
JOBSEEKER_INTERNAL_TOKEN_CLI=/absolute/path/to/your-token-cli
JOBSEEKER_INTERNAL_TOKEN_CLI_ARGS=--token-type=oauth --interactivity-type=none
JOBSEEKER_INTERNAL_OAUTH_AUDIENCE=<your client/audience id>
```
The CLI is expected to print one of: `oauth-id <jwt>`, `oauth-id: <jwt>`, or just the bare JWT. Tokens are decoded for their `exp` claim and cached until ~5 min before expiry.

**C. Direct Anthropic (public, paid):**
```
ANTHROPIC_API_KEY=sk-ant-api03-...
```
Get the key at https://console.anthropic.com/settings/keys. Add ~$10 credit at /billing. Costs ~$0.01-0.05/day at our usage.

**D. Custom proxy with extra headers:**
```
ANTHROPIC_BASE_URL=https://your.proxy/anthropic
ANTHROPIC_EXTRA_HEADERS_JSON={"Authorization":"Bearer ..."}
```

**Apply**:

```bash
set -a; source .env; set +a    # exports all .env vars into current shell
```

## 3. Start infrastructure

```bash
docker compose up -d postgres redis minio temporal nats

# Verify everything is healthy
docker compose ps
# Should show all 5 as 'healthy' or 'running'

# Check NATS is reachable
nats stream ls -s nats://localhost:4222
# Empty list is fine — means NATS is up
```

UIs available:
- Temporal: http://localhost:8080
- MinIO: http://localhost:9001 (login: minioadmin / minioadmin123)

## 4. Bootstrap your profile (one-time)

```bash
# Parses ~/Downloads/saikrishna-resume-uptodate.pdf into structured YAML
python3 scripts/parse_resume.py
# Output: profiles/sai/profile.parsed.yaml

# Read it, sanity-check the seniority/skills it extracted
cat profiles/sai/profile.parsed.yaml
```

## 5. Build the company catalog (one-time, repeat monthly)

```bash
# Discovers careers URLs + ATS detection for all 216 companies (~3 min)
python3 scripts/discover_careers.py
# Outputs:
#   companies/catalog.yaml             (canonical scraping methods)
#   companies/catalog_mismatches.yaml  (where seed disagrees with discovery)
```

After it finishes, paste me the printed summary and I'll auto-correct `seed_500.yaml` from the mismatches.

## 6. Run the scraper

```bash
cd services/go-scraper
go build -o /tmp/go-scraper .

# One-shot test: scrape once, write NDJSON, view jobs
/tmp/go-scraper --once --mode file --output /tmp/jobs.jsonl --seed ../../companies/seed_500.yaml 2>/tmp/scrape.err
python3 ../../scripts/view-jobs.py /tmp/jobs.jsonl --sre

# Production mode: pipe to NATS for the scoring service
/tmp/go-scraper --mode nats --seed ../../companies/seed_500.yaml &
# (use `make stop` or kill the PID to halt)
```

## 7. Run the scoring + notifier services

```bash
# Install Python deps (one-time)
cd ~/jobseeker
pip3 install -r services/scorer/requirements.txt
pip3 install -r services/notifier/requirements.txt

# Run scorer (subscribes to jobs.raw, publishes to jobs.scored)
cd services/scorer && python3 main.py &

# Run notifier (subscribes to jobs.scored, dedups, sends Telegram)
cd ../notifier && python3 main.py &
```

## 8. Verify the pipeline end-to-end

```bash
# Watch the NATS subjects in three terminals
nats sub jobs.raw       # raw scraped jobs
nats sub jobs.scored    # AI-scored
nats sub jobs.new       # deduped, NEW only — these trigger Telegram

# In a fourth, see your DB
sqlite3 ~/.jobseeker/seen.db "SELECT company, title, score FROM seen_jobs ORDER BY first_seen_at DESC LIMIT 20;"

# In Telegram, you should start receiving alerts within 2-3 min of the scraper finding a new high-score job
```

## 9. Local LLM on Windows (optional, saves ~$15/month)

On your Windows PC:

```powershell
winget install Ollama.Ollama
ollama pull qwen2.5:14b           # 9 GB — for reranking
ollama pull nomic-embed-text       # 274 MB — for embeddings
ollama serve                       # exposes 0.0.0.0:11434
```

Find your Windows IP: `ipconfig` → look for IPv4 (e.g. 192.168.1.42). On your Mac, set `OLLAMA_HOST=http://192.168.1.42:11434` in `.env`.

When `OLLAMA_HOST` is set, the scorer will use Ollama instead of sentence-transformers. Free embeddings, runs on your home network.

## 10. Tearing down

```bash
docker compose down            # stops services, keeps data volumes
docker compose down -v         # nukes data too — only when you really mean it
pkill -f go-scraper            # stop scraper
pkill -f "scorer/main.py"      # stop scorer
pkill -f "notifier/main.py"    # stop notifier
```

## Where everything lives

```
~/jobseeker/
  services/
    go-scraper/     Go     ATS API scraping, fan-out concurrency, NATS publish
    scorer/         Python AI scoring against profile, NATS pub/sub
    notifier/       Python Dedup + Telegram dispatcher
    worker/         Python (legacy) Temporal activity stubs — to be wired later
    api/            Python FastAPI dashboard — placeholder
  scripts/
    parse_resume.py        Resume PDF -> profile.parsed.yaml (Claude)
    discover_careers.py    All 216 companies -> catalog.yaml (HTTP probe)
    generate-seed.py       Canonical company list -> seed_500.yaml
    view-jobs.py           Terminal viewer for scraped NDJSON
  companies/
    seed_500.yaml          Canonical company list (input)
    catalog.yaml           Discovered scraping methods (output)
    catalog_mismatches.yaml Where seed disagrees with discovery
  profiles/
    sai/profile.yaml          Operational config (email, telegram, resume paths)
    sai/profile.parsed.yaml   AI-extracted resume structure (matching)
    gf/...                    Same shape, separate person
  ~/.jobseeker/
    seen.db                SQLite store of every job ID we've ever seen
```

## Common gotchas

- **`go: command not found` after `cd services/go-scraper`** — `go.mod` lives in that dir. Run `go build` from there, not from project root.
- **`Forbidden` errors on every API call** — you're probably running inside Claude's sandbox. The actual ATS APIs work fine on real network.
- **Scorer downloads model on first run (~80 MB)** — slow once, cached after. Cache is at `~/.cache/sentence_transformers/`.
- **No Telegram messages arriving** — check `TELEGRAM_MIN_SCORE` in `.env` (default 0.65). Lower it temporarily to 0.3 to verify the pipe works, then raise it back.
- **Postgres connection refused** — `docker compose ps` first. If postgres is `unhealthy`, check `docker compose logs postgres`.
