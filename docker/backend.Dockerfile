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
    && chown -R root:root /app \
    && chmod 0755 /app \
    && chown -R tgcurator:tgcurator /var/lib/tgcurator

WORKDIR /app

COPY pyproject.toml README.md ./
RUN mkdir -p tgcurator apps \
    && printf '"""Dependency cache stub."""\n' > tgcurator/__init__.py \
    && printf '"""Dependency cache stub."""\n' > apps/__init__.py \
    && pip install --no-cache-dir ".[runtime]" \
    && rm -rf tgcurator apps

COPY tgcurator ./tgcurator
COPY apps ./apps
COPY migrations ./migrations
COPY alembic.ini ./
COPY scripts ./scripts

RUN pip install --no-cache-dir --no-deps --no-build-isolation . \
    && python -c 'import pathlib; pathlib.Path("/usr/local/lib/python3.12/site-packages/sitecustomize.py").write_text("import os, urllib.parse as u\nif not (os.environ.get(\"TGCURATOR_DATABASE_URL\") or \"\").strip() and not (os.environ.get(\"DATABASE_URL\") or \"\").strip():\n    host = (os.environ.get(\"POSTGRES_HOST\") or \"\").strip()\n    pwd = os.environ.get(\"POSTGRES_PASSWORD\")\n    if host and pwd is not None:\n        user = u.quote(os.environ.get(\"POSTGRES_USER\") or \"tgcurator\", safe=\"\")\n        enc_pwd = u.quote(pwd, safe=\"\")\n        port = (os.environ.get(\"POSTGRES_PORT\") or \"5432\").strip()\n        db = u.quote(os.environ.get(\"POSTGRES_DB\") or \"tgcurator\", safe=\"\")\n        os.environ[\"TGCURATOR_DATABASE_URL\"] = f\"postgresql+asyncpg://{user}:{enc_pwd}@{host}:{port}/{db}\"\n", encoding="utf-8")' \
    && chmod 0755 /app/scripts/*.sh \
    && chown -R root:root /app

USER tgcurator

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]
