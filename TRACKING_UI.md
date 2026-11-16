# TRACKING_UI — Theo dõi tiến độ Demo Console

Kế hoạch: [PLAN_UI.md](PLAN_UI.md).
Ký hiệu: `[ ]` chưa làm · `[~]` đang làm / làm dở (ghi phần còn lại) · `[x]` xong (đạt Definition of Done) · `[-]` bỏ.

**Trạng thái chung:** 📝 Đang làm UI-0 (chỉ tài liệu). Còn chờ C2, C3a. **Chưa bắt đầu code.**
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
| C2 | Embedding khi chuyển model | _chờ_ (đề xuất E2 + profile offline dự phòng, PLAN §8.2) | |
| C3 | Model thật cho chế độ live | GPT (OpenAI) | 2026-11-16 |
| C3a | Model cụ thể | _chờ_ (đề xuất `gpt-4o-mini` cho mọi vai trò) | |
| C4 | Benchmark Explorer lên P1 | Có | 2026-11-16 |
| C5 | Guard config live chỉ C0, C3 | Có | 2026-11-16 |
| C6 | Bước kế: viết ADR-0018 + specs (chỉ tài liệu) | Đồng ý | 2026-11-16 |

---

## 2. Công việc

### UI-0 Quyết định & spec
- [x] UI-0.1 Khảo sát hiện trạng, viết PLAN_UI.md và TRACKING_UI.md
- [x] UI-0.2 Chốt Q1–Q10
- [x] UI-0.2a Tạo nhánh `feat/demo-ui`
- [~] UI-0.2b Chốt C1–C6 — còn lại: C2, C3a
- [ ] UI-0.3 ADR-0018 "Demo console" + cập nhật `specs/design.md`
- [ ] UI-0.4 Thêm requirement mới (R23.8+) vào `specs/requirements.md`
- [ ] UI-0.5 Thêm task UI vào `specs/tasks.md`
- [ ] UI-0.6 ⛔ **Hỏi chủ dự án trước khi bắt đầu code**

### UI-1 Backend demo
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

---

## 4. Vướng mắc / cần hỏi

| # | Vấn đề | Trạng thái |
|---|---|---|
| I1 | Endpoint demo và guard-trong-ai-worker là thay đổi spec (AGENTS.md §7): cần ADR và chủ dự án duyệt | Mở, xử lý ở UI-0.3 |
| I2 | Nút chuyển model: env đọc lúc khởi động container nên không đổi được giữa chừng; embedding không được đổi theo (R5.10) | Mở, chờ C1, C2 |
| I3 | `GuardedDraftingService` nằm trong `evaluation/`; service không nên import từ đó, cần chuyển phần dùng chung | Mở, xử lý trong ADR-0018 |
