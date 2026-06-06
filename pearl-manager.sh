#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$DIR/shell_env.sh"
CONFIG_FILE="${PEARL_CONFIG:-${CONFIG_FILE:-$DIR/config.env}}"
export PEARL_CONFIG="$CONFIG_FILE"

if [[ ! -f "$CONFIG_FILE" && -f "$DIR/config.env.example" ]]; then
  cp "$DIR/config.env.example" "$CONFIG_FILE"
  chmod 600 "$CONFIG_FILE" || true
fi

pearl_load_env_file "$CONFIG_FILE"

MINER_SERVICE="${MINER_SERVICE:-pearl-miner.service}"
WEB_SERVICE="${WEB_SERVICE:-pearl-web.service}"
BOT_SERVICE="${BOT_SERVICE:-pearl-bot.service}"

validate_service_name() {
  local value="$1"
  if [[ "$value" != "${value##*/}" || ! "$value" =~ ^[A-Za-z0-9_.@-]+\.service$ ]]; then
    whiptail --msgbox "Invalid service name: $value" 8 70
    exit 1
  fi
}

quote_env_value() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  value="${value//\$/\\$}"
  value="${value//\`/\\\`}"
  printf '"%s"' "$value"
}

validate_service_name "$MINER_SERVICE"
validate_service_name "$WEB_SERVICE"
validate_service_name "$BOT_SERVICE"

require_wallet() {
  if [[ -z "${WALLET_ADDRESS:-}" || "${WALLET_ADDRESS}" == "CHANGE_ME_PEARL_WALLET" ]]; then
    whiptail --msgbox "Set WALLET_ADDRESS in $CONFIG_FILE before controlling miner services." 8 74
    return 1
  fi
}

update_config() {
  local key="$1"
  local value="$2"
  local encoded
  local tmp
  encoded="$(quote_env_value "$value")"
  tmp="$(mktemp)"
  awk -v key="$key" -v encoded="$encoded" '
    BEGIN { updated = 0 }
    $0 ~ "^[[:space:]]*" key "[[:space:]]*=" {
      print key "=" encoded
      updated = 1
      next
    }
    { print }
    END {
      if (!updated) print key "=" encoded
    }
  ' "$CONFIG_FILE" > "$tmp"
  cat "$tmp" > "$CONFIG_FILE"
  rm -f "$tmp"
}

restart_stack() {
  sudo -n systemctl daemon-reload
  sudo -n systemctl restart "$WEB_SERVICE" || true
  sudo -n systemctl restart "$BOT_SERVICE" || true
}

configure_values() {
  local wallet pool_host pool_port worker token chat
  wallet=$(whiptail --title "Wallet" --inputbox "Pearl wallet address:" 10 86 "${WALLET_ADDRESS:-}" 3>&1 1>&2 2>&3) && update_config WALLET_ADDRESS "$wallet"
  worker=$(whiptail --title "Worker" --inputbox "Worker name:" 10 60 "${WORKER_NAME:-NTD_Rig}" 3>&1 1>&2 2>&3) && update_config WORKER_NAME "$worker"
  pool_host=$(whiptail --title "AlphaPool Host" --inputbox "Closest AlphaPool stratum host:" 10 70 "${POOL_HOST:-sg1.alphapool.tech}" 3>&1 1>&2 2>&3) && update_config POOL_HOST "$pool_host"
  pool_port=$(whiptail --title "AlphaPool Port" --inputbox "AlphaPool stratum port:" 10 40 "${POOL_PORT:-5566}" 3>&1 1>&2 2>&3) && update_config POOL_PORT "$pool_port"
  token=$(whiptail --title "Telegram Token" --inputbox "Bot token:" 10 86 "${TELEGRAM_TOKEN:-}" 3>&1 1>&2 2>&3) && update_config TELEGRAM_TOKEN "$token"
  chat=$(whiptail --title "Telegram Chat ID" --inputbox "Authorized chat id:" 10 60 "${TELEGRAM_CHAT_ID:-${CHAT_ID:-}}" 3>&1 1>&2 2>&3) && update_config TELEGRAM_CHAT_ID "$chat"
  whiptail --msgbox "Saved $CONFIG_FILE. Run setup again to rewrite systemd services if miner path/pool changed." 9 78
}

apply_profile() {
  local profile="$1"
  "$DIR/venv/bin/python" - <<PY
from miner_services import apply_oc_profile
print(apply_oc_profile("${profile}"))
PY
}

notify_miner_started() {
  local action="$1"
  "$DIR/venv/bin/python" - <<PY || true
from miner_services import notify_miner_started
notify_miner_started("${action}")
PY
}

while true; do
  choice=$(whiptail --title "PEARL MINER MANAGER" --menu "Choose an action:" 22 78 12 \
    "1" "Start miner" \
    "2" "Stop miner" \
    "3" "Restart miner" \
    "4" "Start web dashboard" \
    "5" "Start Telegram bot" \
    "6" "Apply OC: Eco" \
    "7" "Apply OC: Balance" \
    "8" "Apply OC: Max" \
    "9" "Edit config" \
    "10" "Run setup_env.sh" \
    "11" "Tail miner logs" \
    "12" "Exit" 3>&1 1>&2 2>&3) || break

  case "$choice" in
    1) require_wallet && sudo -n systemctl start "$MINER_SERVICE" && notify_miner_started start && whiptail --msgbox "Miner started." 8 40 ;;
    2) sudo -n systemctl stop "$MINER_SERVICE" && whiptail --msgbox "Miner stopped." 8 40 ;;
    3) require_wallet && sudo -n systemctl restart "$MINER_SERVICE" && notify_miner_started restart && whiptail --msgbox "Miner restarted." 8 40 ;;
    4) sudo -n systemctl restart "$WEB_SERVICE" && whiptail --msgbox "Web dashboard restarted." 8 45 ;;
    5) sudo -n systemctl restart "$BOT_SERVICE" && whiptail --msgbox "Telegram bot restarted." 8 45 ;;
    6) require_wallet && apply_profile eco; read -r -p "Press Enter..." ;;
    7) require_wallet && apply_profile balance; read -r -p "Press Enter..." ;;
    8) require_wallet && apply_profile max; read -r -p "Press Enter..." ;;
    9) configure_values; restart_stack ;;
    10) "$DIR/setup_env.sh"; read -r -p "Press Enter..." ;;
    11) journalctl -u "$MINER_SERVICE" -f ;;
    12) break ;;
  esac
done
