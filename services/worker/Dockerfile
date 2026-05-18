FROM docker-upstream.apple.com/library/python:3.12-slim

WORKDIR /app

# System deps for python-docx and lxml
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libxml2-dev libxslt-dev \
    && rm -rf /var/lib/apt/lists/*

COPY services/worker/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Shared models + config
COPY shared/ /app/shared/
# Worker source (activities + workflows + services package)
COPY services/worker/ /app/worker/
COPY services/workflows/ /app/services/workflows/
COPY services/scraper/main.py /app/scraper/main.py
COPY services/scraper/connectors/ /app/connectors/
COPY scripts/ /app/scripts/

ENV PYTHONPATH=/app:/app/shared
CMD ["python", "-m", "worker.main"]
