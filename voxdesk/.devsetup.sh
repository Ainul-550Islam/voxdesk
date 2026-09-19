#!/bin/sh
pip install -q structlog sqlalchemy pydantic pydantic-settings pytest pytest-asyncio \
  httpx fastapi python-multipart aiosqlite bcrypt "pyjwt>=2.8" email-validator ruff 2>&1 | grep -v notice
