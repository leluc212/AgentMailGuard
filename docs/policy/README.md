# Policy xử lý sự cố của AgentMailGuard

Tài liệu nội bộ của nhóm, dùng để vận hành hệ thống và chuẩn bị phản biện với giảng viên. Nháp 1, ngày 20/10/2026, viết dựa trên code ở commit `790accb`.

| File | Nội dung | Dùng khi |
|---|---|---|
| [01-requirements.md](01-requirements.md) | Mức SEV1–SEV4, 7 nguyên tắc, 59 yêu cầu (`R-*`) có trạng thái và dẫn code, 7 khoảng trống, sổ sự cố thật (`I-*`) | Kiểm tra hệ thống đang làm được gì |
| [02-protocol.md](02-protocol.md) | Vai trò, vòng đời sự cố, ma trận leo thang, 11 playbook (`PB-*`), checklist vận hành benchmark, mẫu hồ sơ sự cố | Đang có sự cố |
| [03-critical-questions.md](03-critical-questions.md) | 31 câu phản biện nhóm tự hỏi (`Q-DET`, `Q-RC`, `Q-FIX`, `Q-VER`, `Q-VAL`): khi phát hiện, tìm nguyên nhân, sửa, kiểm chứng, đánh giá kết quả | Tìm nguyên nhân gốc; kiểm tra một con số trước khi báo cáo |
| [04-assumptions.md](04-assumptions.md) | 33 giả định (`A-*`) về mối đe dọa, hệ thống, dữ liệu, đánh giá, vận hành; thứ tự ưu tiên kiểm chứng | Viết phần giới hạn; chọn việc cần kiểm chứng thêm |
| [05-defense-prep.md](05-defense-prep.md) | Ôn tập phản biện: bảng số liệu tham chiếu, 16 câu giảng viên có thể hỏi (`Q-LEC`), mỗi câu có ý chính, số liệu và điều nên thừa nhận | Trước buổi phản biện |

Các mã liên kết với nhau. Một hồ sơ sự cố ghi playbook (`PB-*`), câu hỏi đã trả lời (`Q-*`), giả định bị ảnh hưởng (`A-*`) và yêu cầu cần cập nhật (`R-*`).

Số liệu trong `05-defense-prep.md` hiện có gpt-4o-mini và Qwen2.5-7B (1,800 case). Llama-3.1-8B sẽ được bổ sung khi benchmark chạy xong.
