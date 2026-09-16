# Task Completion Workflow & Definition of Done

**Spec Alignment:** `specs/requirements.md §0.3` · `specs/tasks.md` · `GEMINI.md` · `.agent/rules/superpowers.md`

---

## 1. Superpowers Execution Lifecycle

For all non-trivial tasks, follow the staged Superpowers lifecycle:

```
1. Brainstorm ──▶ 2. Plan Gate ──▶ 3. User Approval ──▶ 4. Execution ──▶ 5. TDD & Verify ──▶ 6. Review ──▶ 7. Finish
   (Scope/Risks)    (Steps/Files)     (/superpowers-    (Implement)     (Tests & Lint)    (Severity Pass)  (Mark [x])
                                      execute-plan)
```

1. **Brainstorm (`/superpowers-brainstorm`):** Identify goals, constraints, risks, options, and acceptance criteria.
2. **Plan Gate (`/superpowers-write-plan`):** Formulate step-by-step tasks citing exact file paths and verification commands. Write plan to `artifacts/superpowers/`.
3. **Approval Gate:** Wait for user approval before modifying code. Instruct user to run `/superpowers-execute-plan`.
4. **Implementation (`/superpowers-execute-plan`):** Execute plan step by step with continuous verification.
5. **Review (`/superpowers-review`):** Audit code for correctness, security, and edge cases, grouping findings by severity (`Blocker`, `Major`, `Minor`, `Nit`).
6. **Finish (`/superpowers-finish`):** Verify repository hygiene, run full regression checks, and persist finish summary.

---

## 2. Definition of Done (DoD)

A task in `specs/tasks.md` is considered complete and eligible for `[x]` ONLY when all 6 conditions are verified:

1. **Criteria Fulfillment:** Every cited requirement ID (`R...`) from `specs/requirements.md` is fully implemented.
2. **Automated Test Coverage:** Unit and integration tests prove cited criteria hold under normal and edge conditions.
3. **Stack Health:** `make up` boots the entire stack to a completely healthy state.
4. **Configuration Documentation:** All new config parameters exist in both `.env.example` and `docs/configuration.md`.
5. **Observability Emitted:** Structured JSON logs and Prometheus metrics specified in R21 are actively emitted.
6. **Regression-Free CI:** Linter, type check, and existing test suites pass cleanly.
