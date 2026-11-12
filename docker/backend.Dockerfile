FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        ffmpeg \
        tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 tgcurator \
    && useradd --system --uid 10001 --gid tgcurator --create-home --home-dir /home/tgcurator tgcurator \
    && mkdir -p /app /var/lib/tgcurator/archive /var/lib/tgcurator/tmp \
    && chown -R tgcurator:tgcurator /app /var/lib/tgcurator

WORKDIR /app

COPY pyproject.toml README.md ./
COPY tgcurator ./tgcurator
COPY apps ./apps
COPY migrations ./migrations
COPY alembic.ini ./
COPY scripts ./scripts

RUN pip install --no-cache-dir ".[runtime]" \
    && chmod +x /app/scripts/*.sh \
    && chown -R tgcurator:tgcurator /app

USER tgcurator

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
