#!/bin/bash
# Start the raw_gadget BJJCZ bridge from clean state.
# Run after reboot.
set -e

echo "=== Loading modified modules ==="

# Block stock dummy_hcd from loading
rmmod dummy_hcd 2>/dev/null || true

# Load our modified dummy_hcd (with ep8in-bulk)
insmod /home/kenneth/raw-gadget/dummy_hcd/dummy_hcd.ko
echo "  dummy_hcd loaded (with ep8in-bulk)"

# Load raw_gadget
insmod /home/kenneth/raw-gadget/raw_gadget/raw_gadget.ko
echo "  raw_gadget loaded"

# Load usbip
modprobe usbip-core
modprobe usbip-host
echo "  usbip modules loaded"

echo ""
echo "=== Starting BJJCZ emulator ==="
cd /opt/d1ultra-bridge
PYTHONUNBUFFERED=1 python3 raw_gadget_bjjcz.py &
BRIDGE_PID=$!
echo "  Bridge PID: $BRIDGE_PID"

# Wait for device to enumerate
sleep 5
lsusb | grep 9588 || echo "WARNING: device not in lsusb yet"

echo ""
echo "=== Starting USB/IP ==="
usbip bind --busid 2-1
usbipd -D
echo "  USB/IP ready on port 3240"

VM_IP=$(hostname -I | awk '{print $1}')
echo ""
echo "From Windows:"
echo "  usbip.exe attach --remote $VM_IP --busid 2-1"
echo ""
echo "Bridge running. Ctrl+C or kill $BRIDGE_PID to stop."
wait $BRIDGE_PID
