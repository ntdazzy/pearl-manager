#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$DIR/shell_env.sh"
CONFIG_FILE="${PEARL_CONFIG:-${CONFIG_FILE:-$DIR/config.env}}"
export PEARL_CONFIG="$CONFIG_FILE"

if [[ ! -f "$CONFIG_FILE" && -f "$DIR/config.env.example" ]]; then
  cp "$DIR/config.env.example" "$CONFIG_FILE"
  chmod 600 "$CONFIG_FILE" || true
  echo "Created $CONFIG_FILE from config.env.example. Run setup_env.sh before controlling services."
  exit 1
fi
pearl_load_env_file "$CONFIG_FILE"

MINER_SERVICE="${MINER_SERVICE:-pearl-miner.service}"
WEB_SERVICE="${WEB_SERVICE:-pearl-web.service}"
BOT_SERVICE="${BOT_SERVICE:-pearl-bot.service}"

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

echo "================================================="
echo " TAT PEARL MINER MANAGER"
echo "================================================="

stop_service() {
  local service="$1"
  local output
  if ! output="$(sudo -n systemctl stop "$service" 2>&1)"; then
    echo "WARN: could not stop $service: $output" >&2
  fi
}

stop_service "$BOT_SERVICE"
stop_service "$WEB_SERVICE"
stop_service "$MINER_SERVICE"

echo "All Pearl services stopped."
echo "================================================="
