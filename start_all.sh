#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$DIR/shell_env.sh"
CONFIG_FILE="${PEARL_CONFIG:-${CONFIG_FILE:-$DIR/config.env}}"
export PEARL_CONFIG="$CONFIG_FILE"

if [[ ! -f "$CONFIG_FILE" && -f "$DIR/config.env.example" ]]; then
  cp "$DIR/config.env.example" "$CONFIG_FILE"
  chmod 600 "$CONFIG_FILE" || true
  echo "Created $CONFIG_FILE from config.env.example. Edit WALLET_ADDRESS before starting services."
  exit 1
fi
pearl_load_env_file "$CONFIG_FILE"

MINER_SERVICE="${MINER_SERVICE:-pearl-miner.service}"
WEB_SERVICE="${WEB_SERVICE:-pearl-web.service}"
BOT_SERVICE="${BOT_SERVICE:-pearl-bot.service}"
WEB_PORT="${WEB_PORT:-8555}"

validate_service_name() {
  local value="$1"
  if [[ "$value" != "${value##*/}" || ! "$value" =~ ^[A-Za-z0-9_.@-]+\.service$ ]]; then
    echo "Invalid systemd service name: $value" >&2
    exit 1
  fi
}

validate_service_name "$MINER_SERVICE"
validate_service_name "$WEB_SERVICE"
validate_service_name "$BOT_SERVICE"
if [[ -z "${WALLET_ADDRESS:-}" || "${WALLET_ADDRESS}" == "CHANGE_ME_PEARL_WALLET" ]]; then
  echo "Set WALLET_ADDRESS in $CONFIG_FILE before starting services." >&2
  exit 1
fi
if [[ ! "$WEB_PORT" =~ ^[0-9]+$ || "$WEB_PORT" -lt 1 || "$WEB_PORT" -gt 65535 ]]; then
  echo "Invalid WEB_PORT: $WEB_PORT" >&2
  exit 1
fi

echo "================================================="
echo " KICH HOAT PEARL MINER MANAGER"
echo "================================================="

notify_miner_started() {
  if [[ -x "$DIR/venv/bin/python" ]]; then
    "$DIR/venv/bin/python" - <<'PY' || true
from miner_services import notify_miner_started
notify_miner_started("start")
PY
  fi
}

sudo -n systemctl start postgresql.service
sudo -n systemctl start "$MINER_SERVICE"
notify_miner_started
sudo -n systemctl start "$WEB_SERVICE"
if [[ -n "${TELEGRAM_TOKEN:-}" && -n "${TELEGRAM_CHAT_ID:-${CHAT_ID:-}}" ]]; then
  sudo -n systemctl start "$BOT_SERVICE"
  echo "Telegram bot: started"
else
  echo "Telegram bot: skipped (missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID)"
fi

echo "Miner:   $(systemctl is-active "$MINER_SERVICE" || true)"
echo "Web:     $(systemctl is-active "$WEB_SERVICE" || true)"
echo "Bot:     $(systemctl is-active "$BOT_SERVICE" || true)"
echo "Open:    http://localhost:${WEB_PORT}"
echo "================================================="
