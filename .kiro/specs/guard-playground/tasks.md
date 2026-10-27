# Implementation Plan: Guard Playground

## Overview

Thứ tự theo `requirements.md` (MUST trước, SHOULD sau). Mọi code mới nằm trong `services/guard_playground/`, `scripts/playground/`, `tests/unit/test_guard_playground_*.py`. Không sửa `agentmailguard/`, `Dockerfile`, `.dockerignore`. Mọi test không gọi mạng, không cần khóa API. UI tiếng Anh.

## Tasks

- [x] 1. Khung service, settings, schema và cô lập khỏi guard
  - [x] 1.1 Tạo khung `services/guard_playground` và health
    - `__init__.py`, `settings.py` (`PlaygroundSettings`, env prefix `PLAYGROUND__`, đường dẫn mặc định suy ra từ `repo_root`; khi chạy trên máy `repo_root` là thư mục repo), `main.py` với `create_app(settings=None, *, runner=None)` và biến `app`.
    - Tự thêm `<repo_root>/agentmailguard` vào `sys.path` nếu `mailguard` chưa import được; nếu vẫn không import được thì `/guard` báo "Guard library unavailable" còn `/healthz` vẫn trả 200.
    - Route `GET /healthz`, `GET /readyz` (guard import được, số case, số entry Replay).
    - Mount `/static` của Review_UI (`review.css`, `htmx.min.js`) ở dạng chỉ đọc và `/pg-static` cho `playground.css`, `playground.js`.
    - Thêm `hypothesis` ghim phiên bản vào nhóm dev của `pyproject.toml` và cập nhật `uv.lock` (D10).
    - Tạo `tests/unit/conftest` hoặc fixture chung cho Playground: chặn mạng (vá `httpx.AsyncClient.send` và `socket.socket.connect` tới địa chỉ không phải loopback), xóa biến môi trường khóa.
    - _Requirements: 9.1, 9.2, 9.8, 10.1, 11.1, 11.2_
  - [x] 1.2 `core/schema.py`: `RunResult` schema v1 (`extra="forbid"`) và các model con theo design; `final` tính từ `outbound or inbound`
    - _Requirements: 7.11, 5.2_
  - [x]* 1.3 Property test round-trip `RunResult` bằng hypothesis
    - **Property 1: Replay round-trip**
    - **Validates: Requirements 7.11**
  - [x] 1.4 `core/redact.py`: `secrets()`, `redact()`, `RedactingFilter` gắn vào root logger khi tạo app
    - _Requirements: 8.3, 8.4_
  - [x]* 1.5 Property test redact
    - **Property 7: Xóa khóa**
    - **Validates: Requirements 8.3, 8.4, 11.5**

- [x] 2. Dữ liệu case, policy text và chuyển GuardReport sang RunResult
  - [x] 2.1 `core/cases.py`: `load_cases` (cases.jsonl + `agentmailguard/tests/fixtures/emails.json`), ánh xạ Case_Kind/Case_Source, bỏ dòng hỏng và đếm, lỗi thiếu file; `filter_cases`; `demo_set`
    - _Requirements: 1.1, 1.2, 1.5, 1.6, 1.7_
  - [x]* 2.2 Property test lọc case
    - **Property 8: Lọc case**
    - **Validates: Requirements 1.2, 1.6**
  - [x] 2.3 `core/policy_text.py`: đọc `agentmailguard/configs/policy.yaml` một lần, `reason(rule_id)` hoặc "rule reason not found"
    - _Requirements: 5.3, 5.7_
  - [x] 2.4 `RunResult.from_guard(report, draft, bundle, *, request, settings, guard_models)` trong `core/schema.py` hoặc `core/runner.py`
    - Luôn đủ 3 dòng layer theo thứ tự pipeline; dòng không chạy có `not_run_reason` ("disabled by preset" hoặc "stopped by policy"); `judge_configured`; `error`/`fallback_reason` từ verdict và metadata `llm_fallback`.
    - Stripped segments, user intent, removed_ratio; chunks với lý do cách ly là finding điểm cao nhất của chunk; quarantine_ratio; inbound/outbound decision; `gate_passes`; isolation; draft sau L4 (None khi chặn ở cửa vào, không dùng placeholder); guarded prompt `bundle.messages[1].content`; thresholds đọc từ settings thật đã dùng.
    - Test với `MailGuardPipeline` thật, provider giả (`FakeLLMProvider`) và agent giả, cho C0, C3 bị chặn ở pass 1, C3 có chunk bị cách ly, C3 đi tới L4.
    - _Requirements: 4.1–4.10, 5.1–5.5, 15.5_

- [x] 3. View model: banner, stepper, layer, ngưỡng, highlight
  - [x] 3.1 `core/view_model.py`: `build_column`, Outcome_Banner theo bảng (6 kiểu, icon SVG khác hình, aria-label), rule reason, Deciding_Layer, cờ mở rộng section, score_line của L1/L2
    - _Requirements: 12.1–12.10, 15.7, 15.8, 5.6_
  - [x] 3.2 `stages(r)`: Pipeline_Stepper 7 stage theo thuật toán trong design, "0 chunks scanned"
    - _Requirements: 13.1–13.7, 13.10, 13.11_
  - [x]* 3.3 Property test banner và stepper
    - **Property 2: Banner khớp với quyết định**
    - **Property 3: Stepper nhất quán**
    - **Validates: Requirements 12.2, 12.5, 13.2–13.7, 13.10, 11.10**
  - [x] 3.4 Định dạng điểm số kèm ngưỡng ("0.91 / block ≥ 0.85, flag ≥ 0.50", "strip ≥", "quarantine ≥", "(no threshold)"), làm tròn 2 chữ số
    - _Requirements: 15.1–15.6, 4.1, 4.2_
  - [x] 3.5 `core/highlight.py`: `spans`, `render` (escape mọi chữ, gộp khoảng giao nhau, giữ khoảng chỉ chạm nhau, link tới finding, ghi chú "excerpt not located in the shown text"), back link và số lần xuất hiện
    - _Requirements: 14.1–14.3, 14.5, 14.6, 14.8, 14.9, 8.9_
  - [x]* 3.6 Property test highlight
    - **Property 4: Highlight giữ nguyên văn bản**
    - **Property 5: Highlight không chèn được thẻ**
    - **Property 6: Gộp khoảng tô sáng**
    - **Validates: Requirements 14.1, 14.5, 14.8, 8.9, 11.11**

- [x] 4. Replay_Store và trang `/guard` ở Replay_Mode
  - [x] 4.1 `core/replay.py`: nạp `data/replay/<case_id>__<preset>__<model_key>.json`, bỏ file sai schema và đánh dấu "replay unavailable", tra cứu theo case, preset, model; danh sách model có replay cho một cặp case/preset
    - _Requirements: 7.2, 7.11, 7.12, 7.13, 7.17_
  - [x] 4.2 Templates `base.html`, `playground.html`, `_case_list.html`, `_case_detail.html`, `_column.html`, `_stepper.html`, `_layers.html` và `playground.css`
    - Toolbar: công tắc Replay/Live (mặc định Replay), chọn model, so sánh C0 vs C3 hoặc single mode với 9 preset theo thứ tự; nhãn "REPLAY · generated YYYY-MM-DD HH:MM · <model>".
    - Rail trái: bộ lọc kind/source, số đếm, danh sách có dấu ★ Demo_Set, thông báo danh sách rỗng, cảnh báo dòng bị loại, lỗi thiếu file.
    - Email và chunk có Evidence_Highlight; chunk bị cách ly gạch ngang kèm nhãn "QUARANTINED"; cột kết quả theo thứ tự banner, stepper, quyết định inbound/final, draft ("Unguarded_Draft (C0)" khi so sánh với C0), các section `<details>`, guarded prompt.
    - Chữ thường ≥ 18 px, tiêu đề ≥ 24 px, tương phản AA, viền focus, 2 cột vừa 1280 × 720.
    - _Requirements: 1.1–1.4, 3.1, 3.2, 3.5, 3.6, 3.9, 4.5, 4.6, 5.1–5.6, 7.1, 7.3, 10.2–10.7, 12.1, 12.6, 12.7, 13.8, 13.9, 14.2–14.4, 14.7, 14.10_
  - [x] 4.3 Routes `GET /guard`, `GET /guard/cases`, `GET /guard/cases/{id}`, `POST /guard/run` ở Replay_Mode (không gọi model, nút chạy bị khóa và hiện "replay unavailable" khi thiếu entry hoặc khi là Custom_Email); kiểm tra hai Run dùng cùng email và chunk
    - _Requirements: 1.1–1.7, 3.3, 3.4, 3.7, 5.8, 7.2, 7.13, 2.6_
  - [x] 4.4 Test route ở Replay_Mode: danh sách và lọc case, chọn case, chạy so sánh từ một Replay entry dựng sẵn trong `tmp_path`, nhãn "REPLAY" và thời điểm sinh, entry sai schema hiện "replay unavailable", cases.jsonl thiếu, banner cho cả 5 action, stepper dừng ở pass 1 và tới L4, excerpt chứa HTML được escape, không có file nào dưới `evaluation/results/` bị đổi
    - _Requirements: 11.1, 11.3, 11.7, 11.8, 11.10, 11.11_

- [x] 5. Checkpoint: bản demo Replay
  - Chạy toàn bộ test của Playground và test Review_UI hiện có; tất cả phải qua, tổng thời gian dưới 60 giây. Hỏi người dùng nếu có vấn đề.

- [x] 6. An toàn, giới hạn đầu vào, cô lập và kế hoạch dự phòng
  - [x] 6.1 Kiểm tra giới hạn đầu vào (subject 500, body 20,000, tối đa 5 chunk, chunk 4,000, body rỗng): một thông báo liệt kê mọi giới hạn bị vi phạm, giữ nguyên dữ liệu trong form
    - _Requirements: 8.5, 8.6, 2.2_
  - [x]* 6.2 Property test giới hạn đầu vào
    - **Property 9: Giới hạn đầu vào**
    - **Validates: Requirements 8.5, 11.4**
  - [x] 6.3 Test an toàn: khóa giả không xuất hiện trong trang, RunResult, log; `audit=False` ở mọi lần dựng `MailGuardPipeline`; không file nào dưới `agentmailguard/` và `evaluation/results/` bị ghi; Review_UI chỉ thêm đúng một link "Guard"
    - Thêm một dòng nav "Guard" trỏ tới `http://127.0.0.1:3010/guard` vào `services/frontend/templates/base.html`.
    - _Requirements: 8.1, 8.3, 8.7, 8.8, 9.3–9.7, 11.5, 11.6, 11.7_
  - [x] 6.6 Đổi bảng màu Outcome_Banner theo D11 trong `playground.css` và kiểm tra tương phản ≥ 4.5:1 bằng test tính tỷ lệ tương phản
    - _Requirements: 12.4, 10.7_
  - [x] 6.4 Docker và Fallback_Launcher (`run_playground.ps1` mặc định Replay, `-Live` bật Live theo D13)
    - Thêm service `guard-playground` vào `docker-compose.yml` đúng như design (cùng image, mount chỉ đọc, `127.0.0.1:3010`, không `depends_on`).
    - `scripts/playground/run_playground.ps1 -ReplayOnly`: chạy uvicorn bằng `.venv` với `PYTHONPATH` gồm repo và `agentmailguard`, `PLAYGROUND__LIVE_ENABLED=false`, chờ `/readyz` rồi in URL `/guard`; khi Live tắt thì giao diện ghi "Live mode requires the Docker service".
    - _Requirements: 8.1, 9.9, 9.10, 16.4, 16.8, 16.9_
  - [x] 6.5 Playground_Docs `services/guard_playground/README.md`: cách chạy (Docker Replay/Live, Fallback_Launcher), không có đăng nhập và chỉ chạy trên 127.0.0.1, thứ tự dự phòng và điều kiện chuyển, quy định về video ghi màn hình, checklist trước demo
    - _Requirements: 8.2, 16.1–16.7_

- [x] 7. Live_Mode
  - [x] 7.1 `core/models.py`: 3 Draft_Model (gpt-4o-mini qua OpenAI; Qwen qua OpenRouter ghim Phala; Llama qua OpenRouter ghim CoreWeave bf16, routing chép đúng `evaluation/mailguard_bench/guard_models.yaml`), `credential_present`, `guard_providers` (5 judge trên cùng model, D9), `draft_agent` gửi messages của guard và đọc JSON reply
    - System instructions giống benchmark: `DEFAULT_ENTERPRISE_INSTRUCTIONS` cộng reply-format rules (`guarded_system_instructions`).
    - _Requirements: 7.14, 7.15, 7.16_
  - [x] 7.2 `core/runner.py`: `RunRequest`, `execute` (settings như `guard_factory.guard_settings`, `audit=False`, timeout 120 s, phân loại lỗi connection/auth/rate limit/provider error, redact), `RunRegistry` theo session (start, status, cancel), wrapper ghi stage đang chạy
    - _Requirements: 7.5, 7.7, 7.15, 8.4, 8.11, 9.4, 20.1–20.3_
  - [x] 7.3 Routes và UI Live: `POST /guard/run` ở Live trả khung tiến trình, `GET /guard/progress/{run_id}` polling mỗi 1 s, nhãn "LIVE · <model> (<route>)", khóa điều khiển khi đang chạy, nút "Show replay" khi Live lỗi hoặc quá giờ, khóa nút chạy khi thiếu khóa, hiện tên judge chưa cấu hình
    - _Requirements: 3.8, 7.4–7.8, 7.14, 7.16, 8.10, 20.1–20.8_
  - [x] 7.4 Test Live với runner giả: nhãn "LIVE" và tên model ở từng cột, verdict từng layer, lỗi API hiện loại lỗi và gợi ý Replay, quá 120 s hiện thông báo timeout, một cột lỗi cột kia vẫn hiện, Replay không polling
    - _Requirements: 11.3, 11.8, 3.7, 5.8, 20.8_
  - [x] 7.5 `scripts/playground/build_replay.py`: `--model {gpt-4o-mini|qwen|llama|all}`, `--cases`, `--presets`, `--list-candidates`; giữ nguyên entry cũ khi một cặp bị lỗi, in các cặp lỗi và thoát mã khác 0; ghi qua `redact`
    - _Requirements: 7.9, 7.10, 7.17_

- [x] 8. Custom_Email và External_Data
  - [x] 8.1 Form Custom_Email: ô Subject, ô body lớn, "Advanced" gập chứa 0–5 ô chunk có nút thêm/xóa, tự tách dòng `Subject:`, nút mẫu từ `data/samples.json` (nguyên văn case thật), bỏ chunk rỗng, giữ dữ liệu sau khi chạy, khóa chạy ở Replay kèm thông báo (D12)
    - _Requirements: 2.1–2.8_
  - [x] 8.2 `core/external.py` và routes upload/batch: đọc `.eml`, `.json`, `.jsonl` (5 MB, 50 email), loại từng email lỗi, 1 email thì điền vào form, nhiều email thì chạy lô tối đa 2 Run cùng lúc, đếm tiến độ, "Stop batch", bảng tổng hợp, chọn dòng để xem chi tiết, nhãn "External data", "Clear external data", chỉ giữ trong bộ nhớ
    - _Requirements: 21.1–21.14_
  - [x] 8.3 Test External_Data: mỗi định dạng, file bị từ chối, email bị loại, chạy lô với runner giả, không ghi ra đĩa
    - _Requirements: 21.1–21.14, 11.1_

- [x] 9. Benchmark_Record, KPI và Results_Page
  - [x] 9.1 `core/benchmark.py`: bảng 3 model × preset đang hiện, "no record", "record unreadable", tên thư mục kết quả, ẩn với Custom_Email và fixture, "no benchmark record" với External_Data
    - _Requirements: 6.1–6.8, 21.12_
  - [x] 9.2 `core/kpi.py` và `_kpi.html`: 3 thẻ (email DR C0→C3, RAG DR C0→C3, FPR ở C3), thứ tự gpt-4o-mini, Llama 3.1 8B, Qwen 2.5 7B, "n = scored/planned", làm tròn half away from zero, "not available"
    - _Requirements: 18.1–18.11_
  - [x] 9.3 `GET /guard/results` và `GET /guard/figures/{name}` (danh sách cho phép fig1..fig6, PNG và PDF), dòng provenance đọc từ manifest, "figure not available", nav qua lại giữa hai trang
    - _Requirements: 18.12–18.15_
  - [x] 9.4 Test Benchmark_Record, KPI (số liệu khớp `metrics.csv` thật, ví dụ email DR gpt-4o-mini 75.7% → 100.0%) và Results_Page
    - _Requirements: 6.1–6.8, 18.1–18.15_

- [x] 10. Demo_Tour và Presenter_Mode
  - [x] 10.1 `data/demo_set.json` và `data/demo_tour.json` chọn từ dữ liệu benchmark thật (case mà C0 bị tấn công thành công và C3 chặn, cộng benign và một false positive), kiểm tra định nghĩa tour khi tải trang
    - _Requirements: 17.1, 17.2, 17.8_
  - [x] 10.2 Thanh tour: "Start tour", "Step k of n", talking point, Next/Previous, phím N/P (bỏ qua khi đang gõ), "Exit tour", lý do không chạy được, hủy kết quả của bước cũ
    - _Requirements: 17.3–17.10_
  - [x] 10.3 Presenter_Mode: nút bật tắt có trạng thái aria, chữ ≥ 20/26 px, thu gọn guarded prompt, không đổi trạng thái khác, mặc định tắt khi tải trang
    - _Requirements: 19.1–19.8_
  - [x] 10.4 Test tour (định nghĩa hợp lệ và không hợp lệ) và Presenter_Mode (markup có toggle, mặc định tắt)
    - _Requirements: 17.1–17.10, 19.1_

- [x] 11. Checkpoint cuối
  - Chạy toàn bộ test (Playground, Review_UI, unit hiện có), ruff trên code mới, khởi động thử bằng Fallback_Launcher và gọi `/readyz`, `/guard`. Hỏi người dùng nếu có vấn đề.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2", "1.4", "2.1", "2.3"] },
    { "id": 2, "tasks": ["1.3", "1.5", "2.2", "2.4", "3.4", "3.5"] },
    { "id": 3, "tasks": ["3.1", "3.2", "3.6", "4.1"] },
    { "id": 4, "tasks": ["3.3", "4.2"] },
    { "id": 5, "tasks": ["4.3"] },
    { "id": 6, "tasks": ["4.4", "6.1"] },
    { "id": 7, "tasks": ["6.2", "6.3", "6.4", "7.1"] },
    { "id": 8, "tasks": ["6.5", "7.2"] },
    { "id": 9, "tasks": ["7.3", "7.5"] },
    { "id": 10, "tasks": ["7.4", "8.1"] },
    { "id": 11, "tasks": ["8.2", "9.1"] },
    { "id": 12, "tasks": ["8.3", "9.2"] },
    { "id": 13, "tasks": ["9.3"] },
    { "id": 14, "tasks": ["9.4", "10.1"] },
    { "id": 15, "tasks": ["10.2"] },
    { "id": 16, "tasks": ["10.3"] },
    { "id": 17, "tasks": ["10.4"] }
  ]
}
```

Các task sửa chung `playground.html`, `routes/runs.py` hoặc `main.py` được đặt ở các wave khác nhau để hai subagent không ghi đè lên nhau.

## Notes

- Task có `*` là property test, tùy chọn về thứ tự nhưng nên làm.
- Sinh Replay_Store thật (`build_replay.py --model all`) cần khóa API và credit OpenRouter, do người dùng chạy sau khi code xong.
