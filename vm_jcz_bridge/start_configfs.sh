#!/bin/bash
# Start the D1 Ultra bridge using configfs + modified dummy_hcd (ep8in-bulk).
# Run after reboot: sudo bash /opt/d1ultra-bridge/start_configfs.sh
set -e

echo "=== Loading modified dummy_hcd (ep8in-bulk only) ==="
# Remove stock dummy_hcd if loaded
rmmod dummy_hcd 2>/dev/null || true
# Load our version (ep1in/ep6in/ep11in/ep2in-bulk removed, only ep8in-bulk remains)
insmod /home/kenneth/raw-gadget/dummy_hcd/dummy_hcd.ko
echo "  dummy_hcd loaded"

# Ensure other required modules
modprobe configfs 2>/dev/null || true
modprobe libcomposite 2>/dev/null || true
modprobe usb_f_fs 2>/dev/null || true
modprobe usbip-core 2>/dev/null || true
modprobe usbip-host 2>/dev/null || true
echo "  all modules loaded"

echo ""
echo "=== Setting up configfs gadget ==="
bash /opt/d1ultra-bridge/setup_gadget.sh

echo ""
echo "=== Starting bridge ==="
cd /opt/d1ultra-bridge
PYTHONUNBUFFERED=1 python3 jcz_bridge.py > /tmp/bridge.log 2>&1 &
BRIDGE_PID=$!
echo "  Bridge PID: $BRIDGE_PID"

# Wait for bridge to write descriptors and bind UDC
sleep 3

echo ""
echo "=== Verifying endpoint addresses ==="
lsusb -v -d 9588:9899 2>&1 | grep -E 'bEndpointAddress|idVendor|idProduct' || echo "WARNING: device not found"

echo ""
echo "=== Setting up USB/IP ==="
bash /opt/d1ultra-bridge/setup_usbip.sh

VM_IP=$(hostname -I | awk '{print $1}')
echo ""
echo "From Windows:"
echo "  usbip.exe attach --remote $VM_IP --busid 2-1"
echo ""
echo "Bridge log: tail -f /tmp/bridge.log"
echo "Bridge PID: $BRIDGE_PID"
wait $BRIDGE_PID
