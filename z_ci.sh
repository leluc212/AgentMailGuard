#!/bin/sh
# The CI jobs of .github/workflows/ci.yml, run locally (integration uses the running compose stack).
apt-get update -qq >/dev/null 2>&1 && apt-get install -y -qq git make curl >/dev/null 2>&1
git config --global --add safe.directory '*'
uv sync --all-extras --dev -q 2>&1 | tail -3
echo "=== QUALITY: ruff format"; uv run ruff format --check . 2>&1 | grep -E 'would be|-->' | head
echo "=== QUALITY: ruff check"; uv run ruff check . --output-format concise 2>&1 | tail -8
echo "=== QUALITY: mypy"; uv run mypy packages services tests evaluation 2>&1 | tail -8
echo "=== QUALITY: openapi"; uv run python -m services.api.openapi --check 2>&1 | tail -5
echo "=== UNIT"; OPENAI_API_KEY= uv run pytest tests/unit -q -p no:cacheprovider 2>&1 | grep -E '^(FAILED|ERROR)|passed|failed' | tail -12
echo "=== GUARD UNIT"; make mailguard-unit 2>&1 | grep -E '^(FAILED|ERROR)|passed|failed|Error' | tail -12
echo "=== INTEGRATION"
export DATABASE__HOST=host.docker.internal DATABASE__PORT=5433 DATABASE__USER=postgres DATABASE__PASSWORD=postgres DATABASE__NAME=rag_email
export BROKER__HOST=host.docker.internal BROKER__PORT=5672 BROKER__USER=guest BROKER__PASSWORD=guest
export OBJECT_STORAGE__ENDPOINT=host.docker.internal:9010 STORAGE__ENDPOINT_URL=http://host.docker.internal:9010
uv run pytest tests/integration -q -p no:cacheprovider 2>&1 | grep -E '^(FAILED|ERROR)|passed|failed|^E ' | head -30
