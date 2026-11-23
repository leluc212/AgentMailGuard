# AgentMailGuard: yêu cầu xử lý sự cố (phần 1/4, Requirements)

| | |
|---|---|
| Trạng thái | Đã duyệt ngày 20/10/2026: thang SEV và trạng thái các yêu cầu; quyết định cho mục 7 |
| Ngày | 20/10/2026 |
| Phạm vi | Toàn hệ thống: runtime (rag-email + guard AgentMailGuard) và quy trình benchmark/nghiên cứu |
| Người đọc | Nhóm nội bộ, dùng để phản biện với giảng viên |
| Các phần tiếp theo | `02-protocol.md` (quy trình xử lý), `03-critical-questions.md` (câu hỏi phản biện), `04-assumptions.md` (giả định) |

## 0. Cách đọc tài liệu này

- Mỗi yêu cầu có mã riêng (ví dụ `R-GRD-01`) và viết theo dạng: **Khi** <điều kiện>, <thành phần> **phải** <hành vi>.
- Cột **Trạng thái**:
  - **Có**: code đã làm, có dẫn file.
  - **Một phần**: code làm được một phần, hoặc chỉ in cảnh báo.
  - **Chưa có**: khoảng trống. Đây là chỗ giảng viên dễ hỏi nhất, nên nhóm cần chuẩn bị câu trả lời.
- Cột **Căn cứ** dẫn file và dòng trong repo. Phần dẫn chứng lấy từ việc đọc code ngày 20/10/2026 tại commit `790accb`. Những chỗ chỉ dựa vào tài liệu mà chưa đọc code được ghi rõ là "theo tài liệu".
- Mục 8 là sổ sự cố thật đã xảy ra trong đợt benchmark 17–20/10. Mỗi yêu cầu quan trọng nên có ít nhất một sự cố thật hoặc một test làm bằng chứng.

## 1. Phạm vi

| Miền | Gồm | Không gồm |
|---|---|---|
| A. Runtime | Pipeline rag-email (api, triage-worker, knowledge-worker, ai-worker, email-worker, dispatch-worker, mail-connector, RabbitMQ, Postgres, MinIO, Prometheus, Grafana) và guard AgentMailGuard (L1 đến L4, channel isolation, policy engine) | Hạ tầng production thật: hệ thống chưa triển khai cho người dùng thật |
| B. Nghiên cứu | Benchmark kit, dataset và license, credit/chi phí model, tính tái lập, báo cáo và biểu đồ | Nội dung khoa học của bài báo (phần đó do nhóm viết) |

## 2. Thuật ngữ

| Thuật ngữ | Nghĩa trong hệ thống này |
|---|---|
| Fail-closed | Khi một lớp bảo vệ lỗi, hệ thống chọn hành động an toàn hơn (chờ người duyệt), không bỏ qua bước kiểm tra |
| Fail-open | Ngược lại: lớp bảo vệ lỗi nhưng job vẫn đi tiếp như chưa có gì. Đây là điều cấm |
| Error row | Một case benchmark không chấm điểm được vì lỗi hạ tầng hoặc lỗi model (ví dụ `triage_stage_failure`) |
| Scored fallback | Guard trả lời sai định dạng nhưng vẫn được chấm điểm như hành vi của guard (có mục `guard_fallbacks`) |
| STOP | Dòng log của breaker khi hết credit, hết quota hoặc mất provider đã ghim. Runner thoát mã 3 và kit dừng cả campaign |
| RUN | Một thư mục kết quả trong `evaluation/results/mailguard_bench/<RUN>/` |
| Pin | Commit, hash dataset, hash classifier và provider đã cố định cho một RUN |
| DLQ | Dead-letter queue: nơi chứa job lỗi vĩnh viễn, chờ người xem và replay |

## 3. Phân loại mức độ sự cố

Yêu cầu `R-IR-00`: mọi sự cố phải được xếp vào một trong bốn mức dưới đây ngay khi phát hiện. Khi phân vân giữa hai mức, chọn mức nặng hơn.

| Mức | Định nghĩa | Ví dụ trong hệ thống này |
|---|---|---|
| SEV1 | Bảo vệ thất bại thật sự, lộ bí mật, hoặc số liệu đã công bố bị sai | Draft làm theo lệnh tiêm mà vẫn được gửi (`auto_send`); khóa API bị commit; rò rỉ train/bench làm sai số đã báo cáo |
| SEV2 | Bảo vệ có thể bị vô hiệu mà không ai biết, hoặc kết quả sinh ra dưới code hay pin sai | Một lớp guard tắt im lặng; một rule trong `policy.yaml` bị bỏ qua; RUN chạy với image khác commit; mất provider đã ghim giữa run |
| SEV3 | Gián đoạn, có thể phục hồi, không làm sai kết quả | Run dừng vì hết credit; error rows của một config vượt khoảng 2%; DLQ tăng |
| SEV4 | Không ảnh hưởng tới kết quả hay bảo vệ | Lỗi hiển thị của script theo dõi; bố cục biểu đồ |

## 4. Nguyên tắc chung

Các yêu cầu ở mục 5 đến 7 đều xuất phát từ bảy nguyên tắc sau.

| Mã | Nguyên tắc | Hệ thống đang thể hiện ở đâu |
|---|---|---|
| NT-1 | Fail-closed, không bao giờ fail-open | `MailGuardCoreSettings.fail_closed`, rule `P00` |
| NT-2 | Hành động chặt nhất thắng | `contracts/policy.py:46-71`, inbound floor (ADR-0015 quyết định 3) |
| NT-3 | Dừng sớm còn hơn ghi dữ liệu xấu | Breaker `STOP` trong `route.py`, các lệnh từ chối của kit |
| NT-4 | Có bằng chứng rồi mới kết luận | Audit id, manifest, log theo config |
| NT-5 | Không sửa lịch sử kết quả: thay đổi thì dùng RUN mới | `docs/BENCHMARK.md` (bản 2026-10-18), scheme refusal |
| NT-6 | Không để lộ bí mật ở bất kỳ đầu ra nào | `stack_env.py`, lệnh `package` của kit, audit không chứa nội dung email |
| NT-7 | Con người quyết định khi rủi ro cao hoặc khó hoàn tác | `requires_human`, xác nhận của owner trước khi thay đổi thiết kế thí nghiệm |

## 5. Miền A: yêu cầu cho runtime

### 5.1 Guard (L1 đến L4, channel isolation)

| Mã | Yêu cầu | Trạng thái | Căn cứ |
|---|---|---|---|
| R-GRD-01 | Khi một lớp guard gặp lỗi nội bộ (exception, timeout), guard **phải** coi đó là mức HIGH và chuyển job sang `human_approval`, không được để job đi tiếp như sạch. | Có | `config/settings.py:186-200` (`fail_closed=True`); rule `P00-internal-error-fail-closed` |
| R-GRD-02 | Khi judge LLM trả lời sai định dạng hoặc thất bại, guard **phải** gắn lỗi `judge_invalid:<stage>` vào verdict để rule `P00` kích hoạt. Chỉ được tắt hành vi này bằng `MAILGUARD__FAIL_CLOSED_ON_JUDGE_ERROR=false` trong thí nghiệm so sánh với v2, và phải ghi lại khi tắt. | Có | `llm/judge_calls.py:35, 66-74`; ADR-0015 quyết định 5 |
| R-GRD-03 | Khi model của một stage guard không khả dụng lúc khởi động, guard **phải** ghi cảnh báo và báo rõ stage nào bị tắt. Kết quả benchmark của RUN đó **phải** ghi nhận stage đã tắt. | Một phần: hiện chỉ có `logger.warning`, run meta không ghi lại | `pipeline.py:197-205` |
| R-GRD-04 | Khi channel isolation lỗi, prompt **phải** chỉ giữ phần system và task (bỏ toàn bộ dữ liệu không tin cậy), và lỗi phải đi tới `P00`. | Có | `prompting/channel_isolation.py:207-262` |
| R-GRD-05 | Khi pass 1 của L3 Inbound Gate trả về `block` hoặc `quarantine`, pipeline **phải** dừng trước L2 và trước mọi lời gọi model sinh nội dung, rồi trả draft giữ chỗ cố định. | Có | `pipeline.py:249`; `build_escalated_draft` `:273-291` |
| R-GRD-06 | L4 **phải** gộp quyết định outbound với inbound floor: hành động cuối không được nhẹ hơn floor, và `requires_human` là phép OR của hai bên. Floor nằm trong code, không cấu hình được. | Có | ADR-0015 quyết định 3; `output_guard/guard.py:184-187` |
| R-GRD-07 | Khi số lần gọi guard trong một job vượt `MAX_GUARD_CALLS_PER_JOB`, hệ thống **phải** cảnh báo và đếm vào metric. | Một phần: chỉ có cảnh báo trong log | `pipeline.py:483-494` |
| R-GRD-08 | Ngưỡng của L1 và L2 **phải** chọn trên tập dev riêng, không bao giờ chọn trên case đã pin của benchmark. | Có (theo tài liệu) | `agentmailguard/docs/datasets.md`; ADR-0015 quyết định 4 |
| R-GRD-09 | Khi khởi động với khóa env cũ (`L1__*` … `L5__*`), tiến trình **phải** dừng và in bảng chuyển đổi, không chạy với cấu hình hiểu sai. | Có (theo tài liệu) | ADR-0015 quyết định 6 |

### 5.2 Policy engine

| Mã | Yêu cầu | Trạng thái | Căn cứ |
|---|---|---|---|
| R-POL-01 | Khi nhiều rule cùng khớp, engine **phải** chọn hành động chặt nhất theo thứ tự quarantine > block > human_approval > draft_only > auto_send. Hòa thì chọn priority nhỏ hơn. | Có | `contracts/policy.py:46-71`; `configs/policy.yaml` |
| R-POL-02 | `auto_send` **chỉ** được phép khi job sạch (mức ≤ low), nằm trong `auto_send_categories` (acknowledgement, scheduling), action là `reply`, và không có inbound floor. | Có | `P10-clean-auto-send`; ADR-0015 quyết định 3 |
| R-POL-03 | Khi một rule trong `policy.yaml` không hợp lệ, engine **phải** từ chối khởi động, hoặc ít nhất phải báo đến mức không thể bỏ qua. Rule bị bỏ qua im lặng là một lỗ hổng fail-open. | **Chưa có**: hiện rule lỗi chỉ bị bỏ qua kèm log lỗi | `policy/engine.py:134` |
| R-POL-04 | Mỗi quyết định **phải** ghi `policy_version`, rule thắng và toàn bộ rule đã khớp. | Có | `PolicyDecision` trong `contracts/policy.py` |
| R-POL-05 | Mọi thay đổi `policy.yaml` **phải** tăng `version`, và benchmark chạy với version mới **phải** dùng RUN mới. | Một phần: version có ghi, nhưng chưa có kiểm tra tự động | `policy.yaml` dòng `version: "2026.10-v3"` |

### 5.3 Pipeline, hàng đợi, draft

| Mã | Yêu cầu | Trạng thái | Căn cứ |
|---|---|---|---|
| R-PIP-01 | Một draft chưa qua kiểm tra **không bao giờ** được lưu. Thứ tự là kiểm tra, sửa đúng một lần, nếu vẫn lỗi thì ném `UnvalidatedDraftError`. | Có | `packages/llm/validation.py:57`; `generator.py:388, 400, 498`; R16.1–R16.3 |
| R-PIP-02 | Lỗi vĩnh viễn (`UnvalidatedDraftError`, `DraftSchemaContractError`, `FatalError`, `LLMProviderMismatchError`, `LLMQuotaExhaustedError`, HTTP 402 không tạm thời) **phải** đi thẳng vào DEAD_LETTER, không retry. | Có | `services/ai_worker/failure_policy.py:60-120` |
| R-PIP-03 | Lỗi tạm thời **phải** retry theo thang 30 giây / 5 phút / 30 phút, có tôn trọng `Retry-After`. Lỗi không xác định cũng retry, nhưng vẫn phải tới DLQ khi đạt `max_retries`. | Có | `packages/broker/consumer.py:338-372`; `settings.py:594-624` |
| R-PIP-04 | Message không parse được **phải** vào DLQ ngay. | Có | `consumer.py:150-163` |
| R-PIP-05 | Job trong DEAD_LETTER **phải** replay được, và mọi chuyển trạng thái **phải** ghi `processing_event` kèm `trace_id`. | Có (theo tài liệu) | R18.3–R18.7; `packages/domain/state_machine.py:41` |
| R-PIP-06 | Khi triage thất bại hoàn toàn, hệ thống **phải** trả giá trị mặc định an toàn (`general_inquiry`, `normal`) kèm cờ cần xem lại. | Có | `services/triage_worker/cascade.py:352-358`; R6.11 |
| R-PIP-07 | Khi một nhánh retrieval lỗi, hệ thống **phải** chạy tiếp bằng nhánh còn lại, và tăng `retrieval_degraded_total`. | Có | `packages/retrieval/retriever.py:229-347`; R10.6 |
| R-PIP-08 | Khi một job bị giao lại mà đã ở DRAFTED, DISPATCHED, COMPLETED hoặc DEAD_LETTER, worker **phải** ack rồi bỏ, không xử lý hai lần (SC6). | Có | `failure_policy.py` (`ACK_DROP`) |
| R-PIP-09 | Ghi audit thất bại thì **không** được làm hỏng quyết định, nhưng **phải** để lại dấu vết để phát hiện được. | Một phần: chỉ có log, chưa có metric | `policy/engine.py:326` |

### 5.4 Quan sát và bằng chứng

| Mã | Yêu cầu | Trạng thái | Căn cứ |
|---|---|---|---|
| R-OBS-01 | Mỗi quyết định guard **phải** có `audit_id` tất định, tức chạy lại với cùng đầu vào cho ra cùng id. | Có | `policy/engine.py:254` |
| R-OBS-02 | Dòng audit **không** được chứa nội dung email, chỉ chứa id, điểm, rule và hash. | Có | `policy/engine.py:281-327` |
| R-OBS-03 | Mỗi chỉ số lỗi quan trọng **phải** có metric Prometheus. | Một phần: pipeline có (`failed_jobs_total`, `draft_validation_failures_total`, …), nhưng chưa thấy metric riêng của guard | `packages/observability/metrics.py:151-413` |
| R-OBS-04 | Log **phải** gắn `trace_id`, `message_id`, `job_id`, `org_id`. | Có | `consumer.py:313-336` |
| R-OBS-05 | Phải có ngưỡng cảnh báo (alert) cho DLQ, tỷ lệ fallback của guard, và p95 > 10 giây (NFR11/SC5). | **Chưa có**: chưa thấy rule alert nào | `docs/observability.md` mới có PromQL, chưa có alert |

## 6. Miền B: yêu cầu cho benchmark và nghiên cứu

### 6.1 Tính tái lập và pin

| Mã | Yêu cầu | Trạng thái | Căn cứ |
|---|---|---|---|
| R-REP-01 | Kit **phải** từ chối chạy khi image Docker không gắn đúng commit của checkout, hoặc khi checkout có file tracked đã sửa. | Có | `live/run.py:555-600`; sự cố I-03 (mục 8) |
| R-REP-02 | Kit **phải** từ chối chạy khi guard worktree không ở đúng commit đã pin, hoặc khi `git status` của nó không sạch. | Có | `guard_env.require_pinned_worktree` (`:281-292`) |
| R-REP-03 | Case, manifest và classifier L1 **phải** khớp sha256 trong `SHA256SUMS`, và scikit-learn phải đúng phiên bản 1.9.1. | Có | `kit/pinned.py:58-112`; `doctor.py:640, 657, 784` |
| R-REP-04 | Mỗi RUN **phải** có manifest ghi git SHA và hash cấu hình. Thí nghiệm không có manifest thì không được coi là kết quả. | Có | R22.12; `specs/design.md:1103` |
| R-REP-05 | Đổi dataset, policy, guard pin hoặc classifier **phải** dùng RUN mới. Không được ghi tiếp vào RUN cũ. | Có (quy ước, kit kiểm tra một phần) | `docs/BENCHMARK.md` (bản 2026-10-18) |
| R-REP-06 | Một thư mục RUN **không** được trộn scheme. Kit chỉ chạy scheme v3. | Có | `live/run.py:21-23`; ADR-0015 quyết định 6 |
| R-REP-07 | Nếu classifier L1 dùng trong RUN không phải bản owner đã pin (ví dụ bản train lại), sha256 của nó **phải** được ghi trong meta, và **phải** nêu rõ trong mọi bảng hay hình dùng kết quả đó. | Một phần: meta có ghi, nhưng nêu trên hình phải làm tay | `.agents/bench/mg_env.ps1` (`MAILGUARD_ARTIFACTS`); `make_figures.py` (chú thích) |
| R-REP-08 | Reranker của guard-worker **phải** lấy từ chính image của ai-worker và đọc offline, không được tải từ internet. | Có | `kit/campaign.py:371-510`; sự cố I-04 |

### 6.2 Định tuyến model, credit, chi phí

| Mã | Yêu cầu | Trạng thái | Căn cứ |
|---|---|---|---|
| R-RTE-01 | Model chạy qua OpenRouter **phải** ghim đúng một provider và tắt fallback. Một lời gọi do provider khác phục vụ **phải** thành error row, không được tính là phòng thủ thành công. | Có | `route.py`; ADR-0014; `models.yaml` |
| R-RTE-02 | Khi hết credit, hết quota hoặc chạm giới hạn chi tiêu, runner **phải** dừng ngay (`STOP`, mã thoát 3). Kit **phải** dừng cả campaign: không chạy config tiếp, không retry pass, không chuyển sang model khác cùng khóa. | Có | `route.py` (`IMMEDIATE_STOPS`, `ROUTE_STOP_EXIT`); `run_openrouter_1800.ps1` |
| R-RTE-03 | Ba lần liên tiếp provider không khớp hoặc không có provider **phải** dừng run đã ghim. | Có | `route.py` (`STOP_AFTER = 3`) |
| R-RTE-04 | Rate limit theo phút (HTTP 429 tạm thời) **phải** được backoff rồi retry, không được dừng run. | Có | `route.py` (docstring) |
| R-RTE-05 | Lời gọi guard lỗi vì đường truyền hay dịch vụ (402, 404, 408, 409, 5xx, timeout, body không phải JSON) **phải** thành error row (`guard_route_failure`). Model trả lời kém (không phải JSON, sai trường, từ chối) **phải** vẫn được chấm như hành vi của guard. | Có | `route.py`; `scoring.py:55-65` |
| R-RTE-06 | Trước khi chạy một model, người vận hành **phải** ước tính chi phí từ số đo thật và so với số dư. Nếu thiếu, **phải** báo owner trước, không đợi tới khi run bị `STOP`. | Một phần: hiện làm tay bằng `credits.ps1` | Sự cố I-06 |
| R-RTE-07 | Model đọc nghĩa (meaning reader) **không** được là một trong các model đang được benchmark. | Có | ADR-0012; `doctor.py:1047` |

### 6.3 Dữ liệu, license, rò rỉ

| Mã | Yêu cầu | Trạng thái | Căn cứ |
|---|---|---|---|
| R-DAT-01 | Nguồn dùng cho cả train L1 và benchmark **phải** chia theo `sha1(id) % 2`. Không bao giờ được dùng nửa train để đánh giá. | Có | `build_email_benchmark.py` (`bench_side`); `datasets.md` |
| R-DAT-02 | Mọi nguồn dữ liệu **phải** đăng ký trong `sources.py`, có license và URL, và mọi file đã tải **phải** có dòng trong `MANIFEST.json` (sha256, kích thước, thời điểm tải). | Có | `mailguard/datasets/download.py` |
| R-DAT-03 | Dữ liệu tổng hợp (`synthetic_poison_v1`) **phải** gắn nhãn nguồn riêng, và **phải** báo cáo tách khỏi dữ liệu thật trong mọi bảng và hình. Không được thêm dữ liệu giả mà không có quyết định của owner. | Có (quyết định A ngày 19/10; hình 3 tách theo nguồn) | `synthetic_poison.py`; `make_figures_bw.py` |
| R-DAT-04 | Dữ liệu không có license (xTRam1) chỉ được dùng để train, không được phân phối lại. Classifier L1 không được commit hay chia sẻ công khai. | Có | `NOTICE.md`; `datasets.md` |
| R-DAT-05 | Câu hỏi MS MARCO trong PoisonedRAG chỉ được dùng cho nghiên cứu phi thương mại. | Có (theo tài liệu) | `NOTICE.md` |
| R-DAT-06 | Khi phát hiện email benign trùng hoặc gần trùng với dữ liệu train của L1, báo cáo **phải** ghi rằng FPR có thể lạc quan. | Có (theo tài liệu): 138/150 case benign của run 29/09 gần trùng | `NOTICE.md` |

### 6.4 Bí mật và an toàn vận hành

| Mã | Yêu cầu | Trạng thái | Căn cứ |
|---|---|---|---|
| R-SEC-01 | Khóa API **chỉ** được nằm trong `.env` và `.env.stack`, là các file git-ignored. Không được in khóa ra log, terminal hay báo cáo. | Có | `live/stack_env.py`; `credits.ps1` chỉ in số dư |
| R-SEC-02 | Lệnh `package` của kit **phải** từ chối file zip có chứa `.env` hoặc giá trị khóa. | Có | `kit/campaign.py:1250` |
| R-SEC-03 | Biến `LLM__*`, `EMBEDDING__*`, `RETRIEVAL__*` không được export sẵn trong shell khi chạy campaign. | Có | `campaign.py` (`_refusals`) |
| R-SEC-04 | Sau khi chạy xong model cuối, người vận hành **phải** trả `.env` về cấu hình thường: comment các dòng `EMBEDDING__`, đặt `LLM__TIMEOUT_S=15.0` và `RETRIEVAL__CATEGORY_FILTER_ENABLED=true`, xóa `.env.stack`. Việc này cần owner xác nhận. | Một phần: kit có nhắc, nhưng chưa có kiểm tra | `campaign.py` (`RESTORE_ENV`) |

### 6.5 Vận hành benchmark

| Mã | Yêu cầu | Trạng thái | Căn cứ |
|---|---|---|---|
| R-OPS-01 | Chỉ được chạy một campaign tại một thời điểm trên một stack, vì chỉ có một guard-worker. | Có (quy ước) | `run_c0t_after.ps1` (chờ campaign trước) |
| R-OPS-02 | Tạm dừng hay tiếp tục **phải** không mất dữ liệu: case đã ghi được bỏ qua, config đã xong không chạy lại. | Có | `steplog.finished_configs`; sự cố I-07 |
| R-OPS-03 | Trong lúc tạm dừng, **không** được `git pull` hay build lại image, vì phần còn lại của config sẽ chạy trên code khác. | Có (kit từ chối khi commit lệch) | R-REP-01 |
| R-OPS-04 | Error row còn lại sau retry pass **phải** được báo cáo theo loại (`triage_stage_failure`, `fail_closed_validation`, …) và theo nguồn. Không được xóa hay sửa tay các dòng đó. | Một phần: kit đếm, phân loại phải làm tay | Sự cố I-05 |
| R-OPS-05 | Mọi con số trong bảng và hình **phải** đọc tự động từ `metrics.csv` hoặc file kết quả. Không gõ tay. Tổng theo nguồn **phải** khớp với tổng trong `metrics.csv`, lệch thì script phải dừng. | Có | `make_figures_bw.py` (`rag_by_source`) |
| R-OPS-06 | Thư mục RUN thử hỏng mà chưa ghi case nào được phép xóa. RUN đã có case thì **không** được xóa nếu owner chưa đồng ý. | Có (quy ước) | Sự cố I-03 |
| R-OPS-07 | Trong lúc run đang chạy, mọi thao tác đọc file kết quả (`raw/*.jsonl`) **phải** mở ở chế độ chia sẻ, không khóa ghi. Trên Windows: dùng `Get-Content`, không dùng `[System.IO.File]::ReadLines` hay công cụ khóa file. Tốt nhất là chỉ đọc khi config đó đã xong. | **Chưa có**: chỉ là quy ước, runner không chịu được khi bị khóa | Sự cố I-10 |

## 7. Khoảng trống cần ưu tiên

Thứ tự theo mức rủi ro. Cột **Quyết định** do nhóm chốt ngày 20/10/2026.

| # | Mã | Khoảng trống | Vì sao quan trọng | Đề xuất | Quyết định |
|---|---|---|---|---|---|
| 1 | R-POL-03 | Rule lỗi trong `policy.yaml` bị bỏ qua, chỉ ghi log | Một rule bảo vệ biến mất mà không ai biết là fail-open, mức SEV2 | Từ chối khởi động khi có rule lỗi, kèm test | **Sửa trong code**, sau khi benchmark Llama xong (xem ghi chú bên dưới) |
| 2 | R-OBS-05 | Chưa có alert nào | Sự cố chỉ được phát hiện khi có người nhìn log | Thêm alert Prometheus cho DLQ, fallback rate và p95 | Chuẩn bị câu trả lời (Q-LEC-14) |
| 3 | R-GRD-03 | Stage guard tắt khi khởi động nhưng run meta không ghi | Kết quả có thể là của một guard yếu hơn guard được mô tả | Ghi các stage đã tắt vào meta, và kit từ chối chạy nếu có | Chuẩn bị câu trả lời |
| 4 | R-OPS-04 | Error row phải phân loại tay | Giảng viên sẽ hỏi "loại bỏ 1.2% case có làm lệch kết quả không" | Script phân loại error row theo loại và nguồn, đưa vào report | Chuẩn bị câu trả lời (Q-LEC-10) |
| 5 | R-RTE-06 | Ước tính credit làm tay | Run dừng giữa đêm | Kiểm tra số dư trước khi chạy model trong script | Chuẩn bị câu trả lời |
| 6 | R-SEC-04 | Trả `.env` về cấu hình thường phải nhớ làm tay | Stack thường sẽ dùng endpoint embedding trả phí và timeout 60 giây | Thêm bước kiểm tra vào `bench-doctor` hoặc `make up` | Chuẩn bị câu trả lời; làm tay sau benchmark, có owner xác nhận |
| 7 | R-REP-07 | Ghi chú classifier train lại phải làm tay trên hình | Hình có thể bị đọc như kết quả của classifier đã pin | Hình tự lấy sha256 từ meta và in vào chú thích | Chuẩn bị câu trả lời (Q-LEC-07) |

**Ghi chú về khoảng trống số 1.** Bản sửa nằm trong `agentmailguard/mailguard/policy/engine.py`, thuộc guard worktree mà benchmark đang dùng. Kit bắt buộc worktree này sạch và đúng commit đã pin (R-REP-02). Vì vậy sửa lúc benchmark đang chạy sẽ làm kit từ chối các config còn lại, hoặc làm phần còn lại chạy trên code khác. Bản sửa chỉ được làm sau khi benchmark Llama xong. Sau khi sửa, guard sẽ lệch commit đã pin `d95e542`: mọi benchmark về sau phải pin lại và dùng RUN mới (R-REP-05). Việc pin lại cần owner quyết định.

## 8. Sổ sự cố thật (đợt benchmark 17–20/10/2026)

Đây là các sự cố có thật, dùng làm bằng chứng rằng các yêu cầu ở trên không phải lý thuyết.

| Id | Ngày | Mức | Chuyện gì xảy ra | Cơ chế nào bắt được | Yêu cầu liên quan | Xử lý |
|---|---|---|---|---|---|---|
| I-01 | 18/10 | SEV3 | Run Qwen (550 case) dừng giữa C0. Không còn tiến trình nào, log không có `STOP`. Có thể do terminal bị đóng | Không có cơ chế nào bắt: phát hiện vì file kết quả 11 phút không có dòng mới | R-OBS-05, R-OPS-02 | Chạy lại cùng script, kit tiếp từ case 268 |
| I-02 | 18/10 | SEV3 | Qwen trả category `automation_notification` (đúng phải là `automated_notification`). Triage dùng mặc định an toàn | `triage_stage_failure`, error row | R-PIP-06, R-OPS-04 | Ghi là lỗi của model. Không sửa schema, vì sửa sẽ làm lệch giữa các model |
| I-03 | 19/10 | SEV2 (bị chặn trước khi gây hại) | Sau `git pull`, image vẫn mang commit cũ `ecdf33d` | Kit từ chối: "container image(s) not built from this checkout's commit" | R-REP-01 | Build lại image gắn commit `790accb`, xóa thư mục RUN hỏng (chưa ghi case nào) |
| I-04 | 19/10 | SEV3 | Kit không xóa được `.cache/reranker` (WinError 5): Windows đánh dấu file read-only | `RerankModelError`, run dừng trước config đầu tiên | R-REP-08 | Gỡ cờ read-only, kit copy lại reranker từ image mới |
| I-05 | 19–20/10 | SEV3 | Qwen 1,800 case: mỗi config 22–32 error row, sau retry còn 19–24. Trong đó có 4 email benign của Enron | Error row được phân loại | R-OPS-04, R-DAT-03 | Báo cáo tỷ lệ lỗi theo loại và nguồn |
| I-06 | 19–20/10 | SEV3 (dự báo) | Credit OpenRouter không đủ cho Llama trên dataset 1,800 case | Ước tính tay từ `credits.ps1` | R-RTE-02, R-RTE-06 | Báo owner nạp thêm. Breaker sẽ `STOP` an toàn nếu thiếu |
| I-07 | 19–20/10 | SEV4 | Tạm dừng và tiếp tục nhiều lần theo yêu cầu owner | – | R-OPS-02, R-OPS-03 | Kit bỏ qua case đã ghi, commit không đổi trong lúc dừng |
| I-08 | 19/10 | SEV4 | Script vẽ hình lỗi: cận Wilson ra lệch 1e-15 khi đạt 60/60 | matplotlib từ chối `yerr` âm | R-OPS-05 | Ép cận về đúng 0 hoặc 100% khi k=0 hoặc k=n, số liệu không đổi |
| I-10 | 20/10 | SEV3 | Llama C2 dừng ở 940/1,800 với `PermissionError` khi runner ghi `C2.jsonl`. Nguyên nhân: người vận hành (Kiro) đọc file đó bằng `[System.IO.File]::ReadLines` trong lúc run đang ghi, lệnh này mở file ở chế độ chặn ghi | `FAIL C2: live.run exited 1`, kit chuyển sang config tiếp theo | R-OPS-07, R-OPS-02 | Kiểm tra 3 dòng cuối của file vẫn là JSON hợp lệ. Retry pass cuối campaign chạy tiếp C2 từ case thứ 941 |
| I-09 | 19/10 | SEV2 (quyết định thiết kế) | Commit `790accb` thêm 60 case tổng hợp, trái với yêu cầu ban đầu "không dùng dữ liệu giả" | Người vận hành đọc diff trước khi pull | R-DAT-03 | Dừng lại hỏi owner. Owner chọn phương án A: chạy đủ, báo cáo tách riêng |

## 9. Tiêu chí duyệt

Phần requirements được coi là duyệt khi:

1. Nhóm đồng ý với cách phân mức SEV1 đến SEV4 ở mục 3.
2. Nhóm đồng ý trạng thái "Có / Một phần / Chưa có" của từng yêu cầu, hoặc chỉ ra chỗ sai để sửa.
3. Nhóm chọn được những khoảng trống ở mục 7 sẽ sửa trong code, và những khoảng trống chỉ cần chuẩn bị câu trả lời.

Sau khi duyệt, các phần 2 đến 4 sẽ tham chiếu theo mã yêu cầu ở đây.

## 10. Tài liệu tham khảo

- NIST, [SP 800-61 Rev. 3: Incident Response Recommendations and Considerations for Cybersecurity Risk Management (CSF 2.0 Community Profile)](https://csrc.nist.gov/pubs/sp/800/61/r3/final), bản cuối tháng 4/2025. Bản này thay thế Rev. 2, và gắn xử lý sự cố vào các chức năng của CSF 2.0 (Govern, Identify, Protect, Detect, Respond, Recover). Phần protocol (`02-protocol.md`) sẽ dùng khung này.
- OWASP Gen AI Security Project, [OWASP Top 10 for LLM Applications 2025](https://genai.owasp.org/llm-top-10/). Các mục liên quan trực tiếp: LLM01 Prompt Injection (L1, L4), LLM02 Sensitive Information Disclosure (L4 redaction), LLM07 System Prompt Leakage (L4 kiểm tra 8-gram), LLM08 Vector and Embedding Weaknesses (L2 Doc Scanner).
- Tài liệu nội bộ: ADR-0012, ADR-0014, ADR-0015 (`docs/adr/`); `specs/requirements.md` (R6.11, R10.6, R16.1–R16.3, R18.1–R18.7, R22.12, NFR11, SC1–SC10); `docs/BENCHMARK.md`; `evaluation/mailguard_bench/pinned/NOTICE.md`; `agentmailguard/docs/datasets.md`.

Nội dung từ hai nguồn bên ngoài đã được diễn đạt lại để tuân thủ giới hạn bản quyền.
