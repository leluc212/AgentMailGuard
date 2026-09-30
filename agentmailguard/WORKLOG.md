# WORKLOG — AgentMailGuard

> **ĐỌC FILE NÀY TRƯỚC KHI LÀM TIẾP.** Mọi thứ đã làm, đang dở, và bước kế tiếp đều ở đây.
> (Read this first when resuming. Everything done, in progress, and next steps are here.)

- Repo: https://github.com/leluc212/AgentMailGuard
- Branch làm việc: **`feature/mailguard-defense-stack`** (tạo từ `origin/main`, KHÔNG push vào `main`).
  Core hệ thống RAG email nằm ở branch `RAG_Email_System` (packages/, services/); mailguard tích hợp
  bằng duck-typing, không import core.
- Máy đang dùng: Windows 11, Python 3.13, RTX 3050 4 GB (không fine-tune 7B/8B được), chưa cài Ollama,
  chưa có OPENAI_API_KEY. Bash tool bị lỗi backslash trong heredoc → dùng Write/Edit cho file có regex.

---

## TRẠNG THÁI HIỆN TẠI (cuối phiên 2026-09-26)

### Đã hoàn thành và đã push (4 commit trên branch)
| # | Commit | Nội dung |
|---|---|---|
| 1 | `5a9bd2a` | 6 layer (L1–L5 + L3b), pipeline, config, prompt, 61 unit test, registry dataset |
| 2 | `878ba60` | Build dataset/benchmark, train L1 classifier, harness đánh giá, training LoRA, docs, worker, CI |
| 3 | `a89da87` | Sửa harness (query L3b, query-echo, goal check), kết quả naive-all, bảng Table V/VI, paper drafts |
| 4 | (phiên này) | `evaluation/baselines.py` + WORKLOG cuối phiên |

### Kiến trúc & code (xem `README.md`, `docs/architecture.md`)
- `mailguard/layers/`: L1 `EmailInjectionScanner` (rules → TF-IDF/LR → LLM judge), L2 `UserIntentExtractor`,
  L3 `ChannelIsolation` (spotlighting), L3b `RetrievedDocumentScanner`, L4 `OutputScanner`, L5 `PolicyEngine`.
- `mailguard/pipeline.py`: `MailGuardPipeline` + `GuardConfig.preset("C0"|"C1"|"C2"|"C3"|"C3-L1"…"C3-L5")`.
- Config: `configs/{injection_rules,channels,pii_patterns,policy,models}.yaml`; prompt: `mailguard/prompts/*.txt`.
- Tests: `python -m pytest tests/unit -q` → **68 pass**, không cần LLM/mạng.
- Tích hợp: `mailguard/integration/adapters.py`, `services/guard_worker/main.py`, `scripts/scan_email.py`.

### Dữ liệu (nguồn uy tín, xem `docs/datasets.md`, `mailguard/datasets/sources.py`)
- Đã tải về `datasets/raw/` (git-ignored, ~380 MB, có `MANIFEST.json`): deepset, jackhhao, xTRam1, Lakera Gandalf,
  TrustAIRLab ITW-jailbreak (CCS'24), Microsoft LLMail-Inject (raw phase-2 + nhãn), InjecAgent (ACL'24),
  BIPIA (KDD'25), PoisonedRAG (USENIX Sec'25), Enron ham, Bitext support. Tải lại: `python -m mailguard.datasets.download --all --max-mb 400`.
- Đã build (git-ignored trừ `stats.json`): `datasets/processed/l1_injection/` (24,382 dòng), `email_bench/cases.jsonl`
  (1,405 case: 862 attack [email 694 / rag 168] + 543 benign), `rag_poison/chunks.jsonl` (2,883 chunk).
- Kiểm soát rò rỉ train/bench: hash-split (`bench_side`), seed template chỉ dùng cho benchmark.

### Kết quả đã có (tất cả tái tạo được bằng lệnh ở mục "Cách chạy")
- **L1 classifier** (`artifacts/models/l1_injection_clf_v1.metrics.json`, train 20 s CPU):
  test P 0.966 / R 0.927 / **F1 0.946** / AUROC 0.990 / FPR 2.0% / 0.9 ms.
- **Detector riêng** (`evaluation/results/detectors/{l1,l3b}.json`): L1 rules+ML phát hiện **89.8%** email tấn công
  @FPR 1.8% (LLMail 100%, BIPIA 86%, InjecAgent 75.5%; yếu: paraphrase 0/10, quoted 1/5, role-play 5/10 → cần LLM judge);
  L3b chunk-level P 0.956 / R 0.853 / F1 0.902 (PoisonedRAG recall 85.1%).
- **Benchmark với agent mô phỏng `naive`** (`evaluation/results/naive-all/summary.json`, `paper/tables/*`) — CHỈ để
  kiểm chứng harness, KHÔNG phải kết quả model: C0 ASR 63.9% → C1 35.4% → C2 0.0% → C3 0.0%; TSR 98.5–99.6%;
  FPR ≤ 0.2%; latency guard 4–18 ms. Ablation: bỏ L3 → ASR RAG 13.7% (McNemar p = 7.5e-9); các ablation khác 0%
  vì agent naive bị marker vô hiệu hóa hoàn toàn → cần model thật mới thấy đóng góp L1/L2/L4/L5.

### Đang dở / chưa chạy
- `evaluation/baselines.py` đã viết (so sánh L1 với ProtectAI DeBERTa-v3, Prompt Guard 2) nhưng **chưa chạy xong**
  (job bị dừng khi kết thúc phiên). Chạy: `python -m evaluation.baselines --model mailguard-l1 mailguard-l1r protectai`
  (tải model ~700 MB, CPU ~5–10 phút) → `evaluation/results/baselines/*.json`.

---

## BƯỚC KẾ TIẾP (theo thứ tự ưu tiên)
1. **Chạy 3 model thật** để điền Table V/VI của paper (`docs/experiments.md`):
   - Cài Ollama; `ollama pull qwen2.5:7b-instruct`; `ollama pull llama3.1:8b-instruct-q4_K_M`; tạo `.env` từ `.env.example`
     (đặt `OPENAI_API_KEY`, `GUARD_MODELS__*`).
   - `python -m evaluation.run_benchmark --agent qwen2.5-7b-instruct --guard-model qwen2.5-7b-instruct --config all --run-name qwen-all`
     (tương tự `llama-3.1-8b-instruct`, `gpt-4o-mini`; thử `--limit 300` trước để ước lượng thời gian/chi phí).
   - `python -m evaluation.report evaluation/results/qwen-all evaluation/results/llama-all evaluation/results/gpt-all --out paper/tables`.
2. Chạy `evaluation/baselines.py` (ở trên) → thêm bảng baseline L1 vào paper.
3. (Tùy chọn) Fine-tune judge: `python -m training.build_sft_dataset` → `python -m training.finetune_llm_judge --config training/configs/qwen2.5-7b_lora.yaml`
   trên Colab/Kaggle T4 → `training/EXPORT_OLLAMA.md` → chạy lại benchmark với `--guard-model mailguard-qwen2.5-7b`.
4. Đánh giá adaptive attack (Zhan et al. NAACL'25) + human review 100 draft cho TSR.
5. Viết số vào `paper/KLTN.pdf` (dùng `paper/sections_draft.md`, `paper/outline.md`, `paper/tables/*.tex`), thay `[Student N]`.

## Cách chạy nhanh
```bash
python -m pytest tests/unit -q
python -m mailguard.datasets.download --all --max-mb 400
python -m mailguard.datasets.build_l1_corpus && python -m training.train_l1_classifier
python -m mailguard.datasets.build_email_benchmark && python -m mailguard.datasets.build_rag_poison
python -m evaluation.eval_detectors l1 && python -m evaluation.eval_detectors l3b
python -m evaluation.run_benchmark --agent naive --config all --run-name naive-all
python -m evaluation.report evaluation/results/naive-all --out paper/tables
python scripts/scan_email.py tests/fixtures/emails.json --pick attacks:0 --kb datasets/seed/support_kb -v
```

## Quy ước làm việc
- Commit nhỏ, tuần tự, push dần lên `feature/mailguard-defense-stack`; không push `main`.
- Số liệu trong paper chỉ lấy từ `evaluation/results/**`; run `naive` chỉ dùng để kiểm chứng harness.
- Cập nhật file này ở cuối mỗi phiên (mục "TRẠNG THÁI HIỆN TẠI" + "BƯỚC KẾ TIẾP").
