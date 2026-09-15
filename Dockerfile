# ============================================================ stage 1: web ===
# Build the React dashboard into static assets. This keeps node entirely out
# of the runtime image.
FROM node:20-alpine AS dashboard-build

WORKDIR /srv
COPY dashboard/package.json dashboard/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY dashboard/ ./
RUN npm run build

# ============================================================ stage 2: api ===
# Base images are pinned to a Debian codename so the OS userland does not
# float across major releases; the patch level within a codename still floats
# by design (security patches come in automatically). Moving this pin is a
# deliberate, tested change, never a blind upgrade — see docs/DEPLOYMENT.md.
FROM python:3.12-slim-bookworm

# ffmpeg: audio processing for the voice pipeline. libsndfile1 + build deps:
# several audio wheels (resampling, etc.) need them at install time.
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential ffmpeg libsndfile1 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application, migrations, worker scripts, and the built dashboard. Alembic is
# the sole schema owner in production, so the full alembic/ tree ships here and
# the entrypoint runs `alembic upgrade head` before serving.
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini .
COPY scripts ./scripts
COPY --from=dashboard-build /srv/dist ./dashboard/dist

# Unprivileged runtime user; writable dirs for the local knowledge backend and
# mounted secrets.
RUN useradd --create-home --uid 10001 appuser \
 && mkdir -p /srv/var/knowledge /srv/secrets \
 && chown -R appuser:appuser /srv
USER appuser

ENV PYTHONUNBUFFERED=1

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)"

# entrypoint applies migrations, then execs uvicorn with production defaults
# (multi-worker, proxy-header aware). See scripts/entrypoint.sh.
ENTRYPOINT ["sh", "scripts/entrypoint.sh"]
