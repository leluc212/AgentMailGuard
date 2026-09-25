# WORKLOG — AgentMailGuard

Nhật ký công việc để tiếp tục làm ở các phiên sau. Cập nhật ở cuối mỗi phiên làm việc.
(Work log so the next session can pick up where this one stopped.)

Branch làm việc: `feature/mailguard-defense-stack` (tạo từ `origin/main`, repo
https://github.com/leluc212/AgentMailGuard). Core hệ thống RAG email nằm ở branch
`RAG_Email_System` (369 file, packages/ + services/); mailguard là subsystem cross-cutting,
tích hợp bằng duck-typing (không import core). Đọc `README.md` + `docs/` trước khi làm tiếp.

---

## Phiên 2026-09-26 (Claude Fable 5.1) — trạng thái cuối phiên

### Đã hoàn thành (tất cả đã commit + push lên branch)
- [x] `git init`, remote `origin`, branch mới `feature/mailguard-defense-stack`; 3 commit đã push.
- [x] 6 layer hoàn chỉnh + pipeline + config + prompt (xem `docs/architecture.md`):
      L1 `EmailInjectionScanner` (rules → ML → LLM judge), L2 `UserIntentExtractor`,
      L3 `ChannelIsolation` (spotlighting delimit/datamark/encode), L3b `RetrievedDocumentScanner`,
      L4 `OutputScanner`, L5 `PolicyEngine`; `MailGuardPipeline` với preset C0/C1/C2/C3 + `C3-Lx`.
- [x] Unit tests: 68 test pass (`python -m pytest tests/unit -q`), không cần LLM/mạng.
- [x] Dataset uy tín: registry + downloader (`mailguard/datasets/`), 12 nguồn đã tải về `datasets/raw/` (~380 MB,
      có `MANIFEST.json` sha256/license): deepset, jackhhao, xTRam1, Lakera Gandalf, TrustAIRLab ITW (CCS'24),
      Microsoft LLMail-Inject (raw phase-2 263 MB + nhãn), InjecAgent (ACL'24), BIPIA (KDD'25), PoisonedRAG (USENIX Sec'25),
      Enron ham, Bitext support + seed corpus tự viết.
- [x] Build dữ liệu: `build_l1_corpus` (24,382 dòng, 10 nguồn), `build_email_benchmark` (1,405 case: 862 attack /
      543 benign; email 694 + rag 168), `build_rag_poison` (2,883 chunk). Kiểm soát rò rỉ: hash-split train/bench.
- [x] Train L1 classifier (`training/train_l1_classifier.py`, 20 s CPU): **test P 0.966 / R 0.927 / F1 0.946 / AUROC 0.990 /
      FPR 2.0% / 0.9 ms**. Artifact `artifacts/models/l1_injection_clf_v1.joblib` (git-ignored, train lại 20 s) + `.metrics.json` (committed).
- [x] Đánh giá detector riêng (`evaluation/eval_detectors.py`): L1 rules+ML phát hiện 89.8% email tấn công @FPR 1.8%
      (LLMail 100%, BIPIA 86%, InjecAgent 75.5%; yếu ở paraphrase 0/10, quoted 1/5, role-play 5/10 → cần LLM judge);
      L3b chunk-level P 0.956 / R 0.853 / F1 0.902 (PoisonedRAG recall 85.1%). Kết quả: `evaluation/results/detectors/*.json`.
- [x] Harness benchmark (`evaluation/harness.py`, `run_benchmark.py`, `report.py`): agent `naive` (mô phỏng, offline) hoặc
      model thật; đo ASR/TMR/DER/TSR/FPR/latency + Wilson CI + McNemar; xuất Table V/VI (md + LaTeX) vào `paper/tables/`.
- [x] Training LLM judge: `training/build_sft_dataset.py`, `training/finetune_llm_judge.py` (LoRA/QLoRA), configs
      cho Qwen2.5-7B / Llama-3.1-8B / smoke 0.5B, `training/EXPORT_OLLAMA.md`.
- [x] Tích hợp: `mailguard/integration/adapters.py` (GuardedReplyAgent, decision_to_job_result, dispatch_allowed),
      `services/guard_worker/main.py` (stdin/AMQP), `scripts/scan_email.py` (demo CLI), `.github/workflows/ci.yml`.
- [x] Docs: README, `docs/architecture.md`, `docs/datasets.md`, `docs/experiments.md`, `paper/outline.md`,
      `paper/sections_draft.md` (văn bản nháp mục VI + bảng thành phần benchmark).
- [x] Rule bổ sung: `instruction-probing`, `instruction-override-vi` (tiếng Việt) trong `configs/injection_rules.yaml`.

### Kết quả benchmark với agent mô phỏng (naive-all, chỉ để kiểm chứng harness, KHÔNG phải kết quả model)
Xem `evaluation/results/naive-all/summary.json` và `paper/tables/*.md`. Bản chạy trước khi sửa harness:
C0 ASR 63.5% → C1 35.5% → C2 5.1% → C3 2.9% (email-vector 0%, RAG-vector còn sót do L3b chưa nhận query gốc — đã sửa:
pipeline dùng subject+body làm query, thêm containment cho query-echo, lọc PoisonedRAG có đáp án không kiểm chứng được).
Bản chạy lại sau sửa (1,405 case, agent naive): **C0 ASR 63.9% / TMR 39.7% / DER 40.3% → C1 35.4% → C2 0.0% → C3 0.0%**;
TSR 98.5–99.6%, FPR ≤ 0.2%, guard latency trung bình 4–18 ms. Ablation: chỉ bỏ L3 (channel isolation) làm ASR tăng
(email 0.7%, RAG 13.7%, McNemar p = 7.5e-9) vì agent naive tuân theo chunk độc mà L3b bỏ sót (23/150 PoisonedRAG);
các ablation khác = 0% vì agent naive bị "vô hiệu hóa" hoàn toàn bởi marker → cần model thật để thấy đóng góp của L1/L2/L4/L5.

### Việc còn lại (ưu tiên theo thứ tự)
1. **Chạy 3 model thật** (cần Ollama + OPENAI_API_KEY, xem `docs/experiments.md`):
   `python -m evaluation.run_benchmark --agent qwen2.5-7b-instruct --guard-model qwen2.5-7b-instruct --config all`
   (tương tự llama-3.1-8b-instruct, gpt-4o-mini) rồi `python -m evaluation.report ... --out paper/tables` → điền Table V/VI.
2. Fine-tune judge (tùy chọn, GPU 16 GB): `build_sft_dataset` → `finetune_llm_judge` → export Ollama → chạy lại benchmark.
3. Bổ sung baseline L1 (Prompt Guard 2 / ProtectAI DeBERTa) trong `evaluation/baselines.py` (chưa viết).
4. Đánh giá adaptive attack (Zhan et al. NAACL'25) và human review 100 draft cho TSR.
5. Điền số vào `paper/KLTN.pdf` (Table V, VI, abstract), thay `[Student N]`.

### Môi trường máy này
- Python 3.13, torch 2.10 (CPU), transformers 5.3, sklearn 1.8, pytest 9, pyarrow đã cài.
- GPU: RTX 3050 Laptop 4 GB → KHÔNG đủ fine-tune 7B/8B (dùng Colab/Kaggle T4). Ollama chưa cài; OPENAI_API_KEY chưa set.
- Lưu ý tool: heredoc trong Bash bị lỗi với backslash → dùng Write/Edit cho file có regex.

### Cách chạy nhanh
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

### Việc cần người dùng chuẩn bị
1. Cài Ollama, `ollama pull qwen2.5:7b-instruct` và `ollama pull llama3.1:8b-instruct-q4_K_M` (16 GB RAM chạy được Q4).
2. Tạo `.env` từ `.env.example`, đặt `OPENAI_API_KEY` và `GUARD_MODELS__*`.
3. Chọn nơi fine-tune (Colab/Kaggle) nếu muốn hàng "fine-tuned judge" trong paper.
