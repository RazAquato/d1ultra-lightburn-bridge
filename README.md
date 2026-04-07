# D1 Ultra LightBurn Bridge

A reverse-engineered bridge that lets [LightBurn](https://lightburnsoftware.com/) control the **Hansmaker D1 Ultra** laser engraver.

The D1 Ultra uses a proprietary binary protocol over TCP — not GRBL. This project provides two approaches to bridge that gap, plus a complete protocol specification for anyone building their own integration.

---

## Two Approaches

### JCZ Bridge (active development)

Emulates a **BJJCZ galvo controller** over USB/IP. LightBurn sees the D1 Ultra as a native JCZ galvo device, unlocking full galvo features: **live framing, split marking, cylinder correction, native speed/power control**.

```
Windows PC (LightBurn)
  |  USB/IP over LAN
  v
Linux VM (Debian 13+)
  |  Virtual USB device (VID 0x9588, PID 0x9899, EP IN 0x88)
  |  configfs + FunctionFS + modified dummy_hcd
  |  jcz_bridge.py translates JCZ commands
  v
D1 Ultra laser (192.168.12.1:6000 via USB RNDIS)
```

**Status:** LightBurn connects, detects the device, sends framing and engrave jobs successfully. Waiting for USB cable to test actual laser engraving.

**Requirements:** Linux machine (VM or bare metal) + USB/IP client on the LightBurn PC.

See **[jcz_bridge/README.md](jcz_bridge/README.md)** for setup and details.

### GRBL Bridge (stable, no longer actively developed)

Translates LightBurn's GRBL G-code to D1 Ultra protocol. Simpler setup (runs on same machine as LightBurn), but limited to basic line engraving — no live framing, no fill/raster, no galvo features.

```
LightBurn  --GRBL/TCP-->  Bridge (localhost:9023)  --D1 Ultra/TCP-->  Laser
```

**Status:** Working (v2.3) — line engraving confirmed on real hardware.

See **[grbl_bridge/README.md](grbl_bridge/README.md)** for setup and details.

### Which One Should I Use?

| | GRBL Bridge | JCZ Bridge |
|---|---|---|
| **Setup complexity** | Simple — runs on any OS | Requires a Linux VM + USB/IP |
| **LightBurn mode** | GRBL (Ethernet/TCP) | JCZFiber (USB) |
| **Line engraving** | Yes | Yes (in testing) |
| **Fill/raster** | No | Planned |
| **Live framing** | No | Yes |
| **Split marking** | No | Yes |
| **Development** | Stable, not active | Active |

If you just want to engrave some lines today with minimal setup, use the GRBL bridge.
If you want full galvo features and are comfortable setting up a Linux VM, use the JCZ bridge.

---

## For Integrators (LightBurn, etc.)

If you're implementing native D1 Ultra support, these are the two files you need:

| File | What it is |
|------|-----------|
| **[PROTOCOL.md](PROTOCOL.md)** | Full binary protocol specification — packet format, CRC, command reference, job sequence, preview/framing, peripheral control. Verified against 26 Wireshark captures. |
| **[d1ultra_protocol.py](d1ultra_protocol.py)** | Working Python implementation of the protocol. Clean API — `connect()`, `identify()`, `engrave()`, `preview()`, `set_peripheral()`, etc. No GRBL, no CLI — just the protocol. Zero external dependencies. |

The `wireshark_captures/` directory contains all 26 pcapng files from M+ sessions for verification. Filter by `tcp.port == 6000` in Wireshark.

---

## About the JCZ Bridge: USB/IP and the Linux VM

The JCZ bridge requires a Linux machine between LightBurn and the laser. Here's why:

**The problem:** LightBurn's JCZ driver talks to BJJCZ controllers over USB. The D1 Ultra doesn't have a BJJCZ controller — it's a different kind of laser entirely. We need to create a virtual USB device that looks exactly like a BJJCZ board.

**The solution:** Linux's USB gadget framework can create virtual USB devices. We use `configfs` + `FunctionFS` to create a device with the correct VID/PID (0x9588:0x9899) and endpoint addresses (OUT 0x02, IN 0x88). This device is then exported over the network using USB/IP, so any PC on the LAN can attach it as if it were a local USB device.

**What you need:**
- A Linux machine (a Proxmox VM works great — that's what we use)
- [usbip-win2](https://github.com/vadimgrn/usbip-win2/releases) installed on the Windows PC (signed driver, simple installer)
- The D1 Ultra's USB cable passed through to the Linux VM

**Network setup:**
```
Windows PC ──── LAN ──── Linux VM ──── USB ──── D1 Ultra
     |                       |
     └── USB/IP (TCP 3240) ──┘
```

The Linux VM handles all the protocol translation. LightBurn on Windows just sees a normal USB laser controller.

---

## Important Notices

### Do NOT Contact Hansmaker About This Bridge

**Hansmaker did not create this software and has no involvement with it.** Please do not contact Hansmaker support, forums, or social media with questions or bug reports related to this bridge. They are a small team and have enough to deal with supporting their own products.

- Issues with this bridge -> [open a GitHub issue here](https://github.com/RazAquato/d1ultra-lightburn-bridge/issues)
- Issues with M+ or D1 Ultra hardware -> contact Hansmaker through their official channels

**This project is not affiliated with, endorsed by, or supported by Hansmaker in any way.**

### Disclaimer

**This software is provided as-is, with absolutely no warranty.** It was reverse-engineered from Wireshark captures of the official M+ software and may be incomplete or incorrect. Use at your own risk. The authors are not responsible for any damage to your laser, materials, property, or anything else. Always wear appropriate laser safety equipment and never leave a running laser unattended.

### Community Project

This project is provided to the community as a starting point. Pull requests are welcome. You are welcome to fork it and build on it.

The protocol documentation here is freely available for anyone to use.

---

## Project Structure

```
README.md                    This file
PROTOCOL.md                  D1 Ultra binary protocol specification
d1ultra_protocol.py          Protocol library (standalone, used by both bridges)
LICENSE                      MIT

jcz_bridge/                  JCZ/BJJCZ galvo emulator (active development)
  README.md                  Full setup guide
  TODO.md                    What works, what's next
  jcz_bridge.py              Main bridge application
  jcz_protocol.py            JCZ command parser (standalone)
  d1ultra_protocol.py        Protocol library (copy, for standalone deployment)
  config.py                  Configuration
  kernel/                    Modified dummy_hcd source (for endpoint 0x88)
  start_configfs.sh          One-command startup
  setup_gadget.sh            USB gadget setup
  setup_usbip.sh             USB/IP export
  systemd/                   Service files for auto-start

grbl_bridge/                 GRBL bridge (stable, not actively developed)
  README.md                  Setup guide
  d1ultra_bridge.py          v2.3 (confirmed working)
  NOTTESTED_d1ultra_bridge_v2.4.py  v2.4 (adds framing, untested)

wireshark_captures/          26 pcapng files from M+ sessions
```

---

## How the D1 Ultra Protocol Works

The D1 Ultra connects via USB but presents as a **virtual Ethernet adapter** (RNDIS), not a serial port. It listens on TCP port 6000 at IP 192.168.12.1.

Job execution sequence:

1. **DEVICE_INFO** (0x0018) — query device
2. **PRE_JOB** (0x0005) — signal upcoming job
3. **QUERY_14** (0x0014, sub=0x02) — pre-job setup
4. **JOB_UPLOAD** (0x0002) — job name + PNG preview thumbnail
5. **WORKSPACE** (0x0009) — bounding box of the design
6. **JOB_SETTINGS** (0x0000) + **PATH_DATA** (0x0001) — power/speed/coordinates per path
7. **JOB_CONTROL** (0x0003) — host tells laser to execute
8. **JOB_FINISH** (0x0004) — finalize

See **[PROTOCOL.md](PROTOCOL.md)** for the complete specification.

---

## How It Was Built

The D1 Ultra protocol was entirely reverse-engineered from Wireshark captures of M+ communicating with the laser over TCP port 6000. No Hansmaker proprietary code was used, decompiled, or referenced.

## Support

If you find this project useful and want to support the reverse-engineering work:

[![Buy Me a Coffee](https://img.shields.io/badge/Buy%20Me%20a%20Coffee-ffdd00?style=for-the-badge&logo=buy-me-a-coffee&logoColor=black)](https://buymeacoffee.com/razaqato)

## License

MIT License — see [LICENSE](LICENSE) for the full text.

Free to use, modify, fork, and distribute. No warranty of any kind.
