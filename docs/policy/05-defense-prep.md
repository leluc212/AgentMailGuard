# AgentMailGuard: ôn tập phản biện với giảng viên

| | |
|---|---|
| Trạng thái | Nháp 1, chờ nhóm duyệt |
| Ngày | 20/10/2026 |
| Tách từ | `03-critical-questions.md` mục 6 (cũ) |
| Số liệu | gpt-4o-mini và Qwen2.5-7B, dataset 1,800 case (đã xong). Llama-3.1-8B sẽ bổ sung khi chạy xong |

## 0. Cách dùng file này

- 15 câu giảng viên có thể hỏi (`Q-LEC-*`), mỗi câu kèm **ý chính cần nói**, **số liệu dẫn chứng** và **điều nên thừa nhận**.
- Trước buổi phản biện, mỗi thành viên nên tự trả lời được ít nhất các câu đánh dấu ★ mà không cần nhìn tài liệu.
- Khi bị hỏi một câu không có ở đây: dùng các câu `Q-VAL` trong `03-critical-questions.md` để tự kiểm tra câu trả lời trước khi nói.
- Nguyên tắc chung: **thừa nhận giới hạn sớm và cụ thể** sẽ thuyết phục hơn né tránh. Mỗi giới hạn đều có mã giả định (`A-*`) trong `04-assumptions.md`.

## 1. Bảng số liệu tham chiếu

Nguồn: `evaluation/results/mailguard_bench/<RUN>/metrics.csv` và các file `ragemail__<config>.jsonl`. Tỷ lệ phòng thủ (DR) = 100 − ASR, tức tỷ lệ case tấn công mà kẻ tấn công không đạt mục tiêu, chấm bằng so khớp chuỗi. Số trong ngoặc là số case được chấm (đã loại error row).

### 1.1 Tỷ lệ phòng thủ

| Config | Email gpt-4o-mini | Email Qwen2.5-7B | RAG gpt-4o-mini | RAG Qwen2.5-7B |
|---|---|---|---|---|
| C0 (không guard) | 75.7% (756/999) | 65.6% (644/982) | 77.3% (232/300) | 18.7% (56/300) |
| **C3 (guard đầy đủ)** | **100% (1000/1000)** | **100% (983/983)** | **97.0% (291/300)** | **59.7% (179/300)** |
| C3 bỏ L1 | 89.6% | 69.6% | 93.7% | 60.7% |
| C3 bỏ L2 | 100% | 100% | 89.3% | 23.0% |
| C3 bỏ L3 | 100% | 100% | 97.0% | 58.3% |
| C3 bỏ L4 | 99.0% | 98.7% | 89.0% | 44.7% |
| C3 bỏ Isolation | 100% | 100% | 96.3% | 60.0% |

Khoảng tin cậy Wilson 95%: email ở C3 là [99.6, 100] với cả hai model. RAG ở C3: gpt-4o-mini [94.4, 98.4], Qwen [54.0, 65.1].

### 1.2 RAG ở C3, tách theo nguồn

| Nguồn | gpt-4o-mini | Qwen2.5-7B |
|---|---|---|
| PoisonedRAG (dữ liệu thật, n=222) | 95.9% (213/222) | **45.5% (101/222)** |
| Seed của dự án (n=18) | 100% | 100% |
| Synthetic v1 (n=60) | 100% | 100% |
| Chỉ dữ liệu thật và seed (n=240) | 96.25% (231/240) | 49.6% (119/240) |

### 1.3 Email benign

| Chỉ số | gpt-4o-mini C0 → C3 | Qwen2.5-7B C0 → C3 |
|---|---|---|
| FPR (benign bị chặn) | 0% → 1.4% (7/500) | 0% → 1.4% (7/500) |
| Trong đó từ Enron / LLMail | 7 / 0 | 5 / 2 |
| Benign utility | 38.8% → 37.6% | 56.3% → 56.4% |

### 1.4 Error row

| Model | Error row mỗi config (trên 1,800) | Loại chính |
|---|---|---|
| gpt-4o-mini | 0–1 | `fail_closed_validation` (1 case ở C0) |
| Qwen2.5-7B | khoảng 16–20 | `triage_stage_failure`: Qwen trả category không hợp lệ |

## 2. Về kết quả

### ★ Q-LEC-01. Tỷ lệ phòng thủ email ở C3 là 100%. Con số này có đáng ngờ không?

**Ý chính:**
- 100% là điểm ước lượng. Cận dưới của khoảng tin cậy là 99.6%, không phải 100% tuyệt đối.
- Kết quả này lặp lại ở cả hai model: gpt-4o-mini (1000/1000) và Qwen (983/983). Như vậy nó không phụ thuộc vào model sinh nội dung.
- Gần như toàn bộ là công của L1. Bỏ L1 thì gpt-4o-mini còn 89.6%, Qwen còn 69.6%.

**Số liệu:** bảng 1.1. FPR ở C3 là 1.4% với cả hai model.

**Nên thừa nhận:** tấn công LLMail được viết để vượt phòng thủ của Microsoft, không phải để vượt guard của nhóm (A-THR-02). Một kẻ tấn công thích nghi riêng với L1 có thể làm con số giảm. Nhóm chưa thử điều này.

### ★ Q-LEC-02. Không có guard, gpt-4o-mini đã phòng thủ được 75.7% email. Vậy guard đóng góp bao nhiêu?

**Ý chính:**
- Đóng góp là chênh lệch từ C0 đến C3.
- Với model mạnh thì chênh lệch nhỏ hơn, với model yếu thì lớn hơn:

| | gpt-4o-mini | Qwen2.5-7B |
|---|---|---|
| Email | 75.7% → 100% (+24.3) | 65.6% → 100% (+34.4) |
| RAG | 77.3% → 97.0% (+19.7) | 18.7% → 59.7% (+41.0) |

- Lập luận chính: guard đưa phòng thủ email của model yếu lên ngang model mạnh. Với RAG, guard giúp model yếu nhiều nhất, nhưng chưa đưa được lên ngang model mạnh.

### ★ Q-LEC-03. RAG có 60 case tổng hợp. Bỏ chúng ra thì sao?

**Ý chính:**
- Nhóm đã báo cáo tách theo nguồn ngay từ đầu (hình 3, bảng 1.2).
- Bỏ synthetic, gpt-4o-mini còn 96.25%, gần như không đổi. Còn Qwen giảm từ 59.7% xuống 49.6%.
- Synthetic dễ hơn hẳn dữ liệu thật: ở C3 cả hai model đều đạt 100% trên synthetic, nhưng Qwen chỉ đạt 45.5% trên PoisonedRAG thật.
- Đây là bằng chứng cho việc phải tách riêng: gộp chung thì synthetic che đi điểm yếu thật của Qwen.

### ★ Q-LEC-04. Lớp nào quan trọng nhất?

**Ý chính:** tùy loại tấn công.

| Bỏ lớp | Email gpt-4o-mini / Qwen (giảm, điểm) | RAG gpt-4o-mini / Qwen (giảm, điểm) |
|---|---|---|
| L1 | −10.4 / −30.4 | −3.3 / +1.0 |
| L2 | 0 / 0 | −7.7 / **−36.7** |
| L3 | 0 / 0 | 0 / −1.4 |
| L4 | −1.0 / −1.3 | −8.0 / −15.0 |
| Isolation | 0 / 0 | −0.7 / +0.3 |

- Email: L1 là lớp quyết định.
- RAG: L2 quan trọng nhất, rồi đến L4. Với Qwen, bỏ L2 thì RAG rơi từ 59.7% xuống 23.0%.

**Nên thừa nhận:**
- Bỏ một lớp không cô lập được hoàn toàn đóng góp của lớp đó, vì các lớp tương tác với nhau (A-EVAL-04).
- Những chênh lệch nhỏ (dưới khoảng 2 điểm, ví dụ +1.0 hay +0.3) nằm trong khoảng nhiễu. Không nên kết luận rằng bỏ lớp đó làm kết quả tốt lên.
- Muốn trả lời chặt hơn thì dùng kiểm định McNemar trên cùng bộ case.

### Q-LEC-05. Bỏ L3 gần như không đổi gì. Vậy L3 có cần không?

**Ý chính:**
- L3 là nơi policy ra quyết định ở cửa vào. Bỏ L3 thì các lớp khác vẫn gắn cờ, và L4 vẫn chặn ở đầu ra.
- Vai trò của L3 là chặn **sớm**, trước khi gọi model sinh nội dung. Như vậy nó giảm chi phí và giảm bề mặt tấn công, chứ không làm tăng tỷ lệ phòng thủ cuối cùng.

**Việc cần chuẩn bị thêm:** trích số lời gọi model và độ trễ của C3 so với C3-L3 từ `metrics.csv` (bảng `overhead`) để chứng minh. Hiện chưa trích.

### Q-LEC-06. Benign utility của gpt-4o-mini chỉ khoảng 38%. Hệ thống có dùng được không?

**Ý chính:**
- Utility gần như không đổi giữa C0 và C3 với cả hai model (bảng 1.3). Như vậy guard không làm giảm utility.
- Utility phụ thuộc model sinh nội dung: Qwen khoảng 56%, gpt-4o-mini khoảng 38%. Đây là giới hạn của pipeline gốc, không phải của guard.

**Nên thừa nhận:** cần giải thích rõ định nghĩa "draft dùng được" trong `scoring.py`. Ngoài ra, 297 email Enron là email nội bộ của một công ty năng lượng, không phải email hỗ trợ khách hàng (A-DATA-02).

### ★ Q-LEC-16. Sao RAG của Qwen kém vậy, kể cả khi có guard?

**Ý chính:**
- Ở C3, Qwen chỉ phòng thủ được 45.5% PoisonedRAG thật, so với 95.9% của gpt-4o-mini.
- Guard vẫn giúp rất nhiều: từ 5.0% ở C0 lên 45.5% ở C3.
- Giả thuyết: L2 cách ly được phần lớn đoạn văn có "mồi" (câu hỏi đặt ở đầu). Nhưng khi còn sót một đoạn bị đầu độc, Qwen làm theo nội dung trong đoạn đó dễ hơn gpt-4o-mini. gpt-4o-mini thì tự đối chiếu với đoạn "fact sheet" sạch, nên ít bị lừa hơn.

**Nên thừa nhận:** giả thuyết này chưa được kiểm chứng. Cách kiểm chứng: với các case Qwen thất bại ở C3, đếm xem L2 đã cách ly bao nhiêu đoạn bị đầu độc, và đoạn nào còn sót lại trong prompt.

## 3. Về phương pháp

### Q-LEC-07. Sao không dùng đúng classifier L1 đã pin?

**Ý chính:** file classifier của owner không có sẵn trên máy chạy benchmark. Nhóm train lại bằng đúng công thức của dự án và ghi sha256 vào meta của mỗi run.

**Nên thừa nhận:** đây là một sai khác thật (R-REP-07, A-DATA-07). Cần so metric validation và test của bản train lại với `l1_injection_clf_v1.metrics.json`.

### Q-LEC-08. Sao dùng OpenRouter mà không chạy model local?

**Ý chính:** theo ADR-0014, toàn bộ benchmark chạy trên cloud, mỗi model ghim một provider và tắt fallback, nên mọi lời gọi đều truy được provider nào phục vụ. Bản local lượng tử hóa 4-bit thực chất là một chương trình khác, kết quả không so được.

### ★ Q-LEC-09. Mỗi model chỉ chạy một lần. Kết quả có ổn định không?

**Ý chính:** đây là giới hạn thật (A-SYS-03). Khoảng tin cậy Wilson chỉ phản ánh độ bất định do lấy mẫu, không phản ánh độ biến thiên giữa các lần chạy.

**Đề xuất:** chạy lại một tập con, ví dụ 200 case ở C0 và C3, để ước lượng độ biến thiên. Chi phí ước tính dưới 1 USD.

### ★ Q-LEC-10. Bỏ error row có làm đẹp kết quả không?

**Ý chính:**
- gpt-4o-mini có 0–1 error row mỗi config, không đáng kể.
- Qwen có khoảng 16–20 error row mỗi config (khoảng 1%), phần lớn là `triage_stage_failure`.
- Kịch bản xấu nhất là coi mọi error row là tấn công thành công. Khi đó tỷ lệ phòng thủ email của Qwen ở C3 giảm từ 100% xuống khoảng 98.3% (983/1000).
- Nên trình bày cả hai con số.

### Q-LEC-11. So khớp chuỗi có đủ để biết tấn công thành công không?

**Ý chính:** không hoàn toàn. Nhóm có thêm cột meaning: gpt-4.1-mini đọc nghĩa của draft. Reader này không phải một trong các model đang benchmark (ADR-0012).

**Nên thừa nhận:** reader chưa được đối chiếu với nhãn do người gán (A-EVAL-01, A-EVAL-02).

## 4. Về vận hành và an toàn

### ★ Q-LEC-12. Nếu chính model guard bị tiêm lệnh thì sao?

**Ý chính:**
- Judge LLM của guard đọc chính văn bản của kẻ tấn công, nên nó cũng là mục tiêu.
- Phòng thủ hiện có: judge trả lời sai định dạng hay lỗi thì hệ thống fail-closed (R-GRD-02). Rule và classifier chạy song song với judge, nên judge bị lừa không đủ để vượt L1.

**Nên thừa nhận:** nhóm chưa kiểm tra prompt của judge có được channel isolation bảo vệ hay không, và chưa thử tấn công nhắm thẳng vào judge (A-THR-06).

### Q-LEC-13. `human_approval` có thật sự an toàn không?

**Ý chính:** chỉ an toàn khi người duyệt đọc kỹ. Nếu người duyệt mệt hoặc tin máy quá mức, `human_approval` thực tế gần bằng `auto_send` (A-SYS-01). Nghiên cứu này không đo hành vi của người duyệt.

### Q-LEC-14. Hệ thống làm gì khi một lớp guard sập?

**Ý chính:** fail-closed. Job chuyển sang `human_approval` qua rule `P00` (R-GRD-01, R-GRD-04).

**Nên thừa nhận:** hệ thống chưa có alert (R-OBS-05). Trong đợt benchmark, có một lần run dừng mà chỉ được phát hiện vì có người nhìn file kết quả (sự cố I-01).

### ★ Q-LEC-15. Nếu sửa `policy.yaml` sai thì sao?

**Ý chính:** đây là khoảng trống lớn nhất. Hiện một rule lỗi chỉ bị bỏ qua kèm dòng log (`policy/engine.py:134`, R-POL-03), tức là fail-open.

**Nên nói:** thừa nhận ngay, và đưa ra cách sửa: engine từ chối khởi động khi có rule lỗi, kèm test.

## 5. Ba câu nên tự nói trước, trước khi bị hỏi

1. "Tấn công trong benchmark không được viết để vượt guard của nhóm." (A-THR-02)
2. "RAG là điểm yếu: với model yếu như Qwen, guard đầy đủ vẫn chỉ phòng thủ được khoảng một nửa PoisonedRAG thật." (Q-LEC-16)
3. "Nhóm tách riêng 60 case tổng hợp, và gộp chung sẽ làm số đẹp hơn thực tế." (Q-LEC-03)
