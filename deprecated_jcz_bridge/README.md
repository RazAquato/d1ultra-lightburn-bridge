# JCZ Bridge — D1 Ultra as BJJCZ Galvo Controller

Makes a Hansmaker D1 Ultra laser appear as a **BJJCZ galvo controller** to LightBurn,
unlocking full galvo features: **live framing, split marking, cylinder correction**.

```
Any PC running LightBurn
  └── usbipd-win (Windows) or usbip (Linux) — free USB/IP client
      └── sees VID 0x9588 / PID 0x9899 (BJJCZ controller)
      └── LightBurn connects as JCZFiber device
      └── sends 12-byte JCZ commands in 3072-byte batches
           │
           │  TCP/IP over LAN (USB/IP protocol)
           │
Debian VM (Proxmox or bare metal)
  ├── dummy_hcd         — virtual USB bus (kernel module)
  ├── libcomposite      — USB gadget framework (kernel module)
  ├── configfs gadget   — presents VID 0x9588/PID 0x9899
  ├── FunctionFS        — exposes bulk USB endpoints as file descriptors
  ├── usbip server      — exports virtual BJJCZ device over TCP
  └── jcz_bridge.py     — main bridge:
        reads JCZ commands from FunctionFS
        translates galvo units → mm coordinates
        sends D1 Ultra TCP binary protocol to laser
           │
           │  TCP to 192.168.12.1:6000
           │
Hansmaker D1 Ultra laser
  └── USB → RNDIS virtual ethernet → 192.168.12.1
```

## Requirements

- **Debian 13 (Trixie)** or later (tested on 6.12 kernel)
  - Also works on Ubuntu 24.04+ or any distro with kernel 6.1+
  - Can run as a Proxmox VM, bare metal, or Docker container
- **Python 3.11+** (stdlib only — no pip packages needed)
- **D1 Ultra** connected via USB (passed through to VM if virtualized)
- **PC running LightBurn** with USB/IP client installed

## Quick Start

### 1. Set up the Linux machine

```bash
# Clone the repo (or copy the jcz_bridge folder)
git clone https://github.com/RazAquato/d1ultra-lightburn-bridge.git
cd d1ultra-lightburn-bridge/jcz_bridge

# Install packages and load kernel modules
sudo apt-get update
sudo apt-get install -y python3 git usbip

# Load kernel modules (persists across reboots)
sudo tee /etc/modules-load.d/d1ultra-bridge.conf << 'EOF'
configfs
libcomposite
dummy_hcd
usb_f_fs
usbip-core
usbip-host
EOF

sudo modprobe configfs libcomposite dummy_hcd usb_f_fs usbip-core usbip-host
```

### 2. Configure

Edit `config.py`:

```python
LASER_IP       = "192.168.12.1"   # Usually this, some units use 10.0.0.x
FIELD_SIZE_MM  = 110.0            # Must match your physical lens
LASER_SUBNET   = "192.168.12."   # Adjust if your laser uses a different subnet
```

### 3. Start the bridge

```bash
# Manual start (for testing)
sudo bash setup_gadget.sh
sudo python3 jcz_bridge.py

# In another terminal:
sudo bash setup_usbip.sh
```

Or use the combined start script:
```bash
sudo bash start.sh
```

### 4. Install as system service (auto-start on boot)

```bash
sudo bash install_services.sh
sudo systemctl start gadget-setup jcz-bridge usbip-server
```

Check status:
```bash
sudo systemctl status jcz-bridge
journalctl -u jcz-bridge -f
tail -f /var/log/d1ultra-bridge.log
```

## Windows Setup (LightBurn PC)

### Install USB/IP client

You need a USB/IP **client** for Windows that can attach remote USB devices
from a Linux server. **Do NOT use `usbipd-win` (dorssel)** — that only
shares Windows USB devices to WSL, not the other way around.

**Recommended: `usbip-win2`** (vadimgrn fork, actively maintained, **signed driver**):

1. Download the latest `.msi` installer from:
   https://github.com/vadimgrn/usbip-win2/releases

2. Run the installer — it's signed, so no test signing mode needed.
   Installs the virtual USB bus driver (vhci) and `usbip.exe` CLI.

### Connect to the bridge

Open an **Administrator Command Prompt or PowerShell**:

```powershell
# List available devices on the bridge VM
usbip.exe list --remote <vm-ip-address>

# You should see:
#   2-1: 9588:9899  Beijing JCZ Technology BJJCZ Fiber Laser

# Attach the device
usbip.exe attach --remote <vm-ip-address> --busid 2-1
```

The BJJCZ device should now appear in Windows Device Manager under
"Universal Serial Bus devices" or "USB controllers".

> **Note:** The attach is not persistent across reboots. Re-run the
> attach command after restarting either machine. You can create a
> `.bat` script or scheduled task to automate this.

### Set up LightBurn

1. Open LightBurn
2. Go to **Devices** → **Create Manually**
3. Select **JCZFiber** as the device type
4. Connection: **USB**
5. Field size: **110mm x 110mm** (or whatever matches your `FIELD_SIZE_MM`)
6. Skip markcfg7 import when prompted (not needed)
7. Click **Find My Laser** — it should detect the BJJCZ device

### Test the connection

1. Draw a simple square in LightBurn
2. Click **Frame** — the red dot on the laser should trace the boundary
3. Click **Start** — the laser should engrave

## Linux Client Setup (alternative to Windows)

If LightBurn runs on a Linux machine instead of Windows:

```bash
# Install usbip client
sudo apt-get install linux-tools-common

# Attach the remote device
sudo usbip attach --remote <vm-ip-address> --busid 2-1

# Verify
lsusb | grep 9588
```

## File Structure

```
jcz_bridge/
├── config.py              — All tuneable parameters (laser IP, field size, etc.)
├── d1ultra_protocol.py    — D1 Ultra binary protocol library (standalone)
├── jcz_protocol.py        — JCZ/BJJCZ command parser (standalone)
├── jcz_bridge.py          — Main bridge application
├── laser_monitor.py       — RNDIS interface watcher (laser on/off detection)
├── setup_gadget.sh        — Create USB gadget via configfs
├── setup_usbip.sh         — Export gadget via USB/IP
├── start.sh               — Start everything manually
├── install_services.sh    — Install systemd services for auto-start
├── systemd/
│   ├── gadget-setup.service
│   ├── jcz-bridge.service
│   └── usbip-server.service
├── tests/
│   ├── test_d1ultra_protocol.py
│   └── test_jcz_protocol.py
└── README.md              — This file
```

### For LightBurn developers

The protocol libraries are **standalone** — no bridge dependencies:

- **`d1ultra_protocol.py`** — Complete D1 Ultra binary protocol implementation.
  `PacketBuilder` constructs all packet types, `ResponseParser` parses responses,
  `D1Ultra` class manages TCP connection with heartbeat and job execution.
  See `PROTOCOL.md` in the parent directory for the full binary spec.

- **`jcz_protocol.py`** — JCZ/BJJCZ command parser. `JCZOp` enum, `JCZCommand`
  class, chunk parsing, and galvo↔mm coordinate conversion.
  Based on [balor](https://gitlab.com/bryce15/balor) reverse engineering.

## How it works

### Laser detection

The bridge does **not** ping the laser forever. Instead, it watches for the
RNDIS network interface that the D1 Ultra creates when powered on:

- Laser ON → USB passthrough → RNDIS interface appears → bridge connects
- Laser OFF → RNDIS interface disappears → bridge enters standby
- LightBurn stays connected to the virtual BJJCZ device the whole time

Configure `LASER_SUBNET` in `config.py` if your laser uses something other
than `192.168.12.x`.

### Command translation

LightBurn sends 12-byte JCZ commands. For status polling (opcode 0x0009),
it sends individual 12-byte packets. For job data, it sends 3072-byte
batches (256 commands). The bridge handles both modes:

1. Responds to 0x0009 heartbeat polls with 12-byte status
2. Parses each JCZ command (TRAVEL, MARK, SET_POWER, etc.)
3. Accumulates path coordinates in galvo units (0-65535)
4. On JOB_END: converts to mm, centres on bounding box, sends D1 Ultra job
5. On LIGHT commands: triggers native WORKSPACE preview (red dot framing)

### Coordinate conversion

JCZ uses 16-bit unsigned galvo coordinates. The D1 Ultra uses IEEE 754
doubles in mm, centred on the design midpoint.

```
JCZ:  0x0000 ─────── 0x8000 ─────── 0xFFFF
      -110mm           0mm          +110mm    (for 220mm field)
```

Set `FIELD_SIZE_MM` in `config.py` to match your physical lens (220mm for D1 Ultra).
To calibrate: engrave a known-size square, measure with calipers, adjust the value.

## Proxmox Setup

If running as a Proxmox VM, pass the D1 Ultra USB device through:

1. On the **Proxmox host**, blacklist the RNDIS driver so it doesn't claim
   the device before the VM can:
   ```bash
   echo "blacklist rndis_host" | sudo tee /etc/modprobe.d/d1ultra-blacklist.conf
   sudo update-initramfs -u
   ```

2. In the Proxmox web UI:
   - Select your VM → Hardware → Add → USB Device
   - Use "USB Vendor/Device ID"
   - Enter the D1 Ultra's VID:PID (find with `lsusb` on the host)

3. The VM will see the D1 Ultra as a USB network adapter. The bridge
   handles the rest.

## Logging

The bridge logs to both the console and `/var/log/d1ultra-bridge.log`.

```bash
# Live log
tail -f /var/log/d1ultra-bridge.log

# Systemd journal
journalctl -u jcz-bridge -f

# Debug mode — edit config.py:
LOG_LEVEL = "DEBUG"
```

## Troubleshooting

### Bridge doesn't start
```bash
# Check kernel modules
lsmod | grep -E 'dummy_hcd|libcomposite|usb_f_fs|usbip'

# Check configfs mount
mount | grep configfs

# Check gadget exists
ls /sys/kernel/config/usb_gadget/bjjcz/
```

### LightBurn can't find the device
```bash
# On the bridge machine — is the device visible?
lsusb | grep 9588

# Is USB/IP running?
usbip list --local

# On the Windows PC — can you see it?
usbipd list --remote <vm-ip>
```

### Laser not detected
```bash
# Check for RNDIS interface
ip addr show | grep 192.168.12

# Try direct ping
ping 192.168.12.1

# Test TCP connection
python3 jcz_bridge.py --test-laser
```

## Known Issues

### USB/IP transfer latency (under investigation)

LightBurn's JCZ driver has aggressive timeouts (~500ms) for USB bulk transfers.
When using USB/IP over a LAN, the network round-trip can cause LightBurn to
cancel (UNLINK) transfers before the bridge can respond, resulting in a
"framing disconnected" loop.

**Status:** Under investigation. A latency test tool is included
(`usb_latency_test.py` + `tests/win_usb_latency_test.py`) to measure actual
round-trip times and determine if this is a fundamental limitation or solvable.

**Potential solutions being explored:**
- FunctionFS async I/O (AIO/io_uring) for lower-latency transfer completion
- Running LightBurn and bridge on the same host (localhost USB/IP)
- Alternative transport (network protocol instead of USB emulation)

## Running Tests

```bash
cd jcz_bridge
python3 -m unittest discover -s tests -v
```

All tests run without hardware — they use mock data to verify packet building,
command parsing, CRC calculation, and coordinate conversion.

## References

- [D1 Ultra Protocol Spec](../PROTOCOL.md) — Full binary protocol documentation
- [balor](https://gitlab.com/bryce15/balor) — BJJCZ reverse engineering by Bryce Schroeder
- [galvoplotter](https://github.com/meerk40t/galvoplotter) — Higher-level BJJCZ library
- [Linux USB Gadget](https://www.kernel.org/doc/html/latest/usb/gadget_configfs.html) — Kernel configfs docs
- [usbipd-win](https://github.com/dorssel/usbipd-win) — Windows USB/IP client
- [USB/IP](https://www.kernel.org/doc/html/latest/usb/usbip_protocol.html) — Linux USB/IP protocol docs
