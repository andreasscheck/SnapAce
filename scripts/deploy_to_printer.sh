#!/usr/bin/env bash
# Copies this repo's Klipper/UI files directly onto a running printer over
# SSH and restarts the affected services - the fast loop for iterating on
# ace.py/filament_feed.py/extruder.py/ace-ui without a full firmware
# rebuild+reflash (see scripts/build_custom_firmware.sh for that).
#
# Usage:
#   scripts/deploy_to_printer.sh <printer-ip>
#
# Env overrides:
#   PASSWORD   SSH/scp password for root@<printer-ip> (default: snapmaker,
#              the stock Extended Firmware default - see docs/ssh_access.md
#              in the firmware repo)

set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 <printer-ip>"
  exit 1
fi

if ! command -v sshpass >/dev/null; then
  echo "sshpass is required: brew install hudochenkov/sshpass/sshpass" >&2
  exit 1
fi

SNAPACE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST="$1"
PASSWORD="${PASSWORD:-snapmaker}"
SSH_OPTS=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null)

ssh_run() { sshpass -p "$PASSWORD" ssh "${SSH_OPTS[@]}" "root@$HOST" "$@"; }
scp_put() { sshpass -p "$PASSWORD" scp "${SSH_OPTS[@]}" "$@"; }

echo ">> Copying Klipper extras/kinematics..."
scp_put "$SNAPACE_DIR/ace.py" "$SNAPACE_DIR/filament_feed.py" \
  "root@$HOST:/home/lava/klipper/klippy/extras/"
scp_put "$SNAPACE_DIR/extruder.py" \
  "root@$HOST:/home/lava/klipper/klippy/kinematics/"

echo ">> Copying ACE status UI..."
ssh_run "mkdir -p /usr/local/ace-ui/html /usr/local/share/firmware-config/functions /etc/nginx/fluidd.d"
scp_put "$SNAPACE_DIR/ace-ui/html/"* "root@$HOST:/usr/local/ace-ui/html/"
scp_put "$SNAPACE_DIR/ace-ui/ace.conf" "root@$HOST:/etc/nginx/fluidd.d/ace.conf"
scp_put "$SNAPACE_DIR/ace-ui/12_links_ace_status.yaml" \
  "root@$HOST:/usr/local/share/firmware-config/functions/12_links_ace_status.yaml"

echo ">> Restarting klipper (a plain FIRMWARE_RESTART gcode keeps the old"
echo "   Python modules cached in the still-running process and won't pick"
echo "   up code changes here - a full service restart is required)..."
ssh_run "/etc/init.d/S60klipper restart"

echo ">> Restarting nginx for the UI changes..."
ssh_run "/etc/init.d/S50nginx restart" || echo "!! nginx restart failed, continuing"

echo ">> Waiting for Klipper to come back..."
STATE="unknown"
for _ in $(seq 1 20); do
  sleep 1
  STATE="$(curl -s "http://$HOST/printer/info" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
    print(d.get("result", d).get("state", "unknown"))
except Exception:
    print("unreachable")
' 2>/dev/null || echo "unreachable")"
  if [[ "$STATE" == "ready" ]]; then
    echo ">> Klipper is ready."
    exit 0
  fi
  if [[ "$STATE" == "shutdown" || "$STATE" == "error" ]]; then
    echo "!! Klipper came back in state: $STATE"
    curl -s "http://$HOST/printer/info"
    echo
    exit 1
  fi
done

echo "!! Timed out waiting for Klipper to become ready (last state: $STATE)"
exit 1
