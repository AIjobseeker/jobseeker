# notifier

Subscribes to NATS `jobs.scored`, dedups against a local SQLite store, and
fans the survivors to Telegram.

## Components

- `dedup.py` — SQLite store keyed by `sha256(company.lower() + "|" + source_id)`.
  Republishes new jobs to `jobs.new`.
- `telegram_dispatch.py` — subscribes to `jobs.new`, formats Markdown,
  rate-limits to 20 msgs/min/chat, sends via Telegram Bot API.
- `main.py` — runs both in one asyncio process so they share the DB connection.
- `healthcheck.py` — prints the last 10 dedup entries; exits 0.

## Env vars

| var | default | meaning |
| --- | --- | --- |
| `NATS_URL` | `nats://localhost:4222` | NATS server |
| `JOBSEEKER_DB_PATH` | `~/.jobseeker/seen.db` | SQLite file path |
| `TELEGRAM_BOT_TOKEN` | (required) | bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | (required) | chat to notify |
| `TELEGRAM_MIN_SCORE` | `0.65` | drop jobs scored below this |

## Run

```
pip install -r requirements.txt
python -m services.notifier.main
```

Tests: `pytest services/notifier/test_notifier.py -q`
