FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    DB_PATH=/data/claw-telegram.db HTTP_PORT=8080

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install ".[claude]" && useradd --system --uid 10001 --home /data claw \
    && mkdir -p /data && chown claw /data

USER claw
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"HTTP_PORT\"]}/healthz', timeout=4)"
CMD ["python", "-m", "claw_telegram"]
