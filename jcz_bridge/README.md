# JCZ Bridge — BJJCZ Galvo Emulator over USB/IP

> **STATUS: Working. LightBurn connects, detects device, sends framing and engrave jobs.**
> Tested 2026-04-07 on Debian 13 (kernel 6.12), LightBurn on Windows 11.

This bridge makes a **Hansmaker D1 Ultra** laser appear as a **BJJCZ galvo controller**
to LightBurn. It runs on a Debian VM and exports the virtual BJJCZ device over USB/IP
to any PC on the LAN.

```
Windows PC (LightBurn)
  |  USB/IP over LAN
  v
Debian VM
  |  configfs USB gadget (VID 0x9588, PID 0x9899)
  |  FunctionFS endpoints (EP OUT 0x02, EP IN 0x88)
  |  jcz_bridge.py translates JCZ -> D1 Ultra protocol
  v
D1 Ultra laser (192.168.12.1:6000 via USB RNDIS)
```

## Why This Exists

LightBurn's JCZ/galvo mode offers features that GRBL mode doesn't: **live framing,
split marking, cylinder correction, and native galvo speed/power control**. The D1 Ultra
isn't a galvo laser, but by emulating the BJJCZ USB protocol, we get access to all
of these features through LightBurn's existing JCZ driver.

An earlier attempt used stock `dummy_hcd`, which assigned endpoint address 0x81 for
the IN endpoint. LightBurn's JCZ driver requires **endpoint 0x88** (matching real
BJJCZ hardware). This version solves that with a modified kernel module.

## The Endpoint 0x88 Problem (and Solution)

Real BJJCZ boards use USB endpoints:
- **EP OUT 0x02** (Bulk) — host sends commands
- **EP IN 0x88** (Bulk) — device sends responses

LightBurn hardcodes these addresses. The Linux kernel's `dummy_hcd` module only offers
`ep1in-bulk` (address 0x81) as the first IN bulk endpoint, so configfs always picks it.

### What we tried

| Approach | Endpoint | USB/IP | Result |
|----------|----------|--------|--------|
| Stock dummy_hcd + configfs | 0x81 | Works | LightBurn can't communicate (wrong endpoint) |
| raw_gadget (userspace EP0) | 0x88 | Broken | EP0 stalls during USB/IP attach — control transfers never reach userspace |
| **Modified dummy_hcd + configfs** | **0x88** | **Works** | **LightBurn connects and sends jobs** |

### The fix

One-line change to `dummy_hcd.c`: comment out `ep1in-bulk`, `ep6in-bulk`, `ep11in-bulk`,
and `ep2in-bulk` (the sa1100 emulation endpoint). This forces the kernel's
`usb_ep_autoconfig()` to pick `ep8in-bulk` (address 0x88) as the first available IN bulk
endpoint.

The modified source is in `kernel/dummy_hcd.c`. The stock `ep2out-bulk` (address 0x02) is
kept for the OUT endpoint.

## Requirements

- **Debian 13 (Trixie)** or later (kernel 6.1+ required)
- **Kernel headers** installed (`apt install linux-headers-$(uname -r)`)
- **Python 3.11+** (stdlib only, no pip packages)
- **D1 Ultra** connected via USB (passed through if running in a VM)
- **Windows PC** with LightBurn and [usbip-win2](https://github.com/vadimgrn/usbip-win2/releases)

## Quick Start

### 1. Build the modified dummy_hcd

```bash
cd kernel/
make
# Produces dummy_hcd.ko
```

### 2. Start the bridge

```bash
sudo bash start_configfs.sh
```

This script:
1. Unloads stock `dummy_hcd`, loads the modified version
2. Creates the configfs USB gadget (VID 0x9588, PID 0x9899)
3. Starts `jcz_bridge.py` (writes FunctionFS descriptors, binds UDC)
4. Verifies endpoint addresses (`lsusb -v` should show 0x02 OUT + 0x88 IN)
5. Exports the device via USB/IP

### 3. Connect from Windows

```powershell
# In an Administrator terminal:
usbip.exe attach --remote <vm-ip> --busid 2-1
```

### 4. Set up LightBurn

1. Devices -> Create Manually -> **JCZFiber** -> USB
2. Field size: **220mm x 220mm** (for D1 Ultra)
3. Click **Find My Laser** — should detect "BJJCZ Fiber Laser"

### 5. Test

- **Frame** — LightBurn sends TRAVEL commands tracing the bounding box
- **Start** — LightBurn sends SET_POWER, SET_MARK_SPEED, MARK commands

## What's Verified (2026-04-07)

- Device enumerates as `9588:9899 BJJCZ Fiber Laser`
- Endpoints: EP OUT 0x02, EP IN 0x88 (confirmed via `lsusb -v`)
- USB/IP attach from Windows succeeds
- LightBurn detects device and shows "Ready"
- `GetSerialNo` (0x0009), `GetVersion` (0x0007) — responded correctly
- `EnableLaser` (0x0004), all SET_* config commands — ACK'd
- **Framing**: TRAVEL commands received (rectangle loop at correct coordinates)
- **Engrave job**: Full job sequence captured:
  - `JOB_BEGIN` -> `SET_Q_PERIOD` -> `SET_POWER` (70% = 0x0B33) -> `SET_MARK_SPEED` (5000) -> `MARK` paths -> `JOB_END`
  - GetVersion polling loop while "job executes"
- 250K+ protocol exchanges, zero errors

## What's NOT Done Yet

See [TODO.md](TODO.md) for the full list. The USB emulation and protocol capture are
working. The remaining work is **translating JCZ commands to D1 Ultra TCP protocol**
to actually drive the laser hardware.

## Architecture

### Kernel layer
- **Modified `dummy_hcd`** — virtual USB host controller with `ep8in-bulk`
- **`configfs` + `libcomposite`** — USB gadget framework
- **`usb_f_fs` (FunctionFS)** — exposes USB endpoints as file descriptors
- **`usbip-host` + `usbipd`** — exports virtual device over TCP

### Userspace
- **`jcz_bridge.py`** — main bridge: reads JCZ commands from FunctionFS, translates
  to D1 Ultra protocol, sends via TCP
- **`jcz_protocol.py`** — standalone JCZ/BJJCZ command parser (12-byte commands,
  3072-byte chunks, galvo coordinate conversion)
- **`d1ultra_protocol.py`** — standalone D1 Ultra binary protocol library (packet
  builder, CRC, TCP connection, job execution)
- **`laser_monitor.py`** — watches for RNDIS interface (laser on/off detection)
- **`config.py`** — all tuneable parameters

### Scripts
- **`start_configfs.sh`** — one-command startup (load modules, create gadget, start bridge, start USB/IP)
- **`setup_gadget.sh`** — create configfs USB gadget
- **`setup_usbip.sh`** — export gadget via USB/IP
- **`install_services.sh`** — install systemd services for auto-start

## File Structure

```
jcz_bridge/
├── README.md                  This file
├── TODO.md                    What needs to be done
├── config.py                  Configuration (laser IP, field size, etc.)
├── jcz_bridge.py              Main bridge application
├── jcz_protocol.py            JCZ/BJJCZ command parser (standalone)
├── d1ultra_protocol.py        D1 Ultra protocol library (standalone)
├── laser_monitor.py           RNDIS interface watcher
├── start_configfs.sh          One-command startup
├── setup_gadget.sh            Create USB gadget
├── setup_usbip.sh             Export via USB/IP
├── install_services.sh        Systemd service installer
├── kernel/
│   ├── dummy_hcd.c            Modified dummy_hcd source (ep8in-bulk)
│   └── Makefile               Build against running kernel headers
├── systemd/                   Service unit files
└── tests/                     Unit tests (no hardware needed)
```

## Boot Procedure

The stock `dummy_hcd` module auto-loads on boot (other modules depend on it).
`start_configfs.sh` handles this by doing `rmmod dummy_hcd` then `insmod` of the
modified version. This is safe because nothing uses the stock module at boot.

For a more permanent solution, you can replace the stock module:
```bash
# Back up the original
sudo cp /lib/modules/$(uname -r)/kernel/drivers/usb/gadget/udc/dummy_hcd.ko.xz \
       /lib/modules/$(uname -r)/kernel/drivers/usb/gadget/udc/dummy_hcd.ko.xz.bak

# Install the modified version
sudo cp kernel/dummy_hcd.ko /lib/modules/$(uname -r)/kernel/drivers/usb/gadget/udc/
sudo depmod -a
```

## Protocol References

- [PROTOCOL.md](../PROTOCOL.md) — D1 Ultra binary protocol specification
- [balor](https://gitlab.com/bryce15/balor) — BJJCZ reverse engineering (Bryce Schroeder)
- [galvoplotter](https://github.com/meerk40t/galvoplotter) — BJJCZ Python library

