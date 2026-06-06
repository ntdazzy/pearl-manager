#!/bin/bash

echo "================================================="
echo "   TẮT HỆ THỐNG PEARL MINER MANAGER"
echo "================================================="

echo "Đang tắt Máy đào SRBMiner..."
sudo systemctl stop pearl-miner.service 2>/dev/null

echo "Đang tắt Bot Telegram..."
pkill -f "telegram_controller.py"

echo "Đang tắt Web Dashboard..."
pkill -f "uvicorn app:app"

echo "✅ Đã tắt toàn bộ hệ thống an toàn!"
echo "================================================="
# Giữ màn hình cho người dùng thấy kết quả
read -p "Nhấn [Enter] để đóng cửa sổ này..."
