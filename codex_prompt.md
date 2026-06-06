# BẢN MÔ TẢ KỸ THUẬT (PRD) VÀ PROMPT DÀNH CHO CODEX / AI
**Tên dự án:** Pearl Miner Manager (Ultimate Edition)
**Mục tiêu:** Xây dựng siêu hệ thống Quản lý máy đào Pearl toàn diện. Tích hợp Web Dashboard thời gian thực, Bot Telegram tương tác qua Menu Inline (Interactive), tự động hóa ép xung, tính toán lợi nhuận/chuyển đổi tiền tệ, dự đoán AI và bảo vệ hệ thống trên Ubuntu.

---

## 🛑 PROMPT DÀNH CHO CODEX (COPY & PASTE BÊN DƯỚI)

**Vai trò của bạn (Codex):** Bạn là một Software Architect (Full-stack Python, DevOps, Linux, Telegram API Expert). Hãy lập trình toàn bộ dự án "Pearl Miner Manager" dựa trên đặc tả (Specification) chuyên sâu dưới đây. Code phải đảm bảo cấp độ Production, có xử lý lỗi (Exception Handling) chặt chẽ, không dùng dữ liệu ảo (Mock data).

### 1. Bối cảnh & Ràng buộc Hệ thống
- **OS:** Ubuntu 24.04 LTS.
- **Phần cứng:** i9-10850K, GPU RTX 3060 12GB. Giao diện (X11) chạy trên Intel UHD 630. GPU NVIDIA chỉ dùng để tính toán.
- **Miner:** `alpha-miner` chạy qua `systemd` cho AlphaPool hiện tại. Có thể chuyển sang SRBMiner bằng `MINER_TYPE=srbminer` nếu môi trường pool/miner tương thích.

### 2. Thành phần 1: Cơ sở dữ liệu (PostgreSQL)
Thiết kế CSDL phức tạp để phục vụ vẽ biểu đồ và dự đoán:
- **Bảng `hardware_logs`:** Ghi nhận mỗi 1 phút: `timestamp`, `temp_c`, `power_w`, `fan_speed`, `hashrate_th`, `vram_gb`.
- **Bảng `mining_rewards`:** Ghi nhận mỗi 1 giờ: `timestamp`, `pearl_mined_hour`.
- **Bảng `settings`:** Lưu trữ cấu hình `wallet_address`, `pool_url`, `telegram_chat_id`.

### 3. Thành phần 2: Telegram Bot Controller (Siêu cấp)
Bot không dùng lệnh gõ tay thô sơ, phải dùng **Inline Keyboard Markup** (Nút bấm tương tác trên màn hình chat).
- **Menu Chính (`/start`):** Gửi 1 tin nhắn kèm Ảnh tổng quan, bên dưới là các nút: 📊 Thống Kê | 🎮 Điều Khiển | 💰 Số Dư | ⚙️ Cài Đặt.
- **Module Số dư & Quy đổi thực tế (Fiat Conversion):**
  - Khi bấm nút `[💰 Số Dư]`: Bot gọi API của AlphaPool (hoặc API ví) để check số lượng Pearl thực tế.
  - Tích hợp API CoinGecko (hoặc tương đương) để lấy giá Pearl hiện tại. Quy đổi ngay số dư ra **USD** và **VNĐ**.
- **Module Dự đoán & Phân tích (Predictive Analytics):**
  - Dựa vào tốc độ Hashrate hiện tại và dữ liệu của mạng lưới để tính toán: Doanh thu ước tính 24h tới, Doanh thu ước tính 7 ngày.
  - Đánh giá: "Tốc độ đang tối ưu" hay "Nhiệt độ đang làm giảm hiệu năng".
- **Module Đồ thị Telegram:**
  - Khi bấm `[📈 Xem Biểu Đồ]`, Bot dùng thư viện `matplotlib` hoặc `plotly`, truy xuất PostgreSQL, render ra 1 tấm ảnh (Image) biểu đồ Hashrate/Nhiệt độ trong 24h và gửi thẳng vào chat.
- **Module Điều khiển & Ép xung qua Bot:**
  - Nút `[Bật Máy]` / `[Tắt Máy]`.
  - Nút `[Chế Độ Ép Xung]`: Hiện menu con gồm `🌱 Eco Mode (Tiết kiệm điện)`, `⚖️ Balance (Cân bằng)`, `🚀 Max Hashrate (Tối đa)`. Chọn chế độ nào Bot sẽ chạy lệnh `nvidia-smi` và `nvidia-settings` tương ứng.
- **Watchdog Tự động:** Chạy luồng ngầm (Background Task). Nếu nhiệt độ > 80°C, rớt Hashrate về 0, hoặc Miner bị crash -> Lập tức nhắn tin cảnh báo và Tắt máy tự động.

### 4. Thành phần 3: Backend Server (FastAPI) & API Layer
- Cung cấp API chuẩn RESTful trên port `8555`.
- **Worker Background:** Có 1 luồng chạy ngầm để lấy thông số bằng `nvidia-smi` và `journalctl` rồi ghi vào PostgreSQL.
- **WebSockets / SSE:** Phục vụ streaming Live Log từ `journalctl -u pearl-miner` tốc độ cao xuống Web mà không làm nghẽn server.
- Phải có cơ chế bỏ qua bước nhập mật khẩu `sudo` khi FastAPI gọi lệnh `systemctl`. Gợi ý: Script cài đặt phải cấu hình `visudo` cẩn thận.

### 5. Thành phần 4: Web Dashboard (Giao diện chuẩn AffiliateBot)
- Layout chuyên nghiệp: Sidebar bên trái, Topbar có User Avatar, Vùng hiển thị chính.
- **Tổng quan (Overview):** 5 ô Widget hiển thị (Tốc độ, Nhiệt độ, Điện năng, VRAM, Coin hôm nay) cập nhật Live mỗi 3 giây.
- **Biểu đồ (Charts):** Dùng `Chart.js` vẽ 2 biểu đồ: Biểu đồ Đường (Lũy kế coin) và Biểu đồ Cột (Sản lượng mỗi ngày).
- **Control Panel:** Nút bấm trực quan để Chuyển đổi Profile Ép xung, Khởi động/Dừng hệ thống.
- **Terminal Ảo:** Một khung `<pre>` đen góc phải dưới màn hình, cuộn log hệ thống thời gian thực nhận qua WebSockets.

### 6. Ràng buộc Coding & Bàn giao
- Phải viết code hoàn chỉnh cho: `database.py`, `models.py`, `telegram_bot.py` (dùng `python-telegram-bot` v20+), `app.py`, `templates/index.html`.
- Phải có script `setup_env.sh` để cài đặt tự động toàn bộ Ubuntu packages, Python venv, cấu hình Systemd Service và Visudo cho người dùng cuối.
