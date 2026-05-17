FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY services/scorer/requirements.txt /build/requirements.txt
RUN pip install --prefix=/install --no-cache-dir -r requirements.txt

ARG EMBEDDING_MODEL=all-MiniLM-L6-v2
ENV EMBEDDING_MODEL=${EMBEDDING_MODEL} \
    HF_HOME=/install/hf-cache \
    SENTENCE_TRANSFORMERS_HOME=/install/hf-cache
RUN PYTHONPATH=/install/lib/python3.12/site-packages \
    python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('${EMBEDDING_MODEL}')"


FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/opt/hf-cache \
    SENTENCE_TRANSFORMERS_HOME=/opt/hf-cache \
    PYTHONPATH=/app

WORKDIR /app

COPY --from=builder /install /usr/local
COPY --from=builder /install/hf-cache /opt/hf-cache

COPY shared/ /app/shared/
COPY services/__init__.py /app/services/__init__.py
COPY services/scorer/ /app/services/scorer/

CMD ["python", "-m", "services.scorer.main"]
