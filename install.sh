#!/bin/bash

echo "================================================="
echo "   PEARL MINER MANAGER - INSTALLATION SCRIPT"
echo "================================================="

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
cd "$DIR"

echo "[1/4] Kiểm tra PostgreSQL & Cài đặt thư viện hệ thống..."
sudo apt-get update
sudo apt-get install -y python3-venv postgresql postgresql-contrib

echo "Đang cấu hình Database PostgreSQL (Tự động)..."
# Tạo user và db (bỏ qua lỗi nếu đã tồn tại)
sudo -u postgres psql -c "CREATE DATABASE pearl_db;" 2>/dev/null
sudo -u postgres psql -c "ALTER USER postgres PASSWORD 'postgres';" 2>/dev/null

echo "[2/4] Tạo môi trường Python ảo biệt lập (venv)..."
if [ ! -d "venv" ]; then
    python3 -m venv venv
    echo "Đã tạo thư mục venv."
fi

echo "[3/4] Cài đặt các thư viện Web và Telegram Bot..."
source venv/bin/activate
pip install --upgrade pip > /dev/null 2>&1
# Cài đặt FastAPI, Uvicorn cho máy chủ Web, SQLAlchemy cho DB
pip install fastapi uvicorn python-telegram-bot psycopg2-binary sqlalchemy requests psutil jinja2 > /dev/null 2>&1
deactivate
echo "Đã cài đặt xong thư viện."

echo "[4/4] Cấp quyền thực thi cho các kịch bản..."
chmod +x "$DIR/start_all.sh" 2>/dev/null
chmod +x "$DIR/telegram_controller.py" 2>/dev/null

echo "================================================="
echo " Cài đặt hoàn tất! Database đã sẵn sàng."
echo " Để khởi động toàn bộ hệ thống, hãy chạy lệnh:"
echo " ./start_all.sh"
echo "================================================="
