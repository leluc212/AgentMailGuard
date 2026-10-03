# AgentMailGuard — Kế hoạch chạy 9 cấu hình (C0, C0T, C1–C7)

## Cấu hình

| Cấu hình | Lớp bật | File | Đi qua |
|---|---|---|---|
| C0 | không lớp nào (prompt gốc của rag-email) | `configs/C0_baseline.yaml` | tấn công + lành tính + **tài liệu độc** |
| C0T | template prompt của guard, không lớp nào | `configs/C0T_guard_template.yaml` | tấn công + lành tính |
| C1 | L1 + L5 | `configs/C1_L1+L5.yaml` | tấn công + lành tính |
| C2 | L2 + L5 | `configs/C2_L2+L5.yaml` | tấn công + lành tính |
| C3 | L3 + L5 | `configs/C3_L3+L5.yaml` | tấn công + lành tính |
| C4 | L3b + L5 | `configs/C4_L3b+L5.yaml` | tấn công + lành tính + **tài liệu độc** |
| C5 | L4 + L5 | `configs/C5_L4+L5.yaml` | tấn công + lành tính |
| C6 | chỉ L5 (đối chứng) | `configs/C6_L5_only.yaml` | tấn công + lành tính |
| C7 | tất cả các lớp (mục tiêu) | `configs/C7_all_layers.yaml` | tấn công + lành tính + **tài liệu độc** |

Đọc kết quả:
- **C0 → C0T**: riêng template prompt thay đổi được bao nhiêu.
- **C1…C5 so với C6**: mỗi lớp thêm được gì so với chỉ có L5.
- **C7**: kết quả chính của bài báo.
- Email lành tính đi qua cả 9 cấu hình → biết **lớp nào hay chặn nhầm** (FPR theo lớp).

## Tập mẫu (chung cho cả 3 model) — xem `hinh/Chia_dataset.png`

1.000 email tấn công (250 × 4 level) · 500 email lành tính · 300 tài liệu độc = **1.800 mẫu**, lưu ở `eval_set_v1.jsonl`.
Dataset: LLMail-Inject — https://huggingface.co/datasets/microsoft/llmail-inject-challenge

## Số lượt chạy

| Phần | Mẫu | Cấu hình | Lượt / model |
|---|---|---|---|
| Tấn công + lành tính | 1.500 | 9 | 13.500 |
| Tài liệu độc | 300 | 3 (C0, C4, C7) | 900 |
| **Tổng mỗi model** | 1.800 | | **14.400** |

Qwen2.5-7B 14.400 · GPT-4o-mini 14.400 · Llama-3.1-8B 14.400 → **tổng 43.200 lượt**.
Chi tiết: `KeHoach_LuotChay_C0-C7.xlsx` (mỗi model một sheet) và `csv/`.

## Chỉ số

- Email tấn công: **ASR** (thành công cuối cùng), **TMR** (agent có *định* gửi tới kẻ tấn công trước khi L5 chặn), DER (rò rỉ dữ liệu)
- Email lành tính: **TSR** (trả lời đúng), **FPR** (bị chặn nhầm)
- Tài liệu độc: ASR đường RAG

⚠️ L5 có mặt từ C1 đến C7, nên ASR có thể đều ≈ 0 → **phải ghi TMR trước L5** (`log_pre_l5_tool_calls: true`) để so sánh được các lớp.

## Lưu ý

- Tên khóa trong YAML là đề xuất — đổi cho khớp cờ trong code. `seed: 42` là tạm.
- Giữ cố định giữa 3 model: cùng file mẫu, cùng template, cùng ngưỡng các lớp, temperature = 0.
- Tên cấu hình đổi so với slide cũ: "đủ lớp" giờ là **C7** → sửa slide kết quả và paper cho khớp.
