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

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    SERVICE_NAME=service

EXPOSE 8000

# The frontend, ai-worker and dispatch-worker still run this stub until they are built
# (Phase 4.11+ / Phase 6); every real service overrides the command in docker-compose.yml.
CMD ["python", "services/placeholder.py"]
