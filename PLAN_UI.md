# PLAN_UI — Demo Console cho rag-email + AgentMailGuard

> Trạng thái: **Q1–Q10, C1, C3–C6 đã chốt (§8). Còn C2 và C3a (§8.2).** Chưa viết dòng code UI nào.
> Nhánh: `feat/demo-ui` (tạo từ `refactor/guard-v4` @ `4957774`).
> Theo dõi tiến độ: [TRACKING_UI.md](TRACKING_UI.md).
>
> **Bối cảnh:** demo dùng cho **buổi bảo vệ luận án**. UI phải kể được hai câu chuyện: (1) rag-email tiết kiệm chi phí nhờ các cổng phân loại; (2) AgentMailGuard chặn tấn công, so sánh C0 với C3 trên cùng một email. Demo phải chạy ổn định khi offline (model `fake`), và chuyển được sang model thật.

---

## 1. Mục tiêu

| # | Yêu cầu của chủ dự án | Nghĩa cụ thể (đề xuất) |
|---|---|---|
| G1 | Có **live demo** hệ thống | Bấm một nút → một email thật đi vào stack đang chạy (`make up`) → xem nó đi qua từng trạng thái `RECEIVED → … → DRAFTED/COMPLETED` theo thời gian thực, kèm dữ liệu của từng bước. |
| G2 | Chạy được **mọi scenario** | Có danh mục scenario (§4) phủ mọi nhánh của pipeline và của guard; mỗi scenario chạy được bằng 1 click, và có ô soạn email tự do. |
| G3 | **Rõ ràng, trực quan** | Sơ đồ pipeline động; mỗi "cổng" (no-reply, template, RAG, guard) hiện rõ vì sao email dừng hay đi tiếp; chi phí và độ trễ hiện bằng số. |
| G4 | **Đẹp, hiện đại** | Design system thống nhất, dark/light mode, animation chuyển trạng thái, responsive cho màn hình trình chiếu (1440×900 trở lên). |

Câu dẫn dắt của hệ thống, và cũng là câu chuyện UI phải kể:

> **Classify first. Retrieve only when required. Generate only when necessary.**

---

## 2. Hiện trạng (đã kiểm tra trong repo, nhánh `refactor/guard-v4` @ `4957774`)

- **Review UI đã có:** `services/frontend/` (FastAPI + Jinja2 + htmx), cổng `127.0.0.1:3001`, không login (ADR-0009). Có 3 màn: hàng đợi draft + approve/edit/reject (R23.4), timeline của message (R23.5), upload knowledge + trạng thái ingest (R23.7). Chỉ gọi API `/v1`. CSS tự viết (`review.css`, ~4 KB), không framework.
- **API `/v1`** (`services/api/`, cổng `127.0.0.1:8000`): `drafts` (list/get/patch/approve/reject), `jobs` (get/timeline/replay), `messages` (get/timeline), `threads`, `mailboxes` (list/get/resync), `knowledge` (upload/list/get/delete), `search/debug` (R23.3).
- **Chưa có cửa để "bơm" một email vào stack từ UI.** Email vào qua mail-connector (Gmail) hoặc qua feeder của benchmark (`evaluation/mailguard_bench/live/feeder.py`, dùng `SyncOrchestrator.sync_mailbox` với một adapter chỉ chứa một email).
- **AgentMailGuard chưa nằm trong đường chạy mặc định.** Chỉ benchmark mới thay `DraftingService` bằng `GuardedDraftingService` (`evaluation/mailguard_bench/live/guarded_drafting.py`). `agentmailguard/services/guard_worker` chạy độc lập (stdin hoặc AMQP).
- **LLM mặc định là `fake`** (`LLM__PROVIDER=fake`, `EMBEDDING__MOCK=true`): stack chạy offline, không tốn tiền, câu trả lời đóng hộp.
- **Quan sát:** Prometheus + Grafana có trong compose; dashboard (task 7.4) chưa xong.
- **Dữ liệu demo:** `make seed` nạp tenant, khách hàng, đơn hàng, knowledge, email mẫu (`packages/db/fixtures/`); thêm 9 file `.eml` trong `tests/fixtures/mail/`; 550/1800 case benchmark trong `evaluation/datasets/mailguard/`.
- **Kết quả benchmark đã có:** `evaluation/results/mailguard_bench/*` (3 model, C0 vs C3) và hình trong `paper/figures/`.

---

## 3. Ràng buộc từ AGENTS.md (UI phải tuân theo)

1. UI chỉ nói chuyện với API `/v1`. Không SQL trực tiếp, không gọi thẳng service, không bỏ qua queue (§5 anti-goal "Skip the queue").
2. Email demo phải vào **đúng cửa của tenant** (mail-connector / `SyncOrchestrator`), không ghi thẳng vào bảng.
3. Mọi request mang `X-Organization-ID`. Demo phải có ≥3 tenant (chọn tenant trên UI).
4. Không tên provider (`gmail`, `graph`, `imap`) ngoài `packages/adapters/`.
5. Endpoint mới, setting mới, requirement mới = **thay đổi spec** → cần chủ dự án duyệt, ADR mới (`docs/adr/0018-…`), cập nhật `specs/design.md`, `specs/tasks.md`, `.env.example`, `docs/configuration.md` (§7 Divergence protocol).
6. Bảo mật nằm ngoài phạm vi (§6): UI giữ nguyên mô hình hiện tại (chỉ bind `127.0.0.1`, không login). Không tự chế cơ chế auth.
7. Test không cần mạng hay key thật: chạy với `LLM__PROVIDER=fake`.

---

## 4. Danh mục scenario (đã chốt, Q3)

Mỗi scenario là một email (hoặc một chuỗi) + tenant + kỳ vọng. UI hiện kỳ vọng cạnh kết quả thật để thấy ngay pass/fail.

### 4.1 Pipeline rag-email

| ID | Scenario | Nguồn dữ liệu sẵn có | Kết quả mong đợi | Điểm cần làm nổi bật |
|---|---|---|---|---|
| S01 | Newsletter / auto-submitted / out-of-office / DSN | `tests/fixtures/mail/01–05` | `reply_required=false`, dừng sau triage | **0 lời gọi AI, $0** |
| S02 | Lịch hẹn / hành chính (template) | `08_calendar_invite.eml`, category `scheduling` | `workflow_hint=template` | 0 retrieval, 0 generation |
| S03 | Hỏi chung, không cần RAG | email mẫu `general_inquiry` | 1 lời gọi generation, không retrieval | |
| S04 | Hỏi hỗ trợ cần RAG | `09_actionable_support.eml` + knowledge seed | Draft có citation tới chunk | Chunk được chọn, điểm BM25/vector/RRF/rerank |
| S05 | Dữ liệu nghiệp vụ live | "What is the status of order 82915?" (Phase 5 gate) | Draft nói "dispatched" + citation | Hai nguồn: bảng nghiệp vụ và knowledge |
| S06 | Thread dài → tóm tắt | thread 12 message (phase4-gate) | Summarizer chạy 1 lần, rồi draft | Ngưỡng >4 message / >1500 token |
| S07 | Định tuyến model (escalation) | thread dài / retrieval yếu | Tier `high_capability` | Vì sao bị nâng tier |
| S08 | Billing khẩn | `06_invoice_inquiry`, `07_urgent_billing` | Draft billing, ưu tiên cao | |
| S09 | Yêu cầu chéo tenant | kịch bản của `make smoke` | `DEAD_LETTER` | Cô lập tenant |
| S10 | Lỗi → retry → replay | ép lỗi LLM (fake) + `POST /v1/jobs/{id}/replay` | `RETRY_PENDING → FAILED → replay → DRAFTED` | Idempotency, at-least-once |
| S11 | Review: duyệt / sửa / từ chối → dispatch | draft của S04/S05 | `DRAFTED → DISPATCHED → COMPLETED`; approve lần 2 không gửi lại | Exactly-once dispatch |
| S12 | Upload knowledge rồi hỏi ngay | upload 1 tài liệu mới | Trạng thái ingest → `active`, câu hỏi kế tiếp cite tài liệu đó | R23.7 |
| S13 | Burst 20× | `make load` / sinh N email | Độ sâu queue tăng rồi giảm, p95 < 10 s | Hệ thống chịu burst nhờ queue |
| S14 | Retrieval debug | `POST /v1/search/debug` | Query, 2 nhánh, RRF, rerank, `retrieval_underfilled` | R23.3, R10.10 |

### 4.2 AgentMailGuard (đã chốt: có, Q2)

| ID | Scenario | Nguồn | C0 (không guard) | C3 (full guard) |
|---|---|---|---|---|
| G01 | Prompt injection trực tiếp | LLMail-Inject case trong `cases.jsonl` | Model bị gọi, draft có thể làm theo lệnh độc | L1 critical → L3 pass 1 `quarantine`, **model không được gọi** |
| G02 | Injection "mềm" (không khớp rule) | case LLMail khác | như trên | Classifier / judge bắt, hoặc `draft_only` |
| G03 | RAG poisoning | case `attack-prag-*` | Chunk độc vào prompt | L2 đánh `quarantined` chunk, L3 pass 2 quyết định |
| G04 | Email lành tính | case `benign-*` | Draft bình thường | Không chặn nhầm; `draft_only`/`auto_send` |
| G05 | Rò rỉ trong output | draft chứa secret / link lạ | Gửi nguyên văn | L4 redact, `human_approval` |
| G06 | Judge lỗi (fail-closed) | ép judge trả JSON hỏng | — | `judge_invalid:<stage>` → human review |

Chế độ so sánh: chạy **cùng một email** qua C0 và C3, hiện hai cột cạnh nhau.

### 4.3 Email tự do

Ô soạn email (from, subject, body, tenant, chọn config C0/C3) để người xem thử email của chính họ.

---

## 5. Các màn hình

| # | Màn | Nội dung | Ưu tiên |
|---|---|---|---|
| V1 | **Live Pipeline** (màn chính) | Bên trái: inbox các email vừa chạy. Giữa: sơ đồ pipeline (mail-connector → email-worker → triage → gate → retrieval → summarizer → guard L1–L4 → generation → draft → dispatch); node sáng lên khi job đi qua, cạnh bị gạch khi email dừng ở cổng. Bên phải: chi tiết bước đang chọn (kết quả triage + confidence, chunk retrieval + điểm, ngân sách prompt, draft, token, $ chi phí, ms). | P0 |
| V2 | **Scenario Runner** | Lưới thẻ scenario (§4) có nút Run; chạy 1 / chạy cả bộ; kết quả pass/fail so với kỳ vọng; ô soạn email tự do. | P0 |
| V3 | **Guard Inspector** | Một email qua 4 layer: verdict từng layer (severity, score, findings, rule khớp), sanitized body, trust từng chunk, prompt sau channel isolation, quyết định cuối. Chế độ C0 ⇄ C3 song song. | P0 |
| V4 | **Review & Dispatch** | Hàng đợi draft, email gốc, tóm tắt thread, chunk được cite, business facts, approve/edit/reject (thay hoặc nhúng review UI hiện có — Q5). | P1 |
| V5 | **Timeline** | Timeline trạng thái của một message/job, thời gian từng bước, replay. | P1 |
| V6 | **Observability** | Funnel (received → no-reply → template → AI → RAG), độ trễ p50/p95, độ sâu queue, chi phí tích lũy (counter). | P1 |
| V7 | **Knowledge** | Upload, trạng thái ingest, xem chunk. | P2 |
| V8 | **Benchmark Explorer** | Kết quả đã commit: defense rate, false positive, chi phí theo model × config; drill-down từng case (đọc file tĩnh, không chạy lại). | P1 |

Mọi màn hình đều **song ngữ Việt / Anh** (Q9): nút chuyển ngôn ngữ trên thanh đầu trang, chuỗi giao diện nằm trong file dịch (`vi.json`, `en.json`). Nội dung email, draft và tên kỹ thuật (state, layer, rule id) giữ nguyên, không dịch.

Thanh đầu trang có thêm **nút chuyển model** (Q8): `Fake (offline)` ⇄ `Live model`, hiện rõ model nào đang chạy và chi phí ước tính khi dùng model thật.

---

## 6. Kiến trúc (đã chốt B, Q4)

| Tiêu chí | **A. Nâng cấp frontend hiện có** (Jinja2 + htmx + Tailwind + SSE) | **B. Ứng dụng SPA mới** (React + Vite + TypeScript + Tailwind + shadcn/ui + React Flow + Recharts) | **C. Next.js** |
|---|---|---|---|
| Hiện đại / đẹp | Khá; animation và sơ đồ động khó hơn | Cao nhất; sơ đồ pipeline động bằng React Flow, chart đẹp | Cao |
| Toolchain | Chỉ Python, giữ nguyên Docker | Thêm Node 20 + build stage trong Dockerfile, CI thêm job | Node + server runtime |
| Nhất quán với repo | Cao (R23.4/5/7 đã trace vào đây) | Frontend thứ hai, cần ADR | Như B, nặng hơn |
| Live update | htmx SSE extension / polling | SSE hoặc polling, state client rõ ràng | như B |
| Công sức | Thấp–trung bình | Trung bình–cao | Cao |
| Rủi ro | Trần thẩm mỹ thấp | Thêm bề mặt bảo trì | SSR không cần thiết, thừa |

**Đã chốt: B** (Q4). Service mới `services/demo_ui/` (build tĩnh, phục vụ bằng nginx trên `127.0.0.1:3002`). Review UI cũ (`services/frontend`, :3001) chạy song song cho tới khi V4 thay được nó (Q5). Đa ngôn ngữ bằng `react-i18next` (Q9). Lý do chọn B: G3/G4 (sơ đồ động, chart, animation) là yêu cầu chính, và A khó đạt.

### 6.1 Phần backend cần thêm (mọi mục đều là thay đổi spec → cần duyệt)

| # | Thành phần | Mô tả | Vì sao cần |
|---|---|---|---|
| B1 | **Demo ingest** | `POST /v1/demo/messages` (hoặc `/v1/demo/scenarios/{id}/run`): nhận email + tenant, đưa vào qua `SyncOrchestrator.sync_mailbox` với adapter một-email (cùng cách feeder benchmark làm), trả `message_id`/`job_id`. Bật bằng setting `DEMO__ENABLED` (mặc định `false`). | G1, G2. Không có cửa này thì UI không thể bơm email. |
| B2 | **Danh mục scenario** | `GET /v1/demo/scenarios`: đọc từ file YAML/JSON trong `config/` (email, tenant, kỳ vọng). | G2, để thêm scenario không cần sửa code UI. |
| B3 | **Cập nhật live: polling** (Q6) | UI gọi `GET /v1/jobs/{id}/timeline` mỗi ~500 ms khi job còn đang chạy, dừng khi job tới trạng thái cuối. Không thêm endpoint stream. | G1. Dùng endpoint đã có, ít thay đổi spec. |
| B4 | **Guard trong đường chạy live** (Q2: đồng ý) | Cho ai-worker dùng `GuardedDraftingService` khi bật setting (ví dụ `GUARD__MODE=off|C3`, mặc định `off`), và lưu verdict của guard để API đọc được (`GET /v1/jobs/{id}/guard`). Code hiện nằm ở `evaluation/mailguard_bench/live/guarded_drafting.py`; cần chuyển phần dùng chung ra chỗ mà service được phép import. | V3, G01–G06. Hiện guard chỉ chạy trong benchmark. |
| B5 | **Số liệu cho Observability: chart vẽ trong UI** (Q7) | API trả số tổng hợp lấy từ Prometheus (`GET /v1/metrics/summary`): funnel, p50/p95, độ sâu queue, chi phí. UI vẽ bằng Recharts. Grafana vẫn giữ cho người vận hành. | V6. Cùng giao diện với các màn khác. |
| B6 | **Ép lỗi cho S10/G06** | Fake LLM có chế độ trả lỗi theo marker trong email (chỉ khi `DEMO__ENABLED`). | S10, G06. |
| B7 | **Nút chuyển model** (Q8) | Xem §6.2. | Q8. |
| B8 | **So sánh C0 ⇄ C3 trên cùng email** | Endpoint demo nhận thêm `guard_mode`; chạy C0 và C3 = hai job riêng cho cùng nội dung email (khác idempotency key). | G01–G06. |

### 6.2 Nút chuyển model (Q8) — cần chọn cơ chế

Hiện model được chọn bằng biến môi trường (`LLM__PROVIDER`, `EMBEDDING__MOCK`…) lúc container khởi động, nên không đổi được giữa chừng. Có hai cách:

| | **M1. Chọn theo từng job** | **M2. Khởi động lại stack theo profile** |
|---|---|---|
| Cách làm | Endpoint demo nhận `model_profile` (`fake` / `live`); giá trị đi theo envelope của job; ai-worker, triage-worker và guard chọn `LLMProvider` theo giá trị đó. Cả hai provider được tạo sẵn lúc khởi động. | UI gọi một lệnh đổi `.env` rồi `docker compose up -d` lại các worker. |
| Chuyển ngay trong demo | Có, tức thì; chạy được fake và live xen kẽ | Mất 30–60 s mỗi lần chuyển |
| Thay đổi spec | Lớn hơn: thêm trường vào envelope, factory provider chọn theo job | Nhỏ hơn về code, nhưng UI phải điều khiển Docker (vượt quyền của một web UI) |
| Rủi ro | Logic chọn provider lan ra nhiều worker | Lỗi khởi động giữa buổi bảo vệ |

**Đã chốt: M1** (C1).

**Vướng mắc về embedding:** nút chuyển chỉ nên đổi model sinh văn bản (LLM), không đổi embedding. Lý do: vector trong knowledge base được tạo bằng một embedding cố định, đổi embedding giữa chừng làm vector truy vấn không khớp với vector đã lưu (R5.10). Vì vậy phải chọn một trong hai:

- **E1. Embedding giả (`EMBEDDING__MOCK=true`) cho cả hai chế độ:** demo chạy hoàn toàn offline. Nhưng ở chế độ live, nhánh tìm kiếm vector gần như vô nghĩa, chỉ còn BM25 tìm đúng.
- **E2. Embedding thật cho cả hai chế độ:** retrieval đúng ở cả hai chế độ. Nhưng chế độ `fake` vẫn cần mạng và key embedding, nên không còn offline hoàn toàn.

---

## 7. Lộ trình

| Phase | Nội dung | Kết quả kiểm chứng |
|---|---|---|
| **UI-0** Quyết định & spec | Chốt §8.1; viết ADR-0018 "Demo console"; thêm requirement mới (R23.8+, không đánh số lại); thêm task vào `specs/tasks.md`; cập nhật `specs/design.md`. Chỉ sửa tài liệu, chưa code. | Chủ dự án duyệt ADR |
| **UI-1** Backend demo | B1, B2, B6, B7 (nút chuyển model), B8. Test unit + integration (Postgres/RabbitMQ thật, ≥3 tenant, fake LLM). | `make up` healthy; gọi B1 bằng curl → job tới `DRAFTED`, cả hai chế độ model |
| **UI-2** Khung UI + design system | Scaffold React + Vite + TS + Tailwind + shadcn/ui, layout, theme dark/light, i18n Việt/Anh, chọn tenant, nút chuyển model, API client có kiểu từ OpenAPI, Docker service. | `make up` có thêm service UI healthy |
| **UI-3** Live Pipeline (V1) | Sơ đồ động, inbox, panel chi tiết từng bước. | Chạy S04 từ UI, thấy đủ bước tới `DRAFTED` |
| **UI-4** Scenario Runner (V2) | Thẻ scenario, chạy 1 / chạy bộ, pass/fail, email tự do. | Toàn bộ S01–S14 chạy được từ UI với fake LLM |
| **UI-5** Guard (B4 + V3) | Guard trong ai-worker theo setting `GUARD__MODE`, API verdict, màn Inspector, C0 ⇄ C3. Chạy `make mailguard-unit` để chắc benchmark không bị ảnh hưởng. | G01–G06 cho kết quả đúng kỳ vọng |
| **UI-6** Review, Timeline, Knowledge (V4, V5, V7) | Làm lại trong UI mới; review UI cũ vẫn chạy song song. | R23.4/5/7 vẫn đạt |
| **UI-7** Observability + Benchmark (V6, V8) | B5 + chart funnel, latency, queue, cost; explorer kết quả benchmark. | Số trên UI khớp Prometheus / file kết quả |
| **UI-8** Hoàn thiện | Test e2e (Playwright), kiểm tra visual 1440×900, docs (`docs/demo-ui.md`), cập nhật demo script trong `docs/demo-runbook.md` §6. | `make ci` xanh; demo 10 phút chạy trọn từ UI |

Mỗi task đi đúng vòng lặp của AGENTS.md §2: một task một lần, test, Definition of Done, commit có số task + requirement ID.

---

## 8. Quyết định của chủ dự án (2026-11-16)

| # | Câu hỏi | Quyết định |
|---|---|---|
| Q1 | Người xem / bối cảnh | Buổi bảo vệ luận án |
| Q2 | Phạm vi | Cả rag-email và AgentMailGuard, có so sánh C0 ⇄ C3. **Đồng ý đưa guard vào luồng xử lý thật** (B4) |
| Q3 | Danh mục scenario | Theo đề xuất §4 (S01–S14, G01–G06, email tự do) |
| Q4 | Kiến trúc | B: SPA React + Vite + TS + Tailwind + shadcn/ui, service `services/demo_ui/` |
| Q5 | Review UI cũ | Giữ song song cho tới khi V4 thay được |
| Q6 | Cập nhật live | Polling `GET /v1/jobs/{id}/timeline` |
| Q7 | Observability | Chart vẽ trong UI, số liệu lấy từ Prometheus qua API (B5) |
| Q8 | Model khi demo | Có nút chuyển fake ⇄ live (cơ chế: §6.2, chờ chốt) |
| Q9 | Ngôn ngữ giao diện | Song ngữ Việt / Anh |
| Q10 | Nhánh git | Nhánh mới `feat/demo-ui` từ `refactor/guard-v4` @ `4957774` (đã tạo, làm ngay trên checkout chính, không dùng worktree) |

### 8.1 Xác nhận bổ sung (2026-11-16)

| # | Điểm | Quyết định |
|---|---|---|
| C1 | Cơ chế nút chuyển model (§6.2) | M1: chọn theo từng job |
| C2 | Embedding khi chuyển model (§6.2) | **Còn mở**: chủ dự án trả lời "theo đề xuất" nhưng bản kế hoạch chưa có đề xuất. Đề xuất mới ở §8.2, chờ xác nhận |
| C3 | Model thật cho chế độ live | GPT (OpenAI). Giả định: `gpt-4o-mini` cho mọi vai trò, giống benchmark; chờ xác nhận ở §8.2 |
| C4 | Ưu tiên Benchmark Explorer (V8) | P1 |
| C5 | Guard config ở chế độ live | Chỉ C0 và C3 (ADR-0017) |
| C6 | Bước kế tiếp | UI-0: viết ADR-0018 và cập nhật `specs/` (chỉ tài liệu). Hỏi chủ dự án trước khi code |

### 8.2 Còn chờ xác nhận

| # | Điểm | Đề xuất |
|---|---|---|
| C2 | Embedding | **E2**: embedding thật `text-embedding-3-small` (OpenAI, 1536 chiều, cùng tài khoản với GPT) cho cả hai chế độ, để retrieval đúng khi hội đồng xem. Phương án dự phòng khi mất mạng: một profile offline riêng (`EMBEDDING__MOCK=true` + `LLM__PROVIDER=fake`), bật bằng `make up` + `make seed` **trước** buổi bảo vệ, không phải nút trên UI |
| C3a | Model cụ thể | `gpt-4o-mini` cho triage, summarizer, reply writer, tier escalated và 5 judge của guard (một model, giống benchmark), thay vì mặc định `gpt-4o` cho tier escalated |
## 9. Rủi ro

| Rủi ro | Giảm thiểu |
|---|---|
| Endpoint demo (B1) bị dùng như đường tắt bỏ qua connector | B1 gọi đúng `SyncOrchestrator`, tắt mặc định, chỉ bind `127.0.0.1` |
| Model thật làm demo chậm / hỏng giữa chừng | Mặc định `fake`; khi dùng model thật, chạy trước và giữ kết quả làm dự phòng (như runbook §6) |
| Đưa guard vào ai-worker làm lệch benchmark | Setting tắt mặc định; benchmark giữ đường riêng; chạy `make mailguard-unit` trước khi merge |
| Frontend thứ hai làm CI chậm | Job CI riêng cho UI, cache `node_modules` |
| Mở rộng phạm vi | Mỗi màn là một task riêng trong `specs/tasks.md`; P2 làm sau cùng |
