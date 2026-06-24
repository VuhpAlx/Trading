---
description: Đồng bộ PROJECT_DEEP_DIVE.md (tài liệu cấu trúc & chức năng hệ thống) với các thay đổi code gần nhất của Quant Trading Terminal.
---

Cập nhật tài liệu hệ thống của project **Quant Trading Terminal** sau khi chỉnh sửa source. Tài liệu chính là `PROJECT_DEEP_DIVE.md` ở thư mục gốc.

## Thực hiện theo thứ tự sau:

### Bước 1 — Xác định thay đổi
Chạy `git status --short` (uncommitted) và `git diff HEAD~1 --name-only` (commit gần nhất) để biết file nào đã đổi. Phân loại theo File Map trong CLAUDE.md:
- Backend core: `main.py`, `data_layer.py`, `indicator_layer.py`, `signal_engine.py`, `simulator_manager.py`, `ai_advisor.py`, `config.py`
- Frontend: `static/index.html`
- Engine chưa wire-up: `trade_engine.py`, `prediction_engine.py`, `training_engine.py`, `position_manager.py`, `core_trading_system.py`
- File mới tạo / bị xóa / đổi tên

### Bước 2 — Đọc trước khi sửa tài liệu
Đọc `PROJECT_DEEP_DIVE.md`. Xác định đúng section tương ứng với phần code vừa thay đổi (file dùng heading đánh số: `## 4. DATA LAYER`, `## 5. INDICATOR LAYER`, `## 6. SIGNAL ENGINE`, `## 9. MAIN.PY`, `## 10. FRONTEND`, …).

### Bước 3 — Cập nhật PROJECT_DEEP_DIVE.md
Với mỗi file đã thay đổi, cập nhật đúng section của nó:
- **Cấu trúc**: sửa `## 2. CẤU TRÚC FILE` nếu thêm/xóa/đổi tên file hoặc đổi trạng thái (UNUSED → Active…).
- **Chức năng / logic**: cập nhật mô tả hàm, flow, công thức trong section của file đó cho khớp code mới. Giữ nguyên các code-block ví dụ nhưng sửa cho đúng giá trị/logic hiện tại.
- **Flow chính**: nếu đổi luồng dữ liệu (on_market_tick, generate_signal, WebSocket protocol…), cập nhật phần mô tả flow và mục `## 1. TỔNG QUAN HỆ THỐNG` nếu cần.
- Nếu sửa/đóng một BUG trong CLAUDE.md "Known Bugs", phản ánh trạng thái mới (đã fix) vào tài liệu.

### Bước 4 — Đồng bộ CLAUDE.md (chỉ khi cần)
Chỉ sửa `CLAUDE.md` khi thay đổi ảnh hưởng tới: File Map (status Active/UNUSED), Known Bugs (bug đã fix → đánh dấu), WebSocket Protocol, REST Endpoints, hoặc danh sách SYMBOLS/TIMEFRAMES trong `config.py`.

### Bước 5 — Báo cáo
Tóm tắt ngắn: đã cập nhật section nào trong PROJECT_DEEP_DIVE.md, có sửa CLAUDE.md không, và còn phần nào cần người dùng xác nhận (vd quyết định integrate/xóa dead code).

**Lưu ý:** Không bịa nội dung. Mọi mô tả phải khớp với code thực tế — nếu không chắc, đọc lại file source trước khi viết.
