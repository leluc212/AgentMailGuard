.PHONY: help up down migrate migrate-down seed test test-unit test-integration test-e2e lint fmt fmt-check ci eval load broker-migrate-retry image-smoke smoke phase4-gate retrieval-gate phase5-gate llm-smoke connect-gmail phase6-gate mailguard-worktree mailguard-prep mailguard-smoke mailguard-probe mailguard-test

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
	@echo "  mailguard-worktree - Check out AgentMailGuard at the pinned commit in ../AgentMailGuard-bench (task 7.19)"
	@echo "  mailguard-prep - One-time: download the guard's datasets and train its L1 classifier (network, no API key; not CI)"
	@echo "  mailguard-smoke - Offline check of the AgentMailGuard install and wiring (not CI)"
	@echo "  mailguard-probe - ONE live guard-judge call on the Gemini API, owner-run (not CI)"
	@echo "  mailguard-test - Guard-side unit tests under the AgentMailGuard overlay (fake models, no network)"

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

# AgentMailGuard benchmark (task 7.19, ADR-0010). The guard is a pinned, detached git worktree
# OUTSIDE this repo (ruff/pytest never see it), overlaid per command with `uv run --with-editable`,
# so pyproject.toml, uv.lock, .venv and `make ci` are untouched. Always `python -m` from this root:
# AgentMailGuard also ships top-level `services`/`evaluation` packages.
MAILGUARD_DIR ?= $(abspath $(CURDIR)/../AgentMailGuard-bench)
MAILGUARD_ARTIFACTS ?= $(abspath $(CURDIR)/../AgentMailGuard-bench-artifacts)
MAILGUARD_REMOTE_BRANCH ?= feature/mailguard-defense-stack
MAILGUARD_COMMIT ?= 81df5d07b15b5bb3d1ecf3aae556df01e304cbe0
MAILGUARD_UV = MAILGUARD_DIR=$(MAILGUARD_DIR) MAILGUARD_COMMIT=$(MAILGUARD_COMMIT) MAILGUARD_ARTIFACTS=$(MAILGUARD_ARTIFACTS) $(UV) run --project $(CURDIR) --with-editable $(MAILGUARD_DIR)
MAILGUARD_EVAL_DEPS = --with 'datasets>=2.20' --with 'pandas>=2.2' --with 'huggingface-hub>=0.24' --with 'tqdm>=4.66' --with 'pyarrow>=15'

mailguard-worktree:
	git fetch origin $(MAILGUARD_REMOTE_BRANCH)
	@if [ -e "$(MAILGUARD_DIR)" ]; then echo "[INFO] $(MAILGUARD_DIR) exists; not re-adding"; \
	else git worktree add --detach "$(MAILGUARD_DIR)" $(MAILGUARD_COMMIT); fi
	@test "$$(git -C "$(MAILGUARD_DIR)" rev-parse HEAD)" = "$(MAILGUARD_COMMIT)" || \
	  { echo "FAIL $(MAILGUARD_DIR) is not at MAILGUARD_COMMIT=$(MAILGUARD_COMMIT)" >&2; exit 1; }
	@echo "ok AgentMailGuard worktree $(MAILGUARD_DIR) @ $(MAILGUARD_COMMIT)"

mailguard-prep: mailguard-worktree
	mkdir -p $(MAILGUARD_ARTIFACTS)
	$(MAILGUARD_UV) $(MAILGUARD_EVAL_DEPS) --directory $(MAILGUARD_DIR) python -m mailguard.datasets.download --all --max-mb 400
	$(MAILGUARD_UV) $(MAILGUARD_EVAL_DEPS) --directory $(MAILGUARD_DIR) python -m mailguard.datasets.build_l1_corpus --out-dir $(MAILGUARD_ARTIFACTS)/l1_injection
	$(MAILGUARD_UV) --directory $(MAILGUARD_DIR) python -m training.train_l1_classifier --corpus $(MAILGUARD_ARTIFACTS)/l1_injection --out $(MAILGUARD_ARTIFACTS)/l1_injection_clf_v1.joblib

mailguard-smoke:
	$(MAILGUARD_UV) python -m evaluation.mailguard_bench.guard_smoke

mailguard-probe:
	$(MAILGUARD_UV) python -m evaluation.mailguard_bench.guard_smoke --live-probe

mailguard-test:
	$(MAILGUARD_UV) python -m pytest tests/unit/test_mailguard_bench_guard.py -v
