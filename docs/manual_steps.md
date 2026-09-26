# Các bước cần làm tay (checklist cho người dùng)

Mọi thứ tự động được đã chạy trong repo. Những việc dưới đây cần tài khoản, máy có GPU hoặc cài đặt phần mềm,
nên phải làm tay. Làm xong bước nào thì Claude (hoặc bạn) chạy lệnh tương ứng ở cột "Sau đó chạy".

## A. Chạy GPT-4o-mini (nhanh nhất, ~6–8 USD cho toàn bộ lưới, 2–3 giờ)

| Bước | Làm gì | Sau đó chạy |
|---|---|---|
| A1 | Tạo API key tại https://platform.openai.com/api-keys | — |
| A2 | Copy `.env.example` → `.env`, điền `OPENAI_API_KEY=sk-...`. KHÔNG commit `.env` (đã git-ignore). | `python -m evaluation.eval_llm_judge --model gpt-4o-mini --set corpus-test bench-emails --limit 600 --concurrency 4` (F1 của judge, ~10 phút, <1 USD) |
| A3 | Chạy benchmark pilot rồi full | `python -m evaluation.run_benchmark --agent gpt-4o-mini --guard-model gpt-4o-mini --config C0 C1 C2 C3 --limit 300 --concurrency 4 --run-name gpt-pilot` rồi `... --config all --run-name gpt-all` |
| A4 | Xuất bảng | `python -m evaluation.report evaluation/results/gpt-all --out paper/tables` |

## B. Chạy Qwen2.5-7B và Llama-3.1-8B local bằng Ollama

Máy hiện tại: RTX 3050 4 GB + 16 GB RAM → chạy được Q4 nhưng chậm (~5–8 token/s, ~1 phút/case).
Khuyến nghị: pilot 200–300 case trên laptop qua đêm, hoặc chạy full trên máy có GPU ≥ 12 GB.

| Bước | Làm gì | Sau đó chạy |
|---|---|---|
| B1 | Cài Ollama: mở PowerShell → `winget install Ollama.Ollama` (hoặc tải từ https://ollama.com/download). Mở lại terminal, kiểm tra `ollama --version`. | — |
| B2 | Tải model (≈4.7 GB + 4.9 GB): `ollama pull qwen2.5:7b-instruct` và `ollama pull llama3.1:8b-instruct-q4_K_M` | `ollama list` |
| B3 | Kiểm tra kết nối | `python -c "from mailguard.llm.ollama_provider import ollama_tags_available as f; print(f())"` |
| B4 | Đo F1 judge (mỗi model ~1–2 giờ trên laptop) | `python -m evaluation.eval_llm_judge --model qwen2.5-7b-instruct --set bench-emails --limit 400 --concurrency 1` (tương tự `llama-3.1-8b-instruct`) |
| B5 | Benchmark pilot (qua đêm) | `python -m evaluation.run_benchmark --agent qwen2.5-7b-instruct --guard-model qwen2.5-7b-instruct --config C0 C1 C2 C3 --limit 250 --run-name qwen-pilot` |
| B6 | Full lưới (máy GPU) | `--config all --run-name qwen-all` / `llama-all`, rồi `python -m evaluation.report evaluation/results/qwen-all evaluation/results/llama-all evaluation/results/gpt-all --out paper/tables` |

## C. Fine-tune judge bằng Unsloth trên Colab/Kaggle (T4 16 GB miễn phí)

| Bước | Làm gì |
|---|---|
| C1 | Mở https://colab.research.google.com → File → Upload notebook → chọn `training/colab_unsloth_finetune.ipynb`. Runtime → Change runtime type → **T4 GPU**. |
| C2 | (Chỉ cho Llama) Vào https://huggingface.co/meta-llama/Llama-3.1-8B-Instruct bấm "Agree", tạo token tại https://huggingface.co/settings/tokens, dán vào ô `HF_TOKEN` của cell 3. Qwen không cần token. |
| C3 | Chạy các cell từ trên xuống. Cell 5: đặt `MAX_STEPS = 60` để thử nhanh (~10 phút), `0` để train đủ 1 epoch (~2 giờ). Cell 6 in ra P/R/F1 của judge sau fine-tune. |
| C4 | Cell 7 lưu adapter + file GGUF `q4_k_m` + `Modelfile` vào Google Drive (`MyDrive/AgentMailGuard/…`). Tải thư mục `gguf` về máy. |
| C5 | Trên máy: `cd <thư mục gguf>` → `ollama create mailguard-qwen2.5-7b:v1 -f Modelfile`. Thêm vào `configs/models.yaml`: `mailguard-qwen2.5-7b: {backend: ollama, tag: mailguard-qwen2.5-7b:v1, num_ctx: 8192}`, đặt `GUARD_MODELS__JUDGE=mailguard-qwen2.5-7b` trong `.env`. |
| C6 | Chạy lại B4/B5 với `--guard-model mailguard-qwen2.5-7b` để có hàng "fine-tuned judge" trong paper. |

Ghi chú: `training/finetune_llm_judge.py` (HF transformers + peft, không Unsloth) làm cùng việc nếu có máy GPU riêng;
Unsloth nhanh hơn ~2× và ít VRAM hơn nên khuyên dùng notebook trên Colab.

## D. Baseline Prompt Guard 2 (tùy chọn)

Model `meta-llama/Llama-Prompt-Guard-2-86M` bị gated: bấm "Agree" trên Hugging Face, rồi
`python -m evaluation.baselines --model promptguard2 --hf-token <token>`. ProtectAI DeBERTa không cần token (đã chạy).

## E. Sau khi có kết quả

1. `python -m evaluation.report <các thư mục results> --out paper/tables` → copy `table_v_security_utility.tex`,
   `table_vi_ablation.tex` vào `paper/KLTN` (Table V, VI).
2. Điền số F1 judge (`evaluation/results/llm_judge/*.json`) và baseline (`evaluation/results/baselines/*.json`) vào mục VII.
3. Commit các file `summary.json`, `paper/tables/*` (đã cho phép trong `.gitignore`); KHÔNG commit `.env`, model, dữ liệu raw.
