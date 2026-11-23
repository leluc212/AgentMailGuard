# Implementation plan: chỉ giữ config C0 và C3 trong benchmark v3

**Trạng thái:** ĐÃ DUYỆT (theo đề xuất D1–D5) và ĐÃ THỰC HIỆN trên nhánh `chore/bench-c0-c3-only`, chưa commit.

**Khác với bản duyệt:**
- ADR mang số **0017**, không phải 0016, vì ADR-0015 quyết định 11 đã giữ số 0016 cho task 7.32. Task mới là **7.33**, vì 7.31 đã có.
- D3 hẹp hơn: chỉ từ chối file `raw/` của **8 config bị xoá** (`scheme.V3_REMOVED_CONFIGS`). Một file lạ tên khác (ví dụ `C3-L5.jsonl` của v1) vẫn bị bỏ qua như trước, vì đã có test bảo vệ hành vi đó (`test_the_meaning_reader_of_a_v3_run_leaves_out_a_stray_v1_config_file`).
- Có thêm vài test phải sửa ngoài danh sách ở 6a/6b: `test_mailguard_bench_guarded_reply.py`, `test_mailguard_live_guarded_drafting.py`, `test_mailguard_live_guard_stages.py`, `test_mailguard_live_guard_worker.py`, và `tests/integration/test_mailguard_bench_guarded_run.py`.
**Nhánh / commit lúc lập kế hoạch:** `refactor/guard-v3` @ `4fe2ec3`
**Ngày lập:** 2026-11-05 (bản sửa lần 2: xoá hẳn các config khác, thay cho cách chỉ chọn `CONFIGS=C0,C3` lúc chạy)

---

## 1. Mục tiêu

Sau khi làm xong, scheme v3 (scheme duy nhất chạy được ở HEAD) chỉ còn hai config:

| Config | Ý nghĩa |
|---|---|
| **C0** | rag-email chạy một mình, không có code AgentMailGuard (baseline) |
| **C3** | Guard đầy đủ: L1 Inbound Guard + L2 Doc Scanner + L3 Inbound Gate + L4 Output Guard + channel isolation (config mục tiêu) |

Bị xoá khỏi v3: `C0T`, `C1`, `C2`, `C3-L1`, `C3-L2`, `C3-L3`, `C3-L4`, `C3-ISO`.

Kết quả mong đợi:
- `make bench-run MODEL=... RUN=...` không cần tham số `CONFIGS` vẫn chỉ chạy C0 rồi C3.
- Gọi tên config đã xoá (`CONFIGS=C1`, `make mailguard-bench CONFIG=C3-L1`, `live.run --config C0T`…) sẽ bị từ chối, kèm thông báo nêu các config hợp lệ.
- Chức năng khác giữ nguyên, xem mục 4.

---

## 2. Phát hiện quan trọng: KHÔNG được sửa thư mục `agentmailguard/`

Benchmark ghim guard theo **tree hash** của thư mục `agentmailguard/`:
- `Makefile:165-176`: `MAILGUARD_COMMIT` và `MAILGUARD_V3_TREE = f1b3fa4d…`
- `evaluation/mailguard_bench/guard_env.py:49-53`: `V3_MAILGUARD_TREE`, `DEFAULT_MAILGUARD_COMMIT`

Mỗi lần chạy, các module sau gọi `require_pinned_worktree(...)` và **từ chối chạy** nếu tree khác pin hoặc có thay đổi chưa commit:

| Module | Dòng |
|---|---|
| `live/run.py` | 1053 |
| `live/guard_worker.py` | 424 |
| `runner.py` | 747 |
| `kit/campaign.py` | 1172 |
| `guard_smoke.py` | 176 |
| `build_cases.py` | 112 |

Nếu sửa `GuardConfig.preset` trong `agentmailguard/mailguard/pipeline.py` để xoá C1, C2 và các bản ablation, thì:
1. Benchmark ngừng chạy cho đến khi ghim lại guard. Ghim lại nghĩa là commit mới, tính tree hash mới, rồi sửa `Makefile`, `guard_env.py` và test kiểm tra hai giá trị này bằng nhau.
2. `mailguard_commit` của các run mới sẽ khác các run v3 đã công bố (`0bc439d`), nên không còn là "cùng một guard" để so sánh.
3. Phải sửa thêm 5 unit test của guard và `agentmailguard/evaluation/run_benchmark.py`. File này có default `--config C0 C1 C2 C3` và `--config all`.

**Đề xuất:** chỉ sửa phía benchmark (repo gốc), giữ nguyên `agentmailguard/`. Benchmark là nơi duy nhất quyết định config nào được chạy, qua `scheme.py`. Các preset C1, C2… vẫn còn bên trong thư viện guard, nhưng benchmark không thể gọi tới chúng nữa. Phương án sửa cả guard nằm ở mục 8 và chỉ làm nếu bạn duyệt riêng.

---

## 3. Nguyên tắc an toàn khi thực hiện

1. **Làm trên nhánh mới**, ví dụ `chore/bench-c0-c3-only`, tách từ `refactor/guard-v3`.
2. **Không đụng các thay đổi đang stage** trong `paper/figures/audit-2026-10-26/` và `.gitignore`. Mỗi commit chỉ `git add` đúng các file có tên trong bước đó, không bao giờ dùng `git add -A`.
3. **Ghi baseline test trước khi sửa** (Bước 0), để so sánh: sau khi sửa không được có test fail mới ngoài các test đã liệt kê sẵn trong plan.
4. **Không đụng scheme v1/v2.** Giữ nguyên `V1_CONFIGS`, `LAYER_ABLATIONS`, `ABLATION_CONFIGS`, `V2_*` (`scheme.py:69-110`), `guard_build.GUARDED_CONFIGS` và `BENCH_PRESETS` (`guard_build.py:54-61`), `runner.BENCH_CONFIGS`/`ABLATION_SETS`, và `legacy_reader.py`. v1 và v2 cũng dùng tên C0T, C1, C2, nên **tuyệt đối không tìm-và-xoá theo chuỗi trên toàn repo**.
5. **Không đụng kết quả đã commit:** `evaluation/results/mailguard_bench/*`, `paper/*`.
6. Mỗi bước là một commit riêng, ghi số task và requirement ID theo AGENTS.md §2.

---

## 4. Những gì KHÔNG thay đổi (cam kết)

| Thành phần | Lý do không bị ảnh hưởng |
|---|---|
| Thư viện guard `agentmailguard/` (mọi layer, policy, judge, preset) | Không sửa file nào. `make mailguard-worktree` vẫn phải báo `ok` |
| Hành vi của C0 và C3 | `V3_LAYERS["C0"]`, `V3_LAYERS["C3"]`, `v3_guard_preset("C3")`, các judge của C3 giữ nguyên |
| Pipeline rag-email (`services/`, `packages/`) | Không sửa |
| Đọc kết quả v1/v2 đã commit (`make mailguard-report LEGACY=1`) | `legacy_reader.py` chỉ import `SCHEME_V1/V2/V3`, `SchemeMixError`, `folder_scheme` từ `scheme.py` (dòng 49-55) và các helper định dạng từ `artifacts.py` (dòng 42-49). Những thứ này không đổi |
| Các run v1/v2 bị từ chối kèm commit cần dùng | `require_run_scheme`, `RETIRED_SCHEME_GUARDS` không đổi |
| Kết quả v3 đã commit (report.md, metrics.csv, summary.json, analysis/) | Là file tĩnh, không bị sinh lại. Không có `raw/` trong git (`.gitignore:90`) |
| Settings, `.env.example`, `docs/configuration.md` về key | Không thêm hay xoá key nào |
| CI workflow `.github/workflows/ci.yml` | Không sửa. Smoke của guard vốn chỉ chạy `--config C0 C3` |

---

## 5. Quyết định cần bạn duyệt trước

| # | Câu hỏi | Đề xuất |
|---|---|---|
| **D1** | Phạm vi: chỉ phía benchmark (mục 6), hay cả thư viện guard (mục 8)? | **Chỉ benchmark** (lý do ở mục 2) |
| **D2** | Code báo cáo layer ablation (`report.layer_ablation`, `artifacts._layer_ablation_section`, các dataclass `LayerAblation*`): xoá, hay giữ ở trạng thái không dùng? | **Giữ.** Hàm này đã xử lý được trường hợp danh sách rỗng (trả `None`, report bỏ mục). Xoá thì phải đụng `artifacts.py`, file mà `legacy_reader` cũng import, nên rủi ro hơn. Muốn bật lại ablation sau này chỉ cần thêm lại vào `V3_LAYERS` |
| **D3** | Nếu ai đó khôi phục `raw/` của một run v3 cũ (từ `bench-results-<id>.zip`) rồi chạy lại report: hiện tại `report.py:612` và `meaning.py:655` sẽ **lặng lẽ bỏ** C1, C2… Xử lý thế nào? | **Từ chối có thông báo:** report và meaning báo `FAIL raw/C1.jsonl is not a config of this checkout (scheme v3 = C0, C3); rebuild that run from commit <commit trước thay đổi>`. Không mất dữ liệu, không có report thiếu dòng mà không ai biết |
| **D4** | Các test dựng dữ liệu run giả có C0T, C1, C2 hay ablation (mục 6, Bước 6): xoá, hay viết lại? | Test về **tên config** thì viết lại theo danh sách mới. Test chỉ kiểm tra **bảng ablation hoặc cột C0T** thì xoá, vì tính năng đó không còn chạy trong v3 (danh sách cụ thể ở Bước 6) |
| **D5** | Spec: AGENTS.md §7 yêu cầu ADR khi thay đổi quyết định trong spec. `specs/tasks.md:884` ghi "C0, C0T and C3 are required", và `plan_v3/exec-plan-chi-tiet.md:493, 506` (Task 16b) yêu cầu chạy ablation. | Viết **ADR-0017**, cập nhật `specs/design.md`, `specs/tasks.md`, plan_v3. Không xoá hay đánh số lại requirement ID nào |

---

## 6. Các bước thực hiện (phương án đề xuất: chỉ phía benchmark)

### Bước 0. Chuẩn bị và ghi baseline (chỉ đọc, không sửa code)

1. `git switch -c chore/bench-c0-c3-only`
2. Ghi lại kết quả test hiện tại, lưu ngoài repo hoặc trong `$env:TEMP`:
   - `uv run pytest tests/unit -q`, lưu số passed/failed/skipped
   - `make mailguard-unit`: toàn bộ unit suite dưới overlay guard, gồm các test `*_guard.py`
   - `uv run ruff check .` và `uv run mypy packages services tests evaluation`
3. `make mailguard-worktree`: xác nhận guard đang ở đúng pin (`ok AgentMailGuard subtree … @ d95e542…`).
4. Ghi lại commit gốc: `git rev-parse HEAD`. Hash này dùng cho thông báo ở D3 và cho ADR.

**Đạt khi:** có đủ số liệu baseline. Nếu baseline đã có test fail sẵn thì ghi lại, không sửa.

### Bước 1. Spec và ADR trước (AGENTS.md §7)

| File | Thay đổi |
|---|---|
| `docs/adr/0017-benchmark-v3-c0-c3-only.md` (mới) | Bối cảnh, quyết định (v3 = C0, C3), lý do không sửa guard (pin tree), hệ quả (không còn C0T vs C3, không còn ablation, run v3 cũ chỉ rebuild được từ commit trước), cách bật lại |
| `specs/design.md:1109`, `:1251` | C0/C0T/C3 → C0/C3; "Nine default configs… 16,200" → "Two configs… 3,600" |
| `specs/tasks.md` | Thêm task mới (ví dụ `7.33 Benchmark v3: C0 and C3 only`, kèm requirement ID R22.12). Ở dòng 884 thêm ghi chú "superseded by ADR-0017". Không xoá dòng cũ |
| `plan_v3/exec-plan-chi-tiet.md:493, 506` | Thêm ghi chú: campaign và tiêu chí (e) đã được ADR-0017 thay thế |

**Đạt khi:** reviewer (bạn) duyệt nội dung ADR.

### Bước 2. `evaluation/mailguard_bench/scheme.py`: nguồn duy nhất của danh sách config v3

| Dòng | Hiện tại | Sau khi sửa |
|---|---|---|
| 7-17 | Docstring bảng 10 config v3 | Bảng 2 config: C0, C3, kèm "ADR-0017" |
| 116-128 `V3_LAYERS` | 10 key | `{"C0": (), "C3": _V3_FULL}` |
| 129 `V3_CONFIGS` | `tuple(V3_LAYERS)` | Giữ nguyên, tự thành `("C0", "C3")` |
| 131-138 `V3_ABLATIONS` | 5 mục | `{}`. Giữ tên và kiểu, vì `report.py:93,124` import nó |
| 139 `V3_DEFAULT_CAMPAIGN` | `V3_CONFIGS` trừ C0T | `V3_CONFIGS`, tức `("C0", "C3")`. Docstring sửa theo |
| 151-157 `V3_AI_STAGES` | Sinh từ `V3_LAYERS` | Giữ nguyên, tự thu gọn |
| 222-224 docstring `live_configs_for` | "v3's ablations run live" | "v3 runs C0 and C3 live" |
| 252-262 `v3_guard_preset` | `"C0" if config == "C0T" else config` | `return config` (bỏ nhánh C0T). Docstring sửa theo |

**Không đổi:** `V3_SWITCHES`, `V3_LAYER_JUDGES`, `V3_STAGES`, `V3_JUDGE_STAGE`, `GUARD_STAGES`, `V3_CLASSIFIER_LAYERS` (đều tính theo layer, không theo config), và toàn bộ v1/v2.

Những module tự động theo danh sách mới mà không cần sửa:
- `kit/campaign.py:120`: `DEFAULT_CONFIGS`
- `live/run.py:306-310` và `live/guard_worker.py:406-410`: qua `require_live_config`
- `scheme_report.py:98-106`: bảng config
- `report.py:612`: khám phá config

### Bước 3. Report: bỏ baseline C0T

| File:dòng | Thay đổi |
|---|---|
| `report.py:121-127` | Comment: C0 là baseline duy nhất. `BASELINES = ("C0",)` |
| `artifacts.py:1441-1464` | Bỏ phần tử C0T khỏi tuple `baselines` và khỏi `missing_baseline`. Comment "B1 = a, b" sửa thành tham chiếu ADR-0017. Nhờ vậy report không còn dòng "C0T ASR: not run" |
| `scheme_report.py` (`NO_LAYER_TEXT`, dòng ~55-58) | Bỏ key `"C0T"` |

**Không đổi:** `layer_ablation(...)`, `_layer_ablation_section` và các dataclass ablation (theo D2). Chúng nhận danh sách rỗng và bỏ mục ablation khỏi report.

### Bước 4. Chặn việc lặng lẽ bỏ config (theo D3)

| File:dòng | Thay đổi |
|---|---|
| `report.py:612-614` | Trước khi lọc, liệt kê `raw/*.jsonl` (bỏ qua các file phụ như `*.meta.json` và `guard-worker.*.log`). Có file nào mang tên config không thuộc `V3_CONFIGS` thì `raise ValueError(...)`, nêu tên file, danh sách config hợp lệ và hướng dẫn rebuild từ commit trước. `report.main` đã bắt `ValueError` để in `FAIL …` và exit 1 (dòng 919-921) |
| `meaning.py:655-657` | Kiểm tra giống hệt, dùng chung một helper đặt trong `scheme.py` (ví dụ `unknown_v3_raw_configs(run_dir)`) để không lặp code |

Test mới:
- Một folder có `raw/C1.jsonl` bị report và meaning từ chối, thông báo có chữ "C1".
- Một folder chỉ có C0 và C3 vẫn chạy như cũ.

### Bước 5. Makefile, docstring, help text

| File:dòng | Thay đổi |
|---|---|
| `Makefile:32` | Help `mailguard-bench … CONFIG=C0\|C3` và mô tả 2 config |
| `Makefile:38`, `:300` | `[CONFIGS=C0,C0T,...]` → `[CONFIGS=C0,C3]` |
| `Makefile:237-238` | Comment danh sách config v3 |
| `Makefile:247` | `case`: `v3/C0\|v3/C3\|v1/*\|v2/*`. Usage: `CONFIG=C0\|C3`. **Giữ `v1/*\|v2/*`** để runner vẫn in thông báo từ chối kèm commit |
| `runner.py:3-8` | Docstring usage và mô tả v3 (phần mô tả v1 ở dòng 10-23 giữ nguyên) |
| `live/run.py:3`, `live/guard_worker.py:14` | Docstring `--config C0\|C3` (guard_worker: `--config C3`) |
| `guard_build.py:15-17` | Docstring v3 |
| `guard_factory.py:122-123` | Ví dụ C3-L1/C3-L2 trong docstring đổi thành mô tả chung "only the layers the preset runs" |
| `kit/campaign.py:66-70` | Docstring nhắc `V3_DEFAULT_CAMPAIGN` (help `--configs` tự cập nhật) |

### Bước 6. Cập nhật test (phía repo gốc; không sửa test nào trong `agentmailguard/`)

**6a. Viết lại theo danh sách mới (test về tên config):**

| File | Test | Sửa |
|---|---|---|
| `tests/unit/test_mailguard_bench_scheme.py` | `test_the_v3_configs_are_the_guard_v3_presets` (42-56) | `V3_CONFIGS == ("C0", "C3")`; `V3_ABLATIONS == {}`; `V3_DEFAULT_CAMPAIGN == ("C0", "C3")`; `v3_guard_preset("C3") == "C3"`; C0T, C1, C3-L1 thì `require_config` raise |
| ″ | `test_every_judge_of_a_layer_a_v3_config_runs_is_live` (60-72) | Chỉ giữ các assert cho C3 và số judge bằng 5 |
| ″ | `test_the_live_configs_leave_out_the_v1_ablation` (238) | Sửa comment. Assert vẫn đúng |
| `tests/unit/test_mailguard_kit_campaign.py` | `test_the_default_config_list_is_the_v3_benchmark` (58-69) | `DEFAULT_CONFIGS == ("C0", "C3")` |
| `tests/unit/test_mailguard_make_targets.py` | Hằng `V3_CONFIGS` (124) | `("C0", "C3")`. Thêm C0T, C1, C2, C3-L1…C3-L4, C3-ISO vào danh sách phải exit 2 ở dòng 175 |
| ″ | Test loop runbook (224) | Theo vòng lặp mới trong `docs/demo-runbook.md` §9.9 (Bước 7) |
| `tests/unit/test_mailguard_bench_scheme_guard.py` | `test_the_scheme_table_is_the_guards_preset_table` (101-107) | Dòng 105 `GuardConfig.ABLATIONS == V3_ABLATIONS` không còn đúng, vì guard vẫn có 5 ablation. Đổi thành: mọi config v3 có guard bằng đúng preset của guard (dòng 102-104, giữ), và `set(V3_ABLATIONS) <= set(GuardConfig.ABLATIONS)` |
| ″ | `test_the_live_judge_rule_of_v3` (145-155) | Bỏ assert cho C0T, C1, C2. Thêm assert rằng chúng raise |
| ″ | `test_v2_and_v1_names_do_not_exist_in_v3` (127) | Thêm C0T, C1, C2, C3-L1, C3-ISO vào danh sách bị từ chối |
| `tests/unit/test_mailguard_bench_scheme_runners.py` | `test_the_v3_ablations_run_live` (262) | Đổi thành "the removed v3 configs are refused live" |
| `tests/unit/test_mailguard_bench_live_run.py` | `test_every_v3_config_records_the_full_case_set` (1128) | Lặp qua `V3_CONFIGS` thay cho `("C1", "C2", "C3-L1")` |
| `tests/unit/test_mailguard_bench_scheme_report.py` | `test_the_config_table_names_what_every_v3_config_turns_on` (50) | Bảng chỉ còn C0, C3 |
| `tests/unit/test_mailguard_bench_artifacts.py` | `test_report_labels_native_c0_and_the_c0t_baseline` (189) | Chỉ kiểm tra nhãn C0. Thêm assert: report **không** chứa "C0T" |
| `tests/unit/test_mailguard_bench_report.py` | `test_c0t_is_checked_as_a_guarded_config` (117) | Đổi thành: một folder có `raw/C0T.jsonl` bị từ chối (theo D3) |

**6b. Xoá (test chỉ kiểm tra tính năng không còn chạy trong v3, theo D4):**

| File | Test |
|---|---|
| `tests/unit/test_mailguard_bench_report.py` | `test_build_report_adds_c0t_columns_and_its_mcnemar_when_c0t_ran` (416) |
| `tests/unit/test_mailguard_bench_layer_ablation.py` | Các test dựng run v3 có `C3-L*`: dòng 84 và 115 (parametrize trên `LAYER_OF`, sẽ rỗng), 122, 149, 164, 208, 276, 319, 366, 380, 451 |
| `tests/unit/test_mailguard_bench_layer_guard.py` | Các test parametrize trên `scheme.V3_ABLATIONS` (39, 84, ~190, 220) |

**Giữ nguyên trong cùng file:**
- `test_mailguard_bench_layer_ablation.py`: dòng 66-72 (phần assert v1) và 201 (`test_a_run_without_ablation_configs_has_no_layer_ablation_section`, bỏ `with_c0t=True`). Test 201 vẫn bảo vệ code ablation đang để không dùng, theo D2.
- `test_mailguard_bench_layer_guard.py`: dòng 55 (C3-L5, C3-L3B, C3-L6 là tên lạ).

**6c. Không được sửa (test của v1/v2 và legacy):** `test_mailguard_bench_legacy_reader.py` (388, 441, 497), `test_mailguard_bench_scheme.py` (phần v1/v2: 75-139, 235-237, 248-254), `test_mailguard_bench_runner.py:383`, `test_mailguard_bench_cases.py:141`.

> Trước khi xoá, mỗi test ở 6b sẽ được đọc lại toàn bộ thân hàm. Test nào còn kiểm tra cả hành vi của C0 hoặc C3 thì sẽ được viết lại để giữ phần đó, không xoá nguyên test.

### Bước 7. Tài liệu

| File:dòng | Thay đổi |
|---|---|
| `docs/BENCHMARK.md:13-14` | "Nine default configs give 16,200…; optional C0T…" → "Two configs (C0, C3) give 3,600 case runs per model" |
| `docs/BENCHMARK.md:23`, `:373` | Danh sách config v3 → C0, C3. Bỏ đoạn về C0T và ablation |
| `docs/BENCHMARK.md:378`, `:394`, `:701` | Lệnh trial `CONFIGS=C0,C0T,C3` → `CONFIGS=C0,C3`; `for c in C0 C0T C3` → `for c in C0 C3` |
| `docs/benchmark-windows-native.md:10`, `:174` | Danh sách mặc định |
| `docs/configuration.md:323`, `:433` | "C0T to C3", "C1, C2, C3, C3-L1 to C3-L4, C3-ISO" → "C3" |
| `docs/demo-runbook.md` §9.9 (và dòng ~591) | Vòng lặp `for c in C0 C3; do run_config $c; …` (xuất hiện 2 lần, test ở dòng 224 kiểm tra). Phần v1 ở §9.x giữ nguyên |
| `README.md:141` | Lệnh trial `CONFIGS=C0,C0T,C3` → `CONFIGS=C0,C3` |
| `README.md:109` | Đoạn này mô tả preset của **thư viện guard**, vẫn đúng vì guard không đổi. Thêm một câu: "benchmark v3 chỉ chạy C0 và C3 (ADR-0017)" |

Kiểm tra cuối bước: tìm `C0T|C3-L[1-4]\b|C3-ISO|"C1"|"C2"` trong `docs/`, `Makefile`, `evaluation/mailguard_bench/`, `tests/unit/`. Mọi chỗ còn lại phải là phần v1/v2/legacy, guard (`agentmailguard/`) hoặc ADR, và được liệt kê trong báo cáo cuối.

### Bước 8. Kiểm chứng (bắt buộc trước khi báo xong)

| # | Kiểm tra | Đạt khi |
|---|---|---|
| V1 | `git diff --stat <commit gốc> -- agentmailguard/` | **Rỗng** |
| V2 | `make mailguard-worktree` | `ok AgentMailGuard subtree … @ d95e542…` (pin không đổi) |
| V3 | `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy packages services tests evaluation` | Không có lỗi mới so với baseline |
| V4 | `uv run pytest tests/unit -q` | Không có test fail mới. Số test giảm đúng bằng số test đã xoá ở 6b |
| V5 | `make mailguard-unit` (overlay guard, như CI dòng 105) | Như V4 |
| V6 | `uv run pytest tests/integration` (cần Docker: PostgreSQL, RabbitMQ) | Không có fail mới, đặc biệt `test_mailguard_bench_guarded_run.py` |
| V7 | `make bench-run MODEL=gpt-4o-mini RUN=trial-dry DRY_RUN=1` (**không** có `CONFIGS`) | Dòng đầu `configs C0, C3`. Chuỗi lệnh chỉ có C0 và C3. Không tạo folder nào |
| V8 | `make bench-run MODEL=gpt-4o-mini RUN=trial-dry CONFIGS=C0,C1 DRY_RUN=1` rồi `make mailguard-bench RUN=x CONFIG=C1` | `mailguard-bench` exit 2 với usage `CONFIG=C0\|C3`. Với `bench-run`, kit không kiểm tra tên (`campaign.py:67`), nên bước C1 sẽ bị `live.run`/`guard_worker` từ chối. Cần ghi nhận hành vi này trong báo cáo |
| V9 | `make mailguard-report RUN=2026-09-29-gpt4omini LEGACY=1` và `RUN=2026-09-30-gpt4omini-ablation LEGACY=1` | In ra bảng giống hệt trước khi sửa (so `diff` với output lưu ở Bước 0) |
| V10 | `git status` | Các file ở `paper/figures/audit-2026-10-26/` vẫn ở trạng thái stage như ban đầu, không lẫn vào commit nào |

V9 cần thêm một việc ở Bước 0: lưu output của hai lệnh `LEGACY=1` này trước khi sửa.

### Bước 9. Commit

Mỗi bước một commit, ví dụ:
```
docs(adr): ADR-0017 benchmark v3 runs C0 and C3 only [task 7.33] [R22.12]
bench(v3): scheme v3 keeps C0 and C3 only [task 7.33] [R22.12]
bench(v3): report has one baseline, refuses raw files of removed configs [task 7.33] [R22.12]
chore(make): mailguard-bench accepts C0|C3 [task 7.33]
test(bench): v3 config list is C0, C3 [task 7.33]
docs(bench): runbook and guides for the C0/C3 campaign [task 7.33]
```
Chỉ push khi bạn đồng ý.

---

## 7. Hệ quả cần biết

| Hệ quả | Chi tiết |
|---|---|
| Không còn so sánh C0T vs C3 | Không tách được tác dụng của riêng template prompt so với các layer guard |
| Không còn bảng layer ablation trong run mới | Không đo được đóng góp của từng layer (L1, L2, L3, L4, ISO) |
| Run v3 cũ chỉ rebuild được từ commit trước | Nếu khôi phục `raw/` của run 2026-10-17/18/19 và chạy lại report ở HEAD mới, report sẽ từ chối (theo D3). Muốn rebuild thì checkout commit gốc ghi ở Bước 0. File kết quả đã commit không bị ảnh hưởng |
| Hình `fig2_configurations` và `fig3_ablation` trong paper | Không sinh lại được từ run mới. Hình đã có vẫn giữ nguyên |
| Thư viện guard vẫn có preset C1, C2… | Chỉ dùng được khi gọi guard trực tiếp (`scripts/scan_email.py --config C1`, `agentmailguard/evaluation/run_benchmark.py`). Benchmark không gọi được tới chúng nữa |
| Chi phí mỗi model | 3.600 case run thay vì 16.200 (khoảng 22%) |

---

## 8. Phương án mở rộng, KHÔNG đề xuất: xoá cả preset trong thư viện guard

Chỉ làm nếu bạn duyệt riêng D1 = "cả guard". Việc cần làm thêm:
1. `agentmailguard/mailguard/pipeline.py:59-112`: `PRESETS = ("C0", "C3")`, `ABLATIONS = {}`, `preset()` chỉ nhận C0 và C3, thông báo lỗi vẫn chứa "v3 presets". Bỏ `replace` khỏi import ở dòng 23, nếu không ruff F401 sẽ báo lỗi.
2. Sửa 5 test guard sang `GuardConfig(...)` với cờ tường minh, **giữ nguyên tên hàm**, vì `tests/parity.py` (`_TASK6_TEST`, `_TASK8_TEST`) tham chiếu theo tên:
   - `test_pipeline.py::test_presets`
   - `test_pipeline.py::test_l4_catches_compliant_draft_when_upstream_missed`
   - `test_inbound_guard.py::test_c1_runs_both_inbound_guard_sub_stages`
   - `test_inbound_guard.py::test_c3_l1_runs_no_inbound_guard_sub_stage`
   - `test_output_guard_floor.py::test_the_output_guard_decides_without_the_inbound_gate`, `::test_without_the_output_guard_the_inbound_decision_is_final`
3. `agentmailguard/evaluation/run_benchmark.py:3-5, 32, 118-122, 136`: `ALL_CONFIGS` và default `--config` → `C0 C3`.
4. **Không sửa** `agentmailguard/tests/parity.py`, `tests/golden/*`, `tests/deltas/*`. Chúng dùng nhãn v2 cố định, dựng bằng cờ chứ không qua `preset()`.
5. Docs guard: `README.md:104-117`, `docs/architecture.md:138-139, 169-186`, `docs/experiments.md`, `docs/manual_steps.md:6, 14, 28-29`, docstring `channel_isolation.py:15`, `questions.py:5`.
6. **Ghim lại guard:** commit mới, tính `git rev-parse <commit>:agentmailguard`, cập nhật `Makefile:165, 174-176` và `guard_env.py:47-53` (`V3_MAILGUARD_COMMIT`, `V3_MAILGUARD_TREE`), chạy test kiểm tra hai giá trị bằng nhau.
7. Hệ quả: các run mới có `mailguard_commit` khác các run v3 đã công bố, nên phải ghi rõ trong ADR-0017 rằng đó là một guard pin mới.

---

## 9. Rollback

- Toàn bộ thay đổi nằm trên nhánh `chore/bench-c0-c3-only`. Muốn huỷ: `git switch refactor/guard-v3`, rồi xoá nhánh (chỉ khi bạn đồng ý).
- Bật lại một config sau này: thêm lại vào `V3_LAYERS` (và `V3_ABLATIONS` với ablation), `BASELINES`/`artifacts` với C0T, Makefile `case`, và khôi phục test từ commit gốc.
- Không có migration, dữ liệu hay kết quả nào bị xoá.

---

## 10. Checklist duyệt

- [ ] **D1:** chỉ sửa phía benchmark, không đụng `agentmailguard/` (đề xuất), hay cả guard (mục 8)?
- [ ] **D2:** giữ code layer ablation ở trạng thái không dùng (đề xuất)?
- [ ] **D3:** report và meaning từ chối `raw/` của config đã xoá (đề xuất), hay chỉ cảnh báo?
- [ ] **D4:** đồng ý danh sách test viết lại (6a) và xoá (6b)?
- [ ] **D5:** đồng ý viết ADR-0017 và cập nhật `specs/design.md`, `specs/tasks.md`, plan_v3?
- [ ] Tên nhánh `chore/bench-c0-c3-only` và số task `7.33` có được không?
- [ ] Tôi có được chạy Bước 0 (chỉ đọc và chạy test) ngay sau khi bạn duyệt không?
