#!/bin/bash
# ═════════════��═════════════════���══════════════════════════════════════════��════
# setup_usbip.sh — Export BJJCZ gadget device over USB/IP
#
# After the gadget is created (setup_gadget.sh) and the bridge has written
# FunctionFS descriptors and bound the UDC, this script:
#   1. Finds the dummy_hcd device's bus ID
#   2. Binds it for USB/IP export
#   3. Starts the usbipd daemon
#
# From a Windows PC, connect with:
#   usbipd attach --remote <vm-ip> --busid <busid>
#
# Run as root:
#   sudo bash setup_usbip.sh
# ═══════════════════════════════════════════════════════════════════���═══════════

set -euo pipefail

echo "=== USB/IP Server Setup ==="

# ── Ensure modules are loaded ─��───────────────────────────────────────────────

for mod in usbip-core usbip-host; do
    if ! lsmod | grep -q "^${mod//-/_}"; then
        echo "Loading module: ${mod}"
        modprobe "${mod}"
    fi
done

# ── Find the BJJCZ device bus ID ──���──────────────────────────────────────────

echo "Looking for BJJCZ device on dummy_hcd bus..."

# usbip list --local output format:
#  - busid 2-1 (9588:9899)
#    unknown vendor : unknown product (9588:9899)
# The VID:PID appears on the busid line itself.
BUSID=$(usbip list --local 2>/dev/null \
    | grep -i "busid.*9588:9899" \
    | head -1 \
    | sed -E 's/.*busid\s+([^ ]+).*/\1/')

if [ -z "${BUSID}" ]; then
    echo "ERROR: BJJCZ device (9588:9899) not found."
    echo "       Is the gadget set up and the UDC bound?"
    echo "       Run: sudo bash setup_gadget.sh && python3 jcz_bridge.py --setup-only"
    exit 1
fi

echo "Found BJJCZ device at busid: ${BUSID}"

# ── Bind the device for USB/IP export ─────────────��───────────────────────────

echo "Binding ${BUSID} for USB/IP export..."
usbip bind --busid="${BUSID}" 2>/dev/null || {
    echo "  Already bound or bind failed — continuing"
}

# ── Start usbipd if not running ───────────────────────────────────────────────

if pgrep -x usbipd > /dev/null 2>&1; then
    echo "usbipd is already running"
else
    echo "Starting usbipd daemon..."
    usbipd -D
    sleep 0.5
    if pgrep -x usbipd > /dev/null 2>&1; then
        echo "usbipd started successfully"
    else
        echo "ERROR: usbipd failed to start"
        exit 1
    fi
fi

# ── Show connection instructions ──────────────────────────────────────────────

VM_IP=$(hostname -I | awk '{print $1}')

echo ""
echo "=== USB/IP server ready ==="
echo ""
echo "  Bus ID : ${BUSID}"
echo "  VM IP  : ${VM_IP}"
echo ""
echo "To connect from Windows (install usbipd-win first):"
echo "  usbipd list --remote ${VM_IP}"
echo "  usbipd attach --remote ${VM_IP} --busid ${BUSID}"
echo ""
echo "To connect from Linux:"
echo "  sudo usbip attach --remote ${VM_IP} --busid ${BUSID}"
