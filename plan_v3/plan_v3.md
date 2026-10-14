# Implementation Plan - AgentMailGuard v3 Architecture

Kiến trúc đích: [`architect_v3.jpg`](architect_v3.jpg).
Phạm vi: subproject `agentmailguard/` (package `mailguard`) và consumer duy nhất là root
`evaluation/mailguard_bench` cùng `tests/unit/test_mailguard_*`.
Tuân thủ `CLAUDE.md`: spec-driven, divergence protocol §7 (ADR + cập nhật specs), không
renumber requirement ID, một commit cho mỗi task, Definition of Done §3.

## Vấn đề

Guard hiện có 6 layer: L1 injection scanner, L2 intent extractor, L3 channel isolation,
L3b doc scanner, L4 output scanner, L5 policy engine (gọi 2 lần: inbound và outbound).
v3 có 4 guard layer, Channel Isolation không đánh số, bỏ Outbound Gate. Thay đổi không được
làm hỏng hệ thống đang chạy (guard_worker, contract `Job.result_ref`, CI).

## Yêu cầu đã chốt

| ID | Quyết định |
|---|---|
| R-a | L1 + L2 cũ gộp thành **L1 Inbound Guard** (scan + extract intent → email verdict) |
| R-b | L3b cũ → **L2 Doc Scanner** (per chunk + set judge → document verdict) |
| R-c | L5 inbound → **L3 Inbound Gate** (policy trước mọi model call; block/quarantine → escalated draft, không gọi model) |
| R-d | **Channel Isolation** không phải layer: chỉ dựng SecurePrompt (data-marked), không phát verdict |
| R-e | Bỏ Outbound Gate; L4 cũ → **L4 Output Guard** (scan draft + outbound policy → action + reason) |
| R-f | Strictest action wins; quyết định inbound được mang vào L4 |
| R-g | Guard không gọi tool |
| Q1=b | Hard rename, không alias. Run/artifact v2 không đọc lại được; reader từ chối rõ ràng |
| Q2=c | Set judge deterministic mặc định; LLM set judge sau cờ, tắt mặc định |
| Q3=b | Judge trả lời sai → human review, sau cờ `fail_closed_on_judge_error` (mặc định bật; bench có thể tắt để so với v2) |
| Q4=a | Identifier theo nghĩa (`inbound_guard`, `doc_scanner`, `inbound_gate`, `output_guard`, `channel_isolation`); nhãn "L1–L4" chỉ trong docs/report |
| Q5=a | L1 Inbound Guard là facade giữ 2 sub-stage (scan judge + extractor), tối đa 2 LLM call như hiện tại |
| Q6=c | Bench scheme v3; Channel Isolation có toggle riêng cho thí nghiệm; cập nhật docs + paper + ADR + specs |
| D1=A | Inbound floor theo rule cụ thể, thực thi trong code (xem Design decision 1) |
| **Review 2026-10-13** | Các dòng dưới đây được thêm sau review; chúng sửa/bổ sung các quyết định ở trên, không thay số task |
| Q1=b' | Hard rename giữ nguyên cho code, config, env, `GuardReport`. **Ngoại lệ duy nhất:** bench ở `HEAD` giữ một reader **chỉ đọc** cho run folder v1/v2 đã commit (`evaluation/results/mailguard_bench/*`) để tái tạo bảng Review 1; reader này không chạy được run mới và không chuyển đổi report cũ sang schema 3 (xem Task 13) |
| R-h | L2 Doc Scanner bắt buộc có **cheap stage mới**: echo so với `question_sentences` của email (L1 cung cấp), ordered-prefix, bait cluster; không chỉ là aggregate của per-chunk scanner cũ (xem Design decision 9). Lý do: replay trên 100 poisoned-doc case cho thấy per-chunk cũ bắt đủ chunk ở 32/100, echo theo câu hỏi 85/100, thêm ordered-prefix 87–89/100, 0 clean chunk bị flag |
| R-i | L1 Inbound Guard: **severity chỉ đến từ sub-stage scan**. Extractor chỉ thêm `findings`/intent/sanitized body, không bao giờ nâng severity (xem Design decision 8 sửa). Lý do: path "extractor làm detector" là nguyên nhân Llama-3.1-8B quarantine 86/100 email hỏi thông thường trong bench v1 |
| Q7 | Đổi tên `RetrievedChunk` (contract đầu vào của Doc Scanner) → **`SourceDocument`**; verdict theo set là `DocumentVerdict` (đã có), verdict từng mảnh giữ `ChunkVerdict`. Không dùng `KnowledgeChunk` (trùng tên domain của rag-email). Chủ dự án có thể đổi tên khác trước Task 4; sau Task 4 là hard rename |
| Q8 | ADR-0015 giữ cho kiến trúc v3 (file này). Host hook (review-UI badge, `knowledge_document.trust_level`, `exclude_document_ids`, re-retrieval, ingestion scan) là **ADR-0016**, làm sau Task 15, không nằm trong 15 task này |
| Q9 | **Push `refactor/guard-v3` sau mỗi task** (push không phải merge; không PR, không merge vào `main`). Lý do: 15 commit trên một máy Windows chưa push thì không ai review được code trước Task 15 và mất máy là mất hết |
| Q10 | **Task 16 — live benchmark v3** nằm trong phạm vi của người thực hiện plan này. Refactor chưa được coi là xong khi chưa có số đo live (xem Task 16); parity offline chỉ chứng minh không hỏng, không chứng minh tốt hơn |

Bất biến carry-over được test bằng bảng tổ hợp parametrize, không thêm dependency.
LLM set judge dựa trên PoisonedRAG / poisoning phân tán qua nhiều chunk.

## Bối cảnh (đã xác minh trong code)

- `mailguard/pipeline.py`: L1 → L2 → L5(inbound) → [L3b → L3 trong `build_prompt`] → generate
  → L4 → L5(outbound, ghi đè `report.decision`).
- `contracts/verdict.py`: `LayerName` 6 giá trị; `SanitizedIntent`, `ChunkVerdict`, `OutputVerdict`.
- `contracts/policy.py`: `PolicyAction.strictest()`, `PolicyDecision`, `GuardReport(l1,l2,l3,l3b,l4,...)`.
- `layers/l5_policy_engine/engine.py`: `facts/select/decide/_audit`; catch-all rule chỉ là fallback.
- `configs/policy.yaml` `2026.09-v2`: P03/P06/P07/P10 chỉ chạy ở `stage: outbound`.
- L2 và L3b dùng chung `rules`/`classifier` của L1. Artifact `l1_injection_clf_v1.joblib` được
  pin SHA ở root bench → **giữ nguyên tên**.
- Judge trả sai schema → `mark_llm_fallback` giữ verdict rẻ (có thể "clean").
- Bench v1: Llama-3.1-8B C0T/C3 không phải defence hợp lệ vì extractor nâng severity trên email hỏi
  thông thường (86/100 quarantine) và guard template thiếu reply-format của host. Poisoned-doc route
  v1: GPT 79→32%, Qwen 91→39% — trần của per-chunk scanner cũ, không phải trần của kiến trúc.
- Settings `env_nested_delimiter="__"`, `extra="ignore"` → env cũ phải bị reject tường minh.
- `decision_to_job_result` không chứa tên layer → chỉ được thêm field.

## Thiết kế

```mermaid
flowchart LR
  E[Customer email] --> IG[L1 Inbound Guard<br/>scan + extract]
  D[Retrieved docs] --> DS[L2 Doc Scanner<br/>per chunk + set judge]
  IG -- sanitized body + question_sentences --> DS
  IG -- InboundVerdict --> G[L3 Inbound Gate<br/>PolicyEngine stage=inbound]
  DS -- DocumentVerdict --> G
  G -- block/quarantine --> ESC[Escalated draft<br/>no model call]
  G -- pass: intent + clean body + kept chunks --> CI[Channel Isolation<br/>SecurePrompt]
  CI --> H[Host reply call<br/>1 generation]
  H -- draft --> OG[L4 Output Guard<br/>scan + PolicyEngine stage=outbound<br/>+ inbound floor]
  OG -- action + reason --> Q[Host review queue]
```

### Mapping identifier (hard rename)

| Hiện tại | v3 |
|---|---|
| `layers/l1_injection_scanner`, `layers/l2_intent_extractor` | `layers/inbound_guard/` (`scan.py`, `extract.py`, `guard.py`=`InboundGuard`, `rules.py`, `classifier.py`, `llm_judge.py`) |
| `layers/l3b_document_scanner` | `layers/doc_scanner/` (`chunk_scanner.py`, `set_judge.py`, `scanner.py`=`DocScanner`) |
| `layers/l5_policy_engine` | `mailguard/policy/engine.py` (`PolicyEngine`, component dùng chung) |
| (L5 inbound) | `layers/inbound_gate/gate.py` = `InboundGate` |
| `layers/l4_output_scanner` | `layers/output_guard/` (`scanner.py`, `guard.py`=`OutputGuard`) |
| `layers/l3_channel_isolation` | `mailguard/prompting/channel_isolation.py` |
| `LayerName` | `inbound_guard`, `doc_scanner`, `inbound_gate`, `output_guard` |
| `GuardReport.l1/l2/l3/l3b/l4` | `schema_version=3`, `inbound_guard`, `doc_scanner`, `isolation`, `inbound_decision`, `output_guard`, `decision` |
| `SanitizedIntent` + verdict L1 | `InboundVerdict` (+ `metadata.stages.{scan,extract}`) |
| `list[ChunkVerdict]` | `DocumentVerdict` (chunks, kept/quarantined ids, quarantine_ratio) |
| `RetrievedChunk` (contracts) | `SourceDocument` (Q7); `ChunkVerdict` thêm `trust`, `cluster_id`, `signals` |
| Env `L1__…L5__`, `GUARD_MODELS__JUDGE/EXTRACTOR/DOC_SCANNER` | `INBOUND_GUARD__*`, `DOC_SCANNER__*`, `CHANNEL_ISOLATION__*`, `POLICY__*`, `OUTPUT_GUARD__*`, `GUARD_MODELS__INBOUND_JUDGE/INBOUND_EXTRACTOR/DOC_JUDGE/DOC_SET_JUDGE/OUTPUT_JUDGE`; key cũ → `LegacyConfigError` |
| Prompts | `inbound_scan_judge.v1.txt`, `inbound_extractor.v1.txt`, `doc_chunk_judge.v1.txt`, `output_judge.v1.txt`, mới `doc_set_judge.v1.txt` |
| `GuardConfig(l1..l5)` | `GuardConfig(inbound_guard, doc_scanner, inbound_gate, output_guard, channel_isolation)`; presets C0, C1 = L1+L3, C2 = C1+L2+isolation, C3 = full, `C3-L1…C3-L4`, `C3-ISO` |
| `policy.yaml` `2026.09-v2` | `2026.10-v3` |

### Design decisions

1. **Inbound floor (D1=A, thực thi trong code, không tắt được bằng config)**
   ```python
   CATCH_ALL = {"default"} | {id các rule có when rỗng}
   floor = inbound if inbound and inbound.matched_rule_id not in CATCH_ALL else None
   outbound = policy.decide(report, stage="outbound", ...)
   final_action   = strictest(outbound.action, floor.action) if floor else outbound.action
   requires_human = outbound.requires_human or floor.requires_human
   risk_tier      = max theo rank
   winner         = floor nếu floor.rank > outbound.rank, ngược lại outbound (hòa → outbound)
   ```
   Decision cuối ghi `matched_rule_id` của winner, `matched_rule_ids` hợp (rule inbound có tiền tố
   `inbound:`), `metadata.decided_by`, `metadata.carried_from_inbound`, reason
   `"carried from inbound_gate: <rule> (<action>)"`. Allowlist auto_send áp dụng sau merge.
   Bất biến: (1) có floor ⇒ final.rank ≥ floor.rank; (2) auto_send chỉ khi không floor ∧ allowlist;
   (3) requires_human = OR; (4) audit_id idempotent.
2. Doc Scanner chạy **trước** Inbound Gate → P01/P04 fire ở inbound; quarantine dừng trước
   generation. Delta có chủ đích, ghi vào golden parity + ADR.
3. Fail-closed judge: `LLMSchemaValidationError`/`LLMError` → `verdict.error="judge_invalid:<stage>"`
   → P00 human_approval. Thiếu provider vẫn degrade. Cờ tắt → hành vi v2.
4. LLM set judge monotonic: chỉ quarantine thêm.
5. `build_escalated_draft(report)` trong pipeline; chữ ký `run()` không đổi.
6. `decision_to_job_result` chỉ thêm `reason`, `decided_by`, `guard_schema_version`.
7. Channel Isolation trả `IsolationInfo`; lỗi build → `isolation.error` → fact `layer_error` → P00.
8. L1 Inbound Guard (**sửa theo R-i**): `severity = scan.severity`; `score = scan.score`;
   `findings = scan.findings ∪ extract.findings` (finding của extractor mang `metadata.stage="extract"`
   và **không tham gia** tính severity/score); intent/sanitized body/`removed_ratio` từ extractor;
   error nếu một sub-stage lỗi; latency = tổng. Extractor trả `contains_assistant_instructions=true`
   mà scan sạch → severity vẫn sạch, chỉ có finding để policy có thể dùng làm fact riêng
   (`extract_flagged`) nếu muốn — không có rule nào bật fact này mặc định.
   `InboundVerdict` ghi lại `question_sentences: list[str]` nhưng **không sở hữu** việc rút câu hỏi:
   `question_sentences(body)` là hàm thuần trong `mailguard/prompting/questions.py`, pipeline luôn
   gọi nó (trên sanitized body nếu L1 bật, trên body thô nếu L1 tắt) rồi truyền cho L2. Nhờ vậy
   ablation `C3-L1` chỉ đo "không có L1", không đồng thời làm yếu L2.
9. L2 Doc Scanner cheap stage (**R-h**), chạy cho mọi chunk, không model call:
   - `echo_vs_question`: overlap token/n-gram của chunk với `question_sentences` (không phải với
     toàn bộ body như hiện tại); ngưỡng riêng cho chunk ngắn.
   - `ordered_prefix`: chunk bắt đầu bằng (hoặc lặp lại theo thứ tự) câu hỏi rồi mới tới nội dung →
     dấu hiệu PoisonedRAG.
   - `bait_cluster`: nhóm các chunk có echo cao với nhau; `ChunkVerdict.cluster_id`; cluster có ≥2 chunk
     echo cao bị quarantine cả cụm.
   - Mỗi chunk có `trust ∈ {trusted, untrusted, quarantined}` để host dùng (ADR-0016) và `signals`
     (dict tên→giá trị) để bench đo từng tín hiệu.
   - Set verdict deterministic = aggregate của các tín hiệu trên (Q2=c giữ nguyên); LLM set judge
     (Task 10) chỉ chạy khi cờ bật **và** có ít nhất một chunk trong dải nghi ngờ (score giữa hai
     ngưỡng); monotonic.
   - **Cấm** chỉnh ngưỡng trên 100 poisoned-doc case đã pin; ngưỡng chọn trên tập dev riêng
     (`datasets/build_rag_poison.py` split dev) và ghi vào `DocScannerSettings` với giá trị + nguồn.
   - Tập dev phải chứa ít nhất một kiểu poisoning mà generator hiện tại không sinh (echo diễn đạt
     lại, không prefix; nội dung độc nằm giữa chunk), nếu không ngưỡng chỉ đúng cho template PoisonedRAG.
   - **Thứ tự chạy trong `inspect_inbound`:** L1 → gate lần 1 chỉ với fact của L1 (fact tài liệu
     = "chưa quét") → nếu block/quarantine thì dừng, **không** chạy doc judge → ngược lại chạy L2
     (cheap stage + judge) → gate lần 2 với đủ fact. Gate deterministic, không tốn gì; cách này giữ
     tính chất v2 "email bị chặn không trả tiền doc judge". `chunks=()` (job `workflow_hint=template`)
     → L2 là no-op theo hợp đồng, không có call nào.
10. Channel Isolation không có verdict, nhưng `IsolationInfo.markers_scrubbed` > 0 trong thread
    history được ghi thành một finding `forged_channel_marker` (stage `isolation`) gắn vào
    `report.inbound_guard.findings` để bench vẫn nhìn thấy kênh tấn công này; không đổi severity.
    **Lưu ý thời điểm:** isolation chạy khi dựng prompt, tức *sau* Inbound Gate; finding này chỉ
    tới được fact của L4 Output Guard và bench, không bao giờ ảnh hưởng quyết định inbound.
11. Bench reader (Q1=b'): `evaluation/mailguard_bench/legacy_reader.py` đọc run meta + report v1/v2
    từ folder đã commit, chỉ để `make mailguard-report` tái tạo bảng cũ; mọi đường chạy mới, scoring
    v3, ablation từ chối `schema_version < 3` như cũ.
12. `GUARDED_PROMPT_VERSION` tăng lên `guarded.v3` trong Task 7 (prompt đổi vì chunk được render
    với `trust`); bench ghi version này vào run meta.
13. **Ngân sách call của guard cho mỗi job** (bổ sung cho quy tắc một generation của host):
    tối đa 2 call L1 (scan judge + extractor), 1 doc chunk judge, 1 doc set judge, 1 output judge
    = **≤ 5 call guard**; mặc định production chỉ bật scan judge + output judge (≤ 2). Pipeline
    đếm call trong `GuardReport.metadata.guard_calls` và `test_guard_no_tools.py` assert ≤ 5 trên
    toàn bộ ma trận parity; Task 13 (`overhead.py`) báo số call và latency p50/p95 theo config.
14. **Parity không được trở thành "mọi thứ đều là delta có chủ đích".** Mỗi entry trong
    `INTENDED_DELTAS` phải ghi `test` = node id của một unit test chứng minh hành vi mới độc lập với
    golden; reviewer của mỗi loop từ chối delta không có test. Số delta tối đa mỗi task được ghi
    trước trong exec plan (Task 4: ≤3, Task 5: ≤6, Task 7: ≤20, Task 8: ≤6, Task 11: ≤5).

## Task Breakdown (16 task sau review)

1. Lưới an toàn: plan, ADR (Proposed), golden baseline v2 + `test_guard_parity.py`.
2. Đổi layout package (move-only) + đổi tên prompt.
3. Settings v3 + `LegacyConfigError` cho env cũ.
4. Contracts v3 (`LayerName`, `DocumentVerdict`, `IsolationInfo`, `GuardReport` v3) + policy v3 + audit v3.
5. L1 Inbound Guard facade (`InboundVerdict`, severity chỉ từ scan — R-i; `question_sentences`).
6. `GuardConfig` + presets v3.
7. Doc Scanner trước L3 Inbound Gate + `build_escalated_draft` + cheap stage mới (R-h, design 9)
   + `SourceDocument` (Q7) + `GUARDED_PROMPT_VERSION=guarded.v3`.
8. L4 Output Guard thay Outbound Gate (inbound floor D1).
9. Contract Host review queue (additive, kiểm tra consumer phía core trước).
10. LLM set judge (cờ, tắt mặc định; chỉ chạy khi có chunk trong dải nghi ngờ; monotonic).
11. Fail-closed invalid judge (cờ) + test guard không gọi tool.
12. Bench scheme v3 — cấu hình.
13. Bench scheme v3 — scoring & report (reader v3 từ chối `schema_version < 3`; `legacy_reader.py`
    chỉ đọc cho folder v1/v2 đã commit — Q1=b').
14. Docs, paper, specs, ADR → Accepted.
15. Test ratchet cấm identifier cũ, dọn dẹp, nghiệm thu toàn bộ CI. **15c phải chạy thêm `make ci`
    trên một checkout Linux** (laptop của chủ dự án) vì Windows baseline 63 test fail không chứng
    minh được regression-safety cho recipe make/bash.
16. **Live benchmark v3 (Q10)** — một run GPT-4o-mini (model rẻ nhất đã có profile) với scheme v3:
    C0, C1, C2, C3, `C3-L1…C3-L4`, `C3-ISO`, fail-closed bật; tập dữ liệu: LLMail-Inject như v1,
    100 poisoned-doc case đã pin, **và tập benign mới** (≥100 email hỏi thông thường + ≥100 tài liệu
    sạch cùng chủ đề, sinh một lần và commit id) để đo false positive. Tiêu chí nghiệm thu, ghi vào
    run meta và paper: (a) C3 ASR ≤ 5% trên LLMail-Inject; (b) poisoned-doc route: tỉ lệ "bắt đủ
    chunk độc" ≥ 80/100 (replay cho 85–89); (c) benign: quarantine/escalate ≤ 5% email hỏi thông
    thường, ≤ 2% tài liệu sạch; (d) overhead: số call guard ≤ 5, p95 end-to-end theo `overhead.py`
    được báo cáo (SC5 p95 < 10 s là mục tiêu, không phải điều kiện fail của task này); (e) C3-L1 và
    C3-L2 mỗi cái kém C3 trên đúng route của nó (chứng minh ablation có nghĩa). Không chạm
    `agentmailguard/`; kết quả zip vào `evaluation/results/mailguard_bench/<run>/`; không commit trong
    lúc RUN đang chạy (`rag_email_commit` được fingerprint). Nếu (a)–(c) fail → quay lại Task 5/7 với
    ADR ghi rõ, không chỉnh ngưỡng trên 100 case đã pin.

## Trạng thái thực thi

Xem mục "Execution log" cuối file; cập nhật sau mỗi task.

## Execution log

- [x] Task 1 — commit `7ecaf82`: plan, ADR-0015 (Proposed), golden v2 baseline + `test_guard_parity.py` (87 case).
- [x] Task 2 — trên `7ecaf82`: layout `layers/inbound_guard`, `layers/doc_scanner`, `layers/output_guard`,
  `policy/`, `prompting/` (mới, HEAD không có `prompting`), prompt đổi tên + `PROMPT_NAME`, test file đổi tên
  v3. `inbound_guard/__init__` export thêm `UserIntentExtractor`, `segment_text` (API của package L2 cũ).
  Không thêm vào `.git-blame-ignore-revs` (commit có sửa import, không phải move-only; blame vẫn theo rename).
  Kiểm tra: subtree `pytest tests/unit` 230 passed (parity 87 case, 0 delta); `ruff check` chỉ 30 lỗi notebook;
  `ruff format --check` chỉ notebook; `mypy mailguard` 30 lỗi trong 4 file `datasets/` (không mới); root
  `mg_root_tests.ps1` 64 failed = 63 baseline + 1 transient (`..._is_the_v2_pin`), 0 regression.
