#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════════
# setup_gadget.sh — Create BJJCZ USB gadget on dummy_hcd virtual bus
#
# This script uses Linux's configfs USB gadget framework to create a virtual
# USB device that identifies as a BJJCZ laser controller (VID 0x9588,
# PID 0x9899). LightBurn will detect it as a JCZ galvo device.
#
# The device runs on dummy_hcd (a software-only USB bus) and is exported
# over the network via USB/IP, so no physical USB hardware is needed.
#
# Run as root:
#   sudo bash setup_gadget.sh
#
# Prerequisites:
#   Kernel modules: configfs, libcomposite, dummy_hcd, usb_f_fs
#   See /etc/modules-load.d/d1ultra-bridge.conf
# ═══════════════════════════════════════════════════════════════════════════════

set -euo pipefail

GADGET_NAME="bjjcz"
GADGET_DIR="/sys/kernel/config/usb_gadget/${GADGET_NAME}"
FFS_MOUNT="/dev/ffs-bjjcz"
UDC_NAME="dummy_udc.0"

echo "=== BJJCZ USB Gadget Setup ==="

# ── Ensure kernel modules are loaded ──────────────────────────────────────────

for mod in configfs libcomposite dummy_hcd usb_f_fs; do
    if ! lsmod | grep -q "^${mod//-/_}"; then
        echo "Loading module: ${mod}"
        modprobe "${mod}"
    fi
done

# ── Ensure configfs is mounted ────────────────────────────────────────────────

if ! mountpoint -q /sys/kernel/config 2>/dev/null; then
    mount -t configfs configfs /sys/kernel/config
fi

# ── Clean up existing gadget if present ───────────────────────────────────────

if [ -d "${GADGET_DIR}" ]; then
    echo "Removing existing gadget..."
    # Unbind UDC first
    echo "" > "${GADGET_DIR}/UDC" 2>/dev/null || true
    # Unmount FunctionFS
    umount "${FFS_MOUNT}" 2>/dev/null || true
    # Remove symlinks and directories in correct order
    rm -f "${GADGET_DIR}/configs/c.1/ffs.${GADGET_NAME}" 2>/dev/null || true
    rmdir "${GADGET_DIR}/configs/c.1/strings/0x409" 2>/dev/null || true
    rmdir "${GADGET_DIR}/configs/c.1" 2>/dev/null || true
    rmdir "${GADGET_DIR}/functions/ffs.${GADGET_NAME}" 2>/dev/null || true
    rmdir "${GADGET_DIR}/strings/0x409" 2>/dev/null || true
    rmdir "${GADGET_DIR}" 2>/dev/null || true
    echo "  Cleaned up."
fi

# ── Create the gadget ─────────────────────────────────────────────────────────

echo "Creating gadget: ${GADGET_NAME}"
mkdir -p "${GADGET_DIR}"
cd "${GADGET_DIR}"

# BJJCZ identity — these values make LightBurn detect it as JCZ hardware
echo "0x9588" > idVendor        # BJJCZ VID
echo "0x9899" > idProduct       # BJJCZ PID
echo "0x0005" > bcdDevice       # firmware version (matches real board)
echo "0x0200" > bcdUSB          # USB 2.0

# Vendor-specific device class (matches real BJJCZ)
echo "0xFF" > bDeviceClass
echo "0xFF" > bDeviceSubClass
echo "0xFF" > bDeviceProtocol

# String descriptors
mkdir -p strings/0x409
echo "D1ULTRA-BRIDGE-001"       > strings/0x409/serialnumber
echo "Beijing JCZ Technology"   > strings/0x409/manufacturer
echo "BJJCZ Fiber Laser"       > strings/0x409/product

# Create FunctionFS function (userspace-controlled endpoints)
mkdir -p "functions/ffs.${GADGET_NAME}"

# Create configuration
mkdir -p configs/c.1/strings/0x409
echo "JCZ Bridge"              > configs/c.1/strings/0x409/configuration
echo 250                       > configs/c.1/MaxPower   # 500mA (250 x 2)

# Link function to configuration
ln -sf "functions/ffs.${GADGET_NAME}" "configs/c.1/ffs.${GADGET_NAME}"

# ── Mount FunctionFS ──────────────────────────────────────────────────────────

mkdir -p "${FFS_MOUNT}"
mount -t functionfs "${GADGET_NAME}" "${FFS_MOUNT}"
echo "FunctionFS mounted at: ${FFS_MOUNT}"

# ── NOTE: Do NOT bind UDC yet ─────────────────────────────────────────────────
# The bridge Python script must write FunctionFS endpoint descriptors to ep0
# BEFORE the UDC is bound. Binding first will cause an error.
#
# The bridge calls: echo "dummy_udc.0" > /sys/kernel/config/usb_gadget/bjjcz/UDC

echo ""
echo "=== Gadget created successfully ==="
echo ""
echo "  VID:PID      = 0x9588:0x9899 (BJJCZ)"
echo "  FunctionFS   = ${FFS_MOUNT}"
echo "  UDC          = ${UDC_NAME} (not yet bound)"
echo ""
echo "Next: start the bridge (it will write descriptors and bind UDC)"
