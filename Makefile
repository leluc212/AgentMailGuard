.PHONY: help up down migrate migrate-down seed test test-unit test-integration lint fmt fmt-check ci eval load

UV ?= uv

help:
	@echo "Enterprise RAG-Based Intelligent Email Management and Response System"
	@echo "Available targets:"
	@echo "  up       - Boot the Docker Compose infrastructure stack (Task 0.3)"
	@echo "  down     - Tear down the infrastructure stack"
	@echo "  migrate  - Run database migrations (Task 0.4)"
	@echo "  seed     - Seed database with reference organizations/mailboxes (Task 0.12)"
	@echo "  test     - Run unit and integration tests"
	@echo "  lint     - Run static analysis and formatting checks (ruff, mypy)"
	@echo "  fmt      - Auto-format codebase using ruff"
	@echo "  eval     - Run RAG and classification evaluation benchmarks (Phase 7)"
	@echo "  load     - Run end-to-end load tests (Phase 8)"

up:
	@if [ -f docker-compose.yml ]; then \
		docker compose up -d; \
	else \
		echo "[INFO] docker-compose.yml will be created in Task 0.3."; \
	fi

down:
	@if [ -f docker-compose.yml ]; then \
		docker compose down; \
	else \
		echo "[INFO] docker-compose.yml will be created in Task 0.3."; \
	fi

migrate:
	$(UV) run python -m packages.db.cli up

migrate-down:
	$(UV) run python -m packages.db.cli down

seed:
	$(UV) run python -m packages.db.seed

test:
	$(UV) run pytest tests -v

test-unit:
	$(UV) run pytest tests/unit -v

test-integration:
	$(UV) run pytest tests/integration -v

lint:
	$(UV) run ruff check .
	$(UV) run mypy packages services tests evaluation

fmt:
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

fmt-check:
	$(UV) run ruff format --check .

ci: fmt-check lint test-unit test-integration

eval:
	@echo "[INFO] Evaluation benchmark runners configured in Phase 7 / Task 0.13."

load:
	@echo "[INFO] Load test harness configured in Phase 8."
