FROM python:3.13-slim
LABEL org.opencontainers.image.source="https://github.com/Really-Bad-Apps/hivemake-runner" \
      org.opencontainers.image.licenses="MIT"

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --uid 1000 --create-home --shell /usr/sbin/nologin appuser
COPY hivemake_runner.py .
COPY blueprints ./blueprints
USER appuser
ENV PYTHONUNBUFFERED=1 PORT=7183 RUNNER_SOCKET=/run/hivemake-runner/runner.sock
EXPOSE 7183
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://localhost:' + os.environ['PORT'] + '/health', timeout=3)"
CMD exec gunicorn --bind 0.0.0.0:$PORT --workers 2 --threads 4 --access-logfile - "hivemake_runner:create_app()"
