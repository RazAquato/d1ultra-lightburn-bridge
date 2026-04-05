# Raspberry Pi Zero — JCZ-to-D1 Ultra Bridge

**STATUS: Experimental / Untested**

This bridge runs on a Raspberry Pi Zero (2W recommended) and makes the Hansmaker D1 Ultra
appear as a BJJCZ galvo controller to LightBurn. This enables full galvo features:
live framing, split marking, cylinder correction — not available through GRBL emulation.

```
LightBurn (PC)                 Pi Zero                          D1 Ultra
     |                            |                                |
     |  USB (BJJCZ VID/PID)      |  USB (RNDIS Ethernet)         |
     |  ─── JCZ commands ──────> |  ─── TCP binary protocol ──> |
     |  <── JCZ status ───────── |  <── TCP responses ────────── |
     |                            |                                |
     USB Device port              USB Host port                   USB
     (gadget: 0x9588:0x9899)     (to laser: 192.168.12.1:6000)
```

## How it works

1. The Pi Zero's **USB device port** (the micro-USB "USB" port, not "PWR") presents itself
   to your PC as a BJJCZ laser controller using Linux's USB gadget framework
2. LightBurn detects it as a JCZ device and sends 12-byte bulk commands in 3072-byte chunks
3. The bridge parses JCZ movement/laser commands and translates them to D1 Ultra TCP packets
4. The Pi Zero's **USB host port** (via OTG adapter) connects to the D1 Ultra's USB cable,
   which creates the RNDIS virtual Ethernet adapter at 192.168.12.1

## Hardware required

- **Raspberry Pi Zero 2W** (recommended) or Pi Zero W
  - Must have both USB ports: one for PC connection, one for laser connection
  - Pi Zero 2W has quad-core ARM and is fast enough for real-time translation
- **USB OTG adapter/hub** for the host port (micro-USB to USB-A female)
- **Two USB cables**: one to PC, one to D1 Ultra
- **Power**: The Pi is powered through the PC USB connection (USB gadget port)

## Wiring diagram

```
                        Raspberry Pi Zero 2W
                    ┌──────────────────────────┐
                    │                          │
   PC / LightBurn  │  PWR        USB          │  D1 Ultra laser
                    │  port       port         │
                    │  (left)     (right)      │
                    │                          │
                    └────���─────────��───────────┘
                         │         │
                         │         │
                    not used    USB data
                    (or 5V      port
                    power       │
                    only)       │
                         │      │
                         │      ├──── micro-USB to USB-A female (OTG adapter)
                         │      │
                    ┌────┘      └──────────────┐
                    │                          │
              ┌─────┴─────┐            ┌───────┴───────┐
              │           │            │               │
              │    PC     │            │  D1 Ultra     │
              │           │            │  laser         │
              │ LightBurn │            │               │
              │ sees a    │            │  Creates RNDIS │
              │ "BJJCZ    │            │  virtual NIC   │
              │  laser"   │            │  192.168.12.1  │
              │           │            │               │
              └───────────┘            └───────────────┘
```

### Pi Zero 2W port layout (looking at the board, USB ports on the right side):

```
         ┌─────────────────────────────────────────────┐
         │  Pi Zero 2W                                 │
         │                                             │
    ─────┤ HDMI  ┤──────┤ USB (data) ┤──┤ USB (pwr) ├─┤
         │       mini    micro-USB       micro-USB     │
         │               ▲                ▲            │
         │               │                │            │
         │          TO PC via          OPTIONAL:       │
         │          normal USB cable   separate 5V     │
         │          (Pi = USB device,  power supply    │
         │          LightBurn sees     (not needed if  │
         │          BJJCZ controller)  PC provides     │
         │                             enough power)   │
         └─────────────────────────────────────────────┘
```

### Connections summary

| Pi Zero port | Connects to | Cable needed | Purpose |
|-------------|-------------|-------------|---------|
| **USB** (data, right-inner) | Your PC running LightBurn | Micro-USB to USB-A | Pi appears as BJJCZ controller. Also powers the Pi. |
| **PWR** (power, right-outer) | *Optional* 5V supply | Micro-USB power | Only if PC USB can't power Pi + OTG hub |
| **USB host** (via OTG on data port) | D1 Ultra laser | OTG adapter + USB-A cable | Pi talks TCP to laser over virtual ethernet |

**Wait — there's a catch:** The Pi Zero only has ONE data-capable USB port. You need it for
BOTH the PC connection (gadget mode) AND the laser connection (host mode). This requires
a **USB OTG hub** that supports both device and host simultaneously, or using the Pi's
GPIO pins for one of the connections.

**Alternative approach:** Use a **Pi Zero 2W with a USB OTG Y-cable/hub** that splits the
single data port into both a device connection (to PC) and a host connection (to laser).
These exist but are uncommon. A simpler option may be a **Raspberry Pi 4** or **CM4** which
has separate USB-C (device-capable) and USB-A (host) ports.

This is an unsolved hardware challenge — if you figure out a clean wiring setup, please
open a PR with photos!

## Software setup

```bash
# On the Pi Zero (Raspberry Pi OS Lite recommended):

# 1. Enable USB gadget kernel module
echo "dwc2" >> /etc/modules
echo "dtoverlay=dwc2" >> /boot/config.txt

# 2. Install dependencies
pip install pyusb  # only for testing; the bridge uses raw gadget I/O

# 3. Copy bridge files
scp rpi_jcz_bridge.py setup_gadget.sh pi@<pi-ip>:~/bridge/

# 4. Set up the USB gadget (run once after boot)
sudo bash setup_gadget.sh

# 5. Run the bridge
sudo python3 rpi_jcz_bridge.py
```

## File overview

| File | Purpose |
|------|---------|
| `rpi_jcz_bridge.py` | Main bridge: receives JCZ USB commands, translates to D1 Ultra TCP |
| `setup_gadget.sh` | Configures Linux USB gadget to present as BJJCZ controller |
| `jcz_commands.py` | JCZ/BJJCZ command definitions and parser |

## JCZ protocol summary

- **USB endpoints**: Bulk OUT `0x02` (commands from LightBurn), Bulk IN `0x88` (status to LightBurn)
- **Command format**: 12 bytes each — `u16 opcode + 5x u16 parameters` (little-endian)
- **Chunking**: Commands are sent in batches of 256 (3072 bytes). Padded with NOPs if fewer.
- **Coordinates**: 16-bit unsigned (0x0000-0xFFFF) covering the full galvo deflection range

See [PROTOCOL.md](../PROTOCOL.md) for the D1 Ultra side of the translation.

## Limitations

- Requires a Raspberry Pi Zero with two USB ports (device + host)
- Real-time translation adds latency vs native BJJCZ hardware
- Not all JCZ features may have D1 Ultra equivalents
- The D1 Ultra's coordinate system (centered, mm-based) differs from JCZ (0-65535 galvo units)
  and requires calibration

## References

- [balor](https://gitlab.com/bryce15/balor) — Open-source BJJCZ reverse engineering by Bryce Schroeder
- [galvoplotter](https://github.com/meerk40t/galvoplotter) — Higher-level BJJCZ Python library
- [Linux USB Gadget](https://www.kernel.org/doc/html/latest/usb/gadget_configfs.html) — Kernel docs for USB gadget configuration
