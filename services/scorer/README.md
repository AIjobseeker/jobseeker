# scorer

Async Python service that consumes raw jobs off NATS `jobs.raw`, scores each
against a user profile using sentence-transformers + rule overrides, and
publishes a `ScoredJob` to `jobs.scored`.

## Run locally

```bash
cd /path/to/jobseeker
python -m venv .venv && source .venv/bin/activate
pip install -r services/scorer/requirements.txt

export NATS_URL=nats://localhost:4222
export PROFILE_PATH=profiles/sai/profile.parsed.yaml
export EMBEDDING_MODEL=all-MiniLM-L6-v2   # optional
export SCORER_BATCH_SIZE=32                # optional
export PYTHONPATH=.

python -m services.scorer.main
```

The first run downloads the embedding model (~80 MB). The profile embedding is
cached at `.cache/profile_embedding_<model>.npy` and rebuilt if the profile
yaml mtime changes.

## Output schema

Each `jobs.scored` message is a `ScoredJob` JSON: `{job, score, embedding_score,
rule_adjustments, matched_skills, missing_skills, reason}`.

## Tests

```bash
cd services/scorer && python -m pytest test_scorer.py -v
```

Tests use a stubbed profile fixture so they do not depend on
`profile.parsed.yaml`. They do download the embedding model on first run.

## Docker

```bash
docker build -f services/scorer/Dockerfile -t jobseeker-scorer .
docker run --rm -e NATS_URL=nats://host.docker.internal:4222 jobseeker-scorer
```
