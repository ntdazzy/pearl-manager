#!/bin/bash

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
CONFIG_FILE="$DIR/config.env"
MINER_SERVICE="pearl-miner.service"
TELEGRAM_SERVICE="pearl-telegram.service"

# Khởi tạo config rỗng nếu chưa có
if [ ! -f "$CONFIG_FILE" ]; then
    echo "WALLET=" > "$CONFIG_FILE"
    echo "POOL=pearl-ca1.luckypool.io:3360" >> "$CONFIG_FILE"
    echo "MINER_DIR=/home/ntd/SRBMiner-Multi-3-3-4" >> "$CONFIG_FILE"
    echo "TELEGRAM_TOKEN=" >> "$CONFIG_FILE"
    echo "CHAT_ID=" >> "$CONFIG_FILE"
fi

source "$CONFIG_FILE"

function update_config() {
    local key=$1
    local value=$2
    if grep -q "^$key=" "$CONFIG_FILE"; then
        sed -i "s|^$key=.*|$key=$value|" "$CONFIG_FILE"
    else
        echo "$key=$value" >> "$CONFIG_FILE"
    fi
}

function show_menu() {
    while true; do
        CHOICE=$(whiptail --title "PEARL MINER MANAGER" --menu "Chọn chức năng điều khiển:" 20 70 10 \
        "1" "Khởi động Máy đào (Start Miner)" \
        "2" "Dừng Máy đào (Stop Miner)" \
        "3" "Bật/Tắt Bot Telegram (Toggle Telegram)" \
        "4" "Ép xung VGA ngay lập tức (Overclock)" \
        "5" "Cài đặt Thông số (Wallet, Pool, Telegram...)" \
        "6" "Cập nhật Systemd Services (Install Services)" \
        "7" "Xem Log Máy đào" \
        "8" "Thoát" 3>&1 1>&2 2>&3)

        if [ $? -ne 0 ]; then
            break
        fi

        case $CHOICE in
            1)
                sudo systemctl start $MINER_SERVICE
                whiptail --msgbox "Đã gửi lệnh Start đến Miner Service." 8 45
                ;;
            2)
                sudo systemctl stop $MINER_SERVICE
                whiptail --msgbox "Đã dừng Máy đào." 8 45
                ;;
            3)
                if systemctl is-active --quiet $TELEGRAM_SERVICE; then
                    sudo systemctl stop $TELEGRAM_SERVICE
                    whiptail --msgbox "Đã DỪNG Bot Telegram." 8 45
                else
                    sudo systemctl start $TELEGRAM_SERVICE
                    whiptail --msgbox "Đã BẬT Bot Telegram." 8 45
                fi
                ;;
            4)
                whiptail --msgbox "Đang thực thi lệnh Ép xung: 115W, Core 1450MHz, Mem +1000..." 8 60
                sudo nvidia-smi -pm 1
                sudo nvidia-smi --power-limit=115
                sudo nvidia-smi --lock-gpu-clocks=1450,1450
                # Yêu cầu môi trường X11, nếu ssh có thể lỗi
                DISPLAY=:0 nvidia-settings -a '[gpu:0]/GPUGraphicsClockOffset=200' 2>/dev/null
                DISPLAY=:0 nvidia-settings -a '[gpu:0]/GPUMemoryTransferRateOffset=1000' 2>/dev/null
                whiptail --msgbox "Đã ép xung hoàn tất!" 8 45
                ;;
            5)
                setup_config
                ;;
            6)
                install_services
                ;;
            7)
                clear
                echo "Đang hiển thị Log của SRBMiner (Nhấn Ctrl+C để thoát)..."
                sudo journalctl -u $MINER_SERVICE -f
                ;;
            8)
                break
                ;;
        esac
    done
}

function setup_config() {
    local w=$(whiptail --title "Cấu hình Ví" --inputbox "Nhập địa chỉ Ví Pearl của bạn:" 10 60 "$WALLET" 3>&1 1>&2 2>&3)
    [ $? -eq 0 ] && update_config "WALLET" "$w" && WALLET="$w"

    local p=$(whiptail --title "Cấu hình Pool" --inputbox "Nhập địa chỉ Mỏ đào (VD: pearl-ca1.luckypool.io:3360):" 10 60 "$POOL" 3>&1 1>&2 2>&3)
    [ $? -eq 0 ] && update_config "POOL" "$p" && POOL="$p"

    local md=$(whiptail --title "Thư mục SRBMiner" --inputbox "Đường dẫn đến thư mục chứa SRBMiner-MULTI:" 10 60 "$MINER_DIR" 3>&1 1>&2 2>&3)
    [ $? -eq 0 ] && update_config "MINER_DIR" "$md" && MINER_DIR="$md"

    local tt=$(whiptail --title "Telegram Bot Token" --inputbox "Nhập Telegram Bot Token:" 10 60 "$TELEGRAM_TOKEN" 3>&1 1>&2 2>&3)
    [ $? -eq 0 ] && update_config "TELEGRAM_TOKEN" "$tt" && TELEGRAM_TOKEN="$tt"

    local cid=$(whiptail --title "Telegram Chat ID" --inputbox "Nhập Chat ID của bạn (Bảo mật Anti-Hacker):" 10 60 "$CHAT_ID" 3>&1 1>&2 2>&3)
    [ $? -eq 0 ] && update_config "CHAT_ID" "$cid" && CHAT_ID="$cid"
    
    whiptail --msgbox "Đã lưu cấu hình vào config.env!" 8 45
}

function install_services() {
    whiptail --msgbox "Chức năng này sẽ tự động tạo file Service cho Systemd. Yêu cầu quyền sudo." 8 60
    
    # Tạo Miner Service
    cat <<EOF | sudo tee /etc/systemd/system/$MINER_SERVICE > /dev/null
[Unit]
Description=Pearl Miner Service
After=network.target nvidia-persistenced.service
Wants=nvidia-persistenced.service

[Service]
Type=simple
User=root
WorkingDirectory=$MINER_DIR
ExecStartPre=/usr/bin/nvidia-smi -pm 1
ExecStartPre=/usr/bin/nvidia-smi --power-limit=115
ExecStartPre=/usr/bin/nvidia-smi --lock-gpu-clocks=1450,1450
ExecStart=$MINER_DIR/SRBMiner-MULTI --algorithm pearlhash --pool $POOL --wallet $WALLET --api-enable --api-port 21550
Restart=always
RestartSec=15
LimitNOFILE=1048576

[Install]
WantedBy=multi-user.target
EOF

    # Tạo Telegram Service
    cat <<EOF | sudo tee /etc/systemd/system/$TELEGRAM_SERVICE > /dev/null
[Unit]
Description=Pearl Telegram Watchdog & Controller
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$DIR
ExecStart=$DIR/venv/bin/python $DIR/telegram_controller.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

    sudo systemctl daemon-reload
    sudo systemctl enable $MINER_SERVICE
    sudo systemctl enable $TELEGRAM_SERVICE
    whiptail --msgbox "Đã tạo và kích hoạt (enable) các services khởi động cùng hệ thống." 8 60
}

# Chạy menu
show_menu
