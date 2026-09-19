#!/bin/sh
# Production entrypoint: migrate (under an advisory lock), then serve.
#
# Alembic is the sole schema owner in production and staging. The application
# never runs Base.metadata.create_all there, so the schema MUST be brought up
# to date here before any traffic arrives.
#
# scripts/migrate.py wraps `alembic upgrade head` in a PostgreSQL advisory
# lock so that concurrent container starts serialize instead of racing DDL
# (see that file for the rationale). The lock is PostgreSQL-only, matching
# the production/staging topology.
set -e

python scripts/migrate.py

# uvicorn production defaults:
#   --workers            : one per CPU core (override with WEB_CONCURRENCY)
#   --proxy-headers      : trust X-Forwarded-Proto/Host from the TLS terminator
#                          so PUBLIC_BASE_URL, wss:// and Twilio signatures work
#   --forwarded-allow-ips: restrict which proxies are trusted (default "*" for
#                          single-proxy setups; tighten in hardened deployments)
exec uvicorn app.main:app \
  --host 0.0.0.0 \
  --port 8000 \
  --workers "${WEB_CONCURRENCY:-2}" \
  --proxy-headers \
  --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-*}"
