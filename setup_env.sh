#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$DIR/shell_env.sh"
CONFIG_FILE="${PEARL_CONFIG:-${CONFIG_FILE:-$DIR/config.env}}"
export PEARL_CONFIG="$CONFIG_FILE"

if [[ ! -f "$CONFIG_FILE" ]]; then
  if [[ -f "$DIR/config.env.example" ]]; then
    cp "$DIR/config.env.example" "$CONFIG_FILE"
    chmod 600 "$CONFIG_FILE" || true
    echo "Created $CONFIG_FILE from config.env.example. Review wallet, Telegram, and database values before production use."
  else
    echo "Missing config.env and config.env.example." >&2
    exit 1
  fi
fi

pearl_load_env_file "$CONFIG_FILE"

SERVICE_USER="${SERVICE_USER:-${SUDO_USER:-$USER}}"
SERVICE_GROUP="${SERVICE_GROUP:-$(id -gn "$SERVICE_USER")}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
DRY_RUN="${DRY_RUN:-0}"
DRY_RUN_DIR="${DRY_RUN_DIR:-$DIR/.dry-run}"

WEB_PORT="${WEB_PORT:-8555}"
MINER_SERVICE="${MINER_SERVICE:-pearl-miner.service}"
WEB_SERVICE="${WEB_SERVICE:-pearl-web.service}"
BOT_SERVICE="${BOT_SERVICE:-pearl-bot.service}"
SYSTEMCTL="/usr/bin/systemctl"
if [[ ! -x "$SYSTEMCTL" ]]; then
  SYSTEMCTL="$(command -v systemctl)"
fi
NVIDIA_SMI="/usr/bin/nvidia-smi"
if [[ ! -x "$NVIDIA_SMI" ]]; then
  NVIDIA_SMI="$(command -v nvidia-smi || true)"
fi
if [[ -z "$NVIDIA_SMI" ]]; then
  NVIDIA_SMI="/usr/bin/nvidia-smi"
fi

validate_service_name() {
  local value="$1"
  if [[ "$value" != "${value##*/}" || ! "$value" =~ ^[A-Za-z0-9_.@-]+\.service$ ]]; then
    echo "Invalid systemd service name: $value" >&2
    exit 1
  fi
}

validate_account_name() {
  local label="$1"
  local value="$2"
  if [[ ! "$value" =~ ^[A-Za-z_][A-Za-z0-9_-]*[$]?$ ]]; then
    echo "Invalid $label: $value" >&2
    exit 1
  fi
}

reject_newline_value() {
  local name="$1"
  local value="${2:-}"
  if [[ "$value" == *$'\n'* || "$value" == *$'\r'* ]]; then
    echo "Invalid newline in $name" >&2
    exit 1
  fi
}

systemd_escape_value() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  value="${value//%/%%}"
  printf '%s' "$value"
}

systemd_quote_arg() {
  printf '"%s"' "$(systemd_escape_value "$1")"
}

systemd_directive_value() {
  systemd_escape_value "$1"
}

systemd_join_args() {
  local arg
  local output=""
  for arg in "$@"; do
    output+=" $(systemd_quote_arg "$arg")"
  done
  printf '%s' "$output"
}

sudoers_arg_value() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//,/\\,}"
  printf '%s' "$value"
}

validate_service_name "$MINER_SERVICE"
validate_service_name "$WEB_SERVICE"
validate_service_name "$BOT_SERVICE"
validate_account_name "SERVICE_USER" "$SERVICE_USER"
validate_account_name "SERVICE_GROUP" "$SERVICE_GROUP"
for key in SERVICE_USER SERVICE_GROUP WALLET_ADDRESS WORKER_NAME POOL_HOST POOL_PORT MINER_TYPE MINER_DIR MINER_EXEC MINER_ALGORITHM MINER_PASSWORD MINER_EXTRA_ARGS WEB_HOST WEB_PORT DISPLAY XAUTHORITY DB_USER DB_PASS DB_NAME GPU_INDEX STARTUP_OC_PROFILE; do
  reject_newline_value "$key" "${!key:-}"
done
if [[ -z "${WALLET_ADDRESS:-}" || "${WALLET_ADDRESS}" == "CHANGE_ME_PEARL_WALLET" ]]; then
  echo "Set WALLET_ADDRESS in $CONFIG_FILE before installing miner services." >&2
  exit 1
fi
if [[ ! "$WEB_PORT" =~ ^[0-9]+$ || "$WEB_PORT" -lt 1 || "$WEB_PORT" -gt 65535 ]]; then
  echo "Invalid WEB_PORT: $WEB_PORT" >&2
  exit 1
fi
if [[ ! "${POOL_PORT:-}" =~ ^[0-9]+$ || "$POOL_PORT" -lt 1 || "$POOL_PORT" -gt 65535 ]]; then
  echo "Invalid POOL_PORT: ${POOL_PORT:-}" >&2
  exit 1
fi
if [[ ! "${GPU_INDEX:-0}" =~ ^[0-9]+$ ]]; then
  echo "Invalid GPU_INDEX: ${GPU_INDEX:-}" >&2
  exit 1
fi
if [[ "${WEB_HOST:-127.0.0.1}" == "0.0.0.0" && -z "${CONTROL_API_TOKEN:-}" ]]; then
  echo "WARN: WEB_HOST=0.0.0.0 exposes read-only dashboard data on the network."
  echo "WARN: Set CONTROL_API_TOKEN if you want remote dashboard control buttons to work."
fi

MINER_LOGIN="$WALLET_ADDRESS"
if [[ "$MINER_LOGIN" != *.* && -n "${WORKER_NAME:-}" ]]; then
  MINER_LOGIN="${WALLET_ADDRESS}.${WORKER_NAME}"
fi

POOL_TARGET="${POOL_HOST}:${POOL_PORT}"
MINER_TYPE="${MINER_TYPE:-alpha}"
EXTRA_ARGS=()
if [[ -n "${MINER_EXTRA_ARGS:-}" ]]; then
  read -r -a EXTRA_ARGS <<< "$MINER_EXTRA_ARGS"
fi
MINER_CMD_ARGS=()
if [[ "${MINER_TYPE,,}" == "srbminer" ]]; then
  MINER_CMD_ARGS=(--disable-cpu --algorithm "$MINER_ALGORITHM" --pool "$POOL_TARGET" --wallet "$MINER_LOGIN")
else
  MINER_CMD_ARGS=(--pool "stratum+tcp://$POOL_TARGET" --address "$WALLET_ADDRESS")
  if [[ -n "${WORKER_NAME:-}" ]]; then
    MINER_CMD_ARGS+=(--worker "$WORKER_NAME")
  fi
  MINER_CMD_ARGS+=(--password "${MINER_PASSWORD:-x}")
fi

PROFILE_LIMITS_AND_CLOCKS="$(
  PYTHONPATH="$DIR" "$PYTHON_BIN" - <<'PY'
import os
from config import get_oc_profiles, load_config

config = load_config()
profiles = get_oc_profiles(config)
power_limits = sorted({int(profile.get("power_limit", 0)) for profile in profiles.values() if int(profile.get("power_limit", 0)) > 0})
clock_locks = sorted({
    (int(profile.get("gpu_clock_min", 0)), int(profile.get("gpu_clock_max", 0)))
    for profile in profiles.values()
    if int(profile.get("gpu_clock_min", 0)) > 0 and int(profile.get("gpu_clock_max", 0)) > 0
})
print("POWER_LIMITS=" + " ".join(str(value) for value in power_limits))
print("CLOCK_LOCKS=" + " ".join(f"{low},{high}" for low, high in clock_locks))
explicit_startup_profile = "STARTUP_OC_PROFILE" in os.environ
startup_profile = (os.environ.get("STARTUP_OC_PROFILE") if explicit_startup_profile else config.get("STARTUP_OC_PROFILE") or "").strip()
profile = profiles.get(startup_profile) if startup_profile else None
if startup_profile and profile is None:
    if explicit_startup_profile:
        raise SystemExit(f"STARTUP_OC_PROFILE '{startup_profile}' is not defined in OC profiles")
    startup_profile = ""
print("STARTUP_PROFILE=" + startup_profile)
if profile is not None:
    print("STARTUP_POWER_LIMIT=" + str(int(profile.get("power_limit", 0))))
    low = int(profile.get("gpu_clock_min", 0))
    high = int(profile.get("gpu_clock_max", low))
    print("STARTUP_CLOCK_LOCK=" + (f"{low},{high}" if low > 0 and high > 0 else ""))
PY
)"
POWER_LIMITS="$(printf '%s\n' "$PROFILE_LIMITS_AND_CLOCKS" | awk -F= '$1=="POWER_LIMITS"{print $2}')"
CLOCK_LOCKS="$(printf '%s\n' "$PROFILE_LIMITS_AND_CLOCKS" | awk -F= '$1=="CLOCK_LOCKS"{print $2}')"
STARTUP_PROFILE="$(printf '%s\n' "$PROFILE_LIMITS_AND_CLOCKS" | awk -F= '$1=="STARTUP_PROFILE"{print $2}')"
STARTUP_POWER_LIMIT="$(printf '%s\n' "$PROFILE_LIMITS_AND_CLOCKS" | awk -F= '$1=="STARTUP_POWER_LIMIT"{print $2}')"
STARTUP_CLOCK_LOCK="$(printf '%s\n' "$PROFILE_LIMITS_AND_CLOCKS" | awk -F= '$1=="STARTUP_CLOCK_LOCK"{print $2}')"
NVIDIA_SUDO_COMMANDS="${NVIDIA_SMI} -pm 1, ${NVIDIA_SMI} --id=${GPU_INDEX:-0} --reset-gpu-clocks, ${NVIDIA_SMI} --reset-gpu-clocks"
for limit in $POWER_LIMITS; do
  NVIDIA_SUDO_COMMANDS+=", ${NVIDIA_SMI} --id=${GPU_INDEX:-0} --power-limit=${limit}, ${NVIDIA_SMI} --power-limit=${limit}"
done
for clock_lock in $CLOCK_LOCKS; do
  escaped_clock_lock="$(sudoers_arg_value "$clock_lock")"
  NVIDIA_SUDO_COMMANDS+=", ${NVIDIA_SMI} --id=${GPU_INDEX:-0} --lock-gpu-clocks=${escaped_clock_lock}, ${NVIDIA_SMI} --lock-gpu-clocks=${escaped_clock_lock}"
done

STARTUP_OC_PRE=""
if [[ -n "${STARTUP_PROFILE:-}" ]]; then
  if [[ "${STARTUP_POWER_LIMIT:-0}" =~ ^[0-9]+$ && "${STARTUP_POWER_LIMIT:-0}" -gt 0 ]]; then
    STARTUP_OC_PRE+=$'\n'"ExecStartPre=+$(systemd_quote_arg "$NVIDIA_SMI")$(systemd_join_args "--id=${GPU_INDEX:-0}" "--power-limit=${STARTUP_POWER_LIMIT}")"
  fi
  if [[ -n "${STARTUP_CLOCK_LOCK:-}" ]]; then
    STARTUP_OC_PRE+=$'\n'"ExecStartPre=+$(systemd_quote_arg "$NVIDIA_SMI")$(systemd_join_args "--id=${GPU_INDEX:-0}" "--lock-gpu-clocks=${STARTUP_CLOCK_LOCK}")"
  else
    STARTUP_OC_PRE+=$'\n'"ExecStartPre=+$(systemd_quote_arg "$NVIDIA_SMI")$(systemd_join_args "--id=${GPU_INDEX:-0}" "--reset-gpu-clocks")"
  fi
fi

echo "================================================="
echo " PEARL MINER MANAGER - SETUP ENV"
echo "================================================="
if [[ "$DRY_RUN" == "1" ]]; then
  mkdir -p "$DRY_RUN_DIR/systemd" "$DRY_RUN_DIR/sudoers.d"
  echo "DRY_RUN=1: no packages, database, sudoers, or systemd changes will be applied."
fi

echo "[1/6] Installing Ubuntu packages..."
if [[ "$DRY_RUN" == "1" ]]; then
  echo "DRY_RUN: skipping apt install and usermod."
else
  sudo apt-get update
  sudo apt-get install -y \
    python3 python3-venv python3-pip \
    postgresql postgresql-contrib \
    curl ca-certificates whiptail \
    nvidia-settings
  sudo usermod -aG systemd-journal "$SERVICE_USER" || true
fi

echo "[2/6] Preparing PostgreSQL..."
if [[ "$DRY_RUN" == "1" ]]; then
  echo "DRY_RUN: skipping PostgreSQL setup."
else
  sudo systemctl enable --now postgresql
  sudo -u postgres psql -v ON_ERROR_STOP=1 \
    -v db_user="$DB_USER" \
    -v db_pass="$DB_PASS" \
    -v db_name="$DB_NAME" <<'SQL'
SELECT format('CREATE ROLE %I WITH LOGIN PASSWORD %L', :'db_user', :'db_pass')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'db_user')\gexec
SELECT format('ALTER ROLE %I WITH LOGIN PASSWORD %L', :'db_user', :'db_pass')\gexec
SELECT format('CREATE DATABASE %I OWNER %I', :'db_name', :'db_user')
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'db_name')\gexec
SQL
fi

echo "[3/6] Creating Python virtualenv..."
if [[ "$DRY_RUN" == "1" ]]; then
  echo "DRY_RUN: skipping Python dependency installation."
else
  if [[ ! -d "$DIR/venv" ]]; then
    "$PYTHON_BIN" -m venv "$DIR/venv"
  fi
  "$DIR/venv/bin/pip" install --upgrade pip
  "$DIR/venv/bin/pip" install -r "$DIR/requirements.txt"
fi

echo "[4/6] Initializing database schema..."
if [[ "$DRY_RUN" == "1" ]]; then
  echo "DRY_RUN: skipping database schema initialization."
else
  "$DIR/venv/bin/python" - <<'PY'
from database import init_db
init_db()
print("Database schema is ready.")
PY
fi

echo "[5/6] Writing systemd services..."
SYSTEMD_DIR="/etc/systemd/system"
if [[ "$DRY_RUN" == "1" ]]; then
  SYSTEMD_DIR="$DRY_RUN_DIR/systemd"
fi
if [[ "$DRY_RUN" == "1" ]]; then tee "${SYSTEMD_DIR}/${MINER_SERVICE}" >/dev/null; else sudo tee "${SYSTEMD_DIR}/${MINER_SERVICE}" >/dev/null; fi <<EOF
[Unit]
Description=Pearl Miner on AlphaPool
After=network-online.target nvidia-persistenced.service
Wants=network-online.target nvidia-persistenced.service

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_GROUP}
WorkingDirectory=$(systemd_directive_value "$MINER_DIR")
Environment=DISPLAY=$(systemd_quote_arg "${DISPLAY:-:0}")
Environment=XAUTHORITY=$(systemd_quote_arg "${XAUTHORITY:-}")
ExecStartPre=+$(systemd_quote_arg "$NVIDIA_SMI") -pm 1
${STARTUP_OC_PRE}
ExecStart=$(systemd_quote_arg "$MINER_EXEC")$(systemd_join_args "${MINER_CMD_ARGS[@]}")$(systemd_join_args "${EXTRA_ARGS[@]}")
Restart=always
RestartSec=15
LimitNOFILE=1048576

[Install]
WantedBy=multi-user.target
EOF

if [[ "$DRY_RUN" == "1" ]]; then tee "${SYSTEMD_DIR}/${WEB_SERVICE}" >/dev/null; else sudo tee "${SYSTEMD_DIR}/${WEB_SERVICE}" >/dev/null; fi <<EOF
[Unit]
Description=Pearl Miner Manager FastAPI
After=network-online.target postgresql.service
Wants=network-online.target postgresql.service

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_GROUP}
WorkingDirectory=$(systemd_directive_value "$DIR")
Environment=PEARL_CONFIG=$(systemd_quote_arg "$CONFIG_FILE")
Environment=DISPLAY=$(systemd_quote_arg "${DISPLAY:-:0}")
Environment=XAUTHORITY=$(systemd_quote_arg "${XAUTHORITY:-}")
ExecStart=$(systemd_quote_arg "$DIR/venv/bin/uvicorn") app:app --host $(systemd_quote_arg "${WEB_HOST:-127.0.0.1}") --port ${WEB_PORT} --timeout-graceful-shutdown 5
Restart=always
RestartSec=10
TimeoutStopSec=8

[Install]
WantedBy=multi-user.target
EOF

if [[ "$DRY_RUN" == "1" ]]; then tee "${SYSTEMD_DIR}/${BOT_SERVICE}" >/dev/null; else sudo tee "${SYSTEMD_DIR}/${BOT_SERVICE}" >/dev/null; fi <<EOF
[Unit]
Description=Pearl Miner Manager Telegram Bot
After=network-online.target postgresql.service
Wants=network-online.target postgresql.service

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_GROUP}
WorkingDirectory=$(systemd_directive_value "$DIR")
Environment=PEARL_CONFIG=$(systemd_quote_arg "$CONFIG_FILE")
Environment=DISPLAY=$(systemd_quote_arg "${DISPLAY:-:0}")
Environment=XAUTHORITY=$(systemd_quote_arg "${XAUTHORITY:-}")
ExecStart=$(systemd_quote_arg "$DIR/venv/bin/python") $(systemd_quote_arg "$DIR/telegram_bot.py")
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

echo "[6/6] Configuring restricted sudoers..."
SUDOERS_FILE="/etc/sudoers.d/pearl-miner-manager"
if [[ "$DRY_RUN" == "1" ]]; then
  SUDOERS_FILE="$DRY_RUN_DIR/sudoers.d/pearl-miner-manager"
  if [[ -e "$SUDOERS_FILE" ]]; then
    chmod u+w "$SUDOERS_FILE"
  fi
fi
if [[ "$DRY_RUN" == "1" ]]; then tee "$SUDOERS_FILE" >/dev/null; else sudo tee "$SUDOERS_FILE" >/dev/null; fi <<EOF
Cmnd_Alias PEARL_SYSTEMCTL = ${SYSTEMCTL} daemon-reload, ${SYSTEMCTL} start postgresql.service, ${SYSTEMCTL} start ${MINER_SERVICE}, ${SYSTEMCTL} stop ${MINER_SERVICE}, ${SYSTEMCTL} restart ${MINER_SERVICE}, ${SYSTEMCTL} start ${WEB_SERVICE}, ${SYSTEMCTL} stop ${WEB_SERVICE}, ${SYSTEMCTL} restart ${WEB_SERVICE}, ${SYSTEMCTL} start ${BOT_SERVICE}, ${SYSTEMCTL} stop ${BOT_SERVICE}, ${SYSTEMCTL} restart ${BOT_SERVICE}
Cmnd_Alias PEARL_NVIDIA = ${NVIDIA_SUDO_COMMANDS}
${SERVICE_USER} ALL=(root) NOPASSWD: PEARL_SYSTEMCTL, PEARL_NVIDIA
EOF
if [[ "$DRY_RUN" == "1" ]]; then
  chmod 0440 "$SUDOERS_FILE"
  if command -v visudo >/dev/null 2>&1; then
    visudo -cf "$SUDOERS_FILE"
  fi
  if command -v systemd-analyze >/dev/null 2>&1; then
    systemd-analyze verify "${SYSTEMD_DIR}/${MINER_SERVICE}" "${SYSTEMD_DIR}/${WEB_SERVICE}" "${SYSTEMD_DIR}/${BOT_SERVICE}"
  fi
else
  sudo chmod 0440 "$SUDOERS_FILE"
  sudo visudo -cf "$SUDOERS_FILE"
fi

if [[ "$DRY_RUN" == "1" ]]; then
  echo "DRY_RUN: skipping systemctl daemon-reload and enable."
else
  sudo systemctl daemon-reload
  sudo systemctl enable "$MINER_SERVICE" "$WEB_SERVICE" "$BOT_SERVICE"
fi

if [[ "$DRY_RUN" == "1" ]]; then
  echo "DRY_RUN: generated files in $DRY_RUN_DIR"
else
  chmod +x "$DIR/start_all.sh" "$DIR/stop_all.sh" "$DIR/pearl-manager.sh" "$DIR/setup_env.sh" "$DIR/deploy.sh" "$DIR/verify_deploy.sh" "$DIR/shell_env.sh"
fi

echo "================================================="
echo " Setup complete."
echo " Pool: ${POOL_TARGET}"
echo " Wallet login: ${MINER_LOGIN}"
echo " Dashboard: http://localhost:${WEB_PORT}"
echo " Start all: ./start_all.sh"
echo "================================================="
