# TRACKING_UI — Theo dõi tiến độ Demo Console

Kế hoạch: [PLAN_UI.md](PLAN_UI.md).
Ký hiệu: `[ ]` chưa làm · `[~]` đang làm / làm dở (ghi phần còn lại) · `[x]` xong (đạt Definition of Done) · `[-]` bỏ.

**Trạng thái chung:** UI-0 xong, task 9.1 xong. Bước kế: task 9.2 (run options trên job).
**Nhánh:** `feat/demo-ui` (từ `refactor/guard-v4` @ `4957774`).

---

## 1. Quyết định

| # | Câu hỏi | Trả lời | Ngày |
|---|---|---|---|
| Q1 | Người xem / bối cảnh | Bảo vệ luận án | 2026-11-16 |
| Q2 | Phạm vi; guard vào đường live (B4)? | Cả hai, so sánh C0 ⇄ C3; đồng ý đưa guard vào luồng thật | 2026-11-16 |
| Q3 | Danh mục scenario | Theo đề xuất (S01–S14, G01–G06, email tự do) | 2026-11-16 |
| Q4 | Kiến trúc | B: React + Vite + TS + Tailwind + shadcn/ui | 2026-11-16 |
| Q5 | Review UI cũ | Giữ song song | 2026-11-16 |
| Q6 | Polling / SSE | Polling | 2026-11-16 |
| Q7 | Observability | Chart trong UI, số từ Prometheus qua API | 2026-11-16 |
| Q8 | Model khi demo | Có nút chuyển fake ⇄ live | 2026-11-16 |
| Q9 | Ngôn ngữ giao diện | Song ngữ Việt / Anh | 2026-11-16 |
| Q10 | Nhánh git | Nhánh mới `feat/demo-ui`, không worktree | 2026-11-16 |
| C1 | Cơ chế nút chuyển model | M1: theo từng job | 2026-11-16 |
| C2 | Embedding khi chuyển model | E2: `text-embedding-3-small` thật cho cả hai chế độ + profile offline dự phòng | 2026-11-16 |
| C3 | Model thật cho chế độ live | GPT (OpenAI) | 2026-11-16 |
| C3a | Model cụ thể | `gpt-4o-mini` cho mọi vai trò, kể cả 5 judge | 2026-11-16 |
| C4 | Benchmark Explorer lên P1 | Có | 2026-11-16 |
| C5 | Guard config live chỉ C0, C3 | Có | 2026-11-16 |
| C6 | Bước kế: viết ADR-0018 + specs (chỉ tài liệu) | Đồng ý | 2026-11-16 |
| C7 | Lời gọi judge của guard ngoài ngân sách job (ngoại lệ AGENTS.md §4) | Đếm riêng, chỉ job C3 | 2026-11-16 |
| C8 | Phase 9 làm trước gate Phase 7, 8 | Cho phép | 2026-11-16 |
| C9 | Nhóm requirement mới R25; cập nhật số đếm trong AGENTS.md | R25; đã sửa AGENTS.md và CLAUDE.md | 2026-11-16 |

---

## 2. Công việc

### UI-0 Quyết định & spec
- [x] UI-0.1 Khảo sát hiện trạng, viết PLAN_UI.md và TRACKING_UI.md
- [x] UI-0.2 Chốt Q1–Q10
- [x] UI-0.2a Tạo nhánh `feat/demo-ui`
- [x] UI-0.2b Chốt C1–C9
- [x] UI-0.3 ADR-0018 "Demo console" (Accepted) + cập nhật `specs/design.md` §5.9
- [x] UI-0.4 Thêm nhóm R25 (R25.1–R25.12) vào `specs/requirements.md`
- [x] UI-0.5 Thêm Phase 9 (task 9.0–9.15) vào `specs/tasks.md`; task 9.0 `[x]`
- [x] UI-0.6 Chủ dự án yêu cầu bắt đầu code (2026-11-16)

### UI-1 Backend demo
- [x] UI-1.0 Task 9.1 Step-data audit: thêm field vào payload (triage, gate, context_built, DRAFTED), bảng ở `docs/demo-step-data.md`, test `tests/unit/test_step_data_audit.py`
- [ ] UI-1.1 B2 Danh mục scenario (file config + `GET /v1/demo/scenarios`)
- [ ] UI-1.2 B1 Demo ingest qua `SyncOrchestrator` (+ `DEMO__ENABLED`)
- [ ] UI-1.3 B7 Nút chuyển model fake ⇄ live (theo C1, C2)
- [ ] UI-1.4 B6 Ép lỗi có kiểm soát cho fake LLM
- [ ] UI-1.5 B8 Chạy cùng email qua C0 và C3 (hai job)
- [ ] UI-1.6 Test integration ≥3 tenant, fake LLM

### UI-2 Khung UI + design system
- [ ] UI-2.1 Scaffold React + Vite + TS + Tailwind + shadcn/ui + service Docker + `make up` healthy
- [ ] UI-2.2 Design system: màu, typography, dark/light, component cơ bản
- [ ] UI-2.2a i18n Việt / Anh (`react-i18next`)
- [ ] UI-2.3 Layout, điều hướng, chọn tenant, nút chuyển model
- [ ] UI-2.4 API client (kiểu sinh từ OpenAPI)

### UI-3 Live Pipeline (V1)
- [ ] UI-3.1 Sơ đồ pipeline động
- [ ] UI-3.2 Inbox email đã chạy
- [ ] UI-3.3 Panel chi tiết từng bước (triage, retrieval, prompt, draft, token, chi phí, độ trễ)

### UI-4 Scenario Runner (V2)
- [ ] UI-4.1 Thẻ scenario + chạy 1 / chạy bộ
- [ ] UI-4.2 So kỳ vọng ⇄ kết quả (pass/fail)
- [ ] UI-4.3 Soạn email tự do
- [ ] UI-4.4 S01–S14 chạy được từ UI

### UI-5 Guard (B4 + V3)
- [ ] UI-5.1 Guard trong ai-worker theo setting
- [ ] UI-5.2 API trả verdict guard của job
- [ ] UI-5.3 Màn Guard Inspector (4 layer, trust từng chunk, prompt sau isolation)
- [ ] UI-5.4 So sánh C0 ⇄ C3
- [ ] UI-5.5 G01–G06 chạy được từ UI
- [ ] UI-5.6 `make mailguard-unit` xanh (benchmark không bị ảnh hưởng)

### UI-6 Review, Timeline, Knowledge (V4, V5, V7)
- [ ] UI-6.1 Review & Dispatch (UI cũ vẫn chạy song song)
- [ ] UI-6.2 Timeline + replay
- [ ] UI-6.3 Knowledge upload + trạng thái ingest

### UI-7 Observability + Benchmark (V6, V8)
- [ ] UI-7.1 B5 + chart funnel, độ trễ, queue, chi phí
- [ ] UI-7.2 Benchmark Explorer

### UI-8 Hoàn thiện
- [ ] UI-8.1 Test e2e (Playwright)
- [ ] UI-8.2 Kiểm tra visual 1440×900, dark/light
- [ ] UI-8.3 `docs/demo-ui.md`, `.env.example`, `docs/configuration.md`
- [ ] UI-8.4 Cập nhật demo script `docs/demo-runbook.md` §6
- [ ] UI-8.5 `make ci` xanh, chạy thử demo trọn vẹn

---

## 3. Nhật ký

| Ngày | Việc | Ghi chú |
|---|---|---|
| 2026-11-16 | Tạo PLAN_UI.md, TRACKING_UI.md | Khảo sát nhánh `refactor/guard-v4` @ `4957774`. Phát hiện: chưa có endpoint bơm email từ UI; guard chưa nằm trong đường chạy live (chỉ benchmark). |
| 2026-11-16 | Chốt Q1–Q10, tạo nhánh `feat/demo-ui`, cập nhật PLAN_UI.md | Còn C1–C6 cần xác nhận |
| 2026-11-16 | Chốt C1, C3–C6; commit kế hoạch | C2 chưa có đề xuất trong bản trước, đã bổ sung đề xuất E2 |
| 2026-11-16 | Soạn ADR-0018 (Proposed), R25, design §5.9, Phase 9 | Phát hiện thêm: ngân sách lời gọi LLM (C7), thứ tự phase (C8), image Docker chưa chứa `agentmailguard/`, task 8.10 (token Gmail dùng chung) nên adapter demo không được gửi mail thật |
| 2026-11-16 | Chủ dự án duyệt mọi đề xuất; ADR-0018 Accepted; task 9.0 xong; sửa số đếm trong AGENTS.md, CLAUDE.md | Bước kế: task 9.1, chờ xác nhận trước khi code |
| 2026-11-16 | Bắt đầu task 9.1 | Thêm field payload; chưa chạy được test trên máy này |
| 2026-11-16 | Tạm ngưng trên máy 1 (ổ C đầy khi `docker compose up --build`); push nhánh để làm tiếp trên máy khác | Task 9.1 chưa bắt đầu code. Ghi chú môi trường: chạy unit test trên Windows trực tiếp có 191 test fail sẵn (khác biệt đường dẫn/công cụ); trong container Linux (`uv:python3.12-bookworm-slim` + git + make) chỉ 1 test fail sẵn: `tests/unit/test_kit_docs.py::test_the_stated_run_times_follow_from_the_smoke_measurements` (1800 vs 550). Chưa có `.env` (copy từ `.env.example`). Build stack cần nhiều dung lượng, nên để trống ≥25 GB. Classifier L1 riêng tư không có trong git (ADR-0012) |
| 2026-11-16 | Xong task 9.1 | Sửa `test_one_event_records_the_retrieved_chunks_and_a_skipped_summary` theo 5 field điểm mới của chunk. Unit test trong container Linux: chỉ còn 1 test fail sẵn (`test_kit_docs.py`, 1800 vs 550). Ghi chú: shell của agent trên máy này chập chờn; `uv sync` có lúc lỗi tải `torch` do mạng, chạy lại là được |

---

## 4. Vướng mắc / cần hỏi

| # | Vấn đề | Trạng thái |
|---|---|---|
| I1 | Endpoint demo và guard-trong-ai-worker là thay đổi spec (AGENTS.md §7): cần ADR và chủ dự án duyệt | Xong: ADR-0018 Accepted |
| I2 | Nút chuyển model: env đọc lúc khởi động container nên không đổi được giữa chừng; embedding không được đổi theo (R5.10) | Xong: M1 (theo job) + E2 |
| I3 | `GuardedDraftingService` nằm trong `evaluation/`; service không nên import từ đó | ADR-0018 đề xuất: wrapper mới trong ai-worker + parity test, không sửa benchmark |
| I4 | Image Docker loại `agentmailguard/` (`.dockerignore`); classifier L1 là file riêng tư | ADR-0018 đề xuất: cài guard vào image, mount classifier read-only |
| I5 | Task 8.10: `GMAIL_ACCESS_TOKEN` dùng chung cho mọi mailbox Gmail | Adapter demo ghi lại mail gửi đi, không gửi thật (task 9.3) |
