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

show_health() {
  "$DIR/venv/bin/python" - <<'PY'
from miner_services import collect_telemetry_snapshot
from config import load_config

cfg = load_config()
data = collect_telemetry_snapshot(cfg, use_cache=False)
status = data.get("system", {})
gpu = data.get("gpu", {})
effective = data.get("effective_hashrate", {})
finance = data.get("finance", {})
safety = data.get("safety", {})
pool = data.get("pool", {})
local = data.get("local_miner", {})

print("=================================================")
print(" PEARL HEALTH SNAPSHOT")
print("=================================================")
print(f"Service: {status.get('service', cfg.get('MINER_SERVICE'))}")
print(f"State:   {status.get('systemd_state', 'N/A')} | process={status.get('process_running')} | pid={status.get('pid') or 'N/A'}")
print(f"GPU:     {gpu.get('gpu_name', 'N/A')} | {gpu.get('temp_c', 0)}C | {gpu.get('power_w', 0)}W | fan {gpu.get('fan_speed', 0)}%")
print(f"Hash:    {effective.get('hashrate_label', 'N/A')} | source={effective.get('source', 'unknown')} | stale={effective.get('stale')}")
print(f"Local:   {local.get('hashrate_label', 'N/A')} | stale={local.get('stale')} | shares={local.get('submitted_shares', 0)}")
print(f"Balance: {finance.get('balance_prl', 0)} PRL | USD={finance.get('balance_usd', 0):.2f} | shares24h={finance.get('shares24h', 'N/A')}")
print(f"Pool:    network={pool.get('network_hashrate_label', 'N/A')} | pool={pool.get('pool_hashrate_label', 'N/A')}")
print(f"Safety:  {safety.get('level', 'N/A')} | {'; '.join(safety.get('reasons') or ['OK'])}")
print("=================================================")
PY
}

show_events() {
  "$DIR/venv/bin/python" - <<'PY'
from database import SessionLocal
from models import SystemEvent

with SessionLocal() as db:
    rows = db.query(SystemEvent).order_by(SystemEvent.timestamp.desc(), SystemEvent.id.desc()).limit(12).all()
print("=================================================")
print(" PEARL RECENT EVENTS")
print("=================================================")
if not rows:
    print("No events.")
for row in rows:
    ts = row.timestamp.astimezone().strftime("%Y-%m-%d %H:%M:%S") if row.timestamp else "N/A"
    print(f"[{ts}] {row.level.upper()} {row.category}: {row.message}")
    if row.details:
        print(f"  {row.details[:240]}")
print("=================================================")
PY
}

while true; do
  choice=$(whiptail --title "PEARL MINER MANAGER" --menu "Choose an action:" 24 82 14 \
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
    "12" "Health snapshot" \
    "13" "Recent system events" \
    "14" "Exit" 3>&1 1>&2 2>&3) || break

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
    12) show_health; read -r -p "Press Enter..." ;;
    13) show_events; read -r -p "Press Enter..." ;;
    14) break ;;
  esac
done
