# Roadmap & Phase Progression

**Spec Alignment:** `specs/tasks.md` · `specs/requirements.md` · `GEMINI.md`

---

## 1. Master Phase Map

The project is structured across 9 sequential phases (0–8). Work progresses strictly in numerical order:

| Phase | Theme | Deliverable |
|---|---|---|
| **0** | Foundation & infrastructure | `docker compose up` boots a healthy empty stack |
| **1** | Core mail pipeline | Real Gmail $\to$ internal database |
| **2** | Triage & queue architecture | Email $\to$ category queue, with early exit |
| **3** | Knowledge RAG | Email $\to$ relevant organizational knowledge |
| **4** | Context & generation | Email + thread + RAG $\to$ generated draft |
| **5** | Business data integration | Knowledge + live operational data $\to$ response |
| **6** | Mail dispatch & review UI | Real email $\to$ pipeline $\to$ reply in provider mailbox |
| **7** | Observability & evaluation | Quantitative evidence for H1–H5 |
| **8** | Hardening & scale | Burst absorption, recovery, migration path documented |

---

## 2. Working File Directives

- **Primary Working Queue:** `specs/tasks.md` is the authoritative source of work.
- **Sequential Execution:** Always take the next unchecked task in order. Do not skip tasks or phases.
- **Phase Gate Enforcement:** Every phase concludes with an explicit phase verification gate that must pass completely before work on the subsequent phase begins.
- **Task Status Markers:**
  - `[ ]` Not started
  - `[~]` In progress (requires explicit summary of remaining items)
  - `[x]` Done (meets full Definition of Done)
  - `[!]` Blocked (requires explanatory note and human resolution)
