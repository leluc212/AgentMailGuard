# Enterprise RAG-Based Intelligent Email Management and Response System (`rag-email`)

Specification-driven enterprise email automation and selective RAG pipeline.

## Architectural Rule
`Classify first. Retrieve only when required. Generate only when necessary.`

## Directory Structure
- `services/`: Deployable microservices and workers
- `packages/`: Modular shared libraries (`packages/domain` stdlib + core only)
- `specs/`: Authoritative contracts (`requirements.md`, `design.md`, `tasks.md`)
- `docs/`: Configuration, ADRs, and runbooks
- `migrations/`: Versioned database migrations
- `tests/`: Unit, integration, and end-to-end test suites
- `evaluation/`: Benchmark datasets and evaluation harnesses
