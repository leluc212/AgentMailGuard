# TRACKING_UI — Theo dõi tiến độ Demo Console

Kế hoạch: [PLAN_UI.md](PLAN_UI.md).
Ký hiệu: `[ ]` chưa làm · `[~]` đang làm / làm dở (ghi phần còn lại) · `[x]` xong (đạt Definition of Done) · `[-]` bỏ.

**Trạng thái chung:** task 9.0–9.6 và 9.8 xong (commit tới `c323f60`); 9.7 còn thiếu parity test đầy đủ. Bước kế: hoàn tất parity test 9.7, rồi task 9.9 (console React).
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
| C10 | Task 9.2 và 9.7 cùng ghi migration `0006` | 9.2 dùng `0006` (`run_options`), 9.7 dùng `0007` (`guard_report`); đã sửa `specs/tasks.md` | 2026-11-16 |
| C11 | Thứ tự làm Phase 9 | Theo `specs/tasks.md` (9.2 trước), cập nhật TRACKING_UI.md theo | 2026-11-16 |
| C13 | G06 với profile `fake` (job fake không dựng judge) | (a) G06 chỉ chạy với `live`; console tắt nút G06 khi chưa có model thật | 2026-11-16 |
| C14 | Judge của guard khi `live` | (a) truyền provider `live` của rag-email (bọc chuyển kiểu) vào `inbound_judge=`… của `MailGuardPipeline` | 2026-11-16 |
| C15 | Cổng console trùng Grafana (3002) | (a) console ở `127.0.0.1:3003`; Grafana giữ 3002 | 2026-11-16 |
| C12 | Provider của demo mailbox (CHECK chỉ cho `gmail`, `graph`, `imap`) | A: migration `0007` thêm `demo`; adapter `demo` ghi lại mail gửi đi, không gửi thật; `guard_report` lùi sang `0008`. Đã sửa design §5.9, ADR-0018, tasks 9.3, 9.7 | 2026-11-16 |

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
- [x] UI-1.0a Task 9.2 Run options trên job: đã viết migration `0006_job_run_options`, `Job.run_options`, cột trong mọi câu SQL của `PostgresJobStore` và `draft_persistence`, field envelope `run_options`; chép vào envelope ở orchestrator (tham số mới `sync_mailbox(run_options=)`), email-worker, lease reaper, replay, duyệt draft (triage → ai-worker và retry giữ nhờ `model_copy`); test `tests/unit/test_run_options.py`. Unit test xanh (chỉ còn test fail sẵn `test_kit_docs.py`); `docker compose up` healthy, migration `0006` đã áp dụng
- [x] UI-1.2 Task 9.3 Demo ingest door (làm trước UI-1.1 theo thứ tự `specs/tasks.md`): đã viết migration `0007_mailbox_provider_demo`, `packages/adapters/demo.py` (provider `demo`, adapter nhận tin và ghi lại thư gửi đi), `DEMO__ENABLED`, queue `mail.demo.inject`, `services/mail_connector/demo_consumer.py` (prefetch 1), router `/v1/demo` (`capabilities`, `messages`, `requests/{id}`; 404 khi tắt, 422 khi xin `live` mà chưa có model thật, 409 khi tenant chưa có demo mailbox), seed 3 demo mailbox, `.env.example`, `docs/configuration.md` §2.21a, compose. Test: `tests/unit/test_demo_ingest.py`, `tests/integration/test_demo_ingest_e2e.py`. Kết quả: unit test xanh (chỉ còn test fail sẵn `test_kit_docs.py`), ruff và mypy sạch, integration `test_demo_ingest_e2e.py` (3 tenant tới `DRAFTED` qua broker; 404 khi tắt) và `test_phase6_pipeline_e2e.py` xanh; `docker compose up` healthy, migration `0007` áp dụng, topology 28 queue. `test_seed_loader.py` xanh sau khi sửa số mailbox và checkpoint 4 → 7. Commit `1d7147e`
- [x] UI-1.1 Task 9.5 Danh mục scenario: `config/demo_scenarios.yaml` (S01–S14, G01–G06, song ngữ, kỳ vọng, nguồn case MIT, không dùng PoisonedRAG), `services/api/demo_catalog.py` (kiểm tra lúc API khởi động), `GET /v1/demo/scenarios`, `POST /v1/demo/scenarios/{id}/run` (comparison: 2 job C0, C3 ở 2 thread; burst: N email; S14 trả 422), `DEMO__SCENARIOS_PATH`. Test `tests/unit/test_demo_scenarios.py`. **Còn lại:** chạy test. Ghi chú: kỳ vọng của S02, S07 với fake LLM chưa chắc khớp; sẽ chỉnh sau khi chạy thật
- [x] UI-1.3 Task 9.4 Nút chuyển model fake ⇄ live (theo C1, C2): `packages/core/run_context.py` (contextvar, `BaseConsumer` và `BatchConsumer` mở scope theo `envelope.run_options`), `packages/llm/model_profile.py` (`ProfileRoutedLLMProvider`, `build_profile_provider`; lần đầu tôi ghi nhầm đè lên `packages/llm/profile.py` của R14, test bắt được và đã khôi phục từ git), triage-worker và ai-worker dùng nó; label `model_profile` (`fake`/`live`/`default`) cho `estimated_ai_cost_total` và `generated_draft_cost_total`, sửa 4 file test cũ và `docs/observability.md`. Test `tests/unit/test_model_profile.py`. **Còn lại:** chạy test (chủ dự án chọn test gộp sau cùng)
- [x] UI-1.4 Task 9.6 Ép lỗi có kiểm soát cho fake LLM: `FakeLLMProvider(fault_markers=)` với `[[demo-fault:timeout]]` (LLMTimeoutError) và `[[demo-fault:invalid-json]]` (trả nội dung không khớp schema); `DEMO__FAULT_MARKERS_ENABLED` (chỉ có tác dụng khi `DEMO__ENABLED`); chỉ provider fake nhận cờ. Test `tests/unit/test_demo_fault_markers.py`. **Còn lại:** chạy test
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
- [~] UI-5.1 Task 9.7 Guard trong ai-worker theo setting: `services/ai_worker/guarded_drafting.py` (`LiveGuardedDraftingService`, chỉ dùng `MailGuardPipeline`, `GuardConfig.preset("C3")`, `mailguard.integration.adapters`; job fake không có judge, job live dùng provider live qua `JudgeAdapter`), `GUARD__ENABLED`, `GUARD__DEFAULT_MODE`, `GUARD__L1_MODEL_PATH`, `GUARD__AUDIT_LOG_PATH`, counter `guard_llm_calls_total`; image chép `agentmailguard/mailguard` + `configs` vào `/opt/agentmailguard` (không đè `services` của rag-email), compose mount classifier L1 chỉ đọc từ `MAILGUARD_L1_HOST_DIR`. **Còn lại:** chạy test, build image, chạy `make mailguard-unit`; parity hiện chỉ so phần ánh xạ kết quả
- [x] UI-5.2 API trả verdict guard của job: migration `0008_guard_report`, `packages/db/guard_report.py`, ghi trong transaction `persist_drafted`, `GET /v1/jobs/{id}/guard` (404 cho job C0 hoặc tenant khác), `run_options` trong `GET /v1/jobs/{id}`. Test `tests/unit/test_live_guarded_drafting.py`. **Còn lại:** chạy test
- [ ] UI-5.3 Màn Guard Inspector (4 layer, trust từng chunk, prompt sau isolation)
- [ ] UI-5.4 So sánh C0 ⇄ C3
- [ ] UI-5.5 G01–G06 chạy được từ UI
- [ ] UI-5.6 `make mailguard-unit` xanh (benchmark không bị ảnh hưởng)

### UI-6 Review, Timeline, Knowledge (V4, V5, V7)
- [ ] UI-6.1 Review & Dispatch (UI cũ vẫn chạy song song)
- [ ] UI-6.2 Timeline + replay
- [ ] UI-6.3 Knowledge upload + trạng thái ingest

### UI-7 Observability + Benchmark (V6, V8)
- [x] UI-7.1a Task 9.8 (B5) `GET /v1/metrics/summary?window=`: `services/api/routers/metrics_summary.py` đọc Prometheus (`OBSERVABILITY__PROMETHEUS_URL`); funnel và số email nhận lọc theo tenant (`scope: organization`), độ trễ p50/p95, queue, chi phí theo `model_profile` là `scope: stack`; window sai → 422, Prometheus lỗi → 503. Test `tests/unit/test_metrics_summary.py`. **Còn lại:** chạy test
- [ ] UI-7.1 Chart funnel, độ trễ, queue, chi phí (task 9.14)
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
| 2026-11-16 | Task 9.2: viết code và test | C10: `guard_report` chuyển sang migration `0007`, đã sửa `specs/tasks.md`, `specs/design.md` §5.9, ADR-0018. Chưa chạy được test vì shell agent chập chờn |
| 2026-11-16 | Xong task 9.3 | C12: provider `demo` (migration `0007`). Unit, ruff, mypy sạch; integration `test_demo_ingest_e2e.py` (3 tenant tới `DRAFTED` qua broker), `test_phase6_pipeline_e2e.py`, `test_seed_loader.py` xanh; stack healthy, 28 queue. Adapter `demo` lưu thư đã gửi trong bộ nhớ dispatch-worker, mất khi khởi động lại |
| 2026-11-16 | Viết task 9.4, 9.5, 9.6 (chưa chạy test, theo yêu cầu test gộp sau) | Dừng trước 9.7 vì I8, I9; dừng trước 9.9 vì I7 |
| 2026-11-16 | Chốt C13–C15; viết task 9.7, 9.8; chạy unit test | Lần 1: lỗi import vì tôi ghi đè `packages/llm/profile.py` (R14) → khôi phục từ git, chuyển sang `packages/llm/model_profile.py`. Lần 2: 4 test cũ cần cập nhật theo thay đổi có chủ đích (label `model_profile`, quy tắc `.dockerignore`/Dockerfile của task 7.24 mà ADR-0018 quyết định 6 sửa đổi, provider triage là `ProfileRoutedLLMProvider`) → đã sửa test; còn test fail sẵn `test_kit_docs.py` |
| 2026-11-16 | Lần chạy 3 (sau khi sửa) | Unit test xanh (chỉ còn test fail sẵn `test_kit_docs.py`), `ruff check` sạch, `mypy` sạch (218 file), formatter sửa 2 file. Test cần thư viện guard bị skip (`mailguard` không cài trong container). Task 9.4–9.8 vẫn `[~]`: chưa build image mới, chưa `make mailguard-unit`, chưa chạy stack với migration `0008` |
| 2026-11-16 | Kiểm tra 9.4–9.8 | `make mailguard-unit` (container, có cài guard): xanh, chỉ còn `test_kit_docs.py` fail sẵn; 2 test guard mới của 9.7 chạy và pass. Image mới build được, 13 service healthy, migration `0008` áp dụng, ai-worker import được `/opt/agentmailguard/mailguard`. 9.4, 9.5, 9.6, 9.8 → `[x]`; 9.7 giữ `[~]` (parity test mới so ánh xạ kết quả) |
| 2026-11-16 | Dựng stack trên máy này | Không có `make` trên Windows: dùng `docker compose up -d --build`. RabbitMQ lỗi `.erlang.cookie: eacces` do volume cũ hỏng; đã xóa `agentmailguard_rabbitmq_data` (chủ dự án đồng ý), RabbitMQ healthy. Service `init` thoát mã 1 lần đầu vì RabbitMQ báo healthy trước khi mở cổng 5672 (I6); chạy lại `docker compose up -d` thì 13 service healthy, migration `0006` áp dụng. Task 9.2 xong. Grafana đang dùng cổng 3002, trùng với cổng định cho console (task 9.9) |

---

## 4. Vướng mắc / cần hỏi

| # | Vấn đề | Trạng thái |
|---|---|---|
| I1 | Endpoint demo và guard-trong-ai-worker là thay đổi spec (AGENTS.md §7): cần ADR và chủ dự án duyệt | Xong: ADR-0018 Accepted |
| I2 | Nút chuyển model: env đọc lúc khởi động container nên không đổi được giữa chừng; embedding không được đổi theo (R5.10) | Xong: M1 (theo job) + E2 |
| I3 | `GuardedDraftingService` nằm trong `evaluation/`; service không nên import từ đó | ADR-0018 đề xuất: wrapper mới trong ai-worker + parity test, không sửa benchmark |
| I4 | Image Docker loại `agentmailguard/` (`.dockerignore`); classifier L1 là file riêng tư | ADR-0018 đề xuất: cài guard vào image, mount classifier read-only |
| I5 | Task 8.10: `GMAIL_ACCESS_TOKEN` dùng chung cho mọi mailbox Gmail | Adapter demo ghi lại mail gửi đi, không gửi thật (task 9.3) |
| I6 | Healthcheck RabbitMQ (`rabbitmq-diagnostics -q ping`) báo healthy trước khi cổng AMQP mở; với volume mới, `init` kết nối hụt và thoát mã 1, phải chạy lại `docker compose up -d` | Chưa sửa (ngoài phạm vi Phase 9). Đề xuất: đổi sang `check_port_connectivity`; cần chủ dự án duyệt |
| I7 | Grafana dùng cổng 3002, trùng cổng console định trong task 9.9 | Xong: C15 |
| I8 | G06 (judge lỗi, fail-closed) với profile `fake`: job `fake` không dựng judge | Xong: C13 |
| I9 | Judge của guard khi `live` | Xong: C14 |
