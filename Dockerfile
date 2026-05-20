FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

# Install dependencies first to maximize layer caching.
COPY app/requirements.txt /srv/app/requirements.txt
RUN pip install --no-cache-dir -r /srv/app/requirements.txt

COPY app /srv/app
COPY pyproject.toml /srv/pyproject.toml

# Default config/state/log locations. The compose file mounts var/ as a volume.
ENV CONFIG_DIR=/srv/app/config \
    STATE_DIR=/srv/var/state \
    LOG_DIR=/srv/var/logs

RUN mkdir -p /srv/var/state /srv/var/logs && \
    useradd --create-home --uid 10001 nurture && \
    chown -R nurture /srv

USER nurture
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request, sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/healthz').status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
