.PHONY: help up down migrate migrate-down seed test test-unit test-integration test-e2e lint fmt fmt-check ci eval load broker-migrate-retry image-smoke smoke phase4-gate retrieval-gate phase5-gate llm-smoke connect-gmail phase6-gate mailguard-worktree mailguard-prep mailguard-prep-check mailguard-smoke mailguard-probe mailguard-test mailguard-unit mailguard-cases mailguard-bench mailguard-bench-test mailguard-report mailguard-analyses

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
	@echo "  mailguard-worktree - Verify that agentmailguard/ is at the pinned v3 commit (1835a6e; a commit of this repository, so only the subtree layout applies); for a v1 or v2 pin (guard-repository commits) create or check a worktree at MAILGUARD_COMMIT in ../AgentMailGuard-bench (tasks 7.19, 7.24)"
	@echo "  mailguard-prep - One-time: download the guard's datasets and train its L1 classifier (network, no API key; not CI)"
	@echo "  mailguard-smoke - Offline check of the AgentMailGuard install and wiring (not CI)"
	@echo "  mailguard-probe [MODEL=<profile>] - ONE live guard-judge call on the profile's endpoint (default: the Gemma test model on the Gemini API); on an OpenRouter profile it also checks the pinned provider. Owner-run (not CI)"
	@echo "  mailguard-test - ONE guard-side unit file (tests/unit/test_mailguard_bench_guard.py) under the AgentMailGuard overlay (fake models, no network); the whole suite is mailguard-unit"
	@echo "  mailguard-unit - The WHOLE unit suite under the AgentMailGuard overlay (fake models, no network, no classifier): runs the ~260 tests that plain pytest skips for want of the guard; CI runs it on the committed agentmailguard/ (run it before main and before a benchmark)"
	@echo "  mailguard-cases - Build/verify the pinned benchmark case set from the guard's builder (no API calls; not CI)"
	@echo "  mailguard-bench RUN=... CONFIG=C0|C0T|C1|C2|C3|C3-L1..C3-L4|C3-ISO [JUDGE_FALLBACK=1] - Benchmark on the real rag-email path, scheme v3 (guard v3 presets, ADR-0015): C0 no guard, C0T guard template with no layer, C1 Inbound Guard + Inbound Gate, C2 C1 + Doc Scanner + isolation, C3 the full guard, C3-L1..C3-L4 C3 minus one layer, C3-ISO C3 without channel isolation; every judge of a layer the config runs is live. Schemes v1/v2 are refused (their guard commits). Owner-run, live model (not CI)"
	@echo "  mailguard-bench-test - Guard-wiring tests under the AgentMailGuard overlay (fake providers; not CI)"
	@echo "  mailguard-report RUN=... [LEGACY=1] - Score a scheme v3 run (manifest.json, metrics.csv, report.md); LEGACY=1 prints a committed v1/v2 folder's tables read-only; no model calls (task 7.19, v3.13)"
	@echo "  mailguard-analyses RUN=... - Leakage check, first catching layer, worked examples, then the report; no model calls (task 7.19)"
	@echo "  bench-doctor [MODEL=<profile>] [READER=<model>] - Teammate kit: check this machine (Docker, Python, disk, keys in .env, the embedding) before the first run; MODEL adds that profile's key and, for a local model, the Ollama and GPU checks; no model calls (tasks 7.25, 7.29)"
	@echo "  bench-setup - Teammate kit, once per machine: guard worktree, pinned inputs, offline guard smoke, then the stack up and healthy (task 7.23)"
	@echo "  bench-run MODEL=<profile> RUN=<id> [CONFIGS=C0,C0T,...] [LIMIT=n] [CASE_IDS=id,id,...] [CONCURRENCY=1|2] [DRY_RUN=1] - Teammate kit: one embedding check, then one model through every config, the retry pass and the reports (runbook 9.9 steps 3-7); owner-run, live; a rerun resumes, also after another model's run; CASE_IDS is a trial over chosen cases, RUN=trial-... (tasks 7.23, 7.29)"
	@echo "  bench-report RUN=<id> [READER=<model>] - Teammate kit: rebuild the reports of a run, with the meaning column when READER is given (run it with LLM__PROVIDER and LLM__OPENAI_BASE_URL set inline: the reader is served by LLM__*); no model calls except the reader's (task 7.23)"
	@echo "  bench-package RUN=<id> - Teammate kit: bench-results-<id>.zip of the run folder (raw/ included, never a key) and how to commit it to branch bench/<id> (task 7.23)"
	@echo "  bench-canary MODEL=qwen2.5-7b-openrouter|llama-3.1-8b-openrouter - ONE live, billed strict-JSON call through rag-email's pinned client: checks the answer, the served provider and saves the exchange (no key) under evaluation/results/mailguard_bench/canary/; under a cent. Owner- or teammate-run before each OpenRouter run, never CI (task 7.29)"

up:
	@if [ -f docker-compose.yml ]; then \
		GIT_COMMIT=$$(git rev-parse HEAD 2>/dev/null || echo unknown) docker compose up -d --build; \
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

# AgentMailGuard benchmark (task 7.19, ADR-0010; task 7.24). Two layouts, same targets:
#   - subtree  (after the final merge, ADR-0012 decision 6): the guard is committed INSIDE this repo
#     under agentmailguard/ (git subtree, full history). It is pinned by its TREE: the directory
#     is at MAILGUARD_COMMIT iff its tree is that commit's tree and nothing under it differs.
#   - worktree (before it): a pinned, detached git worktree OUTSIDE this repo, ../AgentMailGuard-bench.
# Either way ruff/mypy/pytest/docker never see the guard, and it is overlaid per command with
# `uv run --with-editable`, so pyproject.toml, uv.lock, .venv and `make ci` are untouched. Always
# `python -m` from this root: AgentMailGuard also ships top-level `services`/`evaluation` packages.
# MAILGUARD_DIR / MAILGUARD_ARTIFACTS from the environment or the command line win over both defaults.
ifneq ($(wildcard $(CURDIR)/agentmailguard/mailguard/__init__.py),)
MAILGUARD_DIR ?= $(CURDIR)/agentmailguard
else
MAILGUARD_DIR ?= $(abspath $(CURDIR)/../AgentMailGuard-bench)
endif
# The L1 classifier: the owner's copy in evaluation/mailguard_bench/pinned/ when it is there (it is not
# in git, ADR-0012 decision 15; the kit checks its sha256), else what `make mailguard-prep` trains.
ifneq ($(wildcard $(CURDIR)/evaluation/mailguard_bench/pinned/l1_injection_clf_v1.joblib),)
MAILGUARD_ARTIFACTS ?= $(CURDIR)/evaluation/mailguard_bench/pinned
else
MAILGUARD_ARTIFACTS ?= $(abspath $(CURDIR)/../AgentMailGuard-bench-artifacts)
endif
# Where `make mailguard-prep` writes the corpus and the retrained classifier. Never the pinned,
# git-tracked directory (prep would replace the pinned joblib and break SHA256SUMS): when
# MAILGUARD_ARTIFACTS is that directory, prep writes beside the repository instead, and
# `make ... MAILGUARD_ARTIFACTS=<that directory>` uses what it trained.
MAILGUARD_PINNED_DIR = $(CURDIR)/evaluation/mailguard_bench/pinned
MAILGUARD_PREP_OUT ?= $(if $(filter $(abspath $(MAILGUARD_ARTIFACTS)),$(MAILGUARD_PINNED_DIR)),$(abspath $(CURDIR)/../AgentMailGuard-bench-artifacts),$(MAILGUARD_ARTIFACTS))
MAILGUARD_REMOTE_BRANCH ?= feature/mailguard-defense-stack
# The guard commit the v3 benchmark pins (ADR-0015, plan_v3 Task 15b): the v3 guard after the
# identifier ratchet; guard_env.DEFAULT_MAILGUARD_COMMIT is the same value (a test keeps them equal).
# v1 and v2 stay reproducible at their own pins, in a worktree of their own:
# make <target> MAILGUARD_COMMIT=81df5d07b15b5bb3d1ecf3aae556df01e304cbe0 MAILGUARD_DIR=<worktree at it>.
MAILGUARD_COMMIT ?= 1835a6ea3c120aae8f5074abe92d6c404d7ce3d4
# The subtree layout compares trees: the pin's tree is its guard-repository commit's root tree (v1, v2) or, for a
# commit of this repository (v3), the tree of the guard directory inside it. A shallow clone has no commit object to ask for its tree, so
# the three pins' trees are recorded here too (guard_env.V1_/V2_/V3_MAILGUARD_TREE; a test
# keeps them equal).
MAILGUARD_V1_COMMIT = 81df5d07b15b5bb3d1ecf3aae556df01e304cbe0
MAILGUARD_V1_TREE = 1a955c156a23cb6ad6dbde041c93080dfb69ceca
MAILGUARD_V2_COMMIT = 915cb1e2b86395e4389673deb9914308cc39627b
MAILGUARD_V2_TREE = f659748a611340e093d41189dc1f445a2f842134
MAILGUARD_V3_COMMIT = 1835a6ea3c120aae8f5074abe92d6c404d7ce3d4
MAILGUARD_V3_TREE = e3ecca18d0814334c1c94ba0d3696e11bf1c1b43
MAILGUARD_TREE ?= $(if $(filter $(MAILGUARD_COMMIT),$(MAILGUARD_V1_COMMIT)),$(MAILGUARD_V1_TREE),$(if $(filter $(MAILGUARD_COMMIT),$(MAILGUARD_V2_COMMIT)),$(MAILGUARD_V2_TREE),$(if $(filter $(MAILGUARD_COMMIT),$(MAILGUARD_V3_COMMIT)),$(MAILGUARD_V3_TREE))))
# Non-empty (the directory's path inside its repository) iff MAILGUARD_DIR is a subdirectory of one.
MAILGUARD_PREFIX := $(shell git -C "$(MAILGUARD_DIR)" rev-parse --show-prefix 2>/dev/null)
MAILGUARD_UV = MAILGUARD_DIR=$(MAILGUARD_DIR) MAILGUARD_COMMIT=$(MAILGUARD_COMMIT) MAILGUARD_ARTIFACTS=$(MAILGUARD_ARTIFACTS) $(UV) run --project $(CURDIR) --with-editable $(MAILGUARD_DIR)
MAILGUARD_EVAL_DEPS = --with 'datasets>=2.20' --with 'pandas>=2.2' --with 'huggingface-hub>=0.24' --with 'tqdm>=4.66' --with 'pyarrow>=15'

ifeq ($(MAILGUARD_PREFIX),)
mailguard-worktree:
	git fetch origin $(MAILGUARD_REMOTE_BRANCH)
	@if [ -e "$(MAILGUARD_DIR)" ]; then echo "[INFO] $(MAILGUARD_DIR) exists; not re-adding"; \
	else git worktree add --detach "$(MAILGUARD_DIR)" $(MAILGUARD_COMMIT); fi
	@test "$$(git -C "$(MAILGUARD_DIR)" rev-parse HEAD)" = "$(MAILGUARD_COMMIT)" || \
	  { echo "FAIL $(MAILGUARD_DIR) is not at MAILGUARD_COMMIT=$(MAILGUARD_COMMIT); use another directory (make ... MAILGUARD_DIR=<path>) or set MAILGUARD_COMMIT to that worktree's commit for a v1 run" >&2; exit 1; }
	@echo "ok AgentMailGuard worktree $(MAILGUARD_DIR) @ $(MAILGUARD_COMMIT)"
else
# Subtree layout: no worktree to add, no fetch (the guard came with this repository). Verify the pin.
mailguard-worktree:
	@want="$$(git -C "$(MAILGUARD_DIR)" rev-parse --verify -q "$(MAILGUARD_COMMIT):$(MAILGUARD_PREFIX)" || git -C "$(MAILGUARD_DIR)" rev-parse --verify -q "$(MAILGUARD_COMMIT)^{tree}" || echo "$(MAILGUARD_TREE)")"; \
	have="$$(git -C "$(MAILGUARD_DIR)" rev-parse "HEAD:$(MAILGUARD_PREFIX)" 2>/dev/null)"; \
	if [ -z "$$want" ] || [ "$$have" != "$$want" ]; then \
	  echo "FAIL $(MAILGUARD_DIR) is not at MAILGUARD_COMMIT=$(MAILGUARD_COMMIT) (its tree is '$$have', the pin's is '$$want'); use another directory (make ... MAILGUARD_DIR=<path>) or set MAILGUARD_COMMIT to that checkout's commit for a v1 run" >&2; exit 1; fi; \
	if [ -n "$$(git -C "$(MAILGUARD_DIR)" status --porcelain --untracked-files=normal -- .)" ]; then \
	  echo "FAIL $(MAILGUARD_DIR) has uncommitted changes; the benchmark pins a clean commit (on Windows, clone inside the Linux file system, not under /mnt/c, and check 'git config core.filemode' and core.autocrlf: file-mode and line-ending noise also shows as changes)" >&2; exit 1; fi
	@echo "ok AgentMailGuard subtree $(MAILGUARD_DIR) @ $(MAILGUARD_COMMIT)"
endif

# Listed before mailguard-worktree so a refused output directory stops prep before anything runs.
mailguard-prep-check:
	@case "$(abspath $(MAILGUARD_PREP_OUT))/" in "$(MAILGUARD_PINNED_DIR)/"*) \
	  echo "FAIL MAILGUARD_PREP_OUT=$(MAILGUARD_PREP_OUT) is inside the pinned directory $(MAILGUARD_PINNED_DIR): prep would replace the pinned classifier (its sha256 is in SHA256SUMS); choose another directory" >&2; exit 1;; esac

mailguard-prep: mailguard-prep-check mailguard-worktree
	mkdir -p $(MAILGUARD_PREP_OUT)
	$(MAILGUARD_UV) $(MAILGUARD_EVAL_DEPS) --directory $(MAILGUARD_DIR) python -m mailguard.datasets.download --all --max-mb 400
	$(MAILGUARD_UV) $(MAILGUARD_EVAL_DEPS) --directory $(MAILGUARD_DIR) python -m mailguard.datasets.build_l1_corpus --out-dir $(MAILGUARD_PREP_OUT)/l1_injection
	$(MAILGUARD_UV) --directory $(MAILGUARD_DIR) python -m training.train_l1_classifier --corpus $(MAILGUARD_PREP_OUT)/l1_injection --out $(MAILGUARD_PREP_OUT)/l1_injection_clf_v1.joblib
	@echo "ok prep wrote $(MAILGUARD_PREP_OUT); to benchmark with it: make ... MAILGUARD_ARTIFACTS=$(MAILGUARD_PREP_OUT)"

mailguard-smoke:
	$(MAILGUARD_UV) python -m evaluation.mailguard_bench.guard_smoke

mailguard-probe:
	$(MAILGUARD_UV) python -m evaluation.mailguard_bench.guard_smoke --live-probe $(if $(MODEL),--model-profile $(MODEL))

mailguard-test:
	$(MAILGUARD_UV) python -m pytest tests/unit/test_mailguard_bench_guard.py -v

mailguard-unit:
	$(MAILGUARD_UV) python -m pytest tests/unit -q

mailguard-cases:
	$(MAILGUARD_UV) python -m evaluation.mailguard_bench.build_cases

RUN ?=
MAILGUARD_LLM_TIMEOUT_S ?= 60
# CONCURRENCY reaches the runner as --concurrency only. Make exports a command-line variable
# to every recipe, and AppSettings would read CONCURRENCY as its `concurrency` settings group.
unexport CONCURRENCY
# CASE_IDS (a trial over chosen cases, task 7.29) reaches the kit as --case-ids only, likewise.
unexport CASE_IDS

# The config names have several meanings (ADR-0012 decision 11). Runs at HEAD are SCHEME=v3 (the
# guard v3 presets, ADR-0015): C0, C0T, C1, C2, C3, C3-L1..C3-L4, C3-ISO. SCHEME=v1 and v2 ran on
# other guard commits and the runner refuses them, naming the commit. JUDGE_FALLBACK=1 keeps a
# failed guard judge answer on the v2 visible fallback instead of failing closed (to compare with
# v2). SCHEME and JUDGE_FALLBACK reach the runner as flags only, like CONCURRENCY.
SCHEME ?= v3
unexport SCHEME
unexport JUDGE_FALLBACK

mailguard-bench:
	@case "$(SCHEME)/$(CONFIG)" in v3/C0|v3/C0T|v3/C1|v3/C2|v3/C3|v3/C3-L1|v3/C3-L2|v3/C3-L3|v3/C3-L4|v3/C3-ISO|v1/*|v2/*) ;; *) echo "usage: make mailguard-bench RUN=<id> CONFIG=C0|C0T|C1|C2|C3|C3-L1|C3-L2|C3-L3|C3-L4|C3-ISO [MODEL=gpt-4o-mini|qwen2.5-7b-openrouter|llama-3.1-8b-openrouter|llama-3.1-8b-local|qwen2.5-7b|gemma-4-26b] [LIMIT=n] [CONCURRENCY=1|2] [JUDGE_FALLBACK=1]"; exit 2;; esac
	@test -n "$(RUN)" || { echo "FAIL set RUN=<run_id>" >&2; exit 1; }
	$(MAILGUARD_UV) python -m evaluation.mailguard_bench.runner \
		--config $(CONFIG) --scheme $(SCHEME) --run $(RUN) --retry-errors \
		--llm-timeout-s $(MAILGUARD_LLM_TIMEOUT_S) \
		$(if $(LIMIT),--limit $(LIMIT)) \
		$(if $(MODEL),--model-profile $(MODEL)) \
		$(if $(CONCURRENCY),--concurrency $(CONCURRENCY)) \
		$(if $(JUDGE_FALLBACK),--judge-fallback)

mailguard-bench-test:
	$(MAILGUARD_UV) python -m pytest \
		tests/unit/test_mailguard_bench_guarded_reply.py \
		tests/integration/test_mailguard_bench_guarded_run.py -v

# AgentMailGuard benchmark (task 7.19): scoring and analyses make no model calls.
# `python -m` from the repo root keeps rag-email's `evaluation`/`services` ahead of the
# worktree's packages of the same name; --with-editable leaves pyproject.toml/uv.lock alone.
MAILGUARD_RUN_DIR = evaluation/results/mailguard_bench/$(RUN)
MAILGUARD_PY = $(MAILGUARD_UV) python

# LEGACY=1 re-reads a committed scheme v1/v2 folder (the Review 1 runs) from its committed outputs
# and prints its tables; it scores and writes nothing. Every other call scores a scheme v3 run and
# refuses an older one (plan_v3 Task 13).
mailguard-report:
	@test -n "$(RUN)" || { echo "FAIL set RUN=<run_id>" >&2; exit 1; }
	$(MAILGUARD_PY) -m evaluation.mailguard_bench.report $(if $(LEGACY),--legacy,--run-dir) $(MAILGUARD_RUN_DIR) $(if $(LEGACY),,--mailguard-dir $(MAILGUARD_DIR))

mailguard-analyses:
	@test -n "$(RUN)" || { echo "FAIL set RUN=<run_id>" >&2; exit 1; }
	$(MAILGUARD_PY) -m evaluation.mailguard_bench.report --run-dir $(MAILGUARD_RUN_DIR) --mailguard-dir $(MAILGUARD_DIR)
	@scheme=$$($(MAILGUARD_PY) -m evaluation.mailguard_bench.scheme --run-dir $(MAILGUARD_RUN_DIR)) || exit 1; \
	if [ "$$scheme" = v1 ] || [ "$$scheme" = v2 ]; then \
		echo "NOTE scheme $$scheme is read only by the legacy reader: make mailguard-report RUN=$(RUN) LEGACY=1"; \
	else \
		$(MAILGUARD_PY) -m evaluation.mailguard_bench.analyses --run-dir $(MAILGUARD_RUN_DIR) --mailguard-dir $(MAILGUARD_DIR) && \
		$(MAILGUARD_PY) -m evaluation.mailguard_bench.report --run-dir $(MAILGUARD_RUN_DIR) --mailguard-dir $(MAILGUARD_DIR); \
	fi

# The teammate benchmark kit (task 7.25; ADR-0012 decision 9): pure-Python modules under the same
# overlay as the mailguard-* targets. Make runs in a Linux shell (WSL2 Ubuntu on Windows); the
# modules themselves are pure Python and also run natively on Windows. CONCURRENCY stays an
# argument only (the `unexport CONCURRENCY` above). CONFIGS empty: the kit's own default list.
.PHONY: bench-doctor bench-setup bench-run bench-report bench-package bench-canary

bench-doctor:
	$(MAILGUARD_UV) python -m evaluation.mailguard_bench.kit.doctor $(if $(MODEL),--model-profile $(MODEL)) $(if $(READER),--reader $(READER))

bench-setup:
	$(MAILGUARD_UV) python -m evaluation.mailguard_bench.kit.campaign setup

bench-run:
	@test -n "$(MODEL)" -a -n "$(RUN)" || { echo "usage: make bench-run MODEL=<model profile, see evaluation/mailguard_bench/model_profiles.py> RUN=<id> [CONFIGS=C0,C0T,...] [LIMIT=n] [CASE_IDS=id,id,...] [CONCURRENCY=1|2] [DRY_RUN=1]  (RUN names the results folder and is never made up: a resume needs the same RUN, a smoke run or a CASE_IDS trial its own, trial-...)" >&2; exit 2; }
	@test -z "$(READER)" || { echo "FAIL READER= is not a bench-run option: the reader is served by LLM__* in the shell and bench-run refuses an exported LLM__*. Add the meaning column afterwards: LLM__PROVIDER=... LLM__OPENAI_BASE_URL=... make bench-report RUN=$(RUN) READER=<reader model>" >&2; exit 2; }
	$(MAILGUARD_UV) python -m evaluation.mailguard_bench.kit.campaign run \
		--model-profile $(MODEL) \
		--run $(RUN) \
		$(if $(CONFIGS),--configs $(CONFIGS)) \
		$(if $(LIMIT),--limit $(LIMIT)) \
		$(if $(CASE_IDS),--case-ids $(CASE_IDS)) \
		$(if $(CONCURRENCY),--concurrency $(CONCURRENCY)) \
		$(if $(filter 1 yes true,$(DRY_RUN)),--dry-run)

bench-report:
	@test -n "$(RUN)" || { echo "FAIL set RUN=<run_id>" >&2; exit 1; }
	$(MAILGUARD_UV) python -m evaluation.mailguard_bench.kit.campaign report --run $(RUN) $(if $(READER),--reader $(READER))

bench-package:
	@test -n "$(RUN)" || { echo "FAIL set RUN=<run_id>" >&2; exit 1; }
	$(MAILGUARD_UV) python -m evaluation.mailguard_bench.kit.campaign package --run $(RUN)

# ONE live, billed call (task 7.29): the OpenRouter route's canary, before each OpenRouter run. The
# module refuses a profile that is not routed; it reads the key from .env and never prints it.
bench-canary:
	@test -n "$(MODEL)" || { echo "usage: make bench-canary MODEL=qwen2.5-7b-openrouter|llama-3.1-8b-openrouter  (one live call, under a cent; not CI)" >&2; exit 2; }
	$(MAILGUARD_UV) python -m evaluation.mailguard_bench.openrouter_canary --model-profile $(MODEL)
