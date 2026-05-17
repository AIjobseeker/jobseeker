FROM python:3.12-slim

WORKDIR /app

# System deps for lxml / BeautifulSoup HTML parsing
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libxml2-dev libxslt-dev \
    && rm -rf /var/lib/apt/lists/*

COPY services/scraper/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Shared models + config
COPY shared/ /app/shared/
# Scraper source
COPY services/scraper/ /app/

ENV PYTHONPATH=/app:/app/shared
CMD ["python", "main.py"]
