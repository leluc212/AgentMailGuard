# Biểu đồ dùng cho bài báo (bản chính thức)

Ngày: 21/10/2026. Chỉ dùng biểu đồ trong thư mục `final/`. Các thư mục khác trong `paper/figures/` là bản cũ, xem mục 6.

## 1. Bốn bộ biểu đồ

| Thư mục | Nội dung |
|---|---|
| `final/3models/` | So sánh cả 3 model trên cùng một hình. **Dùng bộ này cho bài báo** |
| `final/gpt-4o-mini/` | Chỉ gpt-4o-mini |
| `final/qwen2.5-7b/` | Chỉ Qwen2.5-7B-Instruct |
| `final/llama-3.1-8b/` | Chỉ Llama-3.1-8B-Instruct |

Mỗi thư mục có: 6 hình, mỗi hình 2 định dạng (PNG 600 dpi và PDF có nhúng font); file `all_figures.pdf` gom cả 6 hình; file `figure_data.csv` chứa mọi con số đã vẽ.

Tạo lại bằng một lệnh, ví dụ bộ 3 model:

```
uv run --project . --with matplotlib==3.9.2 python .agents/bench/make_figures_paper.py paper/figures/final/3models "gpt-4o-mini=evaluation/results/mailguard_bench/2026-10-19-gpt4omini-v3-1800" "Qwen2.5-7B-Instruct=evaluation/results/mailguard_bench/2026-10-19-qwen25-7b-openrouter-v3-1800" "Llama-3.1-8B-Instruct=evaluation/results/mailguard_bench/2026-10-19-llama31-8b-openrouter-v3-1800"
```

## 2. Ba model và cách chúng được phục vụ

Tên trong chú giải của hình lấy tự động từ meta của run, không gõ tay.

| Tên trên hình | Model id gọi tới | Phục vụ bởi | RUN |
|---|---|---|---|
| gpt-4o-mini (OpenAI API) | `gpt-4o-mini` | OpenAI API | `2026-10-19-gpt4omini-v3-1800` |
| Qwen2.5-7B-Instruct (OpenRouter, Phala) | `qwen/qwen-2.5-7b-instruct` | OpenRouter, ghim provider Phala, tắt fallback. Phala không công bố độ chính xác | `2026-10-19-qwen25-7b-openrouter-v3-1800` |
| Llama-3.1-8B-Instruct (OpenRouter, CoreWeave, bf16) | `meta-llama/llama-3.1-8b-instruct` | OpenRouter, ghim provider CoreWeave, bf16, tắt fallback | `2026-10-19-llama31-8b-openrouter-v3-1800` |

Cả 3 run dùng chung (script tự kiểm tra, khác là dừng):

| Thành phần | Giá trị |
|---|---|
| Guard AgentMailGuard | commit `d95e542` (scheme v3, fail-closed với judge) |
| rag-email | commit `790accb` |
| Bộ case | sha256 `df350223898234…`, 1,800 case mỗi config |
| Classifier L1 | sha256 `04ac26a8465d…`. **Bản train lại**, không phải bản owner đã pin. Phải nêu trong bài |
| Embedding | `text-embedding-3-small` |
| Reader của cột meaning | `gpt-4.1-mini`, không phải model đang benchmark |

## 3. Các con số được định nghĩa thế nào

| Ký hiệu | Định nghĩa |
|---|---|
| **DR (defense rate, tỷ lệ phòng thủ)** | 100 − ASR. Tỷ lệ case tấn công mà mục tiêu của kẻ tấn công **không** đạt được, chấm bằng so khớp chuỗi (chỉ số chính của kit). Case bị triage chặn tính là phòng thủ thành công |
| **Thanh sai số** | Khoảng tin cậy Wilson 95%. Với DR, đây là khoảng của ASR do kit tính, lật lại |
| **n = a/b** | a = số case **được chấm** của model đó ở config đó. b = số case **dự kiến**, lấy từ `cases.jsonl` của run |
| **Case không được chấm** | Error row: pipeline không xử lý được case, ví dụ model tự đặt ra category triage không có trong danh sách. Bị loại khỏi a, không tính là thành công hay thất bại |
| **FPR** | Tỷ lệ email benign bị chặn (block hoặc quarantine). Thấp hơn là tốt hơn |
| **Benign utility** | Tỷ lệ email benign có draft dùng được, theo quy tắc `min_draft_chars.v1`. Cao hơn là tốt hơn |

**Số case dự kiến mỗi model, mỗi config:** 1,000 email tấn công LLMail-Inject; 300 tấn công RAG (222 PoisonedRAG, 18 seed, 60 `synthetic_poison_v1`); 500 email benign (203 LLMail-Inject FP, 297 Enron).

## 4. Sáu hình

Mọi hình dùng **thanh đứng**. Trên đầu mỗi thanh có nhãn **viết ngang**: tỷ lệ, rồi `n = số chấm được/số dự kiến`. Thanh nào quá hẹp thì nhãn được ngắt thành nhiều dòng ngắn, vẫn viết ngang:

```
98.8%
n=984
/1000
```

Không có chữ số hay nhãn nào bị xoay dọc. Chỉ tiêu đề trục tung ("Defense rate (%)") giữ hướng dọc như thông thường. Script kiểm tra điều này sau khi dàn trang, và dừng nếu có nhãn bị xoay.

| Hình | Trục ngang | Các thanh | Đọc thế nào |
|---|---|---|---|
| `fig1_guard_effect_c0_vs_c3` | Tên model kèm route | Trắng = C0 (không guard), đen = C3 (guard đầy đủ) | Hiệu quả của guard với từng model. (a) email, (b) RAG |
| `fig2_defense_rate_by_configuration` | Config C0 → C3 | Mỗi model một kiểu thanh (xem chú giải) | Tỷ lệ phòng thủ tăng khi bật thêm lớp |
| `fig3_layer_ablation` | C3, rồi C3 bỏ từng phần | Như fig2 | Bên trái đường gạch là guard đầy đủ, bên phải là guard thiếu một phần |
| `fig4_rag_defense_by_source` | Tên model kèm route | Trắng = C0, đen = C3 | RAG tách theo 3 nguồn, mỗi nguồn một hình con (a), (b), (c). **Synthetic phải báo cáo riêng** |
| `fig5_benign_cost` | Tên model kèm route | Trắng = C0, đen = C3 | Cái giá trên email benign. Đây **không** phải tỷ lệ phòng thủ |
| `fig6_summary_table` | – | – | Bảng mọi model × mọi config, có cột n riêng cho email, RAG, benign. Dòng C3 in đậm |

**Kiểu thanh của mỗi model** (fig2, fig3): gpt-4o-mini = trắng; Qwen2.5-7B-Instruct = trắng gạch chéo; Llama-3.1-8B-Instruct = đen.

## 5. Đã kiểm chứng những gì

| Kiểm tra | Kết quả |
|---|---|
| Mọi chữ số và nhãn viết ngang; nằm trong khung hình và trong hình con của nó; nhãn trên các thanh không đè lên nhau; nhãn trục ngang không đè lên nhau; chú giải không đè lên tiêu đề (script tự kiểm tra sau khi dàn trang, lỗi là dừng) | Đạt, cả 4 bộ |
| Chỉ có đen, trắng và xám: đếm điểm ảnh có màu trong mọi file PNG | 0 điểm ảnh có màu, cả 4 bộ |
| Mọi con số ở fig1, fig2, fig3, fig5 khớp với `metrics.csv` (kiểm tra bằng script độc lập) | 180/180 khớp, 0 lệch |
| Tổng theo nguồn ở fig4 khớp với tổng RAG trong `metrics.csv` | Đạt (script dừng nếu lệch) |
| 3 run cùng commit guard, commit rag-email, bộ case, classifier | Đạt (script dừng nếu khác) |

## 6. Các bộ biểu đồ cũ: không dùng cho bài báo

| Thư mục | Vì sao không dùng |
|---|---|
| `paper/figures/v3-1800-3models/` | Tiêu đề gộp n của nhiều model (ví dụ `n=984/999/1000`, không rõ số nào của model nào); Qwen tô màu xám; chú giải bị cắt |
| `paper/figures/v3-1800-3models-compare/` | Bản trung gian, chưa ghi route của model, chưa có kiểm tra bố cục tự động |
| `paper/figures/2026-10-19-gpt4omini-v3-1800/` | Một model, ghi n gộp, có tô xám |
| `paper/figures/2026-10-19-qwen25-7b-openrouter-v3-1800/` | Như trên |

Các thư mục cũ được giữ lại để đối chiếu. Xóa được nếu nhóm đồng ý.

## 7. Khi đưa vào bài báo

1. Luôn ghi n cạnh mỗi tỷ lệ trong phần chữ, ví dụ "100% (n = 984/1000)", không chỉ ghi "100%".
2. Lấy số cho phần chữ từ `figure_data.csv`, không đọc bằng mắt từ hình.
3. Nêu rõ: classifier L1 là bản train lại; 60 case synthetic được tạo từ template; tấn công LLMail không được viết để vượt guard của nhóm.
4. Khi nói về Qwen, nêu số case không được chấm (khoảng 16–20 mỗi config, phần lớn do triage) và con số theo kịch bản xấu nhất.
5. Không trộn hình từ `final/` với hình từ các thư mục cũ.
