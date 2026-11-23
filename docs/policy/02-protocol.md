# AgentMailGuard: protocol xử lý sự cố (phần 2/4)

| | |
|---|---|
| Trạng thái | Nháp 1, chờ nhóm duyệt |
| Ngày | 20/10/2026 |
| Dựa trên | `01-requirements.md` (mã `R-*`, mức SEV1 đến SEV4, sổ sự cố `I-*`) |
| Khung tham chiếu | NIST SP 800-61 Rev. 3, ánh xạ vào các chức năng của CSF 2.0 |

## 1. Vai trò

Nhóm nhỏ nên một người có thể giữ nhiều vai, nhưng **người duyệt kết quả không được là người đã gây ra sự cố hay đã sửa nó**. Đây là cách giữ tính độc lập tối thiểu.

| Vai | Ai | Quyền quyết định |
|---|---|---|
| Owner | Chủ dự án | Mọi thay đổi thiết kế thí nghiệm, dataset, pin; xóa RUN đã có case; nạp credit; trả `.env` về cấu hình thường; công bố số liệu |
| Operator | Người chạy benchmark và stack | Dừng hay tiếp tục run; build lại image; xóa RUN hỏng chưa có case; phân loại SEV3/SEV4 |
| Incident lead | Người phát hiện, cho tới khi bàn giao | Phân loại mức ban đầu; khởi động playbook; mở hồ sơ sự cố |
| Reviewer | Thành viên khác trong nhóm | Duyệt nguyên nhân gốc và cách sửa; duyệt lại số liệu sau sự cố SEV1/SEV2 |
| Human approver (runtime) | Nhân viên hỗ trợ khách hàng, trong triển khai thật | Duyệt draft có `requires_human=true`. Trong nghiên cứu, vai này chỉ là giả định |

## 2. Vòng đời một sự cố

Thứ tự bên dưới theo NIST SP 800-61 Rev. 3: chuẩn bị nằm trong Govern, Identify và Protect; xử lý nằm trong Detect, Respond và Recover; bài học quay lại Identify (cải tiến).

```
Phát hiện ─▶ Phân loại SEV ─▶ Khoanh vùng ─▶ Thu bằng chứng ─▶ Tìm nguyên nhân gốc
    ─▶ Sửa ─▶ Kiểm chứng ─▶ Phục hồi ─▶ Đánh giá lại số liệu ─▶ Rút kinh nghiệm
```

| Bước | Việc phải làm | Không được làm |
|---|---|---|
| 1. Phát hiện | Ghi thời điểm, nguồn phát hiện (log, metric, kit từ chối, người nhìn) | Sửa ngay khi chưa ghi lại trạng thái |
| 2. Phân loại | Chọn SEV1–SEV4 theo `01-requirements.md` mục 3. Phân vân thì chọn mức nặng hơn | Hạ mức chỉ vì "chắc không sao" |
| 3. Khoanh vùng | SEV1/SEV2: dừng ngay thứ đang tạo ra hậu quả (run, worker, auto_send). SEV3: để cơ chế sẵn có xử lý (retry, DLQ, STOP) | Xóa dữ liệu để "dọn" hiện trường |
| 4. Bằng chứng | Giữ nguyên thư mục RUN, `kit-log.jsonl`, `raw/*.log`, audit id, commit, image id. Chụp số dư credit nếu liên quan | Commit hay chia sẻ file có khóa API |
| 5. Nguyên nhân gốc | Trả lời bộ câu hỏi ở `03-critical-questions.md`, ít nhất các câu Q-RC | Dừng ở triệu chứng ("model lỗi") khi chưa thử kiểm chứng |
| 6. Sửa | Sửa nhỏ nhất đủ chặn nguyên nhân. Có test cho sửa đổi code | Sửa schema hay ngưỡng để "cho qua" một case cụ thể |
| 7. Kiểm chứng | Chạy test, chạy thử trên vài case (RUN `trial-*`), so với trạng thái trước | Coi "chạy không báo lỗi" là đủ |
| 8. Phục hồi | Tiếp tục run (resume), hoặc mở RUN mới nếu code hay pin đã đổi (R-REP-05) | Ghi tiếp vào RUN cũ sau khi đổi code |
| 9. Đánh giá số liệu | Nếu sự cố có thể ảnh hưởng tới kết quả: xác định RUN và bảng hay hình nào bị ảnh hưởng, ghi chú hoặc chạy lại | Giữ nguyên số đã báo cáo mà không ghi chú |
| 10. Rút kinh nghiệm | Ghi vào sổ sự cố (`01-requirements.md` mục 8), cập nhật yêu cầu hay giả định | Bỏ qua bước này với SEV1/SEV2 |

## 3. Mục tiêu thời gian (đề xuất nội bộ)

Đây là mục tiêu nhóm tự đặt cho môi trường nghiên cứu, không phải SLA. Mốc 30 phút do nhóm chốt ngày 20/10/2026.

| Mức | Khoanh vùng | Báo owner | Có nguyên nhân gốc | Rút kinh nghiệm |
|---|---|---|---|---|
| SEV1 | Bắt đầu ngay, xong trong 30 phút | Trong 30 phút | Trong 24 giờ | Bắt buộc, trong 3 ngày |
| SEV2 | Trong 30 phút | Trong 30 phút | Trong 3 ngày | Bắt buộc |
| SEV3 | Theo cơ chế tự động | Trong báo cáo tiến độ kế tiếp | Khi cần | Ghi vào sổ |
| SEV4 | Không cần | Không cần | Không cần | Tùy chọn |

## 4. Ma trận leo thang

| Tình huống | Operator tự quyết | Phải hỏi owner trước |
|---|---|---|
| Run dừng vì hết credit (`STOP`) | Ghi nhận, báo số dư | Nạp credit; chuyển sang model khác hay route khác (ví dụ Ollama local) |
| Image lệch commit | Build lại image cho đúng checkout hiện tại | `git pull` lên commit mới giữa chừng một RUN |
| RUN thử hỏng, chưa có case nào | Xóa thư mục | – |
| RUN đã có case | – | Xóa, hay sửa dòng kết quả |
| Commit mới đổi dataset, policy hay guard | Đọc diff, đánh giá | Pull và chạy; dùng dữ liệu tổng hợp |
| Error rows do model trả sai định dạng | Ghi là lỗi của model | Sửa schema hay prompt để model "qua" |
| Cần đổi cấu hình `.env` của stack | – | Mọi thay đổi |
| Phát hiện lộ khóa | Thu hồi và xoay khóa ngay nếu có quyền | Báo owner ngay sau đó |

## 5. Playbook theo loại sự cố

Mỗi playbook có: dấu hiệu nhận biết, mức mặc định, các bước làm, điều kiện để đóng sự cố. Mã `PB-*` dùng để tham chiếu trong sổ sự cố.

### Miền A: runtime

#### PB-A1. Draft vi phạm đi ra ngoài (bảo vệ thất bại)

- **Dấu hiệu:** một draft làm theo lệnh tiêm, lộ PII hay bí mật, hoặc gửi tới người nhận ngoài, mà có `action=auto_send` hoặc đã DISPATCHED.
- **Mức:** SEV1.
- **Bước làm:**
  1. Tắt `auto_send` ngay: tạm bỏ hết `auto_send_categories` trong `policy.yaml`, tăng `version`. Mọi draft khi đó đều cần người duyệt.
  2. Tìm `audit_id` của job. Đọc `GuardReport`: lớp nào đã chạy, lớp nào cờ, rule nào thắng.
  3. Xác định lỗ hổng nằm ở đâu: L1 bỏ sót, L2 bỏ sót, L4 không phát hiện việc làm theo lệnh, hay policy cho qua.
  4. Tìm các job tương tự trong cùng khoảng thời gian, cùng nguồn hay cùng kỹ thuật tấn công.
  5. Thêm case đó vào bộ kiểm thử hồi quy, nhưng **không** thêm vào bộ case đã pin của benchmark (R-GRD-08).
- **Đóng khi:** có test tái hiện, có bản sửa đã kiểm chứng, và owner đồng ý bật lại `auto_send`.

#### PB-A2. Một lớp guard bị tắt hoặc lỗi hàng loạt

- **Dấu hiệu:** rule `P00` khớp hàng loạt; `judge_invalid` tăng; log có cảnh báo model guard không khả dụng (`pipeline.py:197-205`).
- **Mức:** SEV2. Hành vi fail-closed (R-GRD-01) giữ an toàn, nhưng hệ thống chuyển gần như mọi thứ sang người duyệt.
- **Bước làm:**
  1. Xác nhận hệ thống vẫn fail-closed: các job phải ở `human_approval`, không có job nào `auto_send`.
  2. Kiểm tra endpoint của model guard, khóa API, timeout, và số dư credit.
  3. **Không** đặt `MAILGUARD__FAIL_CLOSED_ON_JUDGE_ERROR=false` để giảm tải người duyệt, vì làm vậy là fail-open.
  4. Nếu đang chạy benchmark: dừng run. Kết quả trong khoảng thời gian lớp đó lỗi không đại diện cho guard đầy đủ.
- **Đóng khi:** tỷ lệ `P00` quay về mức trước sự cố, và nguyên nhân đã được ghi lại.

#### PB-A3. DLQ tăng hoặc job kẹt

- **Dấu hiệu:** `failed_jobs_total` tăng; `queue_depth` tăng mà không giảm; job ở trạng thái GENERATING quá thời hạn lease 300 giây.
- **Mức:** SEV3. Lên SEV2 nếu chạm tới SC6 (xử lý trùng) hoặc mất job.
- **Bước làm:**
  1. Đọc lý do của job trong DEAD_LETTER (`failure_policy.py`). Phân loại: lỗi vĩnh viễn hay lỗi lẽ ra phải retry.
  2. Với `UnvalidatedDraftError` có `finish_reason` là cắt cụt: tăng `max_tokens` trước khi replay.
  3. Với `LLMQuotaExhaustedError` hay HTTP 402: khôi phục credit trước khi replay.
  4. Replay job trong DEAD_LETTER (R-PIP-05), sau khi đã sửa nguyên nhân.
  5. Kiểm tra lease reaper có thu hồi job kẹt không.
- **Đóng khi:** DLQ về 0, hoặc mọi job còn lại đã có lý do và quyết định xử lý.

#### PB-A4. Triage hay retrieval suy giảm

- **Dấu hiệu:** `triage_stage_failure` (triage dùng mặc định an toàn); `retrieval_degraded_total` tăng.
- **Mức:** SEV3.
- **Bước làm:**
  1. Phân biệt lỗi hạ tầng (timeout, endpoint) với lỗi do model trả sai định dạng (ví dụ category không hợp lệ, sự cố I-02).
  2. Lỗi do model: ghi nhận, **không** nới schema riêng cho một model.
  3. Lỗi hạ tầng: kiểm tra `RETRIEVAL__RETRIEVAL_TIMEOUT_MS`, embedding endpoint, Postgres.
- **Đóng khi:** tỷ lệ lỗi về mức bình thường, hoặc đã được giải thích.

#### PB-A5. Lộ khóa hay dữ liệu nhạy cảm

- **Dấu hiệu:** khóa xuất hiện trong log, terminal, file commit, file zip; nội dung email nằm trong audit log.
- **Mức:** SEV1.
- **Bước làm:**
  1. Thu hồi và xoay khóa ngay tại nhà cung cấp (OpenAI, OpenRouter).
  2. Gỡ khóa khỏi nơi bị lộ. Nếu đã push lên git thì coi như đã lộ, kể cả khi đã xóa commit.
  3. Kiểm tra lịch sử sử dụng trên dashboard của nhà cung cấp, xem có lời gọi lạ không.
  4. Tìm nguyên nhân: đoạn code nào in ra khóa, vì sao kiểm tra của `stack_env.py` hay của lệnh `package` không chặn được.
- **Đóng khi:** khóa cũ đã thu hồi, khóa mới đã hoạt động, và có kiểm tra ngăn tái diễn.

### Miền B: benchmark và nghiên cứu

#### PB-B1. Run dừng vì `STOP` (credit, quota, provider)

- **Dấu hiệu:** log có dòng `STOP`; runner thoát mã 3; script in `STOPPED on <model>`.
- **Mức:** SEV3. Lên SEV2 nếu `STOP` là do mất provider đã ghim, vì khi đó phần còn lại phải chạy trên một provider khác.
- **Bước làm:**
  1. Đọc dòng `STOP`: nó nói rõ tài khoản và lời gọi nào.
  2. Hết credit: chạy `credits.ps1`, ước tính phần còn thiếu từ số đo thật, báo owner.
  3. Mất provider đã ghim: **không** đổi provider trong cùng RUN, vì meta đã ghi pin và kit sẽ từ chối. Hỏi owner: chờ provider quay lại, hay mở RUN mới với provider khác.
  4. Sau khi khôi phục: chạy lại đúng script. Kit tiếp từ chỗ dừng.
- **Đóng khi:** run chạy tiếp và vượt qua điểm đã dừng.

#### PB-B2. Kit từ chối chạy

- **Dấu hiệu:** `FAIL` trước config đầu tiên: image lệch commit (I-03), worktree bẩn, sha256 không khớp, biến môi trường export sẵn trong shell, lỗi chép reranker (I-04).
- **Mức:** SEV4 nếu bị bắt trước khi chạy. Chính việc kit từ chối là cơ chế bảo vệ đang hoạt động đúng.
- **Bước làm:**
  1. Đọc đúng câu `FAIL`: nó gợi ý cách sửa.
  2. Sửa nguyên nhân. **Không** sửa code kit để bỏ qua bước kiểm tra.
  3. Nếu RUN đã bị tạo nhưng chưa ghi case nào: xóa thư mục RUN rồi chạy lại.
- **Đóng khi:** kit qua được bước kiểm tra và config đầu tiên bắt đầu ghi case.

#### PB-B3. Run dừng không rõ lý do

- **Dấu hiệu:** file kết quả không có dòng mới quá khoảng 10 phút; không còn tiến trình `uv` hay `campaign`; log không có `STOP` hay `FAIL` (sự cố I-01).
- **Mức:** SEV3.
- **Bước làm:**
  1. Kiểm tra tiến trình, container (`docker ps`), dòng cuối của `services.<config>.log` và `guard-worker.<config>.log`.
  2. Kiểm tra commit và image **trước** khi chạy lại (R-OPS-03).
  3. Chạy lại đúng script.
  4. Nếu lặp lại: tìm nguyên nhân (terminal bị đóng, máy ngủ, Docker restart) trước khi chạy tiếp.
- **Đóng khi:** run chạy tiếp, và nguyên nhân đã được ghi hoặc đánh dấu "không xác định".

#### PB-B4. Error rows cao bất thường

- **Dấu hiệu:** error rows của một config vượt khoảng 2%, hoặc cao hẳn so với các config khác cùng model.
- **Mức:** SEV3. Lên SEV2 nếu lỗi tập trung ở một nguồn hay một loại case, vì khi đó việc loại các case này ra có thể làm lệch kết quả.
- **Bước làm:**
  1. Phân loại theo `error.kind` và theo nguồn (llmail, enron, poisonedrag, …).
  2. Xem lỗi có mất đi sau retry không. Mất đi là lỗi tạm thời; lặp lại là lỗi của model hay của case.
  3. Kiểm tra case lỗi có trùng giữa các model không. Ví dụ `attack-llmail-90b65bd6da80` lỗi ở mọi lần chạy.
  4. Báo cáo theo bảng: loại lỗi × nguồn × config.
- **Đóng khi:** mọi error row còn lại đều có loại và lời giải thích.

#### PB-B5. Thay đổi dataset hay thiết kế thí nghiệm

- **Dấu hiệu:** commit mới đổi `cases.jsonl`, `manifest.json`, `SHA256SUMS`, `policy.yaml`, guard pin, hay có nguồn dữ liệu mới (sự cố I-09).
- **Mức:** SEV2 nếu thay đổi trái với một quyết định đã có. Ví dụ quyết định "không dùng dữ liệu giả".
- **Bước làm:**
  1. `git fetch` và đọc diff **trước** khi pull.
  2. Liệt kê ảnh hưởng: số case, nguồn, license, dữ liệu tổng hợp, chi phí và thời gian chạy.
  3. Đưa owner các phương án. Không tự chọn.
  4. Sau khi owner quyết định: pull, build lại image, mở RUN mới.
- **Đóng khi:** owner đã quyết định, quyết định được ghi lại, và RUN mới đã chạy.

#### PB-B6. Số liệu đã báo cáo có thể sai

- **Dấu hiệu:** phát hiện rò rỉ train/bench, sai pin, sai classifier, script tính sai, hay tổng theo nguồn không khớp với `metrics.csv`.
- **Mức:** SEV1 nếu số đã nộp hay đã trình bày; SEV2 nếu chưa.
- **Bước làm:**
  1. Liệt kê các RUN, bảng và hình bị ảnh hưởng.
  2. Tính lại từ dữ liệu thô bằng script. Không sửa tay.
  3. So số cũ với số mới, ghi rõ mức chênh.
  4. Báo owner và giảng viên nếu số đã được trình bày.
- **Đóng khi:** mọi bảng và hình đã được cập nhật hoặc ghi chú, và có kiểm tra tự động ngăn tái diễn.

## 6. Danh sách kiểm tra vận hành benchmark

### 6.1 Trước khi chạy một model

- [ ] `git status` sạch, commit đúng với commit mà RUN dự kiến dùng.
- [ ] Image gắn đúng commit đó. Nếu vừa pull: chạy `stack_up.ps1`.
- [ ] `docker ps`: mọi service healthy.
- [ ] Không có biến `LLM__*`, `EMBEDDING__*`, `RETRIEVAL__*` export sẵn trong shell.
- [ ] Chạy `credits.ps1`. Số dư lớn hơn chi phí ước tính cộng thêm khoảng 20% dự phòng.
- [ ] Tên RUN mới nếu dataset, policy hay pin đã đổi (R-REP-05).
- [ ] Không có campaign nào khác đang chạy trên cùng stack (R-OPS-01).

### 6.2 Trong khi chạy

- [ ] Kiểm tra mỗi 1–2 giờ: số dòng tăng, log không có `FAIL` hay `STOP`, credit giảm đúng nhịp.
- [ ] Ghi tốc độ thực tế (case/phút) và chi phí mỗi config, để cập nhật ước tính.
- [ ] Không `git pull`, không build image, không sửa `.env`.

### 6.3 Khi tạm dừng

- [ ] Dừng bằng cách tắt tiến trình script. Kit dừng guard-worker một cách gọn gàng.
- [ ] Xác nhận không còn tiến trình benchmark và container ai-worker đã tắt.
- [ ] Ghi lại vị trí dừng: config nào, bao nhiêu case.

### 6.4 Sau khi xong một model

- [ ] Log có `REPORT OK` và đã có `metrics.csv`.
- [ ] Ghi số error row còn lại theo config, theo loại và theo nguồn.
- [ ] Kiểm tra biểu đồ đã vẽ, và tổng theo nguồn khớp `metrics.csv`.

### 6.5 Sau khi xong model cuối

- [ ] Hỏi owner trước khi trả `.env` về cấu hình thường (R-SEC-04).
- [ ] Xóa `.env.stack` sau khi owner đồng ý.
- [ ] Đóng gói kết quả bằng lệnh `package` của kit. Kiểm tra file zip không chứa khóa.

## 7. Mẫu hồ sơ sự cố

Chép mẫu này vào sổ sự cố cho mỗi sự cố SEV1 đến SEV3.

```markdown
### I-XX: <tiêu đề ngắn>

- Ngày, giờ phát hiện:
- Người phát hiện / incident lead:
- Mức: SEV_  (lý do chọn mức này:)
- Miền: A runtime / B nghiên cứu
- Playbook: PB-__
- Dấu hiệu (nguyên văn log, có che khóa):
- Bằng chứng giữ lại: RUN, file log, audit_id, commit, image id
- Phạm vi ảnh hưởng: config / case / RUN / bảng / hình nào
- Nguyên nhân gốc (đã kiểm chứng hay giả thuyết?):
- Sửa đổi (commit, test):
- Kiểm chứng:
- Ảnh hưởng tới số liệu: không / có, xử lý ra sao
- Câu hỏi phản biện đã trả lời (mã Q-*):
- Giả định bị ảnh hưởng (mã A-*):
- Bài học, cập nhật yêu cầu (mã R-*):
- Người duyệt (không phải người sửa):
```

## 8. Ánh xạ với NIST SP 800-61 Rev. 3 / CSF 2.0

| Chức năng CSF 2.0 | Trong hệ thống này |
|---|---|
| Govern (GV) | Vai trò và ma trận leo thang (mục 1 và 4); các ADR; quyết định của owner |
| Identify (ID) | Sổ sự cố, danh sách giả định (`04-assumptions.md`), mục khoảng trống trong `01-requirements.md` |
| Protect (PR) | Fail-closed, strictest-wins, channel isolation, pin, kiểm tra sha256, giữ bí mật |
| Detect (DE) | Breaker `STOP`, các lệnh từ chối của kit, metric Prometheus, error rows, audit id |
| Respond (RS) | Playbook ở mục 5, mẫu hồ sơ ở mục 7 |
| Recover (RC) | Resume run, replay DLQ, RUN mới, tính lại số liệu (PB-B6) |

Nguồn: NIST, [SP 800-61 Rev. 3](https://csrc.nist.gov/pubs/sp/800/61/r3/final) (bản cuối tháng 4/2025). Phần ánh xạ là của nhóm, không phải của NIST, và nội dung đã được diễn đạt lại.
