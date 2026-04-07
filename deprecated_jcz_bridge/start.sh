#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════════
# start.sh — Start the D1 Ultra JCZ Bridge (manual, non-systemd)
#
# Runs all three components in correct order:
#   1. USB gadget setup (configfs)
#   2. Bridge (writes FunctionFS descriptors, binds UDC, runs bridge)
#   3. USB/IP server (exports gadget over network)
#
# For production use, install the systemd services instead:
#   sudo bash install_services.sh
#
# Usage:
#   sudo bash start.sh
# ═══════════════════════════════════════════════════════════════════════════════

set -euo pipefail
cd "$(dirname "$0")"

echo "=== D1 Ultra JCZ Bridge — Starting ==="

# Step 1: Set up the USB gadget
echo "[1/3] Setting up USB gadget..."
bash setup_gadget.sh

# Step 2: Start the bridge in background
echo "[2/3] Starting bridge..."
python3 jcz_bridge.py &
BRIDGE_PID=$!
echo "  Bridge PID: ${BRIDGE_PID}"

# Wait for bridge to write descriptors and bind UDC
sleep 2

# Step 3: Start USB/IP server
echo "[3/3] Starting USB/IP server..."
bash setup_usbip.sh

echo ""
echo "=== Bridge is running ==="
echo "  Bridge PID : ${BRIDGE_PID}"
echo "  Log file   : /var/log/d1ultra-bridge.log"
echo "  Stop with  : kill ${BRIDGE_PID}"
echo ""

# Wait for bridge to exit
wait ${BRIDGE_PID}
