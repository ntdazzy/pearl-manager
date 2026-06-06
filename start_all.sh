#!/bin/bash

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
cd "$DIR"

echo "================================================="
echo "   KÍCH HOẠT HỆ THỐNG PEARL MINER MANAGER"
echo "================================================="

# Khởi động dịch vụ đào ngầm (nếu chưa tạo service thì sẽ fail silent)
sudo systemctl enable pearl-miner.service 2>/dev/null
sudo systemctl start pearl-miner.service 2>/dev/null

# Khởi động PostgreSQL (Nếu dùng mặc định Ubuntu)
sudo systemctl start postgresql 2>/dev/null

# Khởi động Telegram Bot (ngầm)
nohup $DIR/venv/bin/python $DIR/telegram_controller.py > $DIR/telegram.log 2>&1 &

# Khởi động Web Dashboard Server (ngầm) trên Port 8555
nohup $DIR/venv/bin/uvicorn app:app --host 0.0.0.0 --port 8555 > $DIR/web.log 2>&1 &

echo "✅ Đã bật Bot Telegram (Ngầm)"
echo "✅ Đã bật Máy đào SRBMiner (Ngầm qua Systemd)"
echo "✅ Đã bật Web Dashboard (Ngầm)"
echo ""
echo "🚀 HỆ THỐNG ĐÃ HOẠT ĐỘNG!"
echo "👉 Mở trình duyệt và truy cập: http://localhost:8555"
echo "================================================="
echo ""
echo "[Ghi chú] Đang giữ cửa sổ Terminal để bạn theo dõi."
read -p "Nhấn [Enter] để tắt cửa sổ này (Hệ thống vẫn sẽ chạy ngầm)..."
