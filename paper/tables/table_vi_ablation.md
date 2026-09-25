**naive**

| Ablation | ASR Email↓ | ASR RAG↓ | p (McNemar vs C3) |
|---|---|---|---|
| C3 Full (baseline for ablation) | 0.0% | 14.9% | - |
| C3 $-$ Layer 1 (no Email Scanner) | 3.2% | 14.9% | 4.77e-07 |
| C3 $-$ Layer 2 (no Intent Extract) | 0.0% | 14.9% | 1 |
| C3 $-$ Layer 3 (no Channel Iso.) | 0.7% | 90.5% | 3.67e-40 |
| C3 $-$ Layer 3b (no Doc Scanner) | 0.0% | 14.9% | 1 |
| C3 $-$ Layer 4 (no Output Scan) | 2.7% | 14.9% | 3.81e-06 |
| C3 $-$ Layer 5 (no Policy Engine) | 4.6% | 14.9% | 4.66e-10 |

