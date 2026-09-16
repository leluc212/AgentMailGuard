FROM python:3.12-slim

WORKDIR /app

# Install curl for container health checks
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY packages/ packages/
COPY services/ services/

ENV PYTHONPATH=/app
ENV PORT=8000
ENV SERVICE_NAME=service

EXPOSE 8000

CMD ["python", "services/placeholder.py"]
