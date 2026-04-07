"""
D1 Ultra JCZ Bridge — Configuration
====================================

All tuneable parameters in one place.
Edit this file to match your hardware setup.
"""

# ---------------------------------------------------------------------------
# D1 Ultra laser connection
# ---------------------------------------------------------------------------
# The D1 Ultra connects via USB and presents as RNDIS virtual ethernet.
# Proxmox passes the USB device through to this VM.
# The laser is always at 192.168.12.1 on the RNDIS adapter.
LASER_IP   = "192.168.12.1"
LASER_PORT = 6000

# ---------------------------------------------------------------------------
# USB passthrough (Proxmox)
# ---------------------------------------------------------------------------
# The D1 Ultra's USB VID:PID as seen by Proxmox.
# Find with `lsusb` on the Proxmox host while the laser is connected.
# Used in documentation only — Proxmox handles the actual passthrough.
# Format: "VID:PID" (hex, no 0x prefix)
PROXMOX_USB_PASSTHROUGH = "XXXX:XXXX"  # <-- SET THIS to your laser's VID:PID

# ---------------------------------------------------------------------------
# BJJCZ USB gadget identity
# ---------------------------------------------------------------------------
# These values make the virtual USB device look like a real BJJCZ controller.
# LightBurn uses VID:PID to detect JCZ galvo hardware.
BJJCZ_VID           = 0x9588
BJJCZ_PID           = 0x9899
BJJCZ_MANUFACTURER  = "Beijing JCZ Technology"
BJJCZ_PRODUCT       = "BJJCZ Fiber Laser"
BJJCZ_SERIAL        = "D1ULTRA-BRIDGE-001"
BJJCZ_BCD_DEVICE    = 0x0005  # firmware version (matches real board)
BJJCZ_BCD_USB       = 0x0200  # USB 2.0

# ---------------------------------------------------------------------------
# FunctionFS paths (set by gadget setup script)
# ---------------------------------------------------------------------------
FFS_MOUNT     = "/dev/ffs-bjjcz"       # FunctionFS mount point
FFS_EP0       = "/dev/ffs-bjjcz/ep0"   # control endpoint (descriptor setup)
FFS_EP_OUT    = "/dev/ffs-bjjcz/ep1"   # Bulk OUT: LightBurn -> bridge
FFS_EP_IN     = "/dev/ffs-bjjcz/ep2"   # Bulk IN:  bridge -> LightBurn

# configfs gadget path
GADGET_NAME   = "bjjcz"
GADGET_DIR    = f"/sys/kernel/config/usb_gadget/{GADGET_NAME}"

# UDC name (dummy_hcd virtual controller)
UDC_NAME      = "dummy_udc.0"

# ---------------------------------------------------------------------------
# Galvo field calibration
# ---------------------------------------------------------------------------
# Physical lens field size in mm. MUST match your actual lens.
# Common values: 70, 110, 150, 175, 200, 220
# The D1 Ultra has a 220mm field. With conveyor belt: 220x800.
# To calibrate: engrave a known-size square, measure with calipers, adjust.
FIELD_SIZE_MM = 220.0

# JCZ coordinate space
GALVO_MAX    = 0xFFFF   # 65535
GALVO_CENTRE = 0x8000   # 32768

# ---------------------------------------------------------------------------
# Laser network detection (RNDIS)
# ---------------------------------------------------------------------------
# When the D1 Ultra is powered on and its USB is passed through, the VM
# gets a new RNDIS network interface with an IP in this subnet.
# When the laser is off, the interface disappears.
# The bridge watches for this interface instead of pinging forever.
LASER_SUBNET = "192.168.12."   # some units use "10.0.0." — adjust if needed
LASER_IFACE_CHECK_SEC = 2.0    # how often to check for the RNDIS interface
LASER_DHCP_SETTLE_SEC = 120.0  # wait time after RNDIS interface appears before
                                # connecting (DHCP can take up to 2 minutes to
                                # assign the correct IP from the laser)

# ---------------------------------------------------------------------------
# Bridge behaviour
# ---------------------------------------------------------------------------
FRAME_SPEED_MM_MIN = 200.0    # speed for live framing (red dot trace)
JOB_NAME_PREFIX    = "lb"     # prefix for auto-generated job names
STATUS_POLL_MS     = 10       # how often to send status to LightBurn (ms)
HEARTBEAT_INTERVAL = 2.0      # D1 Ultra keepalive interval (seconds)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_LEVEL = "DEBUG"  # DEBUG, INFO, WARNING, ERROR
