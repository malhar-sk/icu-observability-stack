#!/usr/bin/env bash
# Install the device agent as a systemd service: it starts at boot and restarts if it crashes.
# Run this ON THE DEVICE (Linux with systemd), from the folder holding device_agent.py and
# requirements.txt:
#
#   sudo bash install-service.sh --device-id kitchen-pi --endpoint 10.0.0.5:4317
#   bash      install-service.sh --device-id kitchen-pi --endpoint 10.0.0.5:4317 --print   # show only
#   sudo bash install-service.sh --uninstall
#
# Options: --device-id ID (required)  --endpoint HOST:PORT (required)  --interval SECONDS (default 5)
#          --disk-path PATH (default /)  --print  --uninstall  -h/--help
#
# It writes /opt/device-agent (the agent and its own Python venv), /etc/device-agent.env (the settings)
# and /etc/systemd/system/device-agent.service. Nothing else is touched. The service runs as a
# throwaway unprivileged user. Needs network access once, to download the agent's two Python packages.
set -euo pipefail

NAME=device-agent
DIR=/opt/device-agent
ENV_FILE=/etc/device-agent.env
UNIT=/etc/systemd/system/device-agent.service

die() { echo "error: $*" >&2; exit 1; }
usage() { sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'; }

DEVICE_ID="" ENDPOINT="" INTERVAL=5 DISK_PATH=/ PRINT=0 UNINSTALL=0
while [ $# -gt 0 ]; do
  case "$1" in
    --device-id) DEVICE_ID="${2-}"; shift 2 ;;
    --endpoint) ENDPOINT="${2-}"; shift 2 ;;
    --interval) INTERVAL="${2-}"; shift 2 ;;
    --disk-path) DISK_PATH="${2-}"; shift 2 ;;
    --print) PRINT=1; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1 (try --help)" ;;
  esac
done

render_env() {
  printf 'DEVICE_ID=%s\nOTLP_ENDPOINT=%s\nPUSH_INTERVAL=%s\nDISK_PATH=%s\n' "$DEVICE_ID" "$ENDPOINT" "$INTERVAL" "$DISK_PATH"
}

render_unit() {
  cat <<EOF
[Unit]
Description=Device health agent (sends CPU, memory, disk, uptime and temperature to the monitoring stack)
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
EnvironmentFile=$ENV_FILE
ExecStart=$DIR/venv/bin/python $DIR/device_agent.py
Restart=always
RestartSec=5
DynamicUser=yes
NoNewPrivileges=yes
CapabilityBoundingSet=
PrivateTmp=yes
ProtectHome=yes
ProtectProc=invisible
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictNamespaces=yes
RestrictSUIDSGID=yes
RestrictRealtime=yes
LockPersonality=yes
SystemCallArchitectures=native

[Install]
WantedBy=multi-user.target
EOF
}

if [ "$UNINSTALL" = 1 ]; then
  [ "$(id -u)" = 0 ] || die "run with sudo"
  if [ -d /run/systemd/system ]; then
    systemctl disable --now "$NAME" >/dev/null 2>&1 || true
  fi
  rm -f "$UNIT" "$ENV_FILE"
  rm -rf "$DIR"
  if [ -d /run/systemd/system ]; then systemctl daemon-reload; fi
  echo "ok: $NAME is stopped, disabled and removed."
  exit 0
fi

# ---- validate everything before anything is written (the values end up in a systemd env file) ----
[ -n "$DEVICE_ID" ] || die "--device-id is required (a unique name for this device, e.g. kitchen-pi)"
[ -n "$ENDPOINT" ] || die "--endpoint is required (the monitoring server, as HOST:4317)"
[[ "$DEVICE_ID" =~ ^[a-z0-9][a-z0-9._-]{0,62}$ ]] \
  || die "invalid --device-id '$DEVICE_ID': use lowercase letters, digits, '.', '_' and '-' (max 63, start with a letter or digit)"
ENDPOINT="${ENDPOINT#http://}"; ENDPOINT="${ENDPOINT%/}"
[[ "$ENDPOINT" =~ ^[A-Za-z0-9._-]+:[0-9]{1,5}$ ]] \
  || die "invalid --endpoint '$ENDPOINT': it must look like HOST:PORT, e.g. 10.0.0.5:4317 (plain gRPC, no https://)"
if ! { [[ "$INTERVAL" =~ ^[0-9]+(\.[0-9]+)?$ ]] && awk -v v="$INTERVAL" 'BEGIN { exit !(v > 0) }'; }; then
  die "invalid --interval '$INTERVAL': a positive number of seconds"
fi
[[ "$DISK_PATH" =~ ^/[A-Za-z0-9._/-]*$ ]] || die "invalid --disk-path '$DISK_PATH': an absolute path without spaces"

if [ "$PRINT" = 1 ]; then
  echo "# $ENV_FILE"; render_env; echo; echo "# $UNIT"; render_unit
  echo; echo "# plus: the agent and a Python venv under $DIR, then: systemctl enable --now $NAME"
  exit 0
fi

[ "$(id -u)" = 0 ] || die "run with sudo"
[ -d /run/systemd/system ] || die "this machine is not running systemd; run the agent by hand instead"
command -v python3 >/dev/null || die "python3 is not installed"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' || die "Python 3.9 or newer is needed"
python3 -c 'import venv, ensurepip' 2>/dev/null || die "Python's venv module is missing (Debian / Raspberry Pi OS: sudo apt install python3-venv)"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$SRC/device_agent.py" ] && [ -f "$SRC/requirements.txt" ] \
  || die "device_agent.py and requirements.txt must be in the same folder as this script ($SRC)"

mkdir -p "$DIR"
install -m 0644 "$SRC/device_agent.py" "$SRC/requirements.txt" "$DIR/"
[ -x "$DIR/venv/bin/python" ] || python3 -m venv "$DIR/venv"
"$DIR/venv/bin/python" -m pip install -q -r "$DIR/requirements.txt" \
  || die "could not install the agent's Python packages (is this device online?)"
chmod -R a+rX "$DIR"
render_env > "$ENV_FILE"; chmod 0644 "$ENV_FILE"
render_unit > "$UNIT"; chmod 0644 "$UNIT"

systemctl daemon-reload
systemctl enable "$NAME" >/dev/null 2>&1
systemctl restart "$NAME"
for _ in 1 2 3 4 5 6 7 8 9 10; do
  [ "$(systemctl is-active "$NAME")" = active ] && break
  sleep 1
done
if [ "$(systemctl is-active "$NAME")" = active ]; then
  echo "ok: $NAME is running and will start at boot (device id '$DEVICE_ID', sending to $ENDPOINT)."
  echo "    logs:      journalctl -u $NAME -f"
  echo "    status:    systemctl status $NAME"
  echo "    remove:    sudo bash $(basename "${BASH_SOURCE[0]}") --uninstall"
else
  echo "error: $NAME did not start. See: journalctl -u $NAME -n 30" >&2
  exit 1
fi
