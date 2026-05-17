# Multi-stage Dockerfile for html_scraper.
# Runtime image is the official Playwright Python image so chromium + system
# fonts/codecs are already baked in. Build stage installs Python deps into a
# venv-style /install prefix that we copy into the slim runtime layer.

FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

COPY services/html_scraper/requirements.txt /build/requirements.txt
RUN pip install --prefix=/install --no-cache-dir -r requirements.txt


FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    NATS_URL=nats://nats:4222 \
    SEED_PATH=/app/companies/seed_500.yaml \
    HTML_TARGETS_PATH=/app/companies/html_targets.yaml

WORKDIR /app

COPY --from=builder /install /usr/local

COPY shared/ /app/shared/
COPY services/__init__.py /app/services/__init__.py
COPY services/html_scraper/ /app/services/html_scraper/

# companies/ is mounted read-only via docker-compose.

CMD ["python", "-m", "services.html_scraper.main"]
