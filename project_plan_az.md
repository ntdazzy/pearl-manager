# KẾ HOẠCH TRIỂN KHAI TỪ A-Z (ULTIMATE MASTER PLAN)
**Dự án:** Pearl Miner Manager
Đây là đặc tả luồng công việc chi tiết tới từng hàm xử lý.

---

## Giai đoạn 1: Chuẩn bị Môi trường Tự Động Hóa (DevOps)
1. **Khởi tạo Systemd Services:**
   - Tạo `pearl-miner.service` quản lý tiến trình miner Pearl. Bản triển khai hiện tại mặc định dùng `alpha-miner` cho AlphaPool; vẫn hỗ trợ sinh service kiểu SRBMiner nếu đặt `MINER_TYPE=srbminer`.
   - Tạo `pearl-web.service` quản lý FastAPI (Uvicorn).
   - Tạo `pearl-bot.service` quản lý Telegram Bot.
2. **Cấu hình Phân quyền Linux (`visudo`):**
   - Lập trình script tự động cấp quyền NOPASSWD cho group người dùng hiện tại thực thi `nvidia-smi` và `systemctl start/stop pearl-miner`. Chặn mọi lệnh sudo khác để bảo mật.
3. **Thiết lập PostgreSQL:**
   - Schema: `hardware_logs` (thống kê phần cứng), `mining_rewards` (doanh thu), `system_events` (ghi lại các log cảnh báo).

## Giai đoạn 2: Lập trình Telegram Bot Tương Tác (The Smart Assistant)
1. **Kiến trúc Interactive UI:**
   - Sử dụng `InlineKeyboardMarkup` thay vì lệnh text truyền thống.
   - Khi người dùng gửi `/start`, Bot phản hồi bằng giao diện Menu chính:
     - `[ 📊 Thống Kê ]` `[ 💰 Tra Cứu Số Dư ]`
     - `[ ⚡ Ép Xung ]` `[ 🛑 Tắt Máy ]`
2. **Tích hợp API Đa nền tảng:**
   - Hàm `check_balance()`: Gọi API của AlphaPool để lấy số dư Pearl, có fallback parser cho payload kiểu Miningcore/Yiimp nếu pool đổi endpoint.
   - Hàm `convert_to_fiat()`: Gọi API CoinGecko lấy giá USD, sau đó nhân với tỷ giá VCB lấy giá VNĐ.
   - Hàm `predict_revenue()`: AI dự báo doanh thu cuối ngày dựa vào tốc độ Hashrate trung bình của 24h qua.
3. **Module Vẽ Biểu Đồ (Image Generator):**
   - Khi chọn Thống Kê -> Tùy chọn "Gửi Biểu đồ". 
   - Bot gọi thư viện `matplotlib`, query SQL từ `hardware_logs`, render ra biểu đồ nhiệt độ & hashrate, lưu thành `temp.png` và gửi lên Telegram bằng `send_photo()`.
4. **Hệ thống Cảnh báo Chủ Động (Proactive Watchdog):**
   - Viết Background Task chạy mỗi 60s.
   - Logic: Nếu Nhiệt độ > 80°C quá 3 phút -> Gửi cảnh báo đỏ -> Gửi lệnh tắt Miner. Nếu Hashrate = 0 -> Gửi cảnh báo lỗi Card.

## Giai đoạn 3: Thiết kế API Layer & Xử lý Thời Gian Thực (FastAPI)
1. **Hệ thống APIs lõi:**
   - `GET /api/system/status`: Trả về JSON tổng hợp (Tình trạng bật/tắt, Uptime, Tên GPU).
   - `GET /api/gpu/metrics`: Chạy `subprocess` gọi `nvidia-smi --query-gpu` lấy Nhiệt độ, Quạt, Điện, VRAM.
   - `GET /api/mining/finance`: Lấy dữ liệu API pool giống hệt Telegram Bot để show lên Web.
2. **Xử lý Đẩy Log Trực tiếp (Server-Sent Events / WebSockets):**
   - Tạo endpoint `ws://` hoặc `GET /api/logs/stream`.
   - Kết nối tới stdout của lệnh `journalctl -u pearl-miner.service -n 50 -f`. Stream từng dòng về Frontend ngay khi có sự kiện.
3. **Hệ thống Điều Khiển GPU (Control Layer):**
   - `POST /api/gpu/profile`: Nhận payload là ID của profile (Ví dụ: `eco`, `max`, `balance`). Hàm sẽ thực thi chuỗi lệnh `nvidia-settings` tương ứng.

## Giai đoạn 4: Giao diện Tương Tác Siêu Cấp (Web Frontend)
1. **Bố cục Chuẩn AffiliateBot (Light/Dark Theme):**
   - Sử dụng Flexbox/Grid hiện đại.
   - Sidebar động. Header có công cụ tìm kiếm và thông báo báo động.
2. **Bảng Điều Khiển Tài Chính & Phần cứng:**
   - Thẻ hiển thị doanh thu ước tính USD/VNĐ (Đồng bộ với logic Bot).
   - Biểu đồ thời gian thực (Live Chart) sử dụng thư viện `Chart.js`.
3. **Trung tâm Điều Khiển (Control Center):**
   - Các nút Action (Bật/Tắt) có trạng thái Loading/Disabled chống bấm nhầm (Debounce).
   - Khu vực "Live Terminal" hiển thị giao diện hacker ngầu, tự động cuộn xuống dưới cùng (auto-scroll) khi có log mới.

## Giai đoạn 5: Testing & Triển khai
1. Kiểm tra kịch bản Card chết (Ngắt thử phần mềm đào xem Watchdog tele có báo không).
2. Kiểm tra kịch bản API Coingecko sập (Fallback handling hiển thị N/A thay vì lỗi hệ thống).
3. Bàn giao mã nguồn hoàn chỉnh với 1 script `deploy.sh` duy nhất. Không yêu cầu người dùng cấu hình tay.
