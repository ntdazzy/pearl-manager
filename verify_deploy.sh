#!/usr/bin/env bash
set -u

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$DIR/shell_env.sh"
CONFIG_FILE="${PEARL_CONFIG:-${CONFIG_FILE:-$DIR/config.env}}"
export PEARL_CONFIG="$CONFIG_FILE"

if [[ ! -f "$CONFIG_FILE" ]]; then
  echo "FAIL config: $CONFIG_FILE not found"
  exit 1
fi

pearl_load_env_file "$CONFIG_FILE"

MINER_SERVICE="${MINER_SERVICE:-pearl-miner.service}"
WEB_SERVICE="${WEB_SERVICE:-pearl-web.service}"
BOT_SERVICE="${BOT_SERVICE:-pearl-bot.service}"
WEB_PORT="${WEB_PORT:-8555}"
FAILURES=0

if [[ ! "$WEB_PORT" =~ ^[0-9]+$ || "$WEB_PORT" -lt 1 || "$WEB_PORT" -gt 65535 ]]; then
  echo "FAIL config: Invalid WEB_PORT: $WEB_PORT"
  exit 1
fi

check() {
  local name="$1"
  shift
  if "$@" >/tmp/pearl-verify.out 2>&1; then
    echo "OK   $name"
  else
    echo "FAIL $name"
    sed 's/^/     /' /tmp/pearl-verify.out | tail -20
    FAILURES=$((FAILURES + 1))
  fi
}

check_warn() {
  local name="$1"
  shift
  if "$@" >/tmp/pearl-verify.out 2>&1; then
    echo "OK   $name"
  else
    echo "WARN $name"
    sed 's/^/     /' /tmp/pearl-verify.out | tail -20
  fi
}

echo "================================================="
echo " PEARL MINER MANAGER - DEPLOY VERIFY"
echo "================================================="

check "Python venv" test -x "$DIR/venv/bin/python"
check "Required Python imports" "$DIR/venv/bin/python" - <<'PY'
import fastapi, sqlalchemy, telegram, matplotlib, requests
PY
check "Python compile" "$DIR/venv/bin/python" -m py_compile "$DIR/config.py" "$DIR/models.py" "$DIR/database.py" "$DIR/miner_services.py" "$DIR/app.py" "$DIR/telegram_bot.py" "$DIR/telegram_controller.py"
check "Shell syntax" bash -n "$DIR/setup_env.sh" "$DIR/deploy.sh" "$DIR/install.sh" "$DIR/start_all.sh" "$DIR/stop_all.sh" "$DIR/pearl-manager.sh" "$DIR/benchmark_miners.sh" "$DIR/setup_cloudflare_tunnel.sh" "$DIR/verify_deploy.sh" "$DIR/shell_env.sh"

DEFAULT_MINER_EXEC="${MINER_DIR:-}/alpha-miner"
if [[ "${MINER_TYPE:-alpha}" == "srbminer" ]]; then
  DEFAULT_MINER_EXEC="${MINER_DIR:-}/SRBMiner-MULTI"
fi
check "Miner executable" test -x "${MINER_EXEC:-$DEFAULT_MINER_EXEC}"
check "NVIDIA metrics" nvidia-smi --query-gpu=name,temperature.gpu,power.draw,memory.used,memory.total --format=csv,noheader,nounits
check "Database schema" "$DIR/venv/bin/python" - <<'PY'
from database import init_db, engine
from sqlalchemy import inspect
init_db()
tables = set(inspect(engine).get_table_names())
required = {"hardware_logs", "mining_rewards", "settings", "system_events"}
missing = required - tables
if missing:
    raise SystemExit(f"missing tables: {sorted(missing)}")
insp = inspect(engine)
columns = {table: {column["name"] for column in insp.get_columns(table)} for table in required}
expected = {
    "hardware_logs": {"timestamp", "temp_c", "power_w", "fan_speed", "hashrate_th", "vram_gb"},
    "mining_rewards": {"timestamp", "pearl_mined_hour"},
    "settings": {"wallet_address", "pool_url", "telegram_chat_id"},
    "system_events": {"timestamp", "level", "category", "message", "details"},
}
for table, names in expected.items():
    missing_columns = names - columns[table]
    if missing_columns:
        raise SystemExit(f"{table} missing columns: {sorted(missing_columns)}")
constraints = {constraint["name"] for constraint in insp.get_unique_constraints("mining_rewards")}
if "uq_mining_rewards_timestamp" not in constraints:
    raise SystemExit("missing uq_mining_rewards_timestamp")
PY

check_warn "AlphaPool miner API" "$DIR/venv/bin/python" - <<'PY'
from miner_services import fetch_pool_miner_stats
data = fetch_pool_miner_stats()
if not data["available"]:
    raise SystemExit("pool miner API unavailable")
print(data["hashrate_label"])
PY
check_warn "PRL price API" "$DIR/venv/bin/python" - <<'PY'
from miner_services import fetch_price
data = fetch_price()
if not data["available"]:
    raise SystemExit("price API unavailable")
print(data["price_usd"])
PY

check "Telegram chart render" "$DIR/venv/bin/python" - <<'PY'
from miner_services import collect_and_store_sample, render_hardware_chart

collect_and_store_sample()
chart = render_hardware_chart()
if chart is None:
    raise SystemExit("chart render returned None")
if chart.getvalue()[:8] != b"\x89PNG\r\n\x1a\n":
    raise SystemExit("chart is not PNG")
PY

check "Telegram overview render" "$DIR/venv/bin/python" - <<'PY'
from telegram_bot import render_overview_image

image = render_overview_image()
if image is None:
    raise SystemExit("overview render returned None")
if image.getvalue()[:8] != b"\x89PNG\r\n\x1a\n":
    raise SystemExit("overview is not PNG")
PY

check_warn "systemd miner service installed" systemctl status "$MINER_SERVICE" --no-pager
check_warn "systemd web service installed" systemctl status "$WEB_SERVICE" --no-pager
check_warn "systemd bot service installed" systemctl status "$BOT_SERVICE" --no-pager
check_warn "restricted sudo policy listed" sudo -n -l

telegram_config_present() {
  [[ -n "${TELEGRAM_TOKEN:-}" && -n "${TELEGRAM_CHAT_ID:-${CHAT_ID:-}}" ]]
}

check_warn "Telegram config present" telegram_config_present

if curl -fsS "http://127.0.0.1:${WEB_PORT}/api/health" >/tmp/pearl-verify.out 2>&1; then
  echo "OK   local web API on 127.0.0.1:${WEB_PORT}"
else
  echo "WARN local web API on 127.0.0.1:${WEB_PORT}"
  sed 's/^/     /' /tmp/pearl-verify.out | tail -20
fi

local_web_security_headers() {
  curl -fsS -D /tmp/pearl-verify-headers.txt -o /dev/null "http://127.0.0.1:${WEB_PORT}/api/health" &&
    grep -qi 'x-content-type-options: nosniff' /tmp/pearl-verify-headers.txt &&
    grep -qi 'x-frame-options: DENY' /tmp/pearl-verify-headers.txt
}

local_api_validation_json() {
  curl -s -i -X POST "http://127.0.0.1:${WEB_PORT}/api/control/invalid" | grep -qi 'content-type: application/json'
}

local_gpu_metrics_shape() {
  curl -fsS "http://127.0.0.1:${WEB_PORT}/api/gpu/metrics" | grep -q 'hashrate_label'
}

local_finance_shape() {
  curl -fsS "http://127.0.0.1:${WEB_PORT}/api/mining/finance" | grep -q 'balance_prl'
}

local_admin_snapshot_shape() {
  curl -fsS "http://127.0.0.1:${WEB_PORT}/api/admin/snapshot" | grep -q 'effective_hashrate'
}

local_admin_events_shape() {
  curl -fsS "http://127.0.0.1:${WEB_PORT}/api/admin/events?limit=5" | grep -q 'events'
}

check_warn "local web security headers" local_web_security_headers
check_warn "local API validation errors are JSON" local_api_validation_json
check_warn "local GPU metrics API shape" local_gpu_metrics_shape
check_warn "local finance API shape" local_finance_shape
check_warn "local admin snapshot API shape" local_admin_snapshot_shape
check_warn "local admin events API shape" local_admin_events_shape

echo "================================================="
if [[ "$FAILURES" -eq 0 ]]; then
  echo "VERIFY COMPLETE: required checks passed. Review WARN lines for optional deployment state."
else
  echo "VERIFY FAILED: $FAILURES required check(s) failed."
fi
echo "================================================="
exit "$FAILURES"
