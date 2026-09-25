# WORKLOG — AgentMailGuard

Nhật ký công việc để tiếp tục làm ở các phiên sau. Cập nhật ở cuối mỗi phiên làm việc.
(Work log so the next session can pick up where this one stopped.)

Branch làm việc: `feature/mailguard-defense-stack` (tạo từ `origin/main`, repo
https://github.com/leluc212/AgentMailGuard). Core hệ thống RAG email nằm ở branch
`RAG_Email_System` (369 file, packages/ + services/); mailguard là subsystem cross-cutting,
tích hợp bằng duck-typing (không import core).

---

## Phiên 2026-09-26 (Claude Fable 5.1)

### Đã làm
- [x] Khảo sát folder: đã có contracts, settings, LLM providers (fake/ollama/openai), L1 rules + classifier.
- [x] `git init`, remote `origin`, branch mới `feature/mailguard-defense-stack` (base = origin/main).
- [x] Viết config còn thiếu: `configs/channels.yaml` (L3), `configs/pii_patterns.yaml` (L4), `configs/policy.yaml` (L5).
- [x] Prompt templates: `mailguard/prompts/{l1_judge,l2_extractor,l3b_doc_judge,l4_output_judge}.v1.txt`.
- [x] L1 hoàn thiện: `llm_judge.py` (judge có schema), `scanner.py` (cascade rules → ML → LLM, noisy-OR fusion, indicators).
- [x] L2 `UserIntentExtractor`: segment/strip theo rules+ML, heuristic entities/actions, LLM paraphrase (tùy chọn).
- [x] L3 `ChannelIsolation`: spotlighting delimit/datamark/encode, nonce markers, scrub forged markers + chat-template tokens,
      token budget, `GuardedLLMProvider`, chế độ `enabled=False` cho baseline C0/C1.
- [x] L3b `RetrievedDocumentScanner`: rules + heuristics (answer-forcing, ignore-other-docs, query-echo) + ML + LLM (tùy chọn), quarantine.
- [x] L4 `OutputScanner`: PII/secret redaction (Luhn), system-prompt leak (n-gram), citation check, injected-goal compliance,
      unsafe action, external link, LLM judge (tùy chọn).
- [x] L5 `PolicyEngine`: rule YAML theo priority, facts từ GuardReport, audit JSONL idempotent.
- [x] `mailguard/pipeline.py`: `GuardConfig` presets C0/C1/C2/C3 + ablation `C3-Lx`; `MailGuardPipeline.run()` end-to-end.
- [x] Unit tests: `tests/unit/test_{contracts,l1_scanner,l2_extractor,l3_isolation,l3b_doc_scanner,l4_output_scanner,l5_policy,pipeline}.py`
      → 61 tests pass (`python -m pytest tests/unit -q`).
- [x] Dataset registry nguồn uy tín `mailguard/datasets/sources.py` + downloader `mailguard/datasets/download.py`
      (deepset, jackhhao, xTRam1, Lakera Gandalf, TrustAIRLab ITW-jailbreak (CCS'24), Microsoft LLMail-Inject,
      InjecAgent (ACL'24), BIPIA (KDD'25), PoisonedRAG (USENIX Sec'25), Enron ham, Bitext support).
- [x] Seed corpus: `datasets/seed/support_kb/*.md`, `benign_emails.jsonl` (40), `attack_templates.yaml` (24), `poison_templates.yaml` (6).
- [x] `evaluation/attacks/taxonomy.py`, `evaluation/metrics.py` (ASR/TMR/DER/TSR/FPR + Wilson CI + McNemar),
      `evaluation/harness.py` (NaiveSimulatedAgent + LLMAgent + goal checker).
- [x] `training/train_l1_classifier.py`.

### Đang làm / còn dở
- [ ] `mailguard/datasets/build_l1_corpus.py`, `build_email_benchmark.py`, `build_rag_poison.py` (phụ thuộc format file raw đã tải).
- [ ] `evaluation/run_benchmark.py`, `evaluation/report.py` (bảng LaTeX cho Table V/VI).
- [ ] Train L1 classifier trên corpus thật, commit metrics.
- [ ] `training/finetune_llm_judge.py` (LoRA/QLoRA cho Qwen2.5-7B / Llama-3.1-8B) + `training/configs/*.yaml`.
- [ ] `services/guard_worker` (RabbitMQ consumer) + `mailguard/integration` adapters cho core.
- [ ] README.md, docs/architecture.md, docs/datasets.md, docs/experiments.md, paper/outline.md.

### Môi trường máy này
- Python 3.13, torch 2.10 (CPU), transformers 5.3, sklearn 1.8, pytest 9.
- GPU: RTX 3050 Laptop 4 GB VRAM → KHÔNG đủ để fine-tune 7B/8B (cần Colab/Kaggle T4-16GB/A100 hoặc thuê GPU).
- Ollama chưa cài; OPENAI_API_KEY chưa set → LLM stages hiện chạy ở chế độ "cheap only" (rules+ML), tests dùng FakeLLMProvider.

### Cách chạy nhanh
```bash
python -m pytest tests/unit -q                                  # unit tests
python -m mailguard.datasets.download --all --max-mb 120        # tải dataset (datasets/raw/)
python -m mailguard.datasets.download --manifest                # bảng provenance
python -m training.train_l1_classifier --seed-only              # model smoke từ seed
```

### Việc cần người dùng quyết định / chuẩn bị
1. Cài Ollama + `ollama pull qwen2.5:7b-instruct` và `ollama pull llama3.1:8b-instruct-q4_K_M` (RAM 16GB đủ chạy Q4).
2. Đặt `OPENAI_API_KEY` trong `.env` để chạy GPT-4o-mini.
3. Fine-tune 7B/8B: dùng Colab/Kaggle (script sẽ nằm ở `training/finetune_llm_judge.py`).
