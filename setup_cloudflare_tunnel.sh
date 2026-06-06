#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$DIR/shell_env.sh"

CONFIG_FILE="${PEARL_CONFIG:-${CONFIG_FILE:-$DIR/config.env}}"
TUNNEL_ENV="${CLOUDFLARE_TUNNEL_ENV:-$DIR/cloudflare_tunnel.env}"

if [[ ! -f "$CONFIG_FILE" ]]; then
  echo "Missing config file: $CONFIG_FILE" >&2
  exit 1
fi
if [[ ! -f "$TUNNEL_ENV" ]]; then
  if [[ -f "$DIR/cloudflare_tunnel.env.example" ]]; then
    cp "$DIR/cloudflare_tunnel.env.example" "$TUNNEL_ENV"
    chmod 600 "$TUNNEL_ENV" || true
  fi
  echo "Created $TUNNEL_ENV. Paste CLOUDFLARE_TUNNEL_TOKEN, then run this script again." >&2
  exit 1
fi

pearl_load_env_file "$CONFIG_FILE"
pearl_load_env_file "$TUNNEL_ENV"

CLOUDFLARE_TUNNEL_TOKEN="${CLOUDFLARE_TUNNEL_TOKEN:-}"
CLOUDFLARE_TUNNEL_TARGET="${CLOUDFLARE_TUNNEL_TARGET:-http://127.0.0.1:${WEB_PORT:-8555}}"
CLOUDFLARE_TUNNEL_SERVICE="${CLOUDFLARE_TUNNEL_SERVICE:-pearl-cloudflared.service}"

if [[ -z "$CLOUDFLARE_TUNNEL_TOKEN" || "$CLOUDFLARE_TUNNEL_TOKEN" == "CHANGE_ME" ]]; then
  echo "Set CLOUDFLARE_TUNNEL_TOKEN in $TUNNEL_ENV first." >&2
  exit 1
fi
if [[ "$CLOUDFLARE_TUNNEL_SERVICE" != "${CLOUDFLARE_TUNNEL_SERVICE##*/}" || ! "$CLOUDFLARE_TUNNEL_SERVICE" =~ ^[A-Za-z0-9_.@-]+\.service$ ]]; then
  echo "Invalid CLOUDFLARE_TUNNEL_SERVICE: $CLOUDFLARE_TUNNEL_SERVICE" >&2
  exit 1
fi

ensure_dashboard_token() {
  if [[ -n "${CONTROL_API_TOKEN:-}" ]]; then
    return 0
  fi
  local token
  token="$("$DIR/venv/bin/python" - <<'PY'
import secrets
print(secrets.token_urlsafe(32))
PY
)"
  printf '\n# Required for public/tunnel dashboard access.\nCONTROL_API_TOKEN=%s\n' "$token" >> "$CONFIG_FILE"
  chmod 600 "$CONFIG_FILE" || true
  CONTROL_API_TOKEN="$token"
  echo "Generated CONTROL_API_TOKEN in $CONFIG_FILE"
}

install_cloudflared() {
  if command -v cloudflared >/dev/null 2>&1; then
    return 0
  fi
  echo "Installing cloudflared..."
  local keyring="/usr/share/keyrings/cloudflare-main.gpg"
  sudo -n mkdir -p /usr/share/keyrings
  curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | sudo -n tee "$keyring" >/dev/null
  echo "deb [signed-by=$keyring] https://pkg.cloudflare.com/cloudflared any main" | sudo -n tee /etc/apt/sources.list.d/cloudflared.list >/dev/null
  sudo -n apt-get update
  sudo -n apt-get install -y cloudflared
}

write_service() {
  local token_file="/etc/pearl-cloudflared.token"
  local unit_file="/etc/systemd/system/$CLOUDFLARE_TUNNEL_SERVICE"
  printf '%s\n' "$CLOUDFLARE_TUNNEL_TOKEN" | sudo -n tee "$token_file" >/dev/null
  sudo -n chmod 600 "$token_file"
  sudo -n tee "$unit_file" >/dev/null <<EOF
[Unit]
Description=Pearl Miner Manager Cloudflare Tunnel
After=network-online.target pearl-web.service
Wants=network-online.target pearl-web.service

[Service]
Type=simple
ExecStart=/usr/bin/cloudflared tunnel --no-autoupdate run --token-file $token_file
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
}

echo "================================================="
echo " PEARL CLOUDFLARE TUNNEL SETUP"
echo "================================================="
ensure_dashboard_token
install_cloudflared
write_service
sudo -n systemctl daemon-reload
sudo -n systemctl enable --now "$CLOUDFLARE_TUNNEL_SERVICE"

echo "Tunnel service: $(systemctl is-active "$CLOUDFLARE_TUNNEL_SERVICE" || true)"
echo "Dashboard target: $CLOUDFLARE_TUNNEL_TARGET"
echo "In Cloudflare Zero Trust, map the tunnel Public Hostname service to:"
echo "  $CLOUDFLARE_TUNNEL_TARGET"
echo "Open public dashboard with token parameter:"
echo "  https://<your-cloudflare-hostname>/?token=$CONTROL_API_TOKEN"
echo "Recommended: also enable Cloudflare Access for the hostname."
echo "================================================="
