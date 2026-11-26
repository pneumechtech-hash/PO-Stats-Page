FROM python:3.13-slim

# tzdata: SQLite's 'localtime' and Python's local time read the zone set by TZ
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PO_STATS_DB=/data/po_stats.db \
    PO_STATS_IMPORT_DIR=/import

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py db.py ingest.py settings.py ./
COPY templates/index.html templates/

# /data holds the SQLite database. It is world-writable so the container
# still works if you run it as a different user via `user:` in compose.
RUN useradd --uid 1000 --create-home appuser \
    && mkdir -p /data /import \
    && chown appuser:appuser /data \
    && chmod 0777 /data
USER appuser
VOLUME /data

EXPOSE 5000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/healthz', timeout=4)"

# One worker so imports never run twice at once; threads serve page requests
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--threads", "4", "--timeout", "120", "--access-logfile", "-", "app:app"]
