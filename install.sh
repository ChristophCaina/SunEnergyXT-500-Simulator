#!/bin/bash
# SunEnergyXT Simulator — LXC Install Script
# Run as root on a fresh Debian 12 / Ubuntu 24.04 LXC
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/ChristophCaina/SunEnergyXT-500-Simulator/main/install.sh | bash
#   or:
#   bash install.sh [--port 8500] [--sn MyDevice001] [--no-mdns]

set -e

REPO_URL="https://github.com/ChristophCaina/SunEnergyXT-500-Simulator.git"
INSTALL_DIR="/opt/sunenergyxt-simulator"
SIMULATOR_DIR="$INSTALL_DIR"
SERVICE_NAME="sunenergyxt-simulator"
PORT=8500
SN="TBsimulator0001"
EXTRA_ARGS=""

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --port) PORT="$2"; shift 2 ;;
        --sn)   SN="$2";   shift 2 ;;
        --no-mdns) EXTRA_ARGS="$EXTRA_ARGS --no-mdns"; shift ;;
        *) echo "Unknown argument: $1"; exit 1 ;;
    esac
done

echo "=================================================="
echo " SunEnergyXT 500 PRO Simulator — Install"
echo "=================================================="
echo " Repo    : $REPO_URL"
echo " Dir     : $INSTALL_DIR"
echo " Port    : $PORT"
echo " SN      : $SN"
echo "=================================================="

# 1. System packages
echo "[1/5] Installing system packages..."
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv git

# 2. Clone or update repo
echo "[2/5] Cloning repository..."
if [ -d "$INSTALL_DIR/.git" ]; then
    echo "      Repo exists — pulling latest..."
    git -C "$INSTALL_DIR" pull
else
    git clone "$REPO_URL" "$INSTALL_DIR"
fi

# 3. Python venv + dependencies
echo "[3/5] Setting up Python environment..."
cd "$INSTALL_DIR"
python3 -m venv venv
venv/bin/pip install -q --upgrade pip
venv/bin/pip install -q -r requirements.txt

# 4. Systemd service
echo "[4/5] Installing systemd service..."
cat > /etc/systemd/system/${SERVICE_NAME}.service << EOF
[Unit]
Description=SunEnergyXT 500 PRO Simulator
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=${SIMULATOR_DIR}
ExecStart=${SIMULATOR_DIR}/venv/bin/python simulator.py --port ${PORT} --sn ${SN} ${EXTRA_ARGS}
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"

# 5. Start
echo "[5/5] Starting simulator..."
systemctl restart "$SERVICE_NAME"
sleep 2

if systemctl is-active --quiet "$SERVICE_NAME"; then
    IP=$(hostname -I | awk '{print $1}')
    echo ""
    echo "=================================================="
    echo " ✅  Simulator running!"
    echo ""
    echo " Add device in Home Assistant:"
    echo "   IP address : $IP"
    echo "   Port       : $PORT"
    echo ""
    echo " Test:"
    echo "   curl http://$IP:$PORT/read"
    echo ""
    echo " Logs:"
    echo "   journalctl -u $SERVICE_NAME -f"
    echo ""
    echo " Update:"
    echo "   git -C $INSTALL_DIR pull"
    echo "   systemctl restart $SERVICE_NAME"
    echo "=================================================="
else
    echo "❌  Service failed to start. Check logs:"
    echo "   journalctl -u $SERVICE_NAME -n 50"
    exit 1
fi
