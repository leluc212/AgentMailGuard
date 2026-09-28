.PHONY: help up down migrate migrate-down seed test test-unit test-integration test-e2e lint fmt fmt-check ci eval load broker-migrate-retry image-smoke smoke phase4-gate retrieval-gate phase5-gate llm-smoke connect-gmail phase6-gate

UV ?= uv

help:
	@echo "Enterprise RAG-Based Intelligent Email Management and Response System"
	@echo "Available targets:"
	@echo "  up       - Build images from HEAD and boot the full stack (init runs migrations, buckets, topology)"
	@echo "  down     - Tear down the infrastructure stack"
	@echo "  migrate  - Run database migrations (Task 0.4)"
	@echo "  seed     - Seed database with reference organizations/mailboxes (Task 0.12)"
	@echo "  test     - Run unit and integration tests"
	@echo "  test-e2e - Browser tests of the review UI (Playwright; needs 'uv run playwright install chromium')"
	@echo "  lint     - Run static analysis and formatting checks (ruff, mypy)"
	@echo "  fmt      - Auto-format codebase using ruff"
	@echo "  eval     - Run RAG and classification evaluation benchmarks (Phase 7)"
	@echo "  load     - Run end-to-end load tests (Phase 8)"
	@echo "  broker-migrate-retry - One-time: delete empty stale retry queues (RA.3)"
	@echo "  image-smoke - Build the runtime image and smoke-check its entrypoints and assets (RA.11)"
	@echo "  smoke    - End-to-end check of the running stack (RA gate)"
	@echo "  llm-smoke - Live check: one triage + one draft request through the configured LLM (task 5.0; not CI)"
	@echo "  phase5-gate - Live Phase 5 gate on a real model, owner-run (task 5.6)"
	@echo "  connect-gmail ADDRESS=... - Register the Gmail test account as a watched mailbox, owner-run (task 6.10)"
	@echo "  phase6-gate - Live Phase 6 gate: real email -> draft -> approve -> threaded Gmail reply, owner-run (task 6.10)"

up:
	@if [ -f docker-compose.yml ]; then \
		docker compose up -d --build; \
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

test-e2e:
	$(UV) run pytest tests/e2e -v

lint:
	$(UV) run ruff check .
	$(UV) run mypy packages services tests evaluation

fmt:
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

fmt-check:
	$(UV) run ruff format --check .

ci: fmt-check lint test-unit test-integration test-e2e

eval:
	@echo "[INFO] Evaluation benchmark runners configured in Phase 7 / Task 0.13."

load:
	@echo "[INFO] Load test harness configured in Phase 8."

broker-migrate-retry:
	docker exec rag-email-rabbitmq sh -c 'for q in email.retry.30s email.retry.5m email.retry.30m; do \
	  if rabbitmqctl -q -p / list_queues name arguments | grep -F "$$q" | grep -q "email.route"; then \
	    rabbitmqctl -p / delete_queue "$$q" --if-empty || exit 1; \
	  fi; \
	done'
	@echo "Stale retry queues removed; they are redeclared on the next init/worker start."

IMAGE ?= rag-email-runtime:smoke

image-smoke:
	docker build -t $(IMAGE) .
	docker run --rm -i --network none $(IMAGE) python - < scripts/image_smoke.py

smoke:
	$(UV) run python scripts/stack_smoke.py

phase4-gate:
	$(UV) run python scripts/phase4_gate.py --mode $(or $(MODE),default)

retrieval-gate:
	$(UV) run python scripts/retrieval_gate.py

phase5-gate:
	$(UV) run python scripts/phase5_gate.py

connect-gmail:
	@test -n "$(ADDRESS)" || { echo "usage: make connect-gmail ADDRESS=<test account address>"; exit 2; }
	$(UV) run python scripts/connect_gmail.py --address $(ADDRESS)

phase6-gate:
	$(UV) run python scripts/phase6_gate.py

llm-smoke:
	$(UV) run python scripts/llm_smoke.py
