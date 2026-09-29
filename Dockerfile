# Runtime image for every Python service (api + workers), built by docker compose.
# This host has no buildx plugin, so compose uses the classic builder
# (image label com.docker.compose.image.builder=classic): do NOT use RUN --mount.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.19 /uv /uvx /bin/

# curl is required by every healthcheck in docker-compose.yml
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

ENV UV_COMPILE_BYTECODE=1 \
    UV_NO_CACHE=1 \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Layer 1: third-party dependencies exactly as pinned in uv.lock, no dev group.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

# Layer 1b: bake the cross-encoder reranker into the image (R11.1, R11.5). The ai-worker loads it
# from RETRIEVAL__RERANK_MODEL_DIR with local_files_only, so a container without network still
# reranks. The build runs the same CrossEncoder load the worker does, so the cache layout cannot
# drift from what it reads. It needs only the venv above and sits above the source COPYs, so a
# code change does not download the model again. RERANK_MODEL defaults to the settings default
# (RETRIEVAL__RERANK_MODEL); to bake another model, build with --build-arg RERANK_MODEL=<name>
# and run with RETRIEVAL__RERANK_MODEL=<name>.
ARG RERANK_MODEL=cross-encoder/ms-marco-MiniLM-L-6-v2
ENV RETRIEVAL__RERANK_MODEL_DIR=/app/.cache/reranker
RUN /app/.venv/bin/python -c "from sentence_transformers import CrossEncoder; CrossEncoder('${RERANK_MODEL}', cache_folder='${RETRIEVAL__RERANK_MODEL_DIR}')"

# Layer 2: application source plus every file production code loads at runtime.
# Config/prompt/schema/model paths resolve against CWD (/app); migrations resolve via
# packages/db/migrator.py __file__.parents[2] (/app), so keep the editable install.
COPY README.md ./
COPY packages/ packages/
COPY services/ services/
COPY config/ config/
COPY prompts/ prompts/
COPY schemas/ schemas/
COPY migrations/ migrations/
COPY artifacts/models/ artifacts/models/

# Layer 3: install the project itself (editable, into /app/.venv).
RUN uv sync --locked --no-dev

# Layer 4: bake the BPE encoding into the image. tiktoken otherwise downloads it on first use,
# with no timeout, in every fresh container: that delayed ai-worker readiness by ~56 s in the
# 4.13b live gate and would hang offline deployments.
ENV TIKTOKEN_CACHE_DIR=/app/.cache/tiktoken
RUN /app/.venv/bin/python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')"

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    SERVICE_NAME=service

EXPOSE 8000

# Every service sets its own command in docker-compose.yml; this health stub is only the
# image default.
CMD ["python", "services/placeholder.py"]
