# Git Workflow & Commit Guidelines

**Spec Alignment:** `specs/requirements.md` · `specs/tasks.md` · `GEMINI.md`

---

## 1. Commit Message Structure

Every commit message must follow conventional commit formatting, cite the exact task number from `specs/tasks.md`, and cite all relevant requirement IDs from `specs/requirements.md`:

```text
feat(<scope>): <short summary> [task <X.Y>] [R<id>, R<id>]
```

**Examples:**
- `feat(triage): cascade orchestration [task 2.5] [R6.2, R6.7, R6.9, R6.11]`
- `test(retrieval): multi-tenant filtered HNSW under-fill assertions [task 3.3] [R10.10]`
- `fix(sync): advance checkpoint only post-commit [task 1.2] [R2.8]`

---

## 2. Granularity & Task Isolation

- **One Task per Commit:** Implement and commit one task at a time. Never bundle multiple tasks or entire phases into single commits.
- **Atomic Changes:** Only stage changes directly relevant to the cited task and requirement IDs.
- **Never Skip Ahead:** Respect dependency order in `specs/tasks.md`.

---

## 3. Pre-Commit Verification Gate

Before staging and committing code:
1. Ensure all targeted unit and integration tests pass.
2. Ensure linters and type-checkers (`ruff`, `mypy`) pass without errors.
3. Verify that zero secrets, live provider API keys, or credentials are committed. Use references (`mailbox.credentials_ref`).
