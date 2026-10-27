# Two images from one Dockerfile:
#   runtime  what ships to staging and production: app code + runtime deps only
#   dev      runtime + test tools + the synthetic data generator (docker compose uses this)
#
# Build once, promote the same image: CI builds `runtime`, tags it with the git
# commit SHA, and that exact image goes to staging and then production.

# Debian 13 (trixie) base. The dev image adds a v16 postgresql-client to match
# the Postgres 16 server (see the dev stage).
FROM python:3.12-slim-trixie AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
RUN useradd --create-home --uid 1000 appuser

# ---------------------------------------------------------------- runtime
FROM base AS runtime
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY alembic.ini .
COPY migrations ./migrations
COPY app ./app
COPY scripts/db_roles.sql ./scripts/db_roles.sql
ARG GIT_SHA=unknown
ENV GIT_SHA=$GIT_SHA
USER appuser
EXPOSE 8000
# --timeout-graceful-shutdown: on a deploy, finish in-flight requests before
# exiting (ECS waits 30 s by default before force-killing). Work that takes
# longer is recovered on the next start (app/maintenance.py).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*", "--timeout-graceful-shutdown", "25"]

# ---------------------------------------------------------------- dev
FROM base AS dev
# pg_dump/pg_restore for the backup drill (scripts/backup_drill.py), matching
# the Postgres 16 server. Debian's own v17 client can dump a v16 server, but its
# dumps contain `SET transaction_timeout`, which a v16 server rejects on restore.
RUN apt-get update && apt-get install -y --no-install-recommends postgresql-common ca-certificates curl \
    && /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y \
    && apt-get install -y --no-install-recommends postgresql-client-16 \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt requirements-dev.txt ./
RUN pip install -r requirements-dev.txt
COPY . .
RUN mkdir -p /data/storage /app/data && chown -R appuser /app /data
USER appuser
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
