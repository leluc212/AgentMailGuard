# AgentMailGuard — Kế hoạch chạy thực nghiệm

## Nội dung thư mục

| File | Dùng để |
|---|---|
| `AgentMailGuard_LuotChay_TheoModel.xlsx` | Bảng kế hoạch: sheet **Tổng quan** + mỗi model một sheet |
| `csv/LuotChay_<model>.csv` | Cùng nội dung, mỗi model một file CSV (UTF-8, mở bằng Excel được) |
| `configs/*.yaml` | Cờ bật/tắt từng lớp cho mỗi cấu hình (C0–C3 và 6 cấu hình ablation) |

> Tên khóa trong `configs/*.yaml` là đề xuất — đổi cho khớp tên cờ trong code của nhóm.

## Dataset

LLMail-Inject (Microsoft, 2025)
- Dữ liệu: https://huggingface.co/datasets/microsoft/llmail-inject-challenge
- Bài báo: https://arxiv.org/abs/2506.09956

## Tập mẫu chung (cả 3 model dùng đúng tập này)

| Phần | Số mẫu |
|---|---|
| Attack email (250 × 4 level) | 1.000 |
| Tài liệu bị đầu độc (RAG) | 300 |
| Email lành tính | 500 |
| **Tổng mỗi model** | **1.800** |

Chọn một lần, khử trùng lặp, chia theo `team_id`, seed cố định → lưu ID ra `eval_set_v1.jsonl`.

## Mỗi lượt chạy là gì?

Mỗi lượt = **cả pipeline chạy một lần** trên một mẫu, với một bộ lớp được bật.
Không có lượt nào "chạy riêng một layer".

| Cấu hình | Lớp bật | Chạy cho |
|---|---|---|
| C0 | không lớp nào | attack, RAG, lành tính |
| C1 | L1 | attack |
| C2 | L1 + L2 + L3 | attack |
| C3 | đủ 5 lớp | attack, RAG, lành tính |
| C3 − Lx (6 cấu hình) | đủ 5 lớp, tắt đúng một lớp | 500 attack, **chỉ Qwen** |

Ablation tắt từng lớp (không chạy riêng từng lớp) để đo đóng góp của mỗi lớp khi đã có các lớp khác.

## Số lượt chạy

| Model | Thứ tự | Lượt |
|---|---|---|
| Qwen2.5-7B-Instruct (Ollama) | 1 | 5.600 + 3.000 ablation = **8.600** |
| GPT-4o-mini (OpenAI API) | 2 | **5.600** |
| Llama-3.1-8B-Instruct (Ollama) | 3 | **5.600** (sau khi sửa template guard) |
| **Tổng** | | **19.800** |

5.600 = 1.000 × 4 cấu hình + 300 × 2 + 500 × 2.

## Giữ cố định giữa 3 model

Cùng file mẫu · cùng prompt/template guard · cùng ngưỡng các lớp (chỉnh trên tập TRAIN, đóng băng) · temperature = 0 · cùng luật chấm (luật chuỗi chính thức của LLMail-Inject + đọc tay) · báo cáo khoảng tin cậy 95% và McNemar cho C0 vs C3.
