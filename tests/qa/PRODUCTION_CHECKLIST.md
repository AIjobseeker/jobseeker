# Production readiness checklist

Use this before pushing to GitHub or building public Docker images. Every
box below must be ✓ before `git push`.

## Pre-push secret sweep

```bash
# 1. Run the bundled secret sweeper — bails on any hit
python3 scripts/audit_secrets.py

# 2. Manual grep for known hot patterns (should print 0 lines)
grep -rEnIi 'sk-ant-[A-Za-z0-9_\-]{20,}|AAH[A-Za-z0-9_\-]{30,}|AKIA[A-Z0-9]{16}|BEGIN PRIVATE KEY|hvys3fcwcteqrvw3qzkvtk86viuoqv|appleconnect|floodgate\.g\.apple' \
  --include='*.py' --include='*.yaml' --include='*.yml' --include='*.md' --include='*.sh' --include='*.json' \
  --exclude-dir=.git --exclude-dir=__pycache__ --exclude-dir=node_modules \
  . | grep -vE '^\.\/(\.env|\.env\.bak|profiles/.*resume\.yaml)'
```

## Files that must NEVER be in a commit

- `.env`, `.env.bak`, `.env.local`, anything `.env.*` except `.env.example`
- `~/.jobseeker/google_oauth.json`
- `~/.jobseeker/google_client_secret.json`
- Any file containing a `private_key` block (PEM)
- Personal resumes (`profiles/*/resumes/*.docx|pdf|doc`)
- `seen.db*` (SQLite with personal scrape history)
- Any artifact under `/tmp/jobseeker_*/`

`.gitignore` covers all of these — verify nothing slips through:

```bash
git status --short            # nothing risky in 'untracked'
git ls-files | xargs -I{} grep -l "sk-ant-api03\|BEGIN PRIVATE KEY\|hvys3fcwc" {} 2>/dev/null   # should print nothing
```

## Static checklist

- [ ] `.gitignore` has `.env`, `*.token`, `oauth*.json`, `client_secret*.json`,
      `~/.jobseeker/`, `profiles/*/resumes/*`, `/tmp/jobseeker_*/`
- [ ] `.env.example` exists with PLACEHOLDER values only (no real tokens / chat IDs)
- [ ] No file committed contains a real API key, JWT, OAuth secret, or service-account JSON
- [ ] `shared/llm_client.py` has zero employer-internal hostnames, audience IDs, or CLI binary names
- [ ] `docs/DEV_SETUP.md` references vendor-agnostic auth modes only
- [ ] `scripts/parse_resume.py` / `resume_to_yaml.py` / `preflight.py` /
      `demo_tailor.py` have no employer-internal references in error
      messages or docstrings
- [ ] All scripts in `services/` work with environment variables and no
      hardcoded credentials
- [ ] `e2e_test.py --layer L0 --skip-live` passes (env validates)
- [ ] `e2e_test.py --all --skip-live` passes (deterministic checks all green)

## After-push verification (run on a fresh clone)

```bash
git clone <repo-url> /tmp/jobseeker-fresh
cd /tmp/jobseeker-fresh

# Should fail to run anything until env is set up — confirms no secrets leaked
python3 scripts/preflight.py    # expect missing-env warnings, NOT a successful run

# Confirm .env is NOT in the clone
test -f .env && echo "LEAK!!! .env in repo" || echo "ok: .env absent"
```


## Docker Hub private push

### One-time setup

```bash
docker login                        # Docker Hub creds
docker build -t <dockerhub-user>/jobseeker-scorer:latest -f services/scorer/Dockerfile .
docker build -t <dockerhub-user>/jobseeker-notifier:latest -f services/notifier/Dockerfile .
docker build -t <dockerhub-user>/jobseeker-html-scraper:latest -f services/html_scraper/Dockerfile .
# (go-scraper builds via its own Dockerfile if present, else compiled binary)
```

### Push (private)

```bash
docker push <dockerhub-user>/jobseeker-scorer:latest
docker push <dockerhub-user>/jobseeker-notifier:latest
docker push <dockerhub-user>/jobseeker-html-scraper:latest
```

Mark each repo as **Private** in the Docker Hub UI before the first push,
or use `docker hub settings → repos → visibility`.

### docker-compose for prod (consumes pushed images)

Provide a separate compose file that pulls from Docker Hub instead of
building locally — see `docker-compose.prod.yml` (template; configure tags).


## What to do RIGHT NOW

```bash
cd /Users/saikrishnanarvaneni/jobseeker

# 1. Run the secret sweeper
python3 scripts/audit_secrets.py

# 2. Verify env loads + deterministic tests still green
python3 scripts/e2e_test.py --layer L0,L2 --skip-live

# 3. Init git (you confirmed this isn't a repo yet)
git init
git checkout -b main
git add .gitignore
git commit -m "chore: gitignore baseline"

# 4. Verify nothing risky is staged before adding everything else
git add -A
git status

# 5. Final paranoid grep on what would be committed
git diff --cached | grep -iE 'sk-ant-api03|AAH[A-Za-z0-9]{30}|BEGIN PRIVATE KEY|appleconnect|hvys3fcwc'
# ↑ MUST print nothing. If it does, unstage that file and investigate.

# 6. First commit
git commit -m "feat: initial jobseeker pipeline (resume tailor + Drive + Sheet + Telegram)"

# 7. Add remote and push
git remote add origin git@github.com:<your-user>/jobseeker.git   # private repo
git push -u origin main
```

After the push, immediately rotate any secrets that EVER appeared in any
file in the repo's history (even if you removed them before push):

- `ANTHROPIC_API_KEY` — rotate at https://console.anthropic.com/settings/keys
- `TELEGRAM_BOT_TOKEN_*` — `/revoke` via @BotFather, then `/token` for a new one
- `GOOGLE_SERVICE_ACCOUNT_JSON` — Cloud Console → IAM → create new key, delete old

Even if your `.gitignore` was correct, if a secret was ever in your shell
history or in screen-share output, treat it as compromised.
