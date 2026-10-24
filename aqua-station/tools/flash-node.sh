#!/usr/bin/env bash
# Flash a node so a board on the bench can reach the WSL broker.
#
# The board is on WiFi; the broker is inside WSL, which the board cannot reach
# directly. The path is board -> WiFi -> Windows host -> userspace relay
# (0.0.0.0:18830) -> WSL broker (127.0.0.1:1883).
#
# Usage:
#   ./flash-node.sh <node-dir> <ssid> <password> [broker-ip] [broker-port]
#
#   ./flash-node.sh nodo-aerador "Vyryna 5g" "the-password"
#
# broker-ip defaults to the Windows WiFi address, discovered at run time. The
# password is passed as an argument and never written to a file or an env var
# that outlives this command -- it lands only in the compiled binary.
set -euo pipefail

if [[ $# -lt 3 ]]; then
  sed -n '2,14p' "$0"
  exit 2
fi

NODE_DIR=$1
SSID=$2
PASSWORD=$3
BROKER_IP=${4:-}
BROKER_PORT=${5:-18830}

cd "$(dirname "$0")/../$NODE_DIR" 2>/dev/null || cd "/root/aqua/$NODE_DIR"

# Discover the Windows WiFi address when not given: the board must be able to
# route to it, so it has to be the LAN address, not a WSL or Tailscale one.
if [[ -z "$BROKER_IP" ]]; then
  BROKER_IP=$(cmd.exe /c "ipconfig" 2>/dev/null | tr -d '\r' \
    | awk '/Adaptador de LAN inal|Wireless LAN adapter/{a=1;next} a&&/IPv4|Direcci/{print $NF; exit}' \
    | tr -d '\r')
fi
if [[ -z "$BROKER_IP" ]]; then
  echo "could not discover the Windows WiFi address; pass it as arg 4" >&2
  exit 1
fi

echo "node        : $NODE_DIR"
echo "wifi ssid   : $SSID"
echo "broker      : $BROKER_IP:$BROKER_PORT  (relay -> WSL 127.0.0.1:1883)"

# shlex.split strips quotes, so -DX=\"y\" becomes invalid C. Outer single quotes
# with \" inside is the form that survives.
export PLATFORMIO_BUILD_FLAGS="-DAQUA_WIFI_SSID='\"'\"'$SSID'\"'\"' -DAQUA_WIFI_PASSWORD='\"'\"'$PASSWORD'\"'\"' -DAQUA_MQTT_HOST='\"'\"'$BROKER_IP'\"'\"' -DAQUA_MQTT_PORT=$BROKER_PORT"

pio run -e esp32-s3 -t upload
echo
echo "flashed. To watch it join:"
echo "  python3 /tmp/read_board.py /dev/ttyACM0 20"
