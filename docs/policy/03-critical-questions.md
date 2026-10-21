# AgentMailGuard: bộ câu hỏi phản biện (phần 3/4)

| | |
|---|---|
| Trạng thái | Nháp 1, chờ nhóm duyệt |
| Ngày | 20/10/2026 |
| Dùng khi | (1) xử lý sự cố, ở bước 5 "nguyên nhân gốc" của `02-protocol.md`; (2) chuẩn bị phản biện với giảng viên |

## 0. Cách dùng

- Mỗi câu có mã `Q-*`. Khi ghi hồ sơ sự cố, nêu mã các câu đã trả lời.
- Mỗi câu đi kèm **cách kiểm chứng**: một lệnh, một file hay một phép so sánh cụ thể. Câu hỏi không kiểm chứng được thì chỉ là ý kiến.
- File này chỉ chứa câu nhóm tự hỏi khi gặp sự cố hay khi kiểm tra kết quả. Câu giảng viên có thể hỏi, kèm hướng trả lời và số liệu, đã tách sang [`05-defense-prep.md`](05-defense-prep.md).

## 1. Câu hỏi khi phát hiện sự cố (Q-DET)

| Mã | Câu hỏi | Cách kiểm chứng |
|---|---|---|
| Q-DET-01 | Mình biết có sự cố bằng cách nào: cơ chế tự động hay tình cờ có người nhìn? Nếu là tình cờ, sự cố này đã kéo dài bao lâu? | So thời điểm phát hiện với `LastWriteTime` của file kết quả hay dòng log đầu tiên có lỗi |
| Q-DET-02 | Đây là một sự cố, hay chỉ là hành vi bình thường trông giống sự cố? Ví dụ: kit từ chối chạy là bảo vệ đang hoạt động đúng | Đối chiếu với yêu cầu `R-*` tương ứng |
| Q-DET-03 | Còn ai, cái gì khác đang chịu ảnh hưởng mà mình chưa nhìn tới? | Kiểm tra mọi config, mọi model và mọi RUN dùng cùng commit hay cùng pin |
| Q-DET-04 | Mức SEV mình chọn có đang bị hạ xuống vì muốn nhanh không? | Đọc lại định nghĩa SEV. Phân vân thì chọn mức nặng hơn |

## 2. Câu hỏi tìm nguyên nhân gốc (Q-RC)

| Mã | Câu hỏi | Cách kiểm chứng |
|---|---|---|
| Q-RC-01 | Mình đang thấy nguyên nhân, hay chỉ thấy triệu chứng? "Model lỗi" là triệu chứng. "Model trả `automation_notification`, schema chỉ nhận `automated_notification`" mới là nguyên nhân | Viết nguyên nhân thành một câu kiểm chứng được |
| Q-RC-02 | Có tái hiện được không? Nếu chạy lại đúng case đó, lỗi có quay lại? | RUN `trial-*` với `--case-ids` |
| Q-RC-03 | Lỗi này có xảy ra với model khác, config khác, nguồn khác không? | So error row của cùng `case_id` giữa các RUN |
| Q-RC-04 | Trước sự cố, cái gì đã thay đổi: commit, image, `.env`, dataset, provider, phiên bản thư viện, giờ trong ngày? | `git log`, image id, `kit-log.jsonl`, `services.<config>.log` (provider phục vụ) |
| Q-RC-05 | Nếu giả thuyết của mình đúng, còn dấu hiệu nào khác phải xuất hiện? Dấu hiệu đó có xuất hiện thật không? | Tìm dấu hiệu thứ hai độc lập với dấu hiệu ban đầu |
| Q-RC-06 | Có giải thích nào đơn giản hơn không? Ví dụ: terminal bị đóng, máy ngủ | Kiểm tra lịch sử tiến trình, nhật ký hệ thống |
| Q-RC-07 | Có hai nguyên nhân chồng lên nhau không? | Sửa một nguyên nhân rồi xem lỗi còn lại không |
| Q-RC-08 | Mình có đang tin một con số mà chưa tự kiểm tra không, ví dụ ước tính thời gian hay chi phí? | Đối chiếu với số đo thực tế |

## 3. Câu hỏi trước khi sửa (Q-FIX)

| Mã | Câu hỏi | Cách kiểm chứng |
|---|---|---|
| Q-FIX-01 | Bản sửa có làm hệ thống chuyển từ fail-closed sang fail-open ở bất kỳ đâu không? | Đọc lại R-GRD-01, R-GRD-02, R-POL-03 |
| Q-FIX-02 | Bản sửa có làm lệch so sánh giữa các model không? Ví dụ nới schema để Qwen "qua" thì Qwen được ưu đãi | Bản sửa có áp dụng giống nhau cho mọi model không? |
| Q-FIX-03 | Bản sửa có đổi code hay pin của một RUN đang dở không? Nếu có, phải mở RUN mới | R-REP-05, R-OPS-03 |
| Q-FIX-04 | Bản sửa có được chọn để làm đẹp kết quả benchmark không? Ví dụ chỉnh ngưỡng trên case đã pin | R-GRD-08: ngưỡng chỉ chọn trên tập dev |
| Q-FIX-05 | Bản sửa có hoàn tác được không? Nếu không, owner đã đồng ý chưa? | Ma trận leo thang (`02-protocol.md` mục 4) |
| Q-FIX-06 | Có test bắt được lỗi này nếu nó quay lại không? | Test mới phải đỏ trước khi sửa, xanh sau khi sửa |
| Q-FIX-07 | Bản sửa nhỏ nhất đủ chặn nguyên nhân là gì? Mình có đang sửa thêm những thứ không liên quan không? | Đọc diff |

## 4. Câu hỏi sau khi sửa (Q-VER)

| Mã | Câu hỏi | Cách kiểm chứng |
|---|---|---|
| Q-VER-01 | Mình biết bản sửa đúng bằng cách nào ngoài việc "không còn thấy lỗi"? | Test, chạy thử, so trước và sau |
| Q-VER-02 | Số liệu đã báo cáo trước đó có cần tính lại không? | PB-B6 |
| Q-VER-03 | Sổ sự cố, yêu cầu và giả định đã được cập nhật chưa? | `01-requirements.md` mục 8, `04-assumptions.md` |
| Q-VER-04 | Người duyệt có phải là người khác với người sửa không? | `02-protocol.md` mục 1 |

## 5. Câu hỏi về tính hợp lệ của kết quả (Q-VAL)

Đây là những câu cần trả lời trước khi coi một con số là kết quả nghiên cứu.

| Mã | Câu hỏi | Cách kiểm chứng |
|---|---|---|
| Q-VAL-01 | Con số này đo cái gì, và **không** đo cái gì? Tỷ lệ phòng thủ là "mục tiêu tấn công không đạt được" theo so khớp chuỗi, không phải "an toàn tuyệt đối" | Đọc định nghĩa trong `make_figures_bw.py` và trong `scoring.py` |
| Q-VAL-02 | Bỏ error row ra có làm lệch kết quả không? Nếu tính mọi error row là tấn công thành công, con số đổi bao nhiêu? | Tính lại theo kịch bản xấu nhất (xem A-EVAL-03) |
| Q-VAL-03 | Khoảng tin cậy có đủ hẹp để kết luận không? Hai config khác nhau có thật không, hay chỉ do nhiễu? | Wilson 95%, McNemar giữa các config chạy trên cùng case |
| Q-VAL-04 | Có rò rỉ train/bench không? | R-DAT-01, R-DAT-06 |
| Q-VAL-05 | Kết quả tốt có phải do bộ case quá dễ không? Ví dụ: synthetic đạt 100% | Báo cáo theo từng nguồn (hình 3) |
| Q-VAL-06 | Kết quả có phụ thuộc vào provider hay mức lượng tử hóa cụ thể không? | ADR-0014: kết quả OpenRouter không so được với bản local 4-bit |
| Q-VAL-07 | Classifier dùng trong RUN có đúng là bản đã pin không? | sha256 trong run meta (R-REP-07) |
| Q-VAL-08 | Benign utility thấp (khoảng 37–39%) là do guard, hay do pipeline gốc? | So C0 với C3. Ở gpt-4o-mini cả hai đều khoảng 38% |

## 6. Câu hỏi của giảng viên

Đã tách sang [`05-defense-prep.md`](05-defense-prep.md), kèm bảng số liệu của gpt-4o-mini và Qwen2.5-7B.
