#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# setup_gadget.sh — Configure a Raspberry Pi Zero as a BJJCZ USB device
#
# This script uses Linux's configfs USB gadget framework to make the Pi appear
# as a BJJCZ laser controller (VID 0x9588, PID 0x9899) to the host PC.
# LightBurn will detect it as a JCZ galvo controller.
#
# Run as root after every boot:
#   sudo bash setup_gadget.sh
#
# Prerequisites:
#   - Raspberry Pi OS with dwc2 overlay enabled:
#       echo "dtoverlay=dwc2" >> /boot/config.txt
#       echo "dwc2" >> /etc/modules
#   - Reboot after enabling dwc2
# ─────────────────────────────────────────────────────────────────────────────

set -e

GADGET_NAME="bjjcz_bridge"
GADGET_DIR="/sys/kernel/config/usb_gadget/${GADGET_NAME}"

# Clean up any existing gadget
if [ -d "$GADGET_DIR" ]; then
    echo "Removing existing gadget..."
    # Unbind UDC
    echo "" > "${GADGET_DIR}/UDC" 2>/dev/null || true
    # Remove symlinks
    rm -f "${GADGET_DIR}/configs/c.1/func.0" 2>/dev/null || true
    # Remove directories in reverse order
    rmdir "${GADGET_DIR}/configs/c.1/strings/0x409" 2>/dev/null || true
    rmdir "${GADGET_DIR}/configs/c.1" 2>/dev/null || true
    rmdir "${GADGET_DIR}/functions/ffs.jcz" 2>/dev/null || true
    rmdir "${GADGET_DIR}/strings/0x409" 2>/dev/null || true
    rmdir "${GADGET_DIR}" 2>/dev/null || true
fi

# Load required modules
modprobe libcomposite 2>/dev/null || true
modprobe usb_f_fs 2>/dev/null || true

echo "Creating USB gadget: ${GADGET_NAME}"

# Create gadget directory
mkdir -p "${GADGET_DIR}"
cd "${GADGET_DIR}"

# Set BJJCZ vendor/product IDs
echo "0x9588" > idVendor   # BJJCZ VID
echo "0x9899" > idProduct  # BJJCZ PID
echo "0x0100" > bcdDevice  # Device version 1.0
echo "0x0200" > bcdUSB     # USB 2.0

# Device class: vendor-specific (matches real BJJCZ)
echo "0xFF" > bDeviceClass
echo "0xFF" > bDeviceSubClass
echo "0xFF" > bDeviceProtocol

# String descriptors
mkdir -p strings/0x409
echo "D1ULTRA-BRIDGE-001" > strings/0x409/serialnumber
echo "Beijing JCZ Technology"  > strings/0x409/manufacturer
echo "BJJCZ Fiber Laser" > strings/0x409/product

# Create FunctionFS instance for userspace control
mkdir -p functions/ffs.jcz

# Create configuration
mkdir -p configs/c.1/strings/0x409
echo "JCZ Bridge Config" > configs/c.1/strings/0x409/configuration
echo 250 > configs/c.1/MaxPower  # 500mA (250 x 2)

# Link function to configuration
ln -sf functions/ffs.jcz configs/c.1/func.0

# Mount FunctionFS for userspace endpoint access
MOUNT_POINT="/dev/jcz_usb"
mkdir -p "${MOUNT_POINT}"
mount -t functionfs jcz "${MOUNT_POINT}" 2>/dev/null || {
    echo "FunctionFS already mounted or mount failed"
}

echo ""
echo "USB gadget configured."
echo "FunctionFS mounted at: ${MOUNT_POINT}"
echo ""
echo "Next steps:"
echo "  1. Run: sudo python3 rpi_jcz_bridge.py"
echo "     (This will write endpoint descriptors and bind the UDC)"
echo "  2. Connect the Pi's USB device port to your PC"
echo "  3. LightBurn should detect a JCZ device"
echo ""
echo "UDC name: $(ls /sys/class/udc/ 2>/dev/null | head -1)"
