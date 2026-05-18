FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    NATS_URL=nats://nats:4222 \
    SEED_PATH=/app/companies/seed_500.yaml \
    HTML_TARGETS_PATH=/app/companies/html_targets.yaml

WORKDIR /app

COPY services/html_scraper/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY shared/ /app/shared/
COPY services/__init__.py /app/services/__init__.py
COPY services/html_scraper/ /app/services/html_scraper/

# companies/ is mounted read-only via docker-compose.

CMD ["python", "-m", "services.html_scraper.main"]
