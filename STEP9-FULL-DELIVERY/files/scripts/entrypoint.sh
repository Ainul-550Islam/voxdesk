#!/bin/sh
# Production entrypoint: migrate, then serve.
#
# Alembic is the sole schema owner in production. The application never runs
# Base.metadata.create_all when APP_ENV=production, so the schema MUST be
# brought up to date here before any traffic arrives.
set -e

alembic upgrade head

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
