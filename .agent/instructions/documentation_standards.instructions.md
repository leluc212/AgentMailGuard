# Documentation Standards

**Spec Alignment:** `specs/requirements.md` · `specs/design.md` · `GEMINI.md`

---

## 1. Documentation Storage Standard

- Store all project documentation as Markdown (`.md`) files in the `/docs` directory.
- Superpowers workflow artifacts (brainstorms, plans, reviews, finishes) belong exclusively under `artifacts/superpowers/`.

---

## 2. Architecture Decision Records (ADRs)

- When architectural divergence or significant decisions occur, record an ADR in `docs/adr/ADR-XXXX-<title>.md` (R24.6).
- Follow standard ADR format:
  - **Status:** Proposed, Accepted, Deprecated, Superseded.
  - **Context:** Problem statement and forces at play.
  - **Decision:** The chosen path and rationale.
  - **Consequences:** Positive, negative, and neutral trade-offs.
- Follow the Divergence Protocol (GEMINI.md §7): Never silently diverge from `specs/design.md`.

---

## 3. Configuration & Runbook Documentation

- **Config Parity:** Every newly introduced environment variable or configuration parameter must be documented immediately in:
  1. `.env.example`
  2. `docs/configuration.md` (purpose, default value, validation rules, constraints).
- **Runbooks:** Operational procedures, recovery playbooks, and disaster mitigation steps must be kept up to date in `docs/runbook.md`.

---

## 4. Visual Documentation (ASCII Diagrams)

- When explaining or showcasing architectures, data flows, state transitions, pipelines, or component boundaries, always include an ASCII diagram representing the physical mechanism.
- Diagrams must illustrate the actual runtime topology and data movement rather than decorative boxes.
