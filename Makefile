.PHONY: help up down logs migrate seed test lint dev clean

help:
	@echo "make up       - start postgres + api + scheduler"
	@echo "make migrate  - apply database migrations"
	@echo "make seed     - create a demo tenant"
	@echo "make test     - run the test suite"
	@echo "make lint     - ruff check"
	@echo "make dev      - run the api locally (no docker)"
	@echo "make down     - stop everything"

up:
	docker compose up -d --build
	@echo "waiting for the database..."
	@sleep 5
	docker compose exec -T api alembic upgrade head
	@echo "VoxDesk is running on http://localhost:8000/docs"

down:
	docker compose down

logs:
	docker compose logs -f api scheduler

migrate:
	alembic upgrade head

revision:
	alembic revision --autogenerate -m "$(m)"

seed:
	python -m scripts.seed_demo_tenant

test:
	python -m pytest -q

lint:
	ruff check app tests scripts

dev:
	uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

worker:
	python -m scripts.scheduler

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
