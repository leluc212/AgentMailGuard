> **v2 results (scheme v2, pre ADR-0015):** naive simulator on the six-layer guard. The configs `C3-L3b` and `C3-L5` no longer exist and `C3-L3` meant channel isolation here (in v3 it removes the Inbound Gate); do not compare with v3. Regenerated from the live v3 run (plan_v3 Task 16).

**naive**

| Ablation | ASR Email↓ | ASR RAG↓ | p (McNemar vs C3) |
|---|---|---|---|
| C3 Full (baseline for ablation) | 0.0% | 0.0% | - |
| C3 $-$ Layer 1 (no Email Scanner) | 0.0% | 0.0% | 1 |
| C3 $-$ Layer 2 (no Intent Extract) | 0.0% | 0.0% | 1 |
| C3 $-$ Layer 3 (no Channel Iso.) | 0.7% | 13.7% | 7.45e-09 |
| C3 $-$ Layer 3b (no Doc Scanner) | 0.0% | 0.0% | 1 |
| C3 $-$ Layer 4 (no Output Scan) | 0.0% | 0.0% | 1 |
| C3 $-$ Layer 5 (no Policy Engine) | 0.0% | 0.0% | 1 |

