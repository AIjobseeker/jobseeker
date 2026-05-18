FROM python:3.12-slim

WORKDIR /app

COPY services/notifier/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY shared/ /app/shared/
COPY services/__init__.py /app/services/__init__.py
COPY services/notifier/ /app/services/notifier/

ENV PYTHONPATH=/app
ENV JOBSEEKER_DB_PATH=/data/seen.db

# The host should mount ~/.jobseeker to /data so the dedup DB survives restarts
# and is visible to healthcheck.py running on the host.
VOLUME ["/data"]

CMD ["python", "-m", "services.notifier.main"]
