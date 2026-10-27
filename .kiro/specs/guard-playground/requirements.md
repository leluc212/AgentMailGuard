# Requirements Document

## Introduction

Guard Playground là một ứng dụng web riêng (`services/guard_playground`, FastAPI + Jinja2 + htmx, dùng lại giao diện và CSS của Review UI) để trình diễn AgentMailGuard trước hội đồng đồ án. Người trình bày chọn một case mẫu (attack email, RAG poison, benign) hoặc dán email tự soạn, chọn một hoặc hai cấu hình guard (mặc định so sánh C0 với C3), chạy guard và xem kết quả theo từng layer (L1 Inbound Guard, L2 Doc Scanner, L3 Inbound Gate, L4 Output Guard, channel isolation), quyết định policy và bản draft trước/sau khi có guard. Với các case nằm trong bộ benchmark đã ghim, trang hiển thị thêm kết quả benchmark đã ghi nhận cho từng model để nối phần demo với số liệu trong báo cáo.

Vì ai-worker của stack thường không dùng guard và không có API endpoint hay bảng DB nào chứa GuardReport, Playground gọi trực tiếp thư viện guard (`mailguard.pipeline.MailGuardPipeline`) trong tiến trình của Playground_Service, theo cách `agentmailguard/scripts/scan_email.py` đang làm. Playground có hai chế độ: Live_Mode gọi model thật, Replay_Mode dùng kết quả tính sẵn cho một bộ case demo để demo vẫn chạy được khi offline hoặc khi API model lỗi.

Triển khai (đã chốt, cách 1): Playground_Service là một service Docker riêng `guard-playground` trong `docker-compose.yml`, chạy trên `127.0.0.1:3010`. Container của Review UI không chứa guard (`.dockerignore` loại `agentmailguard/` và `evaluation/`), nên Playground không chạy trong container đó. Guard, bộ case, kết quả benchmark, biểu đồ và classifier L1 được gắn vào container ở dạng chỉ đọc. Email trong Live_Mode chỉ đi qua guard, không đi qua pipeline rag-email (mail connector, triage, retrieval từ Postgres). Một lệnh dự phòng (Fallback_Launcher) chạy cùng ứng dụng trên máy, không cần Docker, chỉ ở Replay_Mode, để vẫn trình diễn được nếu Docker lỗi trong phòng bảo vệ.

Ngoài phạm vi (non-goals): không sửa pipeline ai-worker, không lưu GuardReport vào DB, không thêm hệ thống đăng nhập, không làm Grafana dashboard, không làm benchmark dashboard có lọc hay drill-down (phương án C, không được chọn; Requirement 18 chỉ gồm một dải thẻ số liệu và một Results_Page chỉ đọc với 6 biểu đồ đã có).

### Cơ sở thiết kế (design rationale)

Các yêu cầu về luồng demo dựa trên các nhận định sau từ tài liệu UX đã tham khảo:

- Hướng dẫn về độ tin cậy khi demo khuyên dùng dữ liệu chuẩn bị sẵn, có phương án dự phòng và có video ghi màn hình làm phương án cuối: Replay_Mode mặc định (Requirement 7) và kế hoạch dự phòng (Requirement 16).
- Hướng dẫn kể chuyện khi demo khuyên mở đầu bằng vấn đề và chỉ cho xem tính năng liên quan: Demo_Tour có thứ tự từ benign sang tấn công (Requirement 17) và Presenter_Mode ẩn chi tiết dành cho developer (Requirement 19).
- Hướng dẫn thiết kế security dashboard nhấn mạnh thứ bậc thông tin rõ ràng và đặt điểm rủi ro trong ngữ cảnh: kết luận đặt lên đầu (Requirement 12) và mỗi điểm số đi kèm ngưỡng so sánh (Requirement 15).
- Nghiên cứu cho thấy cách giải thích (explainability style) trong giao diện bảo mật ảnh hưởng đến việc người xem hiệu chỉnh mức tin tưởng: đánh dấu bằng chứng ngay trong văn bản (Requirement 14).
- Guardrail thường được mô tả như một pipeline nhiều tầng: thanh các Stage của pipeline (Requirement 13).
- Ứng dụng demo của Lakera có công tắc "simulate blocking" để so sánh chế độ chỉ giám sát với chế độ chặn: so sánh song song C0 với C3 (Requirement 3).

### Phạm vi và thứ tự triển khai

Buổi demo trước hội đồng là thứ Tư 28/10/2026 (3 ngày, tính từ chủ nhật 25/10/2026). Theo quyết định của nhóm (D3), **toàn bộ 21 requirement đều thuộc phạm vi của buổi demo**, không có requirement nào bị cắt. Nhãn trong tiêu đề chỉ quy định **thứ tự làm**, không quy định có làm hay không:

- **[MUST]**: làm trước. Khi xong toàn bộ nhóm này, đã có một bản demo chạy được trọn vẹn ở Replay_Mode.
- **[SHOULD]**: làm ngay sau nhóm [MUST], cũng phải xong trước buổi demo.

Requirement 7 có nhãn theo từng tiêu chí (Replay_Mode là [MUST], Live_Mode là [SHOULD]).

Thứ tự đề xuất: Requirement 9, 1, 3, 4, 5, 7 (phần Replay_Mode), 12, 13, 14, 15, 8, 10, 11, 16; sau đó Requirement 7 (phần Live_Mode), 2, 21, 6, 17, 18, 19, 20. Requirement 21 phụ thuộc Requirement 2 và phần Live_Mode của Requirement 7.

### Quyết định đã chốt (25/10/2026)

- D1: Giao diện bằng tiếng Anh (Requirement 10 tiêu chí 2).
- D2: Mặc định Replay_Mode khi mở trang (Requirement 7 tiêu chí 1).
- D3: Hạn demo là thứ Tư 28/10/2026. Làm đủ toàn bộ yêu cầu, không cắt bớt.
- D4: Presenter chọn được cả 3 Draft_Model trên giao diện (Requirement 7 tiêu chí 14 đến 17).
- D5: Giữ các giới hạn đầu vào như đề xuất (Requirement 8 tiêu chí 5).
- D6: Nhận External_Data do giảng viên đưa: dán, tải file, chạy theo lô (Requirement 21).
- D7: Có Results_Page trình bày số liệu benchmark và 6 biểu đồ cuối cùng (Requirement 18).
- D8: Triển khai bằng Docker theo cách 1 (Playground_Service là service Docker riêng trên 127.0.0.1:3010). Có Fallback_Launcher không cần Docker (Requirement 16). Phần "Fallback_Launcher chỉ ở Replay_Mode" được thay bởi D13.

- D9 (25/10/2026): Trong Live_Mode, mọi LLM judge của guard chạy trên chính Draft_Model đang chọn, giống ba lần chạy benchmark 19/10 (Requirement 7 tiêu chí 15).
- D10 (25/10/2026): Thêm `hypothesis` (phiên bản ghim) vào dev dependency để chạy property test.
- D11 (26/10/2026): Bảng màu Outcome_Banner an toàn cho người mù màu, không dùng cặp đỏ–xanh lá: BLOCKED đỏ đậm `#8B0000`, QUARANTINED tím đậm `#4A148C`, NEEDS HUMAN REVIEW cam `#E66100`, DRAFT ONLY xanh dương `#1A5FB4`, AUTO-SEND xanh ngọc `#006D77`, NO GUARD DECISION xám; mọi banner vẫn kèm chữ và icon khác hình (Requirement 12 tiêu chí 4).
- D12 (26/10/2026): Đơn giản hóa nhập Custom_Email: một ô Subject nhỏ, một ô body lớn (dán vào; nếu dòng đầu có dạng `Subject: ...` và ô Subject trống thì tự tách), KB chunk nằm trong phần "Advanced" gập sẵn, các nút mẫu một chạm lấy nguyên văn từ case thật của bộ benchmark (không tự bịa), và vùng thả file luôn có nút "Choose file" (Requirement 2, 21).
- D13 (26/10/2026): Fallback_Launcher bật được cả Live_Mode bằng tùy chọn `-Live`, Docker trở thành tùy chọn. Thay cho phần "chỉ Replay" của D8 (Requirement 16 tiêu chí 4, 8, 9).
- D15 (27/10/2026): Giao diện responsive cho việc zoom trình duyệt khi trình bày (100–400%, theo WCAG 1.4.10 Reflow): danh sách case lên trên ở ≤ 1.100 px, hai cột kết quả xếp dọc ở ≤ 900 px, stepper chuyển dọc ở ≤ 700 px; không có thanh cuộn ngang (Requirement 10 tiêu chí 8).
- D14 (26/10/2026): Replay_Store thật gồm Demo_Set × {C0, C3} × 3 Draft_Model. Chỉ chạy script tạo Replay sau khi người dùng cho phép (có chi phí API).

### Câu hỏi còn mở

Không còn câu hỏi mở.

## Glossary

- **Playground**: Trang `/guard`, trang `/guard/results` và các route liên quan của Playground_Service để chạy và hiển thị kết quả guard.
- **Playground_Service**: Ứng dụng FastAPI trong `services/guard_playground`, chạy như service Docker `guard-playground` trên 127.0.0.1:3010, hoặc qua Fallback_Launcher.
- **Review_UI**: Service `services/frontend` hiện có (FastAPI + Jinja2 + htmx), chạy trên 127.0.0.1:3001, không có login (ADR-0009).
- **Guard_Pipeline**: Thư viện guard `mailguard.pipeline.MailGuardPipeline` khởi tạo với `GuardConfig.preset(name)` và `audit=False`; `run(...)` trả về `(report, draft, bundle)`.
- **Guard_Package**: Toàn bộ mã trong `agentmailguard/` (gồm `mailguard/`, `configs/`, `evaluation/`), được ghim ở commit `d95e542`.
- **Guard_Settings**: Các giá trị cấu hình của Guard_Pipeline trong một Run (`mailguard.config.settings`), ví dụ `inbound_guard.block_threshold` (mặc định 0.85), `inbound_guard.flag_threshold` (0.50), `inbound_guard.strip_threshold` (0.50), `doc_scanner.quarantine_threshold` (0.70).
- **Preset**: Một cấu hình guard có tên: C0, C1, C2, C3, C3-L1, C3-L2, C3-L3, C3-L4, C3-ISO.
- **Sample_Case**: Một case lấy từ `evaluation/datasets/mailguard/cases.jsonl` (1.800 case) hoặc `tests/fixtures/emails.json`, gồm email và (với RAG poison) các KB chunk kèm theo.
- **Case_Kind**: Loại của Sample_Case: `attack` (LLMail-Inject), `rag_poison` (PoisonedRAG, seed, synthetic_poison_v1) hoặc `benign`.
- **Case_Source**: Nguồn gốc dữ liệu của Sample_Case (ví dụ LLMail-Inject, PoisonedRAG, seed, synthetic_poison_v1, fixture).
- **Custom_Email**: Email do Presenter nhập: subject, body và tùy chọn danh sách KB chunk.
- **Draft_Agent**: Thành phần tạo draft trả lời từ prompt do Guard_Pipeline dựng; trong Live_Mode là model LLM thật được chọn qua Draft_Model.
- **Draft_Model**: Model của Draft_Agent trong Live_Mode, là một trong đúng 3 lựa chọn: `gpt-4o-mini` (OpenAI API), `Qwen2.5-7B-Instruct` (OpenRouter, provider ghim Phala), `Llama-3.1-8B-Instruct` (OpenRouter, provider ghim CoreWeave, bf16). Các LLM judge của Guard_Pipeline chạy trên cùng Draft_Model, giống benchmark (D9).
- **Run**: Một lần chạy Guard_Pipeline trên một email với một Preset.
- **Run_Result**: Kết quả của một Run: verdict theo layer, inbound decision, final decision, stripped segments, quarantined chunks, guarded prompt, draft sau L4, nhãn chế độ, Draft_Model, các ngưỡng Guard_Settings đã dùng và trạng thái từng Stage.
- **Layer_Verdict**: Kết quả một layer trong `report.verdicts()`: layer, severity, score, decided_by, latency_ms, danh sách findings (detector, rule_id, score, technique, excerpt).
- **Policy_Decision**: Quyết định policy: action (quarantine, block, human_approval, draft_only, auto_send), matched_rule_id (P00..P10, P99), risk_tier, requires_human.
- **Outcome_Banner**: Dải kết luận đặt đầu mỗi cột kết quả, nêu kết quả cuối cùng của Run bằng văn bản tiếng Anh kèm biểu tượng.
- **Stage**: Một bước của Guard_Pipeline theo thứ tự: L1 Inbound Guard, L3 Inbound Gate (pass 1), L2 Doc Scanner, L3 Inbound Gate (pass 2), Channel isolation, Generation, L4 Output Guard.
- **Stage_Status**: Trạng thái của một Stage trong một Run: `passed`, `flagged`, `stopped here`, `skipped` hoặc `disabled by preset`.
- **Pipeline_Stepper**: Thanh ngang hiển thị 7 Stage theo thứ tự cùng Stage_Status của từng Stage.
- **Deciding_Layer**: Layer_Verdict có severity cao nhất khác `none` trong một Run (nếu bằng nhau thì lấy layer chạy sau cùng); nếu mọi severity là `none` hoặc Run không có Layer_Verdict thì Run không có Deciding_Layer.
- **Evidence_Highlight**: Đoạn văn bản trong email body hoặc KB chunk được đánh dấu tại chỗ vì trùng với excerpt của một finding hoặc với một stripped segment.
- **Unguarded_Draft**: Draft được tạo khi chạy với Preset C0.
- **Live_Mode**: Chế độ chạy Guard_Pipeline thật, có gọi model LLM (Draft_Agent và LLM judge).
- **Replay_Mode**: Chế độ hiển thị Run_Result tính sẵn từ Replay_Store, không gọi model.
- **Replay_Store**: Tập file Run_Result tính sẵn cho Demo_Set, nằm trong repo, chỉ đọc khi demo.
- **Demo_Set**: Danh sách Sample_Case được chọn lọc cho buổi demo.
- **Demo_Tour**: Danh sách có thứ tự gồm 4 đến 6 bước demo, mỗi bước gắn với một input (Sample_Case thuộc Demo_Set hoặc Custom_Email), một cặp Preset và một câu talking point.
- **KPI_Strip**: Dải tối đa 4 thẻ số liệu chỉ đọc lấy từ `metrics.csv` của ba lần chạy benchmark 1.800 case.
- **Defense_Rate**: 100 − ASR, tính theo phần trăm.
- **Presenter_Mode**: Chế độ hiển thị cho máy chiếu: chữ lớn hơn và ẩn chi tiết dành cho developer.
- **Benchmark_Record**: Kết quả benchmark đã ghi của một case pinned cho một model và một Preset, lấy từ `evaluation/results/mailguard_bench/2026-10-19-*-v3-1800/ragemail__<config>.jsonl` (goal_achieved, blocked, rule).
- **Playground_Docs**: Tài liệu vận hành Playground (cách chạy, giới hạn an toàn, kế hoạch dự phòng cho buổi demo).
- **Presenter**: Thành viên nhóm điều khiển demo.
- **Committee_Viewer**: Thành viên hội đồng xem demo.
- **Maintainer**: Developer bảo trì Playground.
- **External_Data**: Email do người ngoài nhóm đưa vào (ví dụ giảng viên): dán vào Custom_Email hoặc tải lên dạng file `.eml`, `.json` hay `.jsonl` (Requirement 21).
- **Batch_Run**: Một lần chạy mọi email hợp lệ của một file External_Data với các Preset đang chọn và Draft_Model đang chọn.
- **Results_Page**: Trang `/guard/results` trình bày số liệu benchmark và 6 biểu đồ cuối cùng, chỉ đọc.
- **Fallback_Launcher**: Lệnh `scripts/playground/run_playground.ps1` chạy Playground_Service trên Python của máy, không cần Docker; mặc định chỉ Replay_Mode (`-ReplayOnly`), thêm `-Live` để bật Live_Mode (D13).

## Requirements

### Requirement 1: Chọn Sample_Case [MUST]

**User Story:** As a Presenter, I want to pick a sample case by kind and source, so that I can show attack, RAG poison and benign examples without typing.

#### Acceptance Criteria

1. WHEN the Presenter opens `/guard`, THE Playground SHALL display, within 3 seconds, a Sample_Case list containing every Sample_Case loaded from `cases.jsonl` and `tests/fixtures/emails.json`, together with a Case_Kind filter (values: all, `attack`, `rag_poison`, `benign`), a Case_Source filter (values: all, plus each distinct Case_Source present in the loaded Sample_Cases), both filters set to "all", and a count of the Sample_Cases currently listed.
2. WHEN the Presenter changes the value of the Case_Kind filter or the Case_Source filter, THE Playground SHALL, within 1 second, display only the Sample_Cases whose Case_Kind and Case_Source both match the selected filter values (a filter set to "all" matches every Sample_Case) and update the displayed count to the number of listed Sample_Cases.
3. WHEN the Presenter selects a Sample_Case, THE Playground SHALL display the case id, Case_Kind, Case_Source, subject, the full body without truncation, and the full text of each attached KB chunk of the Sample_Case; for a Sample_Case with zero attached KB chunks, THE Playground SHALL display an indication that no KB chunk is attached.
4. THE Playground SHALL display a visible Demo_Set marker next to each Sample_Case in the Sample_Case list that belongs to the Demo_Set, and SHALL display no Demo_Set marker next to any Sample_Case that does not belong to the Demo_Set.
5. IF the dataset file `cases.jsonl` is missing or unreadable, THEN THE Playground SHALL hide the Sample_Case list and filters, display an error message naming the missing or unreadable file, and keep the Custom_Email input available and usable.
6. IF the selected Case_Kind and Case_Source filter values match zero Sample_Cases, THEN THE Playground SHALL display an empty-list message, show a count of 0, and keep both filters available for changing.
7. IF a line in `cases.jsonl` cannot be parsed or lacks a case id, Case_Kind, subject or body, THEN THE Playground SHALL exclude that line from the Sample_Case list, display all remaining valid Sample_Cases, and display a warning stating the number of excluded lines.

### Requirement 2: Nhập Custom_Email [SHOULD]

**User Story:** As a Presenter, I want to paste my own email and optional KB chunks, so that I can answer committee questions such as "what if the attacker writes this?".

#### Acceptance Criteria

1. THE Playground SHALL provide on the `/guard` page one single-line input field for the subject, one multi-line input field for the body, and, inside a collapsed "Advanced" section, from 0 to 5 KB chunk input fields of a Custom_Email, with a control to add a KB chunk field (disabled once 5 fields exist) and a control to remove each KB chunk field (decision D12).
7. WHEN the Presenter submits a Custom_Email whose subject field is empty and whose body's first line starts with "Subject:" (case-insensitive), THE Playground SHALL use the text after "Subject:" on that line, trimmed, as the subject and the remaining lines, with leading blank lines removed, as the body.
8. THE Playground SHALL provide from 3 to 6 one-click sample buttons for the Custom_Email, each labelled in English with the kind of input it loads; activating a button SHALL fill the subject, body and KB chunk fields with the verbatim subject, body and chunks of one named Sample_Case from `cases.jsonl` and SHALL start no Run.
2. IF the Presenter submits a Custom_Email whose body is empty or contains only whitespace characters, THEN THE Playground SHALL reject the submission without starting a Run, display a validation message next to the body field indicating that the body is required, and retain the entered subject and KB chunk values in their input fields.
3. IF the Presenter submits a Custom_Email in which any KB chunk field is empty or contains only whitespace characters, THEN THE Playground SHALL exclude that KB chunk from the submission and treat the remaining non-empty KB chunks as the submitted KB chunks.
4. WHEN the Presenter submits a Custom_Email in Live_Mode whose body is non-empty and whose subject, body and KB chunks are within the limits defined in Requirement 8 criterion 5, THE Playground SHALL start a Run using the submitted subject (an empty subject is accepted), body and non-empty KB chunks exactly as entered, in the order entered.
5. WHEN a Run for a Custom_Email completes, THE Playground SHALL keep the submitted subject, body and KB chunk values in their input fields so that the Presenter can edit and resubmit them.
6. WHILE Replay_Mode is active, THE Playground SHALL disable the Run submission control for Custom_Email, keep the Custom_Email input fields editable, and display a message stating that Custom_Email requires Live_Mode.

### Requirement 3: Chọn Preset và so sánh [MUST]

**User Story:** As a Presenter, I want to compare two guard configurations side by side, so that the committee sees the difference the guard makes.

#### Acceptance Criteria

1. THE Playground SHALL offer exactly the 9 Presets C0, C1, C2, C3, C3-L1, C3-L2, C3-L3, C3-L4 and C3-ISO for selection, in that order, in each Preset selector (one selector per result column in comparison mode, one selector in single mode).
2. WHEN the Presenter opens `/guard`, THE Playground SHALL preselect comparison mode with Preset C0 selected for the left column and Preset C3 selected for the right column, before any Run is started.
3. WHILE comparison mode is selected, WHEN the Presenter starts a comparison, THE Playground SHALL execute exactly 2 Runs, one with the Preset selected for the left column and one with the Preset selected for the right column, using the same email and the same set of KB chunks for both Runs.
4. IF the Runs of a comparison would use different emails or different sets of KB chunks, THEN THE Playground SHALL prevent both Runs from starting, display an error message identifying the input that differs (email or KB chunks), and keep the current Preset selections and inputs unchanged.
5. WHERE single mode is selected, WHEN the Presenter starts a Run, THE Playground SHALL execute exactly 1 Run with the single selected Preset and display 1 result column.
6. WHEN a Run_Result is displayed, THE Playground SHALL label its result column with the Preset name, the mode (Live_Mode or Replay_Mode) and the Draft_Model that produced that Run_Result.
7. IF one Run of a comparison fails, THEN THE Playground SHALL display an error message in that Run's result column indicating the Run failed, and SHALL still display the Run_Result of the other Run in its own column.
8. WHILE a Run or comparison is executing, THE Playground SHALL disable starting another Run and disable changing the Preset selections, the Draft_Model selection and the mode until every Run in progress has produced a Run_Result or failed.
9. WHEN the Presenter switches between comparison mode and single mode, THE Playground SHALL clear the displayed Run_Results and keep the email and KB chunks inputs unchanged.

### Requirement 4: Hiển thị kết quả theo layer [MUST]

**User Story:** As a Committee_Viewer, I want to see what each guard layer detected, so that I understand how the defense works step by step.

#### Acceptance Criteria

1. WHEN a Run completes, THE Playground SHALL display, below the Outcome_Banner (Requirement 12), one row per Layer_Verdict returned by `report.verdicts()`, in pipeline order (L1 Inbound Guard, L2 Doc Scanner, L4 Output Guard), showing layer, severity, score rounded to 2 decimal places in the range 0.00 to 1.00 together with its threshold (Requirement 15), decided_by and latency_ms as an integer number of milliseconds.
2. WHEN a Layer_Verdict contains one or more findings, THE Playground SHALL display detector, rule_id, score rounded to 2 decimal places, technique and excerpt (up to the 400 characters stored in the finding) for each finding, and SHALL display "—" for a rule_id or technique that has no value.
3. WHEN the L1 Inbound Guard verdict contains one or more stripped segments, THE Playground SHALL display the extracted user intent, the text and strip reason of each stripped segment, and the removed_ratio as a percentage with 1 decimal place.
4. WHEN the L2 Doc Scanner verdict contains one or more quarantined KB chunks, THE Playground SHALL display each quarantined chunk id with the rule_id and technique of that chunk's highest-score finding as the quarantine reason, and the quarantine ratio as quarantined chunks over scanned chunks (count and percentage with 1 decimal place).
5. WHERE the Presenter enables the prompt view, THE Playground SHALL display the full guarded prompt (`bundle.messages[1].content`) without truncation, with channel isolation datamarks shown verbatim as escaped text.
6. THE Playground SHALL display, for each Layer_Verdict, a severity indicator that shows the severity text label (none, low, medium, high or critical) together with a color, so that the severity can be read from the text label alone.
7. WHILE a Guard_Pipeline LLM judge used by a layer is not configured, THE Playground SHALL label that layer's Layer_Verdict row as "judge not configured" and display the Layer_Verdict produced by the remaining non-LLM stages.
8. IF a Layer_Verdict has an error value or an llm_fallback mark, THEN THE Playground SHALL display an error label on that row with the error text or the fallback reason, and keep displaying the severity, score and findings that the Layer_Verdict contains.
9. IF a layer did not run in a Run because the Preset disables it or because the Run stopped before that layer, THEN THE Playground SHALL display a row for that layer labeled "not run" with the reason (disabled by Preset, or stopped by the policy action), instead of omitting the layer.
10. IF a Layer_Verdict contains zero findings, THEN THE Playground SHALL display "no findings" for that Layer_Verdict.

### Requirement 5: Hiển thị Policy_Decision và draft [MUST]

**User Story:** As a Committee_Viewer, I want to see the final decision and compare the guarded draft with the unguarded draft, so that I see the practical effect of the guard.

#### Acceptance Criteria

1. WHEN a Run completes, THE Playground SHALL display the inbound Policy_Decision in the result column of that Run, labelled "inbound", with action (one of quarantine, block, human_approval, draft_only, auto_send), matched_rule_id, risk_tier and requires_human shown as a text value "yes" or "no".
2. WHEN a Run completes, THE Playground SHALL display exactly one Policy_Decision labelled "final decision" in the result column of that Run, where the final decision is the outbound Policy_Decision if the Run produced one, and the inbound Policy_Decision otherwise.
3. WHEN the Playground displays a Policy_Decision, THE Playground SHALL display the `reason` text of the rule whose `id` equals the matched_rule_id in `configs/policy.yaml` of the Guard_Package, in the same block as that Policy_Decision.
4. WHEN a Run produces a draft that passed L4 Output Guard, THE Playground SHALL display the full draft text after L4 Output Guard without truncation, labelled with the Preset name of that Run.
5. WHEN a Run stops before draft generation because L3 Inbound Gate returned block or quarantine, THE Playground SHALL display the stopping action, the matched_rule_id, and a statement that no draft was generated, and SHALL NOT display the fixed placeholder draft as a model-generated draft.
6. WHILE comparison mode includes Preset C0 and one other Preset, THE Playground SHALL display the Unguarded_Draft, labelled "Unguarded_Draft (C0)", and the draft or the no-draft statement of the other Preset in two adjacent columns of the same row.
7. IF the matched_rule_id of a Policy_Decision has no matching rule `id` in `configs/policy.yaml`, THEN THE Playground SHALL display the Policy_Decision with the matched_rule_id and a message stating that the rule reason was not found, instead of an empty reason.
8. IF one Run of a comparison fails without producing a Run_Result, THEN THE Playground SHALL display an error message naming the failed Preset in that Run's column, and SHALL keep the Policy_Decision and draft of the other Run displayed.

### Requirement 6: Đối chiếu Benchmark_Record [SHOULD]

**User Story:** As a Committee_Viewer, I want to see what the recorded benchmark said about the same case, so that the live demo connects to the numbers in the thesis.

#### Acceptance Criteria

1. WHEN the Presenter selects a pinned Sample_Case (a Sample_Case taken from `evaluation/datasets/mailguard/cases.jsonl`), THE Playground SHALL display a Benchmark_Record table with exactly 3 model rows (gpt-4o-mini, Llama 3.1 8B, Qwen 2.5 7B) and one column per Preset currently shown in the result columns (1 column in single mode, 2 columns in comparison mode).
2. THE Playground SHALL show in each Benchmark_Record cell goal_achieved as "yes" or "no", blocked as "yes" when the record is blocked inbound or blocked outbound (naming the stage: inbound or outbound) and "no" otherwise, and rule as the recorded rule id or "none" when the record contains no rule.
3. THE Playground SHALL display, for each model row, the name of the result folder the Benchmark_Records were read from, including the run date (2026-10-19) and the model identifier contained in the folder name.
4. IF the result file for a model and Preset is absent, or contains no record whose case id equals the selected pinned Sample_Case id, THEN THE Playground SHALL display "no record" in that cell and keep displaying all other cells.
5. IF the Presenter selects a Custom_Email or a Sample_Case taken from `tests/fixtures/emails.json`, THEN THE Playground SHALL hide the Benchmark_Record section.
6. THE Playground SHALL read Benchmark_Records with zero modified, created or deleted files under `evaluation/results/`.
7. IF a result file is unreadable or the line holding the selected case fails to parse, THEN THE Playground SHALL display "record unreadable" in the affected cells, keep displaying all other cells, and keep the Run_Result columns unchanged.
8. WHILE either Live_Mode or Replay_Mode is active, THE Playground SHALL display the Benchmark_Record section with a heading identifying it as recorded benchmark results, visually separate from the Run_Result columns, and SHALL produce it without calling any model API.

### Requirement 7: Live_Mode và Replay_Mode [MUST: tiêu chí 1–3, 9–13; SHOULD: tiêu chí 4–8, 14–17]

**User Story:** As a Presenter, I want a replay mode with precomputed results, so that the demo works even without internet or when a model API fails.

#### Acceptance Criteria

1. [MUST] THE Playground SHALL provide on the `/guard` page a mode switch with exactly two options, Live_Mode and Replay_Mode, and SHALL select Replay_Mode by default on each page load.
2. [MUST] WHILE Replay_Mode is active, WHEN the Presenter runs a Demo_Set case with a Preset, THE Playground SHALL display the Run_Result for that case and Preset from the Replay_Store within 2 seconds and SHALL make zero model API calls.
3. [MUST] WHILE Replay_Mode is active, THE Playground SHALL display a "REPLAY" label that stays visible for as long as Replay_Mode is active, together with the date and time (to the minute) at which the displayed Replay_Store entry was generated and the Draft_Model recorded in that entry.
4. [SHOULD] WHILE Live_Mode is active, THE Playground SHALL display a "LIVE" label that stays visible for as long as Live_Mode is active, together with the name of the currently selected Draft_Model.
5. [SHOULD] IF a Live_Mode Run fails because a model API call fails (connection error, authentication error, rate limit, or error response from the provider), THEN THE Playground SHALL display an error message stating which of these categories occurred, SHALL not display a partial Run_Result, and SHALL re-enable the run button with the selected case and Preset unchanged.
6. [SHOULD] IF a Live_Mode Run fails or times out and the Replay_Store contains a valid entry for the same case and Preset, THEN THE Playground SHALL show an option to display that Replay_Store Run_Result, and SHALL label the result "REPLAY" with its generation timestamp once the Presenter selects the option.
7. [SHOULD] IF a Live_Mode Run has not finished within 120 seconds of starting, THEN THE Playground SHALL stop waiting for the Run, display a message saying the Run timed out after 120 seconds, and re-enable the run button.
8. [SHOULD] WHILE a Live_Mode Run is in progress, THE Playground SHALL display a progress indicator (detailed in Requirement 20) and disable the run button, the mode switch and the Draft_Model selector until the Run completes, fails, or times out.
9. [MUST] THE Playground SHALL provide a command-line script that runs every Demo_Set case with every Preset in Live_Mode, using a Draft_Model given as a script option (default `gpt-4o-mini`), and writes one Replay_Store entry per case and Preset pair, each recording its generation timestamp and Draft_Model name.
10. [MUST] IF the regeneration script fails to produce a Run_Result for a case and Preset pair, THEN THE script SHALL keep the existing Replay_Store entry for that pair unchanged, continue with the remaining pairs, and on completion print the failed pairs and exit with a non-zero status.
11. [MUST] FOR ALL Run_Results in the Replay_Store, serializing and then deserializing a Run_Result SHALL produce a Run_Result whose layer verdicts, inbound decision, final decision, stripped segments, quarantined chunks, guarded prompt, draft after L4, mode label, Draft_Model, recorded thresholds and Stage_Status values are all equal to the original's.
12. [MUST] IF a Replay_Store file fails schema validation, THEN THE Playground SHALL skip that entry without displaying any of its content, mark the matching case and Preset as "replay unavailable", and keep loading the remaining valid entries.
13. [MUST] IF Replay_Mode is active and the selected input is a Custom_Email or a Sample_Case and Preset pair with no valid Replay_Store entry, THEN THE Playground SHALL disable the run button and display "replay unavailable", making no model API call.
14. [SHOULD] WHILE Live_Mode is active, THE Playground SHALL provide a Draft_Model selector offering exactly 3 options in this order: `gpt-4o-mini` (OpenAI API), `Qwen2.5-7B-Instruct` (OpenRouter, provider Phala), `Llama-3.1-8B-Instruct` (OpenRouter, provider CoreWeave, bf16), with `gpt-4o-mini` selected on each page load.
15. [SHOULD] WHEN the Presenter starts a Live_Mode Run, THE Playground SHALL generate the draft with the selected Draft_Model, routing an OpenRouter Draft_Model only to its pinned provider (Phala for `Qwen2.5-7B-Instruct`, CoreWeave with bf16 for `Llama-3.1-8B-Instruct`), and SHALL run every Guard_Pipeline LLM judge (inbound judge, inbound extractor, doc judge, doc set judge, output judge) on the same selected Draft_Model and route, as the 2026-10-19 benchmark runs did (decision D9), recording the judge model of each judge in the Run_Result.
16. [SHOULD] IF the credential required by the selected Draft_Model (OpenAI API key or OpenRouter API key) is not configured, THEN THE Playground SHALL disable the run button in Live_Mode and display a message naming the Draft_Model whose credential is missing, without displaying any credential value.
17. [SHOULD] THE regeneration script of criterion 9 SHALL accept the option `--model all` and, with it, write one Replay_Store entry per Demo_Set case, Preset and Draft_Model, so that Replay_Mode can show each of the 3 Draft_Models; WHILE Replay_Mode is active, THE Draft_Model selector SHALL offer only the Draft_Models that have a valid Replay_Store entry for the selected case and Preset.

### Requirement 8: An toàn [MUST]

**User Story:** As a Maintainer, I want the playground to stay local and side-effect-free, so that a demo cannot leak secrets, send email or corrupt data.

#### Acceptance Criteria

1. THE Playground_Service SHALL be published only on 127.0.0.1:3010 without a login, consistent with ADR-0009, both as the Docker service `guard-playground` and through the Fallback_Launcher, such that a connection attempt to the Playground from any non-loopback network address is refused.
2. THE Playground_Docs SHALL state that the Playground has no authentication and is reachable only from 127.0.0.1.
3. THE Playground SHALL exclude the value of every configured API key, token and password (including values supplied via environment variables or `.env` files) from every rendered page, Run_Result, Replay_Store file and log line, so that a search of these outputs for any configured credential value returns zero matches.
4. IF an LLM provider call made by the Playground fails and the provider error contains a configured credential value, THEN THE Playground SHALL display and log the error with the credential value removed and SHALL mark the affected step as failed in the Run_Result.
5. IF a Custom_Email subject exceeds 500 characters, a body exceeds 20,000 characters, the chunk count exceeds 5, or a chunk exceeds 4,000 characters, THEN THE Playground SHALL reject the submission without starting a run, display every violated limit in one validation message, and keep the entered subject, body and chunks in the form.
6. IF a Custom_Email submission has a body containing only whitespace, THEN THE Playground SHALL reject the submission without starting a run and display a validation message identifying the empty body field; an empty subject is accepted (Requirement 2 criterion 4).
7. THE Playground SHALL keep every generated draft only inside the Run_Result displayed on the page and SHALL make zero calls to any mail-sending path or outbound API of the normal stack other than the configured LLM providers used by the Guard_Pipeline and the Draft_Agent.
8. THE Playground SHALL perform zero writes to the rag-email database, the message broker or folders under `evaluation/results/`, such that the row counts, queued message counts and file listings of these targets are identical before and after any Playground run.
9. THE Playground SHALL HTML-escape the characters `&`, `<`, `>`, `"` and `'` in every email body, chunk, excerpt, prompt and draft before rendering, including text inside Evidence_Highlights (Requirement 14), so that HTML or script markup in these values is displayed as literal text and is not executed.
10. IF a Guard_Pipeline LLM judge has no credentials configured, THEN THE Playground SHALL display on the `/guard` page the name of each judge that is not configured each time the page is loaded.
11. IF a run is started while a Guard_Pipeline LLM judge has no credentials configured, THEN THE Playground SHALL skip that judge, complete the run with the remaining Guard_Pipeline layers, and mark that judge as not configured in the Run_Result.

### Requirement 9: Cô lập khỏi Guard_Package và benchmark [MUST]

**User Story:** As a Maintainer, I want the playground to only import the guard, so that the pinned benchmark at commit d95e542 stays reproducible.

#### Acceptance Criteria

1. THE Playground SHALL place all new source files, templates, static assets, Replay_Store files and the Demo_Tour definition under `services/guard_playground/`, its tests under `tests/unit/`, and its launch and Replay_Store regeneration scripts under `scripts/playground/`, with zero new files inside the `agentmailguard/` directory.
2. THE Playground SHALL use the Guard_Pipeline only through Python imports, such that a diff of the `agentmailguard/` directory against commit `d95e542` shows zero added, modified or deleted files.
3. WHILE the Playground is running, THE Playground SHALL write zero files inside the `agentmailguard/` directory, including during Live_Mode Runs and during Replay_Store regeneration.
4. WHEN the Playground or the Replay_Store regeneration script constructs a Guard_Pipeline, THE Playground SHALL pass `audit=False` in every construction, so that zero audit records are written by any Run.
5. THE Playground SHALL leave the ai-worker pipeline and the benchmark guard-worker code paths unchanged, such that a diff of their source files and configuration files against the commit preceding the Playground work shows zero modified files.
6. THE Playground SHALL keep the existing Review_UI routes (drafts, timeline, knowledge, health) and their responses unchanged, such that every Review_UI test that passed before the Playground work passes after it with no test modified.
7. THE Playground SHALL limit changes to the Review_UI's `templates/base.html` to exactly one added "Guard" navigation entry linking to `http://127.0.0.1:3010/guard`, with zero other lines changed in that file.
8. IF the Guard_Package cannot be imported when the Playground_Service starts or when `/guard` is requested, THEN THE Playground_Service SHALL keep serving its health route and display on `/guard` an error message stating that the guard library is unavailable, and the Review_UI SHALL keep serving normally.
9. THE Playground SHALL leave the shared `Dockerfile` and `.dockerignore` unchanged; the `guard-playground` service SHALL get the Guard_Package, `evaluation/datasets/`, `evaluation/results/`, `paper/figures/final/` and the L1 classifier artifact through read-only bind mounts declared in `docker-compose.yml`.
10. WHEN the Playground_Service runs as the Docker service `guard-playground`, THE Playground_Service SHALL start without depending on the api, postgres, rabbitmq or minio services, so that the Playground works while the rest of the stack is stopped.

### Requirement 10: Giao diện [MUST]

**User Story:** As a Committee_Viewer, I want a readable page on a projector, so that I can follow the demo from a distance.

#### Acceptance Criteria

1. THE Playground SHALL use the same stack as the Review_UI (FastAPI, Jinja2 templates, htmx, and the Review_UI's `review.css` and `htmx.min.js` served as shared static files) and SHALL serve every page without requiring a Node installation, a Node package install, or a Node build step.
2. THE Playground SHALL render 100% of UI labels, button texts, headings, placeholders, Outcome_Banner texts, Stage_Status texts, Demo_Tour talking points, and system-generated status, validation and error messages in English, with zero such texts in Vietnamese or any other language. Email content, KB chunk content and model output being displayed are excluded from this rule.
3. WHEN the Playground page is displayed in a browser viewport of 1280 × 720 pixels at 100% zoom, with Presenter_Mode on or off, THE Playground SHALL show both comparison columns side by side, each fully visible in width, with no horizontal scrollbar on the page.
4. THE Playground SHALL make every form control reachable with the Tab and Shift+Tab keys in on-screen reading order, operable with Enter or Space (and arrow keys for selection controls), and associated with a visible text label displayed next to or above the control.
5. WHEN a form control receives keyboard focus, THE Playground SHALL display a visible focus indicator on that control that remains visible until focus moves to another element.
6. THE Playground SHALL render body text and comparison-column content at a computed font size of at least 18 pixels and headings at a computed font size of at least 24 pixels at 100% zoom.
7. THE Playground SHALL render all text with a contrast ratio against its background of at least 4.5:1 for body text and at least 3:1 for headings of 24 pixels or larger.
8. WHEN the Presenter zooms the browser from 100% to 400% on a 1280 × 720 screen (a CSS viewport down to 320 pixels wide), THE Playground SHALL reflow the `/guard` and `/guard/results` pages without a horizontal page scrollbar and without hiding content: the case list moves above the results at widths of 1,100 pixels or less, the two comparison columns stack (left column first) at widths of 900 pixels or less, and the Pipeline_Stepper becomes vertical at widths of 700 pixels or less (decision D15).

### Requirement 11: Kiểm thử [MUST]

**User Story:** As a Maintainer, I want automated tests for the new routes, so that changes do not break the demo.

#### Acceptance Criteria

1. THE Playground SHALL have at least one pytest test per new route that asserts a successful response and the expected rendered content. Each test SHALL call the Playground_Service application in-process, using the same transport style as the existing frontend tests, without starting a server process or opening a listening port.
2. THE Playground tests SHALL replace the Draft_Agent and every Guard_Pipeline LLM judge with stubs that return fixed outputs. Every test SHALL pass with all model credential environment variables unset and with outbound network connections blocked. A test that attempts an outbound network connection SHALL fail.
3. THE Playground tests SHALL cover Replay_Mode rendering of at least one Demo_Set case from the Replay_Store, including the "REPLAY" label and the Replay_Store generation timestamp. They SHALL also cover Live_Mode rendering with a stubbed Guard_Pipeline, including the "LIVE" label, the selected Draft_Model name in the label and in each result column, and each layer's verdict.
4. THE Playground tests SHALL verify input-limit rejection for each Requirement 8 limit: subject longer than 500 characters, body longer than 20,000 characters, more than 5 chunks, chunk longer than 4,000 characters, and empty body. For each case, the test SHALL check that no Run is started and that the validation message names every violated limit. The tests SHALL also verify that inputs exactly at each limit (500, 20,000, 5 and 4,000) and an empty subject are accepted.
5. THE Playground tests SHALL set sentinel credential values in the test environment. They SHALL then assert that none of these sentinel values appears in any rendered page, Run_Result or captured log output that the new routes produce in Live_Mode and Replay_Mode.
6. THE Playground tests SHALL verify that the Guard_Pipeline is constructed with `audit=False`.
7. THE Playground tests SHALL record the set of files and their modification times under `evaluation/results/` before a Run. After the Run completes, they SHALL assert that no file under that folder was created, modified or deleted.
8. THE Playground tests SHALL verify error-path rendering for three cases: a stubbed model API failure in Live_Mode (the error category is shown, plus the Replay_Mode offer when a Replay_Store entry exists), a stubbed Run lasting longer than 120 seconds (a timeout message is shown), and a Replay_Store file that fails schema validation (the case is shown as "replay unavailable").
9. WHEN the Maintainer runs the Playground test suite with pytest, THE Playground tests SHALL finish in 60 seconds or less in total, with no manual setup beyond installing the frontend test dependencies.
10. THE Playground tests SHALL verify, for each of the 5 Policy_Decision actions, the Outcome_Banner text defined in Requirement 12 criterion 2, and SHALL verify the Stage_Status sequence of Requirement 13 for at least one Run that stops at L3 Inbound Gate (pass 1) and one Run that reaches L4 Output Guard.
11. THE Playground tests SHALL verify that an excerpt containing HTML markup is rendered as escaped literal text inside its Evidence_Highlight (Requirement 14 and Requirement 8 criterion 9).

### Requirement 12: Verdict-first result layout [MUST]

**User Story:** As a Committee_Viewer, I want each result column to start with a plain statement of the outcome, so that I understand what the guard decided before reading the details.

#### Acceptance Criteria

1. WHEN a Run_Result is displayed, THE Playground SHALL display exactly one Outcome_Banner as the first element of that Run's result column, above the Pipeline_Stepper, the Layer_Verdict rows, the Policy_Decision blocks and the draft, with Presenter_Mode on or off, and independently for each of the two comparison columns.
2. WHEN a Run_Result with a final Policy_Decision (Requirement 5 criterion 2) is displayed, THE Playground SHALL set the Outcome_Banner text, in upper case and with no other outcome wording, from the action of that final Policy_Decision using exactly this mapping: block → "BLOCKED", quarantine → "QUARANTINED", human_approval → "NEEDS HUMAN REVIEW", draft_only → "DRAFT ONLY", auto_send → "AUTO-SEND".
3. WHEN a Run_Result with a final Policy_Decision is displayed, THE Playground SHALL display inside the same Outcome_Banner, on the same line as or directly below the outcome text, the matched_rule_id of the final Policy_Decision (one of P00..P10, P99) and the full `reason` text of the rule with that `id` in `configs/policy.yaml`, without truncation, or the "rule reason not found" message of Requirement 5 criterion 7 when no rule with that `id` exists.
4. THE Playground SHALL display in each Outcome_Banner an icon, the outcome text and a background or border color, where each of the 6 possible banner outcomes (the 5 actions of criterion 2 and "NO GUARD DECISION" of criterion 5) uses an icon of a different shape from the other 5, each icon carries a text alternative equal to the outcome text, and the outcome text has a contrast ratio of at least 4.5:1 against the banner background.
5. IF a Run_Result contains no final Policy_Decision (for example a Run with Preset C0, whose guard layers are all disabled), THEN THE Playground SHALL display an Outcome_Banner with the text "NO GUARD DECISION", the Preset name of that Run and the "NO GUARD DECISION" icon of criterion 4, and SHALL display no matched_rule_id and no rule reason in that Outcome_Banner.
6. WHEN a Run_Result is displayed, THE Playground SHALL display the per-layer detail of the Run (Layer_Verdict rows with findings, stripped segments and quarantined chunks of Requirement 4) below the Outcome_Banner as exactly one collapsible section per Layer_Verdict in the Run_Result, in pipeline order (L1 Inbound Guard, L2 Doc Scanner, L4 Output Guard), where each section header remains visible while collapsed and shows the layer name and its severity.
7. THE Playground SHALL provide in each collapsible layer section header one toggle control that is reachable with Tab and Shift+Tab, toggles the section between expanded and collapsed on Enter, Space or pointer click (Requirement 10 criterion 4), exposes its current state to assistive technology as expanded or collapsed, and changes only that section, leaving other sections, the other comparison column and the Run_Result unchanged and making no model API call.
8. WHEN a Run_Result that has a Deciding_Layer is displayed, THE Playground SHALL show the collapsible section of the Deciding_Layer expanded and every other layer section of that Run collapsed.
9. IF a displayed Run_Result has no Deciding_Layer, THEN THE Playground SHALL show every layer section of that Run collapsed.
10. IF a displayed Run_Result contains zero Layer_Verdicts, THEN THE Playground SHALL display the text "no layer verdicts" below the Outcome_Banner in place of the collapsible layer sections.

### Requirement 13: Pipeline stepper [MUST]

**User Story:** As a Committee_Viewer, I want to see the guard stages in order and where the email was stopped, so that I understand the multi-stage defense.

#### Acceptance Criteria

1. WHEN a Run_Result is displayed, THE Playground SHALL display, directly below the Outcome_Banner of that Run's result column, a horizontal Pipeline_Stepper with exactly 7 Stages, left to right, in this order: "L1 Inbound Guard", "L3 Inbound Gate (pass 1)", "L2 Doc Scanner", "L3 Inbound Gate (pass 2)", "Channel isolation", "Generation", "L4 Output Guard".
2. WHEN a Pipeline_Stepper is displayed, THE Playground SHALL show for each of the 7 Stages exactly one Stage_Status out of "passed", "flagged", "stopped here", "skipped" or "disabled by preset", as an English text label next to an icon, where each of the 5 Stage_Status values uses an icon different from the other 4 and the Stage_Status is identifiable from the text label alone.
3. IF the Preset of the Run disables a Stage, THEN THE Playground SHALL show that Stage as "disabled by preset", regardless of where the Run stopped, and the "Generation" Stage SHALL never be shown as "disabled by preset" (Preset C0 still generates a draft).
4. IF the L3 Inbound Gate returns a Policy_Decision with action block or quarantine, THEN THE Playground SHALL show as "stopped here" the gate pass that produced that decision ("L3 Inbound Gate (pass 1)" when the Run's gate_passes metadata equals 1, "L3 Inbound Gate (pass 2)" when it equals 2), and SHALL show every later Stage that is not "disabled by preset", including "Generation" and "L4 Output Guard", as "skipped".
5. WHEN an enabled L1 Inbound Guard, L2 Doc Scanner or L4 Output Guard Stage ran without stopping the Run and produced a Layer_Verdict with severity other than none, at least 1 stripped segment, or at least 1 quarantined chunk, THE Playground SHALL show that Stage as "flagged".
6. WHEN an enabled Stage ran without stopping the Run and without meeting the condition of criterion 5, THE Playground SHALL show that Stage as "passed"; this includes "L3 Inbound Gate (pass 1)" and "L3 Inbound Gate (pass 2)" when their action is human_approval, draft_only or auto_send, "Channel isolation" when it was applied, and "Generation" when a draft was generated.
7. THE Playground SHALL show at most 1 Stage as "stopped here" per Run, SHALL show zero Stages as "stopped here" when the final decision action is neither block nor quarantine, and SHALL show no Stage left of the "stopped here" Stage as "skipped".
8. WHEN a Pipeline_Stepper is displayed in a browser viewport of 1280 × 720 pixels at 100% zoom with Presenter_Mode on or off, THE Playground SHALL fit all 7 Stages within the width of one comparison column without a horizontal scrollbar (Requirement 10 criterion 3), wrapping each Stage label onto at most 2 lines and showing every Stage label text in full without truncation.
9. WHEN the Presenter activates, by mouse click or by the Enter or Space key while the Stage has keyboard focus, a Stage in the Pipeline_Stepper that has a matching layer section (Requirement 12 criterion 6), THE Playground SHALL expand that layer section and move keyboard focus to that section; Stages without a matching layer section SHALL NOT be focusable or activatable.
10. IF the outbound Policy_Decision of a Run has action block or quarantine after generation (for example policy rule P03 or P06), THEN THE Playground SHALL show "Generation" as "passed" and "L4 Output Guard" as "stopped here", and SHALL show every Stage left of "Generation" with the Stage_Status given by criteria 3, 5 and 6.
11. IF the L2 Doc Scanner Stage is enabled and the Run has zero KB chunks to scan, THEN THE Playground SHALL show "L2 Doc Scanner" as "passed" with the text "0 chunks scanned" next to its Stage_Status label.

### Requirement 14: Evidence highlighting [MUST]

**User Story:** As a Committee_Viewer, I want the suspicious text to be marked in the email itself, so that I can judge whether the guard flagged the right words.

#### Acceptance Criteria

1. WHEN a Run_Result is displayed, THE Playground SHALL display the email body and the KB chunks of that Run with an Evidence_Highlight on every occurrence of each text span that exactly equals (case-sensitive, compared on the raw input text before HTML escaping) a finding excerpt (as stored, up to 400 characters) or a stripped segment text of that Run, so that an excerpt occurring N times in the shown text produces N highlighted occurrences.
2. THE Playground SHALL render each Evidence_Highlight with an underline and a visible marker symbol in addition to a background color, so that each highlighted span can be identified without relying on color.
3. THE Playground SHALL make each Evidence_Highlight a link to the finding or stripped segment that produced the Evidence_Highlight, labelled with the layer and the rule_id (or "—" when the rule_id has no value), and SHALL give that finding or stripped segment a link back to the first occurrence (in reading order: email body first, then KB chunks in displayed order) of its Evidence_Highlight together with the count of occurrences located.
4. WHEN the Presenter activates an Evidence_Highlight link, THE Playground SHALL expand the layer section containing the linked finding or stripped segment (Requirement 12 criterion 6) if it is collapsed and move keyboard focus to that finding or stripped segment.
5. IF two or more findings or stripped segments produce text spans that share at least one character, THEN THE Playground SHALL display one Evidence_Highlight covering the union of those spans, linked to every finding and stripped segment that produced the spans, and SHALL keep spans that are only adjacent (sharing no character) as separate Evidence_Highlights.
6. IF a finding excerpt or stripped segment text does not occur verbatim in the displayed email body or KB chunks, THEN THE Playground SHALL display that finding or stripped segment with the note "excerpt not located in the shown text", SHALL create no Evidence_Highlight for it, and SHALL still display the remaining Evidence_Highlights of the Run.
7. WHEN a Run_Result is displayed whose L2 Doc Scanner verdict contains one or more quarantined KB chunks, THE Playground SHALL display each quarantined KB chunk with strikethrough text and a visible "QUARANTINED" label with the quarantine reason of Requirement 4 criterion 4, while still showing the Evidence_Highlights located inside that chunk.
8. THE Playground SHALL keep the displayed email body and KB chunk text identical to the input text apart from the added highlight markup, such that removing the highlight markup from the rendered text yields the original escaped text, character for character including whitespace and line breaks.
9. IF a finding excerpt or stripped segment text is empty or consists only of whitespace, THEN THE Playground SHALL create no Evidence_Highlight for it and SHALL display that finding or stripped segment with the note "excerpt not located in the shown text".
10. WHEN the Presenter activates the back link of a finding or stripped segment, THE Playground SHALL scroll the linked Evidence_Highlight into view and move keyboard focus to it.

### Requirement 15: Scores with context [MUST]

**User Story:** As a Committee_Viewer, I want each score shown next to the threshold the guard used, so that I can judge what the number means.

#### Acceptance Criteria

1. WHEN the Playground displays the verdict-level score of the L1 Inbound Guard Layer_Verdict, THE Playground SHALL display, on the same line and immediately after the score, the block threshold and the flag threshold of that Run's Guard_Settings in the form "<score> / block ≥ <block_threshold>, flag ≥ <flag_threshold>" (for example "0.91 / block ≥ 0.85, flag ≥ 0.50" with default Guard_Settings).
2. WHEN the Playground displays the score of an L1 stripped segment, THE Playground SHALL display, on the same line and immediately after the score, the strip threshold of that Run's Guard_Settings in the form "<score> / strip ≥ <strip_threshold>" (for example "0.62 / strip ≥ 0.50" with default Guard_Settings).
3. WHEN the Playground displays the verdict-level score of the L2 Doc Scanner Layer_Verdict or the score of a KB chunk, THE Playground SHALL display, on the same line and immediately after the score, the quarantine threshold of that Run's Guard_Settings in the form "<score> / quarantine ≥ <quarantine_threshold>" (for example "0.74 / quarantine ≥ 0.70" with default Guard_Settings).
4. THE Playground SHALL display every score covered by criteria 1 to 3 and 6 rounded to 2 decimal places in the range 0.00 to 1.00, and every displayed threshold rounded to 2 decimal places, using the "≥" symbol to mean that a score equal to or greater than the threshold meets that threshold.
5. WHILE Live_Mode is active, THE Playground SHALL read each displayed threshold from the Guard_Settings actually used by the displayed Run; WHILE Replay_Mode is active, THE Playground SHALL read each displayed threshold only from the thresholds recorded in the displayed Replay_Store entry and SHALL NOT substitute the Guard_Settings default values.
6. IF a displayed score has no corresponding threshold in the Guard_Settings (including every L4 Output Guard score) or the threshold is absent from the displayed Replay_Store entry, THEN THE Playground SHALL display the score rounded to 2 decimal places followed by "(no threshold)" and SHALL display no threshold value for that score.
7. WHERE the Deciding_Layer of a Run is the L1 Inbound Guard or the L2 Doc Scanner, THE Playground SHALL include in the Outcome_Banner the verdict-level score of the Deciding_Layer together with the threshold(s) it was compared with, using the form of criterion 1 for the L1 Inbound Guard and the form of criterion 3 for the L2 Doc Scanner, and with values identical to those shown in the Deciding_Layer's verdict row.
8. IF a Run has no Deciding_Layer or its Deciding_Layer is the L4 Output Guard, THEN THE Playground SHALL display the Outcome_Banner without any score or threshold.

### Requirement 16: Demo fallback plan [MUST]

**User Story:** As a Presenter, I want a documented last-resort backup, so that the committee demo can go on even if the laptop or the page fails.

#### Acceptance Criteria

1. THE Playground_Docs SHALL state that a screen recording of the full demo is prepared no later than 24 hours before the committee demo as a last-resort backup. The recording covers every Demo_Tour step of Requirement 17 in tour order, or every Demo_Set case shown as C0 vs C3 if the Demo_Tour is not built.
2. THE Playground_Docs SHALL state three facts about the screen recording: the file location of the primary copy on the Presenter's laptop and of at least one backup copy kept on a device or storage other than that laptop, the date it was recorded (YYYY-MM-DD), and the git commit hash of the Playground it shows.
3. THE Playground_Docs SHALL state the fallback order (Live_Mode, then Replay_Mode, then the screen recording) and the switch conditions. The Presenter switches from Live_Mode to Replay_Mode if a Live_Mode run shows an error or does not finish within 60 seconds. The Presenter switches from Replay_Mode to the screen recording if `/guard` does not load within 30 seconds or the laptop cannot be used.
4. THE Playground_Docs SHALL list copy-pasteable commands to start the Playground_Service on 127.0.0.1:3010 and open `/guard`, for two cases: (a) Docker running, using `docker compose up -d guard-playground`, in Replay_Mode and in Live_Mode; and (b) Docker not running, using the Fallback_Launcher, in Replay_Mode with zero model credentials configured and in Live_Mode with `-Live`.
5. THE Playground SHALL contain no application code for playing the screen recording; this requirement is met by documentation and the recording file only.
8. THE Playground SHALL provide the Fallback_Launcher as one PowerShell command that starts the Playground_Service on 127.0.0.1:3010 with the repository's Python environment, without Docker, with Live_Mode disabled by default (`-ReplayOnly`) or enabled with the `-Live` switch (decision D13), and that prints the `/guard` URL once the service answers its health route.
9. WHILE the Playground_Service runs with Live_Mode disabled, THE Playground SHALL display Live_Mode as unavailable with a message stating how to enable it (the `-Live` switch of the Fallback_Launcher or the Docker service), and SHALL make zero model API calls.
6. IF the screen recording file is larger than 50 MB, THEN THE Playground_Docs SHALL state that the file is stored outside the git repository, and the git repository SHALL contain no copy of the recording file.
7. THE Playground_Docs SHALL include a pre-demo checklist, completed no later than 24 hours before the committee demo. It covers five checks: the case (a) commands of criterion 4 start `guard-playground` and complete one Live_Mode Run with each of the 3 Draft_Models; the case (b) commands of criterion 4 load `/guard` in Replay_Mode on the demo laptop; the primary copy of the screen recording plays from start to end; the backup copy plays from start to end; and the commit hash recorded in criterion 2 matches the commit checked out on the demo laptop.

### Requirement 17: Guided demo tour [SHOULD]

**User Story:** As a Presenter, I want a prepared sequence of demo steps with talking points, so that I can tell the story from the problem to the defense without searching for cases.

#### Acceptance Criteria

1. THE Playground SHALL provide a Demo_Tour of 4 to 6 ordered steps, each step defining exactly one input (one Demo_Set Sample_Case identified by its case id, or one Custom_Email template with a subject of 1 to 200 characters and a body of 1 to 5,000 characters), either a Preset pair (left column Preset and right column Preset) or a single Preset, exactly one mode (Live_Mode or Replay_Mode), and a talking point of exactly one English sentence of 1 to 200 characters.
2. THE Playground SHALL ship a Demo_Tour definition whose steps are, in this order: a benign email (C0 vs C3); an LLMail-Inject attack email (C0 vs C3); a PoisonedRAG case (C0 vs C3); a seed or synthetic_poison_v1 poison case (C0 vs C3); and a Custom_Email in Live_Mode, where the Case_Kind of each Sample_Case step matches the stated kind (`benign`, `attack`, `rag_poison`, `rag_poison`) and its Case_Source matches the stated source.
3. WHEN the Presenter activates the "Start tour" control or moves to a step, THE Playground SHALL, within 1 second, replace the current input, Preset selection and mode with that step's input, Preset selection and mode, clear any Run_Result displayed for the previous step, display the step number as "Step k of n" (k = 1-based step position, n = total step count) together with the talking point, and SHALL start no Run until the Presenter starts the Run.
4. THE Playground SHALL provide "Next" and "Previous" controls for the Demo_Tour, with "Previous" disabled on step 1 and "Next" disabled on step n, where each enabled control moves exactly one step forward or backward.
5. WHILE the Demo_Tour is active and keyboard focus is outside every text input field, WHEN the Presenter presses the N key or the P key (case-insensitive, without Ctrl, Alt or Meta modifiers), THE Playground SHALL move to the next or previous step respectively, in the same way as the "Next" and "Previous" controls, and SHALL keep the current step unchanged if the corresponding control is disabled.
6. WHILE keyboard focus is inside a text input field, THE Playground SHALL treat N and P key presses as text input and SHALL keep the current Demo_Tour step unchanged.
7. IF a Demo_Tour step requires Live_Mode while Replay_Mode is active, or references a Sample_Case with no valid Replay_Store entry in Replay_Mode, THEN THE Playground SHALL display the step number and its talking point, disable the Run control for that step, display a message stating which of these two reasons makes the Run unavailable, and keep the "Next" and "Previous" controls usable.
8. WHEN the Playground page loads, THE Playground SHALL validate the Demo_Tour definition, and IF the definition fails validation (fewer than 4 or more than 6 steps, an unknown case id, an unknown Preset, a missing or more than 200-character talking point, or a missing input or mode), THEN THE Playground SHALL hide the "Start tour", "Next" and "Previous" controls, display a warning naming the step number of the first invalid step and the failed rule (or the actual step count when the count is out of range), and keep every non-tour control of the page usable.
9. WHEN the Presenter activates the "Exit tour" control while the Demo_Tour is active, THE Playground SHALL hide the step number, talking point, "Next" and "Previous" controls, keep the currently loaded input, Preset selection, mode and any displayed Run_Result unchanged, and stop responding to N and P key presses as tour navigation.
10. IF the Presenter moves to another step while a Run of the current step is in progress, THEN THE Playground SHALL discard that Run's result without displaying it in the new step and load the new step as defined in criterion 3.

### Requirement 18: Results at a glance strip and Results_Page [SHOULD]

**User Story:** As a Committee_Viewer, I want a few headline numbers from the benchmark, so that the single demo case is put in the context of the full evaluation.

#### Acceptance Criteria

1. WHEN the `/guard` page loads, THE Playground SHALL display a KPI_Strip of at most 4 cards, read from the `metrics.csv` of the three 1,800-case runs `evaluation/results/mailguard_bench/2026-10-19-*-v3-1800/`. Each run directory maps to exactly one model by its directory name: `gpt4omini` → gpt-4o-mini, `llama31-8b` → Llama 3.1 8B, `qwen25-7b` → Qwen 2.5 7B.
2. THE Playground SHALL include exactly these 3 cards in the KPI_Strip, in this order: (a) email Defense_Rate C0 → C3 (LLMail-Inject), read from the row with table=llmail, metric=ASR, group=all; (b) RAG Defense_Rate C0 → C3 (RAG poison cases), read from the row with table=rag, metric=ASR, group=all; (c) benign false-positive rate at C3, read from the row with table=llmail, metric=FPR, group=all. In every case the config column must equal C0 or C3 as named on the card.
3. THE Playground SHALL label each Defense_Rate card as a defense rate computed as 100 − ASR, so that a viewer can tell it apart from the ASR values reported in the thesis paper.
4. THE Playground SHALL show one line per model in each card, in the order gpt-4o-mini, Llama 3.1 8B, Qwen 2.5 7B. In the two Defense_Rate cards, each line SHALL show the C0 value and the C3 value separated by "→", each followed by its own sample size (e.g. "75.7% (n = 999/1000) → 100.0% (n = 1000/1000)"). In the false-positive card, each line SHALL show only the C3 value and its sample size.
5. THE Playground SHALL compute each Defense_Rate as 100 − the `pct` value of the matching ASR row. It SHALL display the false-positive rate as the `pct` value of the matching FPR row. Every displayed value SHALL be a percentage rounded half away from zero to exactly 1 decimal place (e.g. 0.0%, 100.0%).
6. THE Playground SHALL take "scored" in "n = scored/planned" as the `total` value of the same row used for the displayed value. It SHALL take "planned" as the count of the matching case kind (email attack, RAG poison, or benign) in the same run's `cases.jsonl`. For the 1,800-case runs these counts are expected to be 1000, 300 and 500.
7. THE Playground SHALL produce the KPI_Strip with zero model API calls and zero created, modified or deleted files under `evaluation/results/`.
8. IF a `metrics.csv` file is missing or unreadable, or lacks a row or value needed by a card line, THEN THE Playground SHALL display "not available" in place of the affected value in that card line. It SHALL keep displaying all other card lines and all other `/guard` page content unchanged.
9. IF a run's `cases.jsonl` is missing or unreadable, THEN THE Playground SHALL display "n = scored/not available" for the affected sample sizes and still display the value from `metrics.csv`.
10. THE Playground SHALL display directly below the KPI_Strip a link to the Results_Page labelled "Full results".
11. THE Playground SHALL keep the KPI_Strip and the Results_Page read-only, with zero filters, zero sorting controls and zero drill-down views.
12. WHEN the Presenter opens `/guard/results`, THE Playground SHALL display the KPI_Strip of criteria 1 to 9 followed by the 6 figures of `paper/figures/final/3models/` (fig1 to fig6), in that order, each as its PNG file with a caption naming the figure and an alt text describing what it shows.
13. THE Results_Page SHALL provide for each figure a link that opens its PDF file, and SHALL display the provenance line shared by the figures (guard commit, rag-email commit, case set sha256, L1 classifier sha256) read from the run metadata.
14. IF a figure file is missing, THEN THE Results_Page SHALL display "figure not available" with the expected file name in place of that figure and keep displaying the other figures and the KPI_Strip.
15. THE Results_Page SHALL be reachable from the `/guard` page navigation, and the `/guard` page SHALL be reachable from the Results_Page navigation.

### Requirement 19: Presenter_Mode [SHOULD]

**User Story:** As a Presenter, I want a projector-friendly view, so that the committee can read the page from a distance without developer clutter.

#### Acceptance Criteria

1. THE Playground SHALL provide a "Presenter mode" toggle on `/guard` with a visible text label, set to off on every page load (including reloads), reachable and operable by keyboard as defined in Requirement 10 criterion 4, and exposing its current on or off state to assistive technology.
2. WHEN the Presenter switches the "Presenter mode" toggle, THE Playground SHALL apply or remove Presenter_Mode within 1 second without reloading the page, without starting a Run and without making any model API call.
3. WHILE Presenter_Mode is on, THE Playground SHALL render body text and comparison-column content (including Layer_Verdict rows, findings, excerpts, Evidence_Highlights and the draft) at a computed font size of at least 20 pixels and headings at a computed font size of at least 26 pixels, measured at 100% browser zoom.
4. WHILE Presenter_Mode is on, THE Playground SHALL keep both comparison columns side by side with no horizontal page scrollbar at a 1280 × 720 pixel viewport (Requirement 10 criterion 3), wrapping long text onto further lines instead of truncating it or hiding it with an ellipsis.
5. WHEN Presenter_Mode is switched on, THE Playground SHALL collapse the guarded prompt view of Requirement 4 criterion 5 in every result column, including any guarded prompt view that was expanded before the switch.
6. WHILE Presenter_Mode is on, THE Playground SHALL keep the guarded prompt view collapsed until the Presenter activates an expand control, labeled with visible text and operable by keyboard, that shows the full guarded prompt of that result column and exposes its expanded or collapsed state to assistive technology.
7. WHEN the Presenter switches Presenter_Mode on or off, THE Playground SHALL keep unchanged the displayed Run_Results, the selected inputs (Sample_Case or Custom_Email text), the selected Presets, the Replay_Mode or Live_Mode selection, the Draft_Model, the Guard_Settings values, the expanded or collapsed state of each layer section (Requirement 12 criterion 6) and the current Demo_Tour step.
8. WHILE Presenter_Mode is on, THE Playground SHALL display every Outcome_Banner, Pipeline_Stepper, Evidence_Highlight and score with its threshold (Requirement 15) that is displayed with Presenter_Mode off for the same Run_Result and the same layer section states, hiding no element other than the guarded prompt view content of criterion 5.

### Requirement 20: Live progress [SHOULD, Live_Mode only]

**User Story:** As a Committee_Viewer, I want to see which guard stage is running during a live run, so that the wait is understandable.

#### Acceptance Criteria

1. WHILE a Live_Mode Run is executing, THE Playground SHALL display the name of the Stage currently executing, using the Stage names of Requirement 13 criterion 1, and the elapsed time since the Run started in whole seconds, from 0 up to 120.
2. WHILE a Live_Mode Run is executing and no Stage has started yet, THE Playground SHALL display a label stating that the Run is starting in place of a Stage name, together with the elapsed time.
3. WHILE a Live_Mode Run is executing, THE Playground SHALL never display as the current Stage a Stage whose Stage_Status for that Run is `disabled by preset` or `skipped`.
4. WHILE a Live_Mode Run is executing, THE Playground SHALL update the displayed Stage name and elapsed time by htmx polling (`hx-trigger="every Ns"`) at an interval of at most 1 second, without WebSocket or Server-Sent Events, so that the displayed Stage changes no more than 1 second after a new Stage starts.
5. WHEN a Live_Mode Run completes, fails (Requirement 7 criterion 5) or times out (Requirement 7 criterion 7), THE Playground SHALL stop polling for that Run within 2 seconds and replace that Run's progress display with the Run_Result, the failure error message or the timeout message respectively.
6. IF a progress polling request fails or returns no response, THEN THE Playground SHALL keep displaying the last received Stage name, continue polling at the same interval, and SHALL NOT treat the failed poll as a Run failure; the Run ends only through the completion, failure or 120-second timeout of criterion 5.
7. WHILE comparison mode is selected, THE Playground SHALL display the current Stage and elapsed time separately for each of the 2 Runs in its own result column, and WHEN one Run completes, fails or times out, THE Playground SHALL replace only that Run's progress display and keep polling and displaying progress for the other Run until it also ends.
8. WHILE Replay_Mode is active, THE Playground SHALL make zero progress polling requests and display no Stage progress display.

### Requirement 21: External data from the committee [SHOULD]

**User Story:** As a Presenter, I want to run emails that a committee member brings, one by one or as a file, so that the committee can test the guard on data the team did not choose.

#### Acceptance Criteria

1. WHILE Live_Mode is active, THE Playground SHALL provide on the `/guard` page a file upload control, labelled with visible text, that accepts one file with the extension `.eml`, `.json` or `.jsonl` and a size of at most 5 MB, presented as a drop zone that also contains a keyboard-operable "Choose file" button (decision D12).
2. WHEN the Presenter uploads a `.eml` file, THE Playground SHALL parse it as one RFC 5322 email message and use its Subject header as the subject and its first `text/plain` part (or, when there is none, the text of its first `text/html` part with markup removed) as the body, with zero KB chunks.
3. WHEN the Presenter uploads a `.json` file, THE Playground SHALL accept either one object or an array of objects, and WHEN the Presenter uploads a `.jsonl` file, one object per non-empty line, where each object has a `body` string and may have a `subject` string, an `id` string and a `chunks` array of strings, or follows the schema of the cases in `evaluation/datasets/mailguard/cases.jsonl`.
4. THE Playground SHALL apply the limits of Requirement 8 criterion 5 to every email of an uploaded file, and SHALL accept at most 50 emails per file.
5. IF an uploaded file has an unsupported extension, is larger than 5 MB, cannot be parsed, or contains more than 50 emails, THEN THE Playground SHALL reject the whole file, start no Run, and display one message naming the failed rule.
6. IF some emails of an accepted file fail the checks of criterion 3 or 4, THEN THE Playground SHALL exclude those emails, display a list naming each excluded email by its position (and its `id` when present) with the failed rule, and keep the remaining valid emails.
7. WHEN an uploaded file holds exactly 1 valid email, THE Playground SHALL load it into the Custom_Email fields as if the Presenter had typed it, so that the Presenter can run it with Requirement 2 and Requirement 3.
8. WHEN an uploaded file holds 2 or more valid emails and the Presenter starts a Batch_Run, THE Playground SHALL run every valid email with each selected Preset and the selected Draft_Model, at most 2 Runs at a time, each Run bounded by the 120-second timeout of Requirement 7 criterion 7.
9. WHILE a Batch_Run executes, THE Playground SHALL display the number of finished Runs out of the total, update it by htmx polling at an interval of at most 2 seconds, and provide a "Stop batch" control that lets running Runs finish and starts no further Run.
10. WHEN a Batch_Run ends, THE Playground SHALL display a summary table with one row per email (position, `id` when present, subject truncated to 80 characters) and, per selected Preset, the Outcome_Banner text of Requirement 12 criterion 2, the matched_rule_id and the Deciding_Layer, or "failed" with the error category of Requirement 7 criterion 5, plus a totals row counting each outcome per Preset.
11. WHEN the Presenter selects a row of the summary table, THE Playground SHALL display the full Run_Results of that email in the result columns, as for a single Run.
12. THE Playground SHALL label every Run_Result and summary row that comes from External_Data as "External data", SHALL show "no benchmark record" in place of the Benchmark_Record section (Requirement 6) for these Runs, and SHALL write no Replay_Store entry for them.
13. THE Playground SHALL keep each uploaded file and its Run_Results only in the memory of the Playground_Service for the browser session, SHALL write zero copies of the uploaded file or its emails to disk, and SHALL discard them when the Presenter activates "Clear external data" or the Playground_Service restarts.
14. WHILE Replay_Mode is active, THE Playground SHALL disable the file upload control and display a message stating that external data requires Live_Mode.
